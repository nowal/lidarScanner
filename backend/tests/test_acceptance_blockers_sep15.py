"""The five acceptance blockers from the Sep 15 audit.

1. The homeowner view carries ``selectedQuoteId`` -- the app can show which
   quote was picked after a relaunch.
2. The opening call on a conversation that is underway resumes it instead of
   replaying turn zero (or regenerating one against mid-flow state).
3. An unset Supabase JWT secret is a named config problem, not a silent
   downgrade to "everyone is anonymous".
4. A lead email queued before a restart is re-queued at the next startup.
5. An export the app uploaded to storage can be ingested by the server.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import zipfile
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

import app.flow_runtime as flow_runtime
import app.main as main_module
from app.config import settings
from app.flow import home_registry
from app.flow.state import FlowState, FlowStep, QuoteRequestRef
from app.flow_quotes import QuoteRequestRecord, ReturnedQuote, quote_store
from app.flow_runtime import HomeAIOpeningRequest
from app.main import app


def client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def _record(**overrides) -> QuoteRequestRecord:
    base = dict(
        id="qr_blockers01",
        createdAt="2026-09-15T12:00:00+00:00",
        threadId="t-blockers",
        status="submitted",
        serviceType="Painting",
        scopeOptions=["walls only"],
        zip="37203",
        contactEmail="dana@example.com",
        firstName="Dana",
    )
    base.update(overrides)
    return QuoteRequestRecord(**base)


def _underway_state(thread_id: str, **kwargs) -> FlowState:
    state = FlowState(thread_id=thread_id, opening_delivered=True, user_turns=3)
    state.mark_complete(FlowStep.RECOGNITION)
    state.mark_complete(FlowStep.ENGAGEMENT)
    state.slots.first_name = "Dana"
    state.slots.project_type = "Painting"
    state.client_flow_aware = True
    for key, value in kwargs.items():
        setattr(state, key, value)
    return state


# ----------------------------------------------------------- 1. selection
def test_homeowner_view_carries_the_selected_quote():
    quote = ReturnedQuote(providerName="Nash Painting", priceUsd=1850)
    record = _record(quotes=[quote], selectedQuoteId=quote.id, status="quotes_ready")
    view = record.homeowner_view()
    assert view["selectedQuoteId"] == quote.id
    assert _record().homeowner_view()["selectedQuoteId"] is None


@pytest.mark.asyncio
async def test_select_endpoint_confirms_the_selection(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "storage_dir", str(tmp_path))
    quote = ReturnedQuote(providerName="Nash Painting", priceUsd=1850)
    await quote_store.save(_record(id="qr_select01", quotes=[quote], status="quotes_ready"))
    async with client() as http:
        resp = await http.post("/api/v1/ai/quote-requests/qr_select01/select", json={"quoteId": quote.id})
    assert resp.status_code == 200
    assert resp.json()["selectedQuoteId"] == quote.id


# -------------------------------------------------------------- 2. resume
@pytest.mark.asyncio
async def test_opening_on_an_underway_thread_resumes_instead_of_replaying(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "storage_dir", str(tmp_path))
    state = _underway_state("t-resume-mid")
    await flow_runtime.persist_flow_state(state)

    response = await flow_runtime.run_opening_turn(HomeAIOpeningRequest(threadId="t-resume-mid"))
    assert response.model == "resume"
    assert "Welcome back, Dana" in response.message.content
    assert "painting" in response.message.content
    assert "first name" not in response.message.content.lower()
    assert response.flow is not None and response.flow.token


@pytest.mark.asyncio
async def test_opening_after_submission_says_where_the_request_is(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "storage_dir", str(tmp_path))
    await quote_store.save(_record(id="qr_resume01", threadId="t-resume-sub"))
    state = _underway_state("t-resume-sub", quote_request=QuoteRequestRef(id="qr_resume01"))
    await flow_runtime.persist_flow_state(state)

    response = await flow_runtime.run_opening_turn(HomeAIOpeningRequest(threadId="t-resume-sub"))
    assert response.model == "resume"
    # Sep 17 (#101): the resume line says where the request is without
    # making the people behind the agent the subject of the sentence.
    content = response.message.content
    assert "out with local providers" in content
    assert "I'll bring their quotes back" in content
    assert "my team" not in content
    assert response.quoteDraft is None
    assert response.state.quoteStatus == "sent"


@pytest.mark.asyncio
async def test_opening_with_quotes_in_says_so(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "storage_dir", str(tmp_path))
    quote = ReturnedQuote(providerName="Nash Painting", priceUsd=1850)
    await quote_store.save(
        _record(id="qr_resume02", threadId="t-resume-quotes", status="quotes_ready", quotes=[quote])
    )
    state = _underway_state("t-resume-quotes", quote_request=QuoteRequestRef(id="qr_resume02"))
    await flow_runtime.persist_flow_state(state)

    response = await flow_runtime.run_opening_turn(HomeAIOpeningRequest(threadId="t-resume-quotes"))
    assert "Quotes are in" in response.message.content


@pytest.mark.asyncio
async def test_opening_after_a_choice_names_the_provider(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "storage_dir", str(tmp_path))
    quote = ReturnedQuote(providerName="AllBright Pro Painting", priceLowUsd=1600, priceHighUsd=2100)
    await quote_store.save(_record(
        id="qr_resume03", threadId="t-resume-chosen", status="quotes_ready",
        quotes=[quote], selectedQuoteId=quote.id,
    ))
    state = _underway_state("t-resume-chosen", quote_request=QuoteRequestRef(id="qr_resume03"))
    await flow_runtime.persist_flow_state(state)
    response = await flow_runtime.run_opening_turn(HomeAIOpeningRequest(threadId="t-resume-chosen"))
    assert "You chose AllBright Pro Painting" in response.message.content


@pytest.mark.asyncio
async def test_the_agent_is_told_which_quote_was_chosen(monkeypatch, tmp_path):
    """The app's Choose control records the selection server-side; the next
    turn's directives must say so, or the agent tells the homeowner it
    cannot route their choice (seen live, Sep 15)."""
    monkeypatch.setattr(settings, "storage_dir", str(tmp_path))
    quote = ReturnedQuote(providerName="AllBright Pro Painting", priceUsd=1900)
    record = _record(id="qr_chosen01", status="quotes_ready", quotes=[quote], selectedQuoteId=quote.id)
    await quote_store.save(record)
    state = _underway_state("t-chosen", quote_request=QuoteRequestRef(id="qr_chosen01"))
    selected = await flow_runtime._selected_quote(state)
    assert selected == {"providerName": "AllBright Pro Painting", "price": "$1,900"}
    plan = flow_runtime._engine.plan_turn(state, "ok")
    text = flow_runtime._build_directives(
        state, plan, opening=False, price_guidance=None, quotes_to_present=None,
        selected_quote=selected,
    )
    assert "CHOSEN a quote: AllBright Pro Painting at $1,900" in text
    assert await flow_runtime._selected_quote(_underway_state("t-none")) is None


@pytest.mark.asyncio
async def test_resume_keeps_an_agreed_card(monkeypatch, tmp_path):
    """A homeowner who said yes to the request and then relaunched must not
    lose the card: the old path blanked it on every opening call."""
    monkeypatch.setattr(settings, "storage_dir", str(tmp_path))
    state = _underway_state("t-resume-card", request_accepted=True)
    state.slots.scope_options = ["walls only"]
    state.slots.zip = "37203"
    state.slots.contact_email = "dana@example.com"
    await flow_runtime.persist_flow_state(state)

    response = await flow_runtime.run_opening_turn(HomeAIOpeningRequest(threadId="t-resume-card"))
    assert response.model == "resume"
    assert response.quoteDraft is not None
    assert response.quoteDraft.serviceType == "Painting"


@pytest.mark.asyncio
async def test_a_fresh_thread_still_gets_the_opener(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "storage_dir", str(tmp_path))
    response = await flow_runtime.run_opening_turn(HomeAIOpeningRequest(threadId="t-fresh"))
    assert response.model != "resume"
    assert response.quoteDraft is None


# ------------------------------------------------------------ 3. config
def test_missing_jwt_secret_is_a_named_config_problem(monkeypatch):
    monkeypatch.setattr(settings, "supabase_jwt_secret", "")
    assert any("JWT_SECRET" in p for p in main_module.config_problems())
    monkeypatch.setattr(settings, "supabase_jwt_secret", "s3cret")
    assert not any("JWT_SECRET" in p for p in main_module.config_problems())


# ------------------------------------------------------- 4. durable outbox
@pytest.mark.asyncio
async def test_a_queued_lead_is_redriven_after_a_restart(monkeypatch, tmp_path):
    from app.flow import ops_email as module

    monkeypatch.setattr(settings, "storage_dir", str(tmp_path))
    monkeypatch.setattr(settings, "ops_email", "ops@example.com")
    monkeypatch.setattr(module, "_queue", None)
    # The last process queued two leads and delivered one before it died;
    # a third predates the stamps and must be left alone.
    await quote_store.save(_record(id="qr_lost01", opsEmailQueuedAt="2026-09-15T12:00:00+00:00"))
    await quote_store.save(_record(
        id="qr_done01", opsEmailQueuedAt="2026-09-15T12:00:00+00:00",
        opsEmailDeliveredAt="2026-09-15T12:01:00+00:00",
    ))
    await quote_store.save(_record(id="qr_old01"))
    sent = []

    async def fake_send(record):
        sent.append(record.id)
        return "sent"

    monkeypatch.setattr(module, "send_ops_email", fake_send)
    worker = asyncio.create_task(module.run_ops_email_worker())
    await asyncio.sleep(0.05)
    await asyncio.wait_for(module._queue.join(), timeout=5)
    worker.cancel()
    assert sent == ["qr_lost01"]


@pytest.mark.asyncio
async def test_delivery_is_stamped_on_the_record(monkeypatch, tmp_path):
    from app.flow import ops_email as module

    monkeypatch.setattr(settings, "storage_dir", str(tmp_path))
    monkeypatch.setattr(settings, "ops_email", "ops@example.com")
    record = _record(id="qr_stamp01", opsEmailQueuedAt="2026-09-15T12:00:00+00:00")
    await quote_store.save(record)

    async def fake_transport(subject, body, html=None, *, outbox_key="message"):
        return "sent"

    monkeypatch.setattr(module, "send_ops_message", fake_transport)
    assert await module.send_ops_email(record) == "sent"
    stored = await quote_store.get("qr_stamp01")
    assert stored.opsEmailDeliveredAt is not None


@pytest.mark.asyncio
async def test_submission_stamps_the_queue_time(monkeypatch, tmp_path):
    """The stamp is what makes the queue durable: it is written with the
    record on submit, before the worker has done anything."""
    from app import flow_api

    monkeypatch.setattr(settings, "storage_dir", str(tmp_path))
    monkeypatch.setattr(flow_api, "_schedule_ops_email", lambda record: True)
    state = _underway_state("t-stamp", request_accepted=True)
    state.slots.scope_options = ["walls only"]
    state.slots.zip = "37203"
    state.slots.contact_email = "dana@example.com"
    await flow_runtime.persist_flow_state(state)
    async with client() as http:
        resp = await http.post("/api/v1/ai/quote-requests", json={"threadId": "t-stamp", "confirm": True})
    assert resp.status_code == 201, resp.text
    stored = await quote_store.get(resp.json()["quoteRequestId"])
    assert stored.opsEmailQueuedAt is not None
    assert stored.opsEmailDeliveredAt is None


# -------------------------------------------------------------- 5. ingest
from tests.test_bundle_check import export_zip  # noqa: E402,F401  (fixture)


@pytest.mark.asyncio
async def test_an_uploaded_export_is_ingested_from_storage(monkeypatch, export_zip):
    from app.flow import supabase_store

    async def fake_download(bucket, object_path, dest: Path):
        assert (bucket, object_path) == ("metashape-exports", "anonymous/inst/3f2c-test/scan.zip")
        shutil.copy(export_zip, dest)
        return True

    monkeypatch.setattr(supabase_store, "download_object", fake_download)
    status = await home_registry.ingest_from_storage(
        "3f2c-test", "metashape-exports", "anonymous/inst/3f2c-test/scan.zip", enrich=False
    )
    assert status["status"] == "done", status
    assert status["roomCount"] == 2
    index = home_registry.load_index("3f2c-test")
    assert index is not None and len(index.rooms) == 2
    assert home_registry.ingest_status("3f2c-test")["status"] == "done"


@pytest.mark.asyncio
async def test_a_missing_object_fails_cleanly(monkeypatch, tmp_path):
    from app.flow import supabase_store

    monkeypatch.setattr(settings, "storage_dir", str(tmp_path))

    async def missing(bucket, object_path, dest):
        return False

    monkeypatch.setattr(supabase_store, "download_object", missing)
    status = await home_registry.ingest_from_storage("nope-home", "metashape-exports", "x/nope-home/a.zip")
    assert status["status"] == "failed"
    assert "download" in status["error"]


@pytest.mark.asyncio
async def test_the_app_can_only_ingest_its_own_upload(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "storage_dir", str(tmp_path))
    queued = []

    async def fake_ingest(home_id, bucket, object_path, *, enrich=True):
        queued.append((home_id, object_path, enrich))
        return {"status": "done"}

    monkeypatch.setattr(home_registry, "ingest_from_storage", fake_ingest)
    async with client() as http:
        wrong = await http.post(
            "/api/v1/ai/homes/abc-123/ingest",
            json={"objectPath": "anonymous/inst/someone-else/scan.zip"},
        )
        assert wrong.status_code == 403
        not_zip = await http.post(
            "/api/v1/ai/homes/abc-123/ingest",
            json={"objectPath": "anonymous/inst/abc-123/scan.tar"},
        )
        assert not_zip.status_code == 422
        ok = await http.post(
            "/api/v1/ai/homes/ABC-123/ingest",
            json={"objectPath": "anonymous/inst/abc-123/scan.zip", "enrich": False},
        )
        assert ok.status_code == 202, ok.text
        assert ok.json()["status"] == "queued"
    assert queued == [("abc-123", "anonymous/inst/abc-123/scan.zip", False)]


@pytest.mark.asyncio
async def test_ops_ingest_needs_the_ops_token(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "storage_dir", str(tmp_path))
    monkeypatch.setattr(settings, "ops_token", "ops-secret")
    async with client() as http:
        denied = await http.post("/api/v1/ops/homes/h1/ingest", json={"objectPath": "any/path.zip"})
        assert denied.status_code == 401
        status = await http.get(
            "/api/v1/ops/homes/h1/ingest", headers={"Authorization": "Bearer ops-secret"}
        )
        assert status.status_code == 200
        assert status.json()["ingested"] is False
