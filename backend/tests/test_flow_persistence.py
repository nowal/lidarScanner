"""Supabase persistence tests — mocked PostgREST transport, real app flow."""

import json

import httpx
import pytest
from httpx import ASGITransport, AsyncClient

import app.flow.supabase_store as supabase_store
from app.config import settings
from app.flow.state import FlowState, Slots
from app.flow_quotes import QuoteRequestRecord, quote_store
from app.main import app
from app.models import now_utc

FAKE_URL = "http://supabase.test"
FAKE_KEY = "service-role-test-key"


class FakePostgrest:
    """Records requests; serves rows seeded per path."""

    def __init__(self):
        self.requests: list[tuple[str, str, dict | list | None]] = []
        self.rows: dict[str, list[dict]] = {}
        self.fail_all = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        if self.fail_all:
            return httpx.Response(500, json={"message": "boom"})
        path = request.url.path.removeprefix("/rest/v1")
        body = json.loads(request.content) if request.content else None
        self.requests.append((request.method, path, body))
        if request.method == "GET":
            return httpx.Response(200, json=self.rows.get(path, []))
        return httpx.Response(201, json=[])


@pytest.fixture()
def fake_supabase(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "storage_dir", str(tmp_path))
    monkeypatch.setattr(settings, "supabase_url", FAKE_URL)
    monkeypatch.setattr(settings, "supabase_service_role_key", FAKE_KEY)
    fake = FakePostgrest()
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(fake.handler), base_url=f"{FAKE_URL}/rest/v1"
    )
    monkeypatch.setattr(supabase_store, "_client", client)
    monkeypatch.setattr(supabase_store, "_client_key", (FAKE_URL, FAKE_KEY))
    yield fake


def client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def auth_headers():
    if not settings.auth_token:
        return {}
    return {"Authorization": f"Bearer {settings.auth_token}"}


@pytest.mark.asyncio
async def test_chat_turn_persists_state_and_journal(fake_supabase):
    async with client() as http:
        resp = await http.post(
            "/api/v1/ai/home-chat",
            json={"message": "email me at pat@example.com — my zip is 37203"},
            headers=auth_headers(),
        )
    assert resp.status_code == 200
    posts = {(m, p) for m, p, _ in fake_supabase.requests if m == "POST"}
    assert ("POST", "/flow_states") in posts
    assert ("POST", "/flow_journal") in posts
    state_row = next(b for m, p, b in fake_supabase.requests if m == "POST" and p == "/flow_states")
    assert state_row["state"]["slots"]["zip"] == "37203"
    journal_row = next(b for m, p, b in fake_supabase.requests if m == "POST" and p == "/flow_journal")
    # Masked before it leaves the process.
    assert "pat@example.com" not in json.dumps(journal_row)
    assert journal_row["record"]["stepName"]
    assert journal_row["kind"] == "chat"


@pytest.mark.asyncio
async def test_state_recovers_from_supabase_after_restart(fake_supabase):
    """No token, no local file (fresh storage dir) — Supabase is the memory."""
    stored = FlowState(
        thread_id="t-durable",
        opening_delivered=True,
        user_turns=4,
        slots=Slots(first_name="Dana", zip="37203"),
    )
    fake_supabase.rows["/flow_states"] = [{"state": stored.model_dump(mode="json")}]
    async with client() as http:
        resp = await http.post(
            "/api/v1/ai/home-chat",
            json={"threadId": "t-durable", "message": "still with me?"},
            headers=auth_headers(),
        )
    slots = resp.json()["flow"]["slots"]
    assert slots["firstName"] == "Dana"
    assert slots["zip"] == "37203"


@pytest.mark.asyncio
async def test_quote_store_prefers_supabase_over_file(fake_supabase):
    record = QuoteRequestRecord(
        id="qr_durable", createdAt=now_utc().isoformat(), threadId="t-q",
        serviceType="Painting", status="submitted",
    )
    await quote_store.save(record)
    saved = next(b for m, p, b in fake_supabase.requests if m == "POST" and p == "/flow_quote_requests")
    assert saved["id"] == "qr_durable"
    assert saved["record"]["serviceType"] == "Painting"

    durable = dict(saved["record"])
    durable["status"] = "quotes_ready"  # differs from the local file copy
    fake_supabase.rows["/flow_quote_requests"] = [{"record": durable}]
    fetched = await quote_store.get("qr_durable")
    assert fetched.status == "quotes_ready"


@pytest.mark.asyncio
async def test_durable_state_outranks_stale_token(fake_supabase):
    """A quote submission advances state server-side; a client echoing the
    pre-submission token must still see the quote request (revision wins).
    This is the exact flaw the first live acceptance rehearsal caught."""
    from app.flow.state import QuoteRequestRef
    from app.flow_runtime import encode_flow_token

    stale = FlowState(thread_id="t-rev", opening_delivered=True, revision=3,
                      slots=Slots(first_name="Dana"))
    stale_token = encode_flow_token(stale)
    advanced = stale.model_copy(deep=True)
    advanced.revision = 5
    advanced.quote_request = QuoteRequestRef(id="qr_new", status="submitted")
    fake_supabase.rows["/flow_states"] = [{"state": advanced.model_dump(mode="json")}]

    from app.flow_runtime import resolve_flow_state

    resolved = await resolve_flow_state("t-rev", stale_token)
    assert resolved.quote_request is not None
    assert resolved.quote_request.id == "qr_new"


@pytest.mark.asyncio
async def test_supabase_outage_never_breaks_a_turn(fake_supabase):
    fake_supabase.fail_all = True
    async with client() as http:
        resp = await http.post(
            "/api/v1/ai/home-chat",
            json={"message": "hello there"},
            headers=auth_headers(),
        )
    assert resp.status_code == 200
    assert resp.json()["flow"]["token"]  # local persistence + token still work
