"""Additivity, enforced empirically (API_CONTRACT_V1 §11).

`fixtures/legacy_ios_decoder.json` transcribes the shipped iOS decoder's
exact expectations — required keys, JSON types, closed enums — from
`HomeAIChatView.swift`. Swift's Codable hard-fails the whole response on a
missing non-optional key, a null where a value is expected, or an unknown
enum value, so every server response must satisfy this file forever. A
value-shape regression (a null model, an enum addition) fails HERE before
it bricks a TestFlight turn.
"""

import json
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

import app.flow_runtime as flow_runtime
from app.config import settings
from app.home_ai import (
    HomeAIChatMessage,
    HomeAIChatResponse,
    HomeAIConversationState,
    HomeAIQuoteDraft,
)
from app.main import app

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "legacy_ios_decoder.json").read_text(encoding="utf-8")
)


@pytest.fixture(autouse=True)
def isolated_storage(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "storage_dir", str(tmp_path))
    yield tmp_path


def client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def auth_headers():
    return {"Authorization": f"Bearer {settings.auth_token}"} if settings.auth_token else {}


def _check_type(value, spec: str, path: str) -> None:
    if spec.startswith("enum:"):
        allowed = FIXTURE["enums"][spec.split(":", 1)[1]]
        assert value in allowed, f"{path}: {value!r} not in the closed enum {allowed}"
        return
    base = spec.split("<", 1)[0]
    expected = {"string": str, "boolean": bool, "object": dict, "array": list, "number": (int, float)}[base]
    assert isinstance(value, expected), f"{path}: expected {spec}, got {type(value).__name__} ({value!r})"
    if spec == "array<string>":
        for i, item in enumerate(value):
            assert isinstance(item, str), f"{path}[{i}]: expected string, got {type(item).__name__}"


def _validate_section(payload: dict, section: str, path: str) -> None:
    spec = FIXTURE[section]
    for key, type_spec in spec.get("required", {}).items():
        assert key in payload, f"{path}.{key}: missing — the iOS decoder hard-fails the whole turn"
        assert payload[key] is not None, f"{path}.{key}: null — the iOS decoder expects a value"
        _check_type(payload[key], type_spec, f"{path}.{key}")
    for key, type_spec in spec.get("optional", {}).items():
        if payload.get(key) is not None:
            _check_type(payload[key], type_spec, f"{path}.{key}")


def validate_against_legacy_decoder(body: dict) -> None:
    _validate_section(body, "response", "response")
    _validate_section(body["message"], "message", "message")
    _validate_section(body["state"], "state", "state")
    if body.get("quoteDraft") is not None:
        _validate_section(body["quoteDraft"], "quoteDraft", "quoteDraft")
    if body.get("visualFocus") is not None:
        _validate_section(body["visualFocus"], "visualFocus", "visualFocus")


@pytest.mark.asyncio
async def test_fallback_chat_response_decodes_on_legacy_ios():
    """The local-fallback path (no provider configured) — the shape every
    turn degrades to in production, so it must decode too."""
    async with client() as http:
        resp = await http.post(
            "/api/v1/ai/home-chat",
            json={"message": "What color should I paint this room?"},
            headers=auth_headers(),
        )
    assert resp.status_code == 200
    validate_against_legacy_decoder(resp.json())


@pytest.mark.asyncio
async def test_model_turn_with_quote_draft_decodes_on_legacy_ios(monkeypatch):
    """A rich model turn: quoteDraft present, flow fields attached — the new
    fields must never disturb the legacy shape."""

    async def stub(request, flow_directives=None, max_images_override=None):
        return HomeAIChatResponse(
            threadId=request.threadId,
            message=HomeAIChatMessage(role="assistant", content="Here is a request draft."),
            state=HomeAIConversationState(
                intent="quote_readiness",
                quoteStatus="awaiting_approval",
                requiresExplicitApproval=True,
                suggestedServiceType="Painting",
                confidence="medium",
            ),
            quoteDraft=HomeAIQuoteDraft(
                serviceType="Painting",
                title="Bedroom repaint",
                homeownerSummary="Walls and trim in a low-VOC warm white.",
                providerRequest="Repaint ~180 sq ft bedroom, walls + trim.",
                scopeNotes=["walls only", "walls + trim"],
                measurementAssumptions=["~180 sq ft from the home capture"],
                missingDetails=[],
            ),
            suggestedReplies=["Sounds good", "Change the scope"],
            model="stub",
            provider="stub",
        )

    monkeypatch.setattr(flow_runtime, "generate_home_ai_response", stub)
    async with client() as http:
        resp = await http.post(
            "/api/v1/ai/home-chat",
            json={"message": "let's set up the quote request"},
            headers=auth_headers(),
        )
    assert resp.status_code == 200
    body = resp.json()
    validate_against_legacy_decoder(body)
    # And the additive fields ride alongside without altering the above.
    assert body["flow"]["token"]
