"""Integration tests for the flow API (API_CONTRACT_V1).

These run the real FastAPI app with the deterministic local fallback (no
OpenAI key), which exercises the full flow runtime: state resolution, scan
reconciliation, gating, slot capture, journaling, tokens, and the additive
wire fields — everything except the model itself.
"""

import json
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

import app.flow_runtime as flow_runtime
from app.config import settings
from app.flow import FlowState, FlowTokenCodec, ScanProcessingState
from app.flow.pii import mask_text
from app.flow.state import Slots
from app.flow.wording import SAFE_SCAN_WAIT_COPY
from app.home_ai import (
    HomeAIChatMessage,
    HomeAIChatRequest,
    HomeAIChatResponse,
    HomeAIConversationState,
)
from app.main import app

# The 5-value enum the shipped iOS client decodes strictly — additions break TestFlight.
LEGACY_INTENTS = {"exploring", "design_advice", "pricing", "quote_readiness", "provider_request"}


@pytest.fixture(autouse=True)
def isolated_storage(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "storage_dir", str(tmp_path))
    monkeypatch.setattr(settings, "openai_api_key", "", raising=False)
    yield tmp_path


def client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def auth_headers():
    if not settings.auth_token:
        return {}
    return {"Authorization": f"Bearer {settings.auth_token}"}


def sample_context() -> dict:
    return {
        "roomCount": 1,
        "rooms": [
            {
                "id": "room-1",
                "name": "Room 1",
                "type": "RoomPlan captured area",
                "floorAreaSquareMeters": 16.7,
                "wallCount": 4,
                "doorCount": 1,
                "windowCount": 2,
                "objectCount": 3,
            }
        ],
        "totals": {"floorAreaSquareMeters": 16.7, "roomCount": 1},
        "meshSummary": {"photorealStatus": "processing", "keyframeCount": 4},
    }


# --------------------------------------------------------------------- legacy
@pytest.mark.asyncio
async def test_legacy_request_shape_still_works_and_flow_is_additive():
    async with client() as http:
        resp = await http.post(
            "/api/v1/ai/home-chat",
            json={"message": "What color should I paint this room?"},
            headers=auth_headers(),
        )
    assert resp.status_code == 200
    body = resp.json()
    # Every field the shipped iOS decoder requires, with legal values.
    for field in ("schemaVersion", "threadId", "message", "state", "suggestedReplies",
                  "model", "provider", "usedFallback"):
        assert field in body, f"legacy field {field} missing"
    assert body["state"]["intent"] in LEGACY_INTENTS
    assert isinstance(body["state"]["requiresExplicitApproval"], bool)
    assert body["state"]["quoteStatus"] in {
        "exploring", "drafting", "awaiting_approval", "approved", "sent"
    }
    assert isinstance(body["message"]["content"], str) and body["message"]["content"]
    # Additive flow object with a signed token.
    assert body["flow"]["token"]
    assert body["flow"]["step"] >= 1
    assert body["flow"]["gates"]["canPromptAdditionalScan"] is False  # no scan info → gated


# ------------------------------------------------------------------ flow state
@pytest.mark.asyncio
async def test_flow_token_round_trip_captures_zip():
    async with client() as http:
        first = await http.post(
            "/api/v1/ai/home-chat",
            json={"message": "I want to repaint the bedroom", "homeContext": sample_context()},
            headers=auth_headers(),
        )
        token = first.json()["flow"]["token"]
        thread_id = first.json()["threadId"]
        second = await http.post(
            "/api/v1/ai/home-chat",
            json={
                "threadId": thread_id,
                "flowToken": token,
                "message": "Sure — my zip code is 37203",
                "homeContext": sample_context(),
            },
            headers=auth_headers(),
        )
    body = second.json()
    assert body["flow"]["slots"]["zip"] == "37203"
    assert 4 in body["flow"]["completedSteps"]


@pytest.mark.asyncio
async def test_state_survives_without_token_via_state_file():
    async with client() as http:
        first = await http.post(
            "/api/v1/ai/home-chat",
            json={"message": "my zip is 37203 by the way", "homeContext": sample_context()},
            headers=auth_headers(),
        )
        thread_id = first.json()["threadId"]
        # Legacy client: echoes nothing.
        second = await http.post(
            "/api/v1/ai/home-chat",
            json={"threadId": thread_id, "message": "what about the floors?"},
            headers=auth_headers(),
        )
    assert second.json()["flow"]["slots"]["zip"] == "37203"


# -------------------------------------------------------------------- SOW §3
@pytest.mark.asyncio
async def test_server_job_state_wins_over_client_claim(monkeypatch):
    monkeypatch.setattr(
        flow_runtime, "_server_job_state", lambda job_id: ScanProcessingState.PROCESSING
    )
    async with client() as http:
        resp = await http.post(
            "/api/v1/ai/home-chat",
            json={
                "message": "hello",
                "scanContext": {"jobId": "job-1", "processingState": "complete"},
            },
            headers=auth_headers(),
        )
    gates = resp.json()["flow"]["gates"]
    assert gates["scanProcessingComplete"] is False
    assert gates["canPromptAdditionalScan"] is False


@pytest.mark.asyncio
async def test_scan_complete_opens_gate(monkeypatch):
    monkeypatch.setattr(
        flow_runtime, "_server_job_state", lambda job_id: ScanProcessingState.COMPLETE
    )
    async with client() as http:
        resp = await http.post(
            "/api/v1/ai/home-chat",
            json={
                "message": "hello",
                "scanContext": {"jobId": "job-1", "processingState": "processing"},
            },
            headers=auth_headers(),
        )
    assert resp.json()["flow"]["gates"]["canPromptAdditionalScan"] is True


@pytest.mark.asyncio
async def test_enforcement_replaces_violating_output(monkeypatch):
    """If the model insists on suggesting more scanning while gated, the
    homeowner sees safe copy, and the drafts are journaled."""

    async def violating_generate(request, **kwargs):
        return HomeAIChatResponse(
            threadId=request.threadId or "t",
            message=HomeAIChatMessage(
                role="assistant",
                content="You should scan another room so I can see more of the house.",
            ),
            state=HomeAIConversationState(intent="design_advice"),
            model="stub",
            provider="stub",
        )

    monkeypatch.setattr(flow_runtime, "generate_home_ai_response", violating_generate)
    async with client() as http:
        resp = await http.post(
            "/api/v1/ai/home-chat",
            json={
                "message": "what else should I do?",
                "scanContext": {"jobId": "job-1", "processingState": "processing"},
            },
            headers=auth_headers(),
        )
    assert resp.status_code == 200
    assert resp.json()["message"]["content"] == SAFE_SCAN_WAIT_COPY
    journal = (
        (Path(settings.storage_dir) / "flow_journal" / "journal.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    )
    last = json.loads(journal[-1])
    assert len(last["suppressedDrafts"]) == 2
    assert last["suppressedDrafts"][0]["violations"] == ["scan_suggestion_while_processing"]


# -------------------------------------------------------------------- opening
def _stub_opening_generate(content: str = "Hi — I can see your living room and that deep green sofa. What should we call you?"):
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
async def test_opening_turn_is_idempotent(monkeypatch):
    monkeypatch.setattr(flow_runtime, "generate_home_ai_response", _stub_opening_generate())
    async with client() as http:
        first = await http.post(
            "/api/v1/ai/home-chat/opening",
            json={"threadId": "thread-open-1", "homeContext": sample_context()},
            headers=auth_headers(),
        )
        second = await http.post(
            "/api/v1/ai/home-chat/opening",
            json={"threadId": "thread-open-1", "homeContext": sample_context()},
            headers=auth_headers(),
        )
    assert first.status_code == 200
    body = first.json()
    assert body["message"]["content"]
    assert body["flow"]["step"] >= 2
    assert 1 in body["flow"]["completedSteps"] and 2 in body["flow"]["completedSteps"]
    assert second.json()["message"]["id"] == body["message"]["id"]


@pytest.mark.asyncio
async def test_fallback_opening_is_never_cached_as_the_opener():
    """A model failure must not freeze generic error copy in as the thread's
    one grounded opener: steps 1-2 stay incomplete and a later call retries."""
    async with client() as http:  # no provider configured → local fallback
        first = await http.post(
            "/api/v1/ai/home-chat/opening",
            json={"threadId": "thread-open-fb", "homeContext": sample_context()},
            headers=auth_headers(),
        )
    assert first.status_code == 200
    body = first.json()
    assert body["usedFallback"] is True
    assert 1 not in body["flow"]["completedSteps"]
    assert 2 not in body["flow"]["completedSteps"]
    # Nothing cached: no opening cache file was written for this thread.
    cache_files = list(Path(settings.storage_dir).glob("flow_state/*opening*"))
    assert cache_files == []


# ------------------------------------------------------------- quotes and ops
def _full_slots_state(thread_id: str) -> FlowState:
    return FlowState(
        thread_id=thread_id,
        opening_delivered=True,
        user_turns=6,
        slots=Slots(
            first_name="Dana",
            zip="37203",
            project_type="Painting",
            scope_options=["walls only", "walls + trim"],
            materials=["low-VOC paint"],
            address="123 Maple Street, Nashville TN",
            contact_email="dana@example.com",
        ),
    )


async def _full_slots_token(thread_id: str) -> str:
    """A submission-ready thread, the way production reaches one: sensitive
    values live in the durable store; the client token carries only
    captured-flags and is merged back server-side."""
    state = _full_slots_state(thread_id)
    await flow_runtime.persist_flow_state(state)
    return flow_runtime.encode_flow_token(state)


@pytest.mark.asyncio
async def test_quote_request_missing_slots_returns_409():
    async with client() as http:
        resp = await http.post(
            "/api/v1/ai/quote-requests",
            json={"threadId": "thread-q0", "confirm": True},
            headers=auth_headers(),
        )
    assert resp.status_code == 409
    assert "projectType" in resp.json()["missingSlots"]


@pytest.mark.asyncio
async def test_quote_lifecycle_end_to_end(monkeypatch):
    monkeypatch.setattr(settings, "ops_token", "ops-secret", raising=False)
    ops_headers = {"Authorization": "Bearer ops-secret"}
    thread_id = "thread-q1"
    token = await _full_slots_token(thread_id)

    async with client() as http:
        # Submit (step 9).
        submitted = await http.post(
            "/api/v1/ai/quote-requests",
            json={
                "threadId": thread_id,
                "flowToken": token,
                "confirm": True,
                "homeContext": sample_context(),
            },
            headers=auth_headers(),
        )
        assert submitted.status_code == 201
        qr_id = submitted.json()["quoteRequestId"]
        # Softened Sep 2: no exact turnaround anywhere homeowner-facing.
        expectations = submitted.json()["expectations"]
        assert "copy" in expectations and "Hours" not in str(list(expectations))

        # Ops sees the package; the address is withheld pre-selection (§12).
        package = (
            await http.get(f"/api/v1/ops/quote-requests/{qr_id}", headers=ops_headers)
        ).json()
        assert package["address"] is None
        assert package["addressReleasePolicy"] == "withheld_until_quote_selected"
        assert package["homeowner"]["contact"]["email"] == "dana@example.com"
        assert package["project"]["serviceType"] == "Painting"
        assert package["project"]["measurements"]["floorAreaSquareFeet"] == pytest.approx(
            179.8, abs=0.5
        )

        # Ops uploads two quotes (step 10).
        upload = await http.post(
            f"/api/v1/ops/quote-requests/{qr_id}/quotes",
            json={
                "quotes": [
                    {"providerName": "Brightline Painting", "priceUsd": 2450,
                     "lineItems": [{"item": "Walls (2 coats)", "priceUsd": 1800}]},
                    {"providerName": "Harbor Coatings", "priceLowUsd": 2100, "priceHighUsd": 2900},
                ]
            },
            headers=ops_headers,
        )
        assert upload.status_code == 200
        assert upload.json()["quotesUploaded"] == 2

        # Homeowner app polls and sees sanitized quotes.
        polled = (
            await http.get(f"/api/v1/ai/quote-requests/{qr_id}", headers=auth_headers())
        ).json()
        assert polled["status"] == "quotes_ready"
        assert len(polled["quotes"]) == 2
        assert "homeownerId" not in json.dumps(polled)

        # The agent presents them on the next turn; a fresh token from the
        # submission-time state carries the quoteRequest ref.
        from app.flow_runtime import resolve_flow_state

        state = await resolve_flow_state(thread_id, None)
        assert state.quote_request is not None

        # A fallback turn must NOT burn the presentation (the homeowner never
        # saw the quotes) — status stays quotes_ready.
        deferred = await http.post(
            "/api/v1/ai/home-chat",
            json={"threadId": thread_id, "message": "any news on my quotes?"},
            headers=auth_headers(),
        )
        assert deferred.json()["usedFallback"] is True
        assert deferred.json()["flow"]["quoteRequest"]["status"] == "quotes_ready"

        # A real model turn presents them.
        monkeypatch.setattr(
            flow_runtime,
            "generate_home_ai_response",
            _stub_opening_generate("Your quotes are in — Brightline at $2,450 and Harbor at $2,100-$2,900."),
        )
        chat = await http.post(
            "/api/v1/ai/home-chat",
            json={"threadId": thread_id, "message": "any news on my quotes?"},
            headers=auth_headers(),
        )
        flow = chat.json()["flow"]
        assert flow["quoteRequest"]["status"] == "presented"
        assert flow["quoteRequest"]["quotesReturnedCount"] == 2
        # Ops can see the homeowner received them.
        ops_after = (
            await http.get(f"/api/v1/ops/quote-requests/{qr_id}", headers=ops_headers)
        ).json()
        assert ops_after["status"] == "presented"

        # A SECOND ops upload (the 48h follow-up) is presented too — the
        # loop is not one-shot.
        second_upload = await http.post(
            f"/api/v1/ops/quote-requests/{qr_id}/quotes",
            json={"quotes": [{"providerName": "Summit Painters", "priceUsd": 2600}]},
            headers=ops_headers,
        )
        assert second_upload.status_code == 200
        chat2 = await http.post(
            "/api/v1/ai/home-chat",
            json={"threadId": thread_id, "message": "anything new?"},
            headers=auth_headers(),
        )
        flow2 = chat2.json()["flow"]
        assert flow2["quoteRequest"]["status"] == "presented"
        assert flow2["quoteRequest"]["quotesReturnedCount"] == 3

        # Selection releases the address (§12).
        quote_id = polled["quotes"][0]["id"]
        await http.post(
            f"/api/v1/ai/quote-requests/{qr_id}/select",
            json={"quoteId": quote_id},
            headers=auth_headers(),
        )
        released = (
            await http.get(f"/api/v1/ops/quote-requests/{qr_id}", headers=ops_headers)
        ).json()
        assert released["address"] == "123 Maple Street, Nashville TN"


@pytest.mark.asyncio
async def test_ops_api_closed_without_token():
    async with client() as http:
        resp = await http.get("/api/v1/ops/quote-requests")
    assert resp.status_code == 503


# ------------------------------------------------------------------- masking
def test_pii_masking_covers_slots_and_patterns():
    slots = Slots(
        first_name="Dana",
        address="123 Maple Street, Nashville TN",
        contact_email="dana@example.com",
        contact_phone="(615) 555-0142",
    )
    text = (
        "Sure Dana, we'll send quotes to dana@example.com or call (615) 555-0142 "
        "about 123 Maple Street, Nashville TN. The room is about 180 square feet."
    )
    masked = mask_text(text, slots)
    for secret in ("Dana", "dana@example.com", "555-0142", "Maple"):
        assert secret not in masked
    assert "180 square feet" in masked
    assert "[first name]" in masked and "[address]" in masked


@pytest.mark.asyncio
async def test_journal_masks_pii(monkeypatch):
    thread_id = "thread-mask-1"
    async with client() as http:
        first = await http.post(
            "/api/v1/ai/home-chat",
            json={"threadId": thread_id, "message": "email me at pat@example.com please"},
            headers=auth_headers(),
        )
    assert first.status_code == 200
    journal = (
        (Path(settings.storage_dir) / "flow_journal" / "journal.jsonl")
        .read_text(encoding="utf-8")
    )
    assert "pat@example.com" not in journal
    last = json.loads(journal.splitlines()[-1])
    assert "[email]" in last["userText"]
