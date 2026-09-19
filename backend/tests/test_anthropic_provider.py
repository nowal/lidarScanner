"""Tests for the Anthropic (Claude Sonnet 5) provider path."""

import json

import pytest
from httpx import ASGITransport, AsyncClient

import app.home_ai as home_ai
from app.anthropic_provider import (
    AnthropicHomeAIError,
    compact_schema,
    convert_responses_input,
)
from app.config import settings
from app.main import app


@pytest.fixture(autouse=True)
def anthropic_mode(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "storage_dir", str(tmp_path))
    monkeypatch.setattr(settings, "ai_provider", "anthropic")
    monkeypatch.setattr(settings, "anthropic_api_key", "test-key")
    monkeypatch.setattr(settings, "openai_api_key", "")
    yield


def model_payload(**overrides) -> dict:
    payload = {
        "assistantMessage": "This room has lovely light — a warm off-white would suit it.",
        "intent": "design_advice",
        "state": {
            "stage": "clarifying_goal",
            "conversionReadiness": "low",
            "userGoals": [],
            "stylePreferences": [],
            "roomsDiscussed": [],
            "servicesDiscussed": [],
            "budgetSensitivity": "unknown",
            "timeline": "unknown",
            "objections": [],
            "nextBestAction": "answer_question",
            "ctaAllowed": False,
            "ctaReason": None,
            "quoteStatus": "exploring",
            "requiresExplicitApproval": False,
            "suggestedServiceType": None,
            "confidence": "medium",
        },
        "suggestedReplies": ["Show me paint ideas"],
        "quoteDraft": None,
        "visualFocus": None,
        "flowCapture": {
            "firstName": None,
            "zip": None,
            "projectType": None,
            "scopeOptions": [],
            "materials": [],
            "address": None,
            "contactEmail": None,
            "contactPhone": None,
        },
    }
    payload.update(overrides)
    return payload


def client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def auth_headers():
    if not settings.auth_token:
        return {}
    return {"Authorization": f"Bearer {settings.auth_token}"}


# ------------------------------------------------------------------ conversion
def test_convert_responses_input_maps_blocks():
    jpeg_b64 = "aGVsbG8="
    responses_input = [
        {"role": "system", "content": "SYSTEM PROMPT"},
        {"role": "developer", "content": "DEVELOPER PROMPT"},
        {
            "role": "user",
            "content": [
                {"type": "input_text", "text": '{"latestHomeownerMessage": "hi"}'},
                {"type": "input_image", "image_url": f"data:image/jpeg;base64,{jpeg_b64}", "detail": "low"},
                {"type": "input_file", "filename": "doc.pdf", "file_data": f"data:application/pdf;base64,{jpeg_b64}"},
                {"type": "input_file", "filename": "doc.docx", "file_data": f"data:application/vnd.openxmlformats-officedocument.wordprocessingml.document;base64,{jpeg_b64}"},
            ],
        },
    ]
    system, messages = convert_responses_input(responses_input)
    assert "SYSTEM PROMPT" in system and "DEVELOPER PROMPT" in system
    assert len(messages) == 1 and messages[0]["role"] == "user"
    blocks = messages[0]["content"]
    assert blocks[0] == {"type": "text", "text": '{"latestHomeownerMessage": "hi"}'}
    assert blocks[1]["type"] == "image"
    assert blocks[1]["source"] == {"type": "base64", "media_type": "image/jpeg", "data": jpeg_b64}
    assert blocks[2]["type"] == "document"
    assert blocks[2]["source"]["media_type"] == "application/pdf"
    assert len(blocks) == 3  # non-PDF file skipped; described in the JSON text instead


def test_compact_schema_fits_grammar_budget_and_keeps_essentials():
    """The Messages API grammar compiler rejects schemas much past ~2KB
    (verified empirically Aug 26 2026) and requires additionalProperties:false
    on every object. Guard both properties so schema growth fails here, not
    in production."""
    schema = compact_schema(home_ai.HOME_AI_RESPONSE_SCHEMA)
    assert len(json.dumps(schema)) < 2600
    assert "maxItems" not in json.dumps(schema)

    def check_objects_closed(node):
        if isinstance(node, dict):
            if node.get("type") == "object":
                assert node.get("additionalProperties") is False
                assert set(node["required"]) == set(node["properties"])
            for value in node.values():
                check_objects_closed(value)
        elif isinstance(node, list):
            for item in node:
                check_objects_closed(item)

    check_objects_closed(schema)
    assert set(schema["properties"]["flowCapture"]["properties"]) == {
        "firstName", "zip", "projectType", "scopeOptions", "materials",
        "address", "contactEmail", "contactPhone", "scopeIntent", "scopeRooms",
    }
    # The model must never author prices on this path.
    assert "estimatedRangeLow" not in json.dumps(schema)


# ------------------------------------------------------------------- turn loop
@pytest.mark.asyncio
async def test_anthropic_turn_end_to_end(monkeypatch):
    captured: dict = {}

    async def fake_call(thread_id, responses_input, schema):
        captured["input"] = responses_input
        captured["schema"] = schema
        return model_payload(
            flowCapture={
                "firstName": "Dana",
                "zip": None,
                "projectType": None,
                "scopeOptions": [],
                "materials": [],
                "address": None,
                "contactEmail": None,
                "contactPhone": None,
            }
        ), "claude-sonnet-5"

    monkeypatch.setattr(home_ai, "call_anthropic_chat", fake_call)
    async with client() as http:
        resp = await http.post(
            "/api/v1/ai/home-chat",
            json={"message": "I'm Dana — thinking about paint colors"},
            headers=auth_headers(),
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["provider"] == "anthropic"
    assert body["model"] == "claude-sonnet-5"
    assert body["usedFallback"] is False
    assert "warm off-white" in body["message"]["content"]
    assert body["flow"]["slots"]["firstName"] == "Dana"
    # Flow directives reached the model input.
    user_text = next(
        block["text"]
        for item in captured["input"]
        if item["role"] == "user"
        for block in item["content"]
        if block["type"] == "input_text"
    )
    assert "flowDirectives" in user_text
    assert "flowCapture" in captured["schema"]["properties"]
    # History is always included on the stateless Anthropic path.
    assert '"conversationHistory": []' in user_text  # no prior messages sent → empty list, still present


@pytest.mark.asyncio
async def test_anthropic_failure_falls_back_locally(monkeypatch):
    async def failing_call(thread_id, responses_input, schema):
        raise AnthropicHomeAIError("Anthropic declined the request (safety)", model="claude-sonnet-5")

    monkeypatch.setattr(home_ai, "call_anthropic_chat", failing_call)
    async with client() as http:
        resp = await http.post(
            "/api/v1/ai/home-chat",
            json={"message": "what would painting cost?"},
            headers=auth_headers(),
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["usedFallback"] is True
    assert body["provider"] == "local"
    assert body["flow"]["token"]  # flow machinery still ran


@pytest.mark.asyncio
async def test_anthropic_retries_without_images_then_succeeds(monkeypatch):
    calls: list[int] = []

    async def flaky_call(thread_id, responses_input, schema):
        image_count = sum(
            1
            for item in responses_input
            if item["role"] == "user"
            for block in item["content"]
            if block["type"] == "input_image"
        )
        calls.append(image_count)
        if len(calls) == 1:
            raise AnthropicHomeAIError("Anthropic API error: overloaded", status_code=529)
        return model_payload(), "claude-sonnet-5"

    monkeypatch.setattr(home_ai, "call_anthropic_chat", flaky_call)
    context = {
        "selectedKeyframes": [
            {"id": "kf-1", "jpegBase64": "aGVsbG8="},
            {"id": "kf-2", "jpegBase64": "aGVsbG8="},
        ]
    }
    async with client() as http:
        resp = await http.post(
            "/api/v1/ai/home-chat",
            json={"message": "hello", "homeContext": context},
            headers=auth_headers(),
        )
    assert resp.status_code == 200
    assert resp.json()["usedFallback"] is False
    assert calls[0] >= 1  # first attempt carried an image
    assert calls[1] == 0  # retry dropped images
