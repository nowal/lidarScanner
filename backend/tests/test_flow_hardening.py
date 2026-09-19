"""Regression tests for the acceptance-hardening pass (Aug 30).

Each test pins one fix:
- client tokens carry no PII and no identity ids (SOW §12)
- JWT sub → homeowners.id resolution (the journal FK) + contact enrichment
- results-return presents every ops batch, not just the first
- ops upload idempotency, price sanity, and status-transition guards
- homeowner-bound quote reads / address-release
- the sanitizer cannot launder a scan suggestion past enforcement
- sticky server-verified scan completion
- research/demo flags default OFF in the delivery build
- address-capture turns withhold the raw user line from the journal
- /health reports degraded config truthfully
"""

import base64
import json

import httpx
import pytest
from httpx import ASGITransport, AsyncClient

import app.flow.supabase_store as supabase_store
import app.flow_runtime as flow_runtime
from app.config import Settings, settings
from app.flow import FlowState, ScanProcessingState
from app.flow import enforcement
from app.flow.machine import FlowEngine, GateDecision
from app.flow.pii import mask_text
from app.flow.state import QuoteRequestRef, Slots
from app.flow_runtime import encode_flow_token, resolve_flow_state
from app.home_ai import (
    HomeAIChatMessage,
    HomeAIChatResponse,
    HomeAIConversationState,
)
from app.main import app

FAKE_URL = "http://supabase.test"
FAKE_KEY = "service-role-test-key"


@pytest.fixture(autouse=True)
def isolated_storage(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "storage_dir", str(tmp_path))
    supabase_store._homeowner_cache.clear()
    yield tmp_path
    supabase_store._homeowner_cache.clear()


def client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def auth_headers():
    if not settings.auth_token:
        return {}
    return {"Authorization": f"Bearer {settings.auth_token}"}


def _sensitive_state(thread_id: str = "t-priv") -> FlowState:
    return FlowState(
        thread_id=thread_id,
        homeowner_id="9f0e9114-1111-2222-3333-444455556666",
        homeowner_auth_sub="auth-sub-abc",
        opening_delivered=True,
        user_turns=6,
        slots=Slots(
            first_name="Dana",
            zip="37203",
            project_type="Painting",
            scope_options=["walls only"],
            address="4482 Grimmelman Trail, Columbus OH",
            contact_email="dana@example.com",
            contact_phone="(614) 555-0142",
        ),
    )


# ------------------------------------------------------------------ token PII
def test_flow_token_payload_contains_no_pii_or_identity():
    state = _sensitive_state()
    token = encode_flow_token(state)
    payload = json.loads(
        base64.urlsafe_b64decode(token.split(".")[0] + "===").decode("utf-8")
    )
    dumped = json.dumps(payload)
    for secret in ("Grimmelman", "dana@example.com", "555-0142", "9f0e9114", "auth-sub-abc"):
        assert secret not in dumped, f"token payload leaks {secret}"
    # Captured-ness survives as flags, so gates never re-ask.
    assert payload["slots"]["address_redacted"] is True
    assert payload["homeowner_linked"] is True
    # And the original state object is untouched.
    assert state.slots.address == "4482 Grimmelman Trail, Columbus OH"


@pytest.mark.asyncio
async def test_redacted_token_merges_values_back_from_durable_store():
    state = _sensitive_state("t-merge")
    await flow_runtime.persist_flow_state(state)
    token = encode_flow_token(state)
    resolved = await resolve_flow_state("t-merge", token)
    assert resolved.slots.address == "4482 Grimmelman Trail, Columbus OH"
    assert resolved.slots.contact_email == "dana@example.com"
    assert resolved.homeowner_id == "9f0e9114-1111-2222-3333-444455556666"


def test_redacted_slots_still_satisfy_gates_but_not_submission_values():
    state = _sensitive_state()
    token = encode_flow_token(state)
    decoded = flow_runtime._codec().decode(token)
    engine = FlowEngine()
    gates = engine.evaluate_gates(decoded, None)
    assert gates.can_ask_address is False          # captured → never re-asked
    assert gates.can_submit_quote_request is True  # app may enable Confirm
    assert engine.missing_submission_slots(decoded) == []
    # But an actual submission needs the real values (durable merge): the
    # token carries a "linked" flag where the identity and contact were.
    assert "contact" in engine.missing_submission_slots(decoded, require_values=True)


# ------------------------------------------------------- identity resolution
class FakeSupabase:
    """PostgREST fake that knows the homeowners table and ENFORCES the
    flow_journal → homeowners(id) foreign key, the way the real schema does."""

    def __init__(self):
        self.homeowners = [
            {"id": "ho-row-1", "auth_user_id": "auth-sub-1",
             "full_name": "Dana Whitfield", "email": "dana@example.com", "phone": None}
        ]
        self.journal_inserts: list[dict] = []
        self.rejected_inserts: list[dict] = []
        self.rows: dict[str, list[dict]] = {}

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/rest/v1")
        body = json.loads(request.content) if request.content else None
        if request.method == "GET" and path == "/homeowners":
            auth_id = request.url.params.get("auth_user_id", "").removeprefix("eq.")
            rows = [h for h in self.homeowners if h["auth_user_id"] == auth_id]
            return httpx.Response(200, json=rows)
        if request.method == "GET":
            return httpx.Response(200, json=self.rows.get(path, []))
        if request.method == "POST" and path == "/flow_journal":
            hid = (body or {}).get("homeowner_id")
            if hid is not None and hid not in {h["id"] for h in self.homeowners}:
                self.rejected_inserts.append(body)
                return httpx.Response(
                    409, json={"code": "23503", "message": "violates foreign key"}
                )
            self.journal_inserts.append(body)
        return httpx.Response(201, json=[])


@pytest.fixture()
def fake_supabase(monkeypatch):
    monkeypatch.setattr(settings, "supabase_url", FAKE_URL)
    monkeypatch.setattr(settings, "supabase_service_role_key", FAKE_KEY)
    fake = FakeSupabase()
    mock_client = httpx.AsyncClient(
        transport=httpx.MockTransport(fake.handler), base_url=f"{FAKE_URL}/rest/v1"
    )
    monkeypatch.setattr(supabase_store, "_client", mock_client)
    monkeypatch.setattr(supabase_store, "_client_key", (FAKE_URL, FAKE_KEY))
    yield fake


@pytest.mark.asyncio
async def test_jwt_sub_resolves_to_homeowners_row_and_journal_insert_passes_fk(
    fake_supabase,
):
    from app.home_ai import HomeAIChatRequest

    request = HomeAIChatRequest(threadId="t-fk", message="thinking about painting")
    response = await flow_runtime.run_flow_turn(request, homeowner_id="auth-sub-1")
    assert response.threadId == "t-fk"
    # The durable journal write used homeowners.id — and was ACCEPTED.
    assert fake_supabase.rejected_inserts == []
    assert len(fake_supabase.journal_inserts) == 1
    assert fake_supabase.journal_inserts[0]["homeowner_id"] == "ho-row-1"
    # Contact enrichment: ops can reach a signed-in homeowner.
    state = await resolve_flow_state("t-fk", None)
    assert state.slots.contact_email == "dana@example.com"
    assert state.slots.first_name == "Dana"


@pytest.mark.asyncio
async def test_unresolvable_sub_writes_null_homeowner_id_not_fk_violation(fake_supabase):
    from app.home_ai import HomeAIChatRequest

    request = HomeAIChatRequest(threadId="t-fk2", message="hello")
    await flow_runtime.run_flow_turn(request, homeowner_id="auth-sub-unknown")
    assert fake_supabase.rejected_inserts == []
    assert len(fake_supabase.journal_inserts) == 1
    assert fake_supabase.journal_inserts[0]["homeowner_id"] is None
    # The identity still counts for the contact gate.
    state = await resolve_flow_state("t-fk2", None)
    assert state.homeowner_auth_sub == "auth-sub-unknown"
    assert state.has_identity


# ------------------------------------------------------------- results return
def _stub_generate(content: str):
    async def _generate(request, flow_directives=None, max_images_override=None):
        return HomeAIChatResponse(
            threadId=request.threadId,
            message=HomeAIChatMessage(role="assistant", content=content),
            state=HomeAIConversationState(intent="exploring"),
            model="stub",
            provider="stub",
        )

    return _generate


@pytest.mark.asyncio
async def test_ops_upload_is_idempotent_and_validates_prices(monkeypatch):
    monkeypatch.setattr(settings, "ops_token", "ops-secret", raising=False)
    ops_headers = {"Authorization": "Bearer ops-secret"}
    state = _sensitive_state("t-ops")
    await flow_runtime.persist_flow_state(state)
    token = encode_flow_token(state)
    async with client() as http:
        submitted = await http.post(
            "/api/v1/ai/quote-requests",
            json={"threadId": "t-ops", "flowToken": token, "confirm": True},
            headers=auth_headers(),
        )
        assert submitted.status_code == 201
        qr_id = submitted.json()["quoteRequestId"]

        quote = {"id": "q-1", "providerName": "Brightline", "priceUsd": 2450}
        first = await http.post(
            f"/api/v1/ops/quote-requests/{qr_id}/quotes",
            json={"quotes": [quote]}, headers=ops_headers,
        )
        assert first.status_code == 200
        # The documented ops workflow is curl — a retry must not duplicate.
        retry = await http.post(
            f"/api/v1/ops/quote-requests/{qr_id}/quotes",
            json={"quotes": [quote]}, headers=ops_headers,
        )
        assert retry.json()["quotesUploaded"] == 1
        # Re-sending the same id corrects the entry instead of appending.
        quote["priceUsd"] = 2550
        corrected = await http.post(
            f"/api/v1/ops/quote-requests/{qr_id}/quotes",
            json={"quotes": [quote]}, headers=ops_headers,
        )
        assert corrected.json()["quotesUploaded"] == 1

        # Price sanity: negative, inverted-range, and priceless quotes bounce.
        for bad in (
            {"providerName": "X", "priceUsd": -5},
            {"providerName": "X", "priceLowUsd": 900, "priceHighUsd": 100},
            {"providerName": "X"},
        ):
            resp = await http.post(
                f"/api/v1/ops/quote-requests/{qr_id}/quotes",
                json={"quotes": [bad]}, headers=ops_headers,
            )
            assert resp.status_code == 422, bad

        # Status transitions are guarded; closed is terminal.
        ok = await http.post(
            f"/api/v1/ops/quote-requests/{qr_id}/status",
            json={"status": "closed"}, headers=ops_headers,
        )
        assert ok.status_code == 200
        reopened = await http.post(
            f"/api/v1/ops/quote-requests/{qr_id}/status",
            json={"status": "in_progress"}, headers=ops_headers,
        )
        assert reopened.status_code == 409
        upload_after_close = await http.post(
            f"/api/v1/ops/quote-requests/{qr_id}/quotes",
            json={"quotes": [{"providerName": "Y", "priceUsd": 10}]},
            headers=ops_headers,
        )
        assert upload_after_close.status_code == 409


@pytest.mark.asyncio
async def test_quote_request_bound_to_homeowner(monkeypatch):
    """A record attributed to a signed-in homeowner cannot be read — or have
    its §12 address release triggered — with the shared service token alone."""
    state = _sensitive_state("t-bind")  # homeowner_id set
    await flow_runtime.persist_flow_state(state)
    token = encode_flow_token(state)
    async with client() as http:
        submitted = await http.post(
            "/api/v1/ai/quote-requests",
            json={"threadId": "t-bind", "flowToken": token, "confirm": True},
            headers=auth_headers(),
        )
        assert submitted.status_code == 201
        qr_id = submitted.json()["quoteRequestId"]
        read = await http.get(f"/api/v1/ai/quote-requests/{qr_id}", headers=auth_headers())
        assert read.status_code == 403
        select = await http.post(
            f"/api/v1/ai/quote-requests/{qr_id}/select",
            json={"quoteId": "whatever"},
            headers=auth_headers(),
        )
        assert select.status_code == 403


# ---------------------------------------------------------------- enforcement
def _closed_scan_gates() -> GateDecision:
    return GateDecision(can_prompt_additional_scan=False)


def test_polished_scan_suggestion_is_still_caught():
    """The homeowner-voice sanitizer rewrites capture→record; the patterns
    must catch the rewritten text too."""
    for text in (
        "Feel free to record the hallway while we wait.",
        "You could record another room when you get a chance.",
        "Maybe add the garage so I can see it too.",
        "How about capturing the basement next?",
    ):
        assert enforcement.check(text, _closed_scan_gates()), text


def test_innocent_text_passes_scan_enforcement():
    for text in (
        "I'd record the paint colors you like in a note.",
        "The hallway is a great place for a runner rug.",
        "Let's add a warmer lamp to the reading corner.",
    ):
        assert not enforcement.check(text, _closed_scan_gates()), text


def test_price_figures_blocked_until_quotes_returned():
    """Client decision (Sep 1): the agent never states a price — the app just
    confirms the request went in and a person comes back with real numbers."""
    gates = GateDecision()  # can_state_prices defaults to False
    for text in (
        "You'd probably be looking at $1,500 to $2,500 for walls this size.",
        "Painters around here charge about 300 dollars a day.",
        "Rough ballpark: $2.5k all in.",
    ):
        found = enforcement.check(text, gates)
        assert any(v.rule == "unauthorized_price_figure" for v in found), text


def test_price_figures_allowed_when_presenting_returned_quotes():
    gates = GateDecision(can_state_prices=True)
    text = "Summit Painting came back at $1,840 for walls and trim."
    assert not enforcement.check(text, gates)


def test_price_figures_must_match_the_guidance_card():
    """A guidance card unlocks prices but does not hand the model a blank
    cheque: it may restate the card's band and nothing else (Sep 12 2026)."""
    gates = GateDecision(can_state_prices=True, allowed_price_range=(2400.0, 6800.0))
    for text in (
        "Roughly $2,400 to $6,800, though that's wide on purpose.",
        "Somewhere around $4,000 is typical, but a provider has to look.",
        "Call it $2.5k on the low end.",
    ):
        assert not enforcement.check(text, gates), text
    for text in (
        "You're probably looking at about $15,000 for this.",
        "Painters around here charge about 300 dollars a day.",
        "Materials alone run $120.",
    ):
        found = enforcement.check(text, gates)
        assert any(v.rule == "price_figure_outside_card" for v in found), text


def test_returned_quotes_stay_unbounded():
    """Real ops quotes are real numbers — no card to bound them against."""
    gates = GateDecision(can_state_prices=True, allowed_price_range=None)
    assert not enforcement.check("Summit came back at $18,400.", gates)


def test_plain_numbers_are_not_prices():
    gates = GateDecision()
    for text in (
        "A room this size is about 120 square feet of wall.",
        "The first response usually comes back within 24 hours, all three within 48.",
        "That accent wall is 3.5 meters wide.",
    ):
        assert not enforcement.check(text, gates), text


@pytest.mark.asyncio
async def test_raw_model_text_checked_before_polish(monkeypatch):
    """A model draft that violates in raw form is caught even when polishing
    would have laundered the final text."""

    async def laundered_generate(request, flow_directives=None, max_images_override=None):
        response = HomeAIChatResponse(
            threadId=request.threadId,
            message=HomeAIChatMessage(
                role="assistant",
                # What the homeowner would see AFTER polish:
                content="Feel free to record the hallway while we wait.",
            ),
            state=HomeAIConversationState(intent="design_advice"),
            model="stub",
            provider="stub",
        )
        # What the model actually wrote:
        response._raw_message = "Feel free to capture the hallway while we wait."
        return response

    monkeypatch.setattr(flow_runtime, "generate_home_ai_response", laundered_generate)
    async with client() as http:
        resp = await http.post(
            "/api/v1/ai/home-chat",
            json={
                "message": "what should I do next?",
                "scanContext": {"jobId": "job-x", "processingState": "processing"},
            },
            headers=auth_headers(),
        )
    assert resp.status_code == 200
    from app.flow.wording import SAFE_SCAN_WAIT_COPY

    assert resp.json()["message"]["content"] == SAFE_SCAN_WAIT_COPY


# ------------------------------------------------------------------ scan gate
def test_verified_complete_is_sticky_across_job_pruning():
    engine = FlowEngine()
    state = FlowState(thread_id="t-scan")
    # Server verifies completion once...
    state.scan = engine.reconcile_scan(
        state,
        client_state=ScanProcessingState.COMPLETE,
        client_job_id="job-1",
        client_scan_id=None,
        client_progress=None,
        server_job_state=ScanProcessingState.COMPLETE,
    )
    assert state.scan.server_verified and state.scan.verified_complete
    # ...then the job record is pruned; the claim stays verified.
    state.scan = engine.reconcile_scan(
        state,
        client_state=ScanProcessingState.COMPLETE,
        client_job_id="job-1",
        client_scan_id=None,
        client_progress=None,
        server_job_state=None,
    )
    assert state.scan.server_verified is True


def test_unverified_client_complete_is_honored_but_marked():
    engine = FlowEngine()
    state = FlowState(thread_id="t-scan2")
    state.scan = engine.reconcile_scan(
        state,
        client_state=ScanProcessingState.COMPLETE,
        client_job_id="job-unknown",
        client_scan_id=None,
        client_progress=None,
        server_job_state=None,
    )
    gates = engine.evaluate_gates(state, None)
    assert gates.can_prompt_additional_scan is True  # the SOW's flag is the client's
    assert state.scan.server_verified is False       # ...but we record the trust level


# -------------------------------------------------------------------- config
def test_research_and_demo_flags_default_off():
    """Research and demo features TakeShape didn't sign for stay off; the
    demo env opts in explicitly.

    Pricing is the exception, both halves of it, since Sep 16 2026 (Chance).
    With guidance off the agent answered every cost question with "I can't
    give you a number myself" -- 19 of them in the Sep 16 eval battery, none
    answered. With research off the number it does give comes from a static
    national table, which is a guess about a market it has never seen;
    "go and look it up" is the substance of the question. One search per
    service per area, cached 30 days, static table still the fallback."""
    defaults = Settings.model_construct()  # class defaults, no env/.env applied
    assert defaults.agent_price_guidance_enabled is True
    assert defaults.price_research_enabled is True
    assert defaults.local_context_enabled is False
    assert defaults.local_provider_research_enabled is False
    assert defaults.raw_turn_log_enabled is False


# ----------------------------------------------------------------------- pii
def test_street_masking_covers_loose_suffixes():
    for text in (
        "I live at 4482 Grimmelman Trail if that helps",
        "it's 12 Fox Run",
        "88 Eagle Crossing, Columbus",
        "7 Willow Loop please",
        "19 Cedar Cove",
        "221B Baker Street",
    ):
        assert "[address]" in mask_text(text), text


def test_measurement_phrases_are_not_masked_as_addresses():
    for text in (
        "it's a 5 minute walk to the park",
        "we did a 2 mile loop this morning",
        "about a 10 minute drive",
        "the yard is 40 feet deep",
    ):
        assert "[address]" not in mask_text(text), text


def test_slot_masking_survives_light_normalization():
    slots = Slots(address="4482 Grimmelman Trail, Columbus OH")
    # The homeowner typed it without the comma; the model normalized with one.
    masked = mask_text("sure — 4482 Grimmelman Trail Columbus OH", slots)
    assert "Grimmelman" not in masked


@pytest.mark.asyncio
async def test_address_capture_turn_withholds_user_text(monkeypatch):
    async def capturing_generate(request, flow_directives=None, max_images_override=None):
        response = HomeAIChatResponse(
            threadId=request.threadId,
            message=HomeAIChatMessage(role="assistant", content="Got it, thanks!"),
            state=HomeAIConversationState(intent="quote_readiness"),
            model="stub",
            provider="stub",
        )
        response._flow_capture = {"address": "4482 Grimmelman Trl Columbus OH 43081"}
        return response

    monkeypatch.setattr(flow_runtime, "generate_home_ai_response", capturing_generate)
    state = FlowState(
        thread_id="t-addr",
        opening_delivered=True,
        user_turns=6,
        step=7,
        slots=Slots(zip="43081", project_type="Painting", scope_options=["walls"]),
    )
    await flow_runtime.persist_flow_state(state)
    async with client() as http:
        resp = await http.post(
            "/api/v1/ai/home-chat",
            json={
                "threadId": "t-addr",
                "flowToken": encode_flow_token(state),
                "message": "4482 grimmelman trl, columbus oh 43081",
            },
            headers=auth_headers(),
        )
    assert resp.status_code == 200
    from pathlib import Path

    journal = (
        Path(settings.storage_dir) / "flow_journal" / "t-addr.jsonl"
    ).read_text(encoding="utf-8")
    last = json.loads(journal.splitlines()[-1])
    assert "grimmelman" not in json.dumps(last).lower()
    assert last["userText"].startswith("[address provided")


# --------------------------------------------------------------------- health
@pytest.mark.asyncio
async def test_health_reports_degraded_config():
    async with client() as http:
        resp = await http.get("/health")
    body = resp.json()
    assert resp.status_code == 200
    # Hermetic test env blanks credentials → degraded, with named reasons.
    assert body["status"] == "degraded"
    assert any("LIDARAI_SUPABASE" in w for w in body["configWarnings"])


# ----------------------------------------------------------------- directives
def test_directives_confirm_submission_and_set_expectations():
    state = _sensitive_state()
    state.quote_request = QuoteRequestRef(id="qr_x", status="submitted")
    engine = FlowEngine()
    plan = engine.plan_turn(state, "did it go through?")
    text = flow_runtime._build_directives(
        state, plan, opening=False, price_guidance=None, quotes_to_present=None
    )
    assert "HAS been submitted" in text
    # Softened Sep 2: no exact turnaround times; the accuracy caveat stays.
    assert "24 hours" not in text and "10%" in text
    assert "NEVER promise a specific turnaround" in text
    assert "NEVER claim" not in text


def test_directives_never_promise_confirm_button_to_legacy_clients():
    state = _sensitive_state()
    state.client_flow_aware = False
    engine = FlowEngine()
    plan = engine.plan_turn(state, "let's get quotes")
    text = flow_runtime._build_directives(
        state, plan, opening=False, price_guidance=None, quotes_to_present=None
    )
    assert "you will be told" not in text
    assert "Confirm button" not in text

    state.client_flow_aware = True
    text = flow_runtime._build_directives(
        state, plan, opening=False, price_guidance=None, quotes_to_present=None
    )
    assert "Confirm" in text


def test_estimate_framing_keys_on_flag_not_notes():
    state = _sensitive_state()
    state.quote_request = QuoteRequestRef(id="qr_y", status="quotes_ready")
    engine = FlowEngine()
    plan = engine.plan_turn(state, "any news?")
    real_bid = [{
        "providerName": "Brightline", "priceUsd": 2450,
        "notes": "Estimate valid 30 days", "isEstimate": False,
    }]
    text = flow_runtime._build_directives(
        state, plan, opening=False, price_guidance=None, quotes_to_present=real_bid
    )
    assert "QUOTES ARE BACK" in text  # a real bid is presented as a real bid
    demo_estimate = [dict(real_bid[0], isEstimate=True)]
    text = flow_runtime._build_directives(
        state, plan, opening=False, price_guidance=None, quotes_to_present=demo_estimate
    )
    assert "NOT actual bids" in text


# ------------------------------------------------------- Sep 15 sweep fixes
@pytest.mark.asyncio
async def test_a_flow_token_is_only_valid_on_the_thread_it_was_minted_for(tmp_path):
    """Replaying one conversation's token against another thread id used to
    take that thread over: the token won the revision race, adopted the other
    party's verified identity, and was persisted over their state."""
    victim = FlowState(thread_id="victim-thread", revision=3, homeowner_auth_sub="auth-sub-victim")
    victim.slots = Slots(first_name="Quintin", zip="37203", address="1 Victim Ln")
    await flow_runtime.persist_flow_state(victim)

    attacker = FlowState(thread_id="attacker-thread", revision=9)
    attacker.slots = Slots(first_name="Mallory", zip="90210")

    resolved = await resolve_flow_state("victim-thread", encode_flow_token(attacker))

    assert resolved.slots.first_name == "Quintin"      # the thread's own state
    assert resolved.homeowner_auth_sub == "auth-sub-victim"
    assert resolved.revision == 3                      # the foreign token was ignored


@pytest.mark.asyncio
async def test_its_own_thread_still_accepts_its_token(tmp_path):
    state = FlowState(thread_id="t-own", revision=4, slots=Slots(first_name="Dana"))
    await flow_runtime.persist_flow_state(FlowState(thread_id="t-own", revision=1))
    resolved = await resolve_flow_state("t-own", encode_flow_token(state))
    assert resolved.revision == 4 and resolved.slots.first_name == "Dana"


@pytest.mark.asyncio
async def test_an_oversized_body_is_refused_before_it_is_parsed(monkeypatch):
    monkeypatch.setattr(settings, "max_request_bytes", 2048)
    async with client() as http:
        resp = await http.post(
            "/api/v1/ai/home-chat",
            headers=auth_headers(),
            json={"threadId": "t-big", "message": "hi", "homeContext": {
                "roomCount": 200,
                "rooms": [{"id": f"room-{i}", "name": "Room"} for i in range(200)],
            }},
        )
    assert resp.status_code == 413
