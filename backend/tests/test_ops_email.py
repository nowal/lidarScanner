"""Ops email loop (provider-finder scope, agreed Sep 1): lead email
composition, outbox capture when SMTP is unconfigured, the signed
quote-entry link, and the entry page's path back to the homeowner."""

import json
import time

import pytest
from httpx import ASGITransport, AsyncClient

import app.flow.supabase_store as supabase_store
from app.config import settings
from app.flow.ops_email import (
    build_ops_email,
    entry_signature,
    send_ops_email,
    verify_entry_signature,
)
from app.flow.partners import find_partners
from app.flow_quotes import QuoteRequestRecord, quote_store
from app.main import app


@pytest.fixture(autouse=True)
def isolated_storage(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "storage_dir", str(tmp_path))
    supabase_store._homeowner_cache.clear()
    yield tmp_path
    supabase_store._homeowner_cache.clear()


def client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


ADDRESS = "123 Hidden Lane, Nashville TN"


def _record(**overrides) -> QuoteRequestRecord:
    base = dict(
        id="qr_testloop01",
        createdAt="2026-09-01T12:00:00+00:00",
        threadId="t-ops-email",
        status="submitted",
        serviceType="Painting",
        scopeOptions=["walls + trim"],
        materials=["low-VOC paint"],
        zip="37203",
        address=ADDRESS,
        contactEmail="dana@example.com",
        contactPhone="(615) 555-0100",
        firstName="Dana",
        synopsis="Dana is interested in painting.",
        measurements={"floorAreaSquareFeet": 420},
        modelLink={"kind": "supabase_signed_url", "url": "https://signed.example/model.usdz"},
    )
    base.update(overrides)
    return QuoteRequestRecord(**base)


# ---------------------------------------------------------------- partners
def test_the_seed_is_never_surfaced_as_a_partner():
    """The seed keeps the local file from being empty; it is not data. It used
    to be returned here and reached Quintin's lead email as a "TakeShape
    partner" with a caption (Sep 13). Now no reader ever sees it."""
    assert find_partners("Painting", "37203") == []
    assert find_partners("Painting", None) == []
    assert find_partners(None, "37203") == []


# ------------------------------------------------------------- composition
def test_ops_email_never_contains_the_address():
    subject, body = build_ops_email(_record(), find_partners("Painting", "37203"), None)
    assert ADDRESS not in body and "Hidden Lane" not in body
    assert "withheld until the homeowner selects a quote" in body
    assert "dana@example.com" in body           # contact IS for ops
    assert "qr_testloop01" in subject
    assert "https://signed.example/model.usdz" in body


def test_ops_email_shows_no_entry_link_without_base_url(monkeypatch):
    monkeypatch.setattr(settings, "public_base_url", "")
    _, body = build_ops_email(_record(), [], None)
    assert "/ops/entry/" not in body
    assert "LIDARAI_PUBLIC_BASE_URL" in body


# ------------------------------------------------------------ signed links
def test_entry_signature_roundtrip_and_expiry():
    exp = int(time.time()) + 600
    sig = entry_signature("qr_abc", exp)
    assert verify_entry_signature("qr_abc", exp, sig)
    # Flip the last hex digit to something it demonstrably is not. Pinning a
    # constant here ("00") silently passes the real signature back in
    # whenever it happens to end that way, which is one run in 256.
    tampered = sig[:-1] + ("f" if sig[-1] != "f" else "e")
    assert tampered != sig
    assert not verify_entry_signature("qr_abc", exp, tampered)
    assert not verify_entry_signature("qr_other", exp, sig)
    stale = int(time.time()) - 5
    assert not verify_entry_signature("qr_abc", stale, entry_signature("qr_abc", stale))


# ------------------------------------------------------------------ outbox
@pytest.mark.asyncio
async def test_outbox_capture_without_smtp(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "ops_email", "ops-test@example.com")
    monkeypatch.setattr(settings, "smtp_host", "")
    monkeypatch.setattr(settings, "public_base_url", "https://demo.example")
    record = _record()
    result = await send_ops_email(record)
    assert result == "outbox"
    captured = json.loads((tmp_path / "ops_outbox" / "qr_testloop01.json").read_text(encoding="utf-8"))
    assert captured["to"] == "ops-test@example.com"
    # exp/sig ride in the path — query-string "=" gets mangled by email
    # quoted-printable encoding (observed live).
    assert "/api/v1/ops/entry/qr_testloop01/" in captured["body"]
    assert "?" not in [l for l in captured["body"].splitlines() if "/ops/entry/" in l][0]
    assert ADDRESS not in captured["body"]
    # The seed's placeholder used to be listed with a caption. It reached
    # Quintin's inbox as "TakeShape partner" (Sep 13); now nothing downstream
    # of the row matcher ever sees a sample row.
    assert "SAMPLE ROW" not in captured["body"]
    assert "Sample row" not in captured["body"]


@pytest.mark.asyncio
async def test_ops_email_disabled_without_recipient(monkeypatch):
    monkeypatch.setattr(settings, "ops_email", "")
    assert await send_ops_email(_record()) == "disabled"


@pytest.mark.asyncio
async def test_resend_transport_preferred_over_smtp(monkeypatch):
    """PaaS hosts block outbound SMTP, so when a Resend key exists it wins."""
    import app.flow.ops_email as ops_email_mod

    calls = {}

    async def fake_resend(to, subject, body, html=None):
        calls["to"] = to
        calls["has_html"] = bool(html)

    monkeypatch.setattr(settings, "ops_email", "ops-test@example.com")
    monkeypatch.setattr(settings, "resend_api_key", "re_test_key")
    monkeypatch.setattr(settings, "smtp_host", "smtp.example.com")
    monkeypatch.setattr(settings, "smtp_username", "user")
    monkeypatch.setattr(settings, "smtp_password", "pass")
    monkeypatch.setattr(ops_email_mod, "_send_via_resend", fake_resend)
    assert await send_ops_email(_record()) == "sent"
    assert calls["to"] == "ops-test@example.com"
    assert calls["has_html"]  # the polished HTML part rides along


# -------------------------------------------------------------- entry page
@pytest.mark.asyncio
async def test_entry_page_and_quote_submission_flow():
    record = _record()
    await quote_store.save(record)
    exp = int(time.time()) + 3600
    sig = entry_signature(record.id, exp)
    async with client() as http:
        # Path variant (what the email links); query variant kept for compat.
        page = await http.get(f"/api/v1/ops/entry/{record.id}/{exp}/{sig}")
        assert page.status_code == 200
        assert "Enter the checked quote" in page.text
        assert ADDRESS not in page.text
        assert "withheld until the homeowner selects a quote" in page.text
        legacy = await http.get(f"/api/v1/ops/entry/{record.id}", params={"exp": exp, "sig": sig})
        assert legacy.status_code == 200

        bad = await http.get(f"/api/v1/ops/entry/{record.id}", params={"exp": exp, "sig": "f" * 64})
        assert bad.status_code == 403

        incomplete = await http.post(
            f"/api/v1/ops/entry/{record.id}",
            json={"exp": exp, "sig": sig, "providerName": "Summit Painting"},
        )
        assert incomplete.status_code == 422

        posted = await http.post(
            f"/api/v1/ops/entry/{record.id}",
            json={
                "exp": exp,
                "sig": sig,
                "providerName": "Summit Painting",
                "priceUsd": 1850,
                "notes": "Walls and trim, two coats.",
            },
        )
        assert posted.status_code == 200
        assert posted.json()["status"] == "quotes_ready"

    updated = await quote_store.get(record.id)
    assert updated.status == "quotes_ready"
    assert len(updated.quotes) == 1
    homeowner = updated.homeowner_view()
    assert homeowner["quotes"][0]["providerName"] == "Summit Painting"
    assert homeowner["quotes"][0]["priceUsd"] == 1850
    first_id = updated.quotes[0].id

    # A corrected price for the same company replaces the entry (the page
    # mints a new id each time, so this used to stack a duplicate -- seen
    # 2026-09-14); the quote id survives so a selection still points at it.
    # A different company is a second quote.
    async with client() as http:
        corrected = await http.post(
            f"/api/v1/ops/entry/{record.id}",
            json={"exp": exp, "sig": sig, "providerName": "summit  painting", "priceUsd": 1950},
        )
        assert corrected.status_code == 200
        assert corrected.json()["quoteId"] == first_id
        other = await http.post(
            f"/api/v1/ops/entry/{record.id}",
            json={"exp": exp, "sig": sig, "providerName": "Harpeth Painting", "priceUsd": 2400},
        )
        assert other.status_code == 200
    updated = await quote_store.get(record.id)
    assert [(q.providerName, q.priceUsd) for q in updated.quotes] == [
        ("summit  painting", 1950),
        ("Harpeth Painting", 2400),
    ]
    assert updated.quotes[0].id == first_id


@pytest.mark.asyncio
async def test_entry_page_rejects_expired_link():
    record = _record(id="qr_expired01")
    await quote_store.save(record)
    exp = int(time.time()) - 10
    sig = entry_signature(record.id, exp)
    async with client() as http:
        page = await http.get(f"/api/v1/ops/entry/{record.id}", params={"exp": exp, "sig": sig})
        assert page.status_code == 403


# ------------------------------------------------------------ delivery queue
@pytest.mark.asyncio
async def test_lead_email_survives_the_request_that_created_it(monkeypatch, tmp_path):
    """A task spawned inside a request handler dies with that request's
    context: a submission through the demo proxy logged "scheduled" and then
    nothing, losing the lead silently. The worker is owned by startup."""
    import asyncio

    from app.flow import ops_email as module

    monkeypatch.setattr(settings, "ops_email", "ops@example.com")
    monkeypatch.setattr(module, "_queue", None)
    sent = []

    async def fake_send(record):
        sent.append(record.id)
        return "sent"

    monkeypatch.setattr(module, "send_ops_email", fake_send)
    worker = asyncio.create_task(module.run_ops_email_worker())
    await asyncio.sleep(0)                       # let the worker create the queue

    async def a_request_that_ends_immediately():
        assert module.queue_ops_email(_record(id="qr_queued01")) is True

    await a_request_that_ends_immediately()
    await asyncio.wait_for(module._queue.join(), timeout=5)
    worker.cancel()
    assert sent == ["qr_queued01"], "the lead must be delivered after the request is gone"


@pytest.mark.asyncio
async def test_a_failing_lead_does_not_kill_the_worker(monkeypatch):
    import asyncio

    from app.flow import ops_email as module

    monkeypatch.setattr(settings, "ops_email", "ops@example.com")
    monkeypatch.setattr(module, "_queue", None)
    seen = []

    async def explode_once(record):
        seen.append(record.id)
        if len(seen) == 1:
            raise RuntimeError("provider research blew up")
        return "sent"

    monkeypatch.setattr(module, "send_ops_email", explode_once)
    worker = asyncio.create_task(module.run_ops_email_worker())
    await asyncio.sleep(0)
    module.queue_ops_email(_record(id="qr_bad"))
    module.queue_ops_email(_record(id="qr_good"))
    await asyncio.wait_for(module._queue.join(), timeout=5)
    worker.cancel()
    assert seen == ["qr_bad", "qr_good"], "one bad lead must not stop the queue"


def test_queueing_is_a_no_op_when_email_is_off(monkeypatch):
    from app.flow import ops_email as module

    monkeypatch.setattr(settings, "ops_email", "")
    assert module.queue_ops_email(_record()) is False
