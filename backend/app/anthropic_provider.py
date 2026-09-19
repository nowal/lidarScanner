"""Anthropic (Claude) provider for the Home Guide chat
(`LIDARAI_AI_PROVIDER=anthropic`).

Runs the same turn as the OpenAI path — same prompts, same strict response
schema, same post-processing — through the official ``anthropic`` SDK.
Differences from the OpenAI path, on purpose:

- **Stateless.** No ``previous_response_id`` chain; the client-sent history
  (last 8 turns) is included every call. Nothing depends on server-side
  provider state, so a Render restart costs nothing here.
- **No provider-side retention semantics to configure.** The OpenAI path
  sets ``store: true`` (SOW §12 flag); Messages API requests don't create a
  stored conversation object.
- **Structured output** via ``output_config.format`` (json_schema), which
  guarantees the first content block is valid JSON for the schema.
- Retries for 429/5xx/network are the SDK's own (``max_retries``); the
  caller's fallback ladder (drop images → local fallback) stays in charge
  above that.

Claude Sonnet 5 runs adaptive thinking by default; depth is steered with
``output_config.effort`` (``LIDARAI_ANTHROPIC_EFFORT``, default ``medium``).
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from typing import Any

import anthropic

from .config import settings

logger = logging.getLogger("lidarai.home_ai.anthropic")

_DATA_URL = re.compile(r"^data:(?P<media>[^;,]+);base64,(?P<data>.+)$", re.DOTALL)


class AnthropicHomeAIError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None, model: str | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.model = model


def sanitize_schema(schema: Any) -> Any:
    """The Messages API json_schema format rejects `maxItems` on arrays
    (accepted by OpenAI strict mode). Strip it recursively — the server-side
    post-processing already truncates every list it cares about, so the cap
    is enforcement-by-code rather than schema either way."""
    if isinstance(schema, dict):
        return {k: sanitize_schema(v) for k, v in schema.items() if k != "maxItems"}
    if isinstance(schema, list):
        return [sanitize_schema(item) for item in schema]
    return schema


def compact_schema(full_schema: dict[str, Any]) -> dict[str, Any]:
    """The full OpenAI-strict schema compiles to a constrained-decoding
    grammar the Messages API rejects as too large. Keep only the constraints
    the server actually depends on structurally; every enum and optional
    field is coerced with safe defaults in ``_conversation_state_from_payload``
    and friends, and the developer prompt still describes the full shape.
    ``flowCapture`` stays strict — it drives the slot machine."""
    props = full_schema.get("properties", {})
    nullable_str = {"anyOf": [{"type": "string"}, {"type": "null"}]}
    str_array = {"type": "array", "items": {"type": "string"}}

    def closed(properties: dict[str, Any]) -> dict[str, Any]:
        return {
            "type": "object",
            "additionalProperties": False,
            "required": list(properties),
            "properties": properties,
        }

    # 8 highest-signal state fields; the rest have server-side fallbacks.
    state_schema = closed(
        {
            "stage": {"type": "string"},
            "quoteStatus": {"type": "string"},
            "ctaAllowed": {"type": "boolean"},
            "requiresExplicitApproval": {"type": "boolean"},
            "suggestedServiceType": nullable_str,
            "confidence": {"type": "string"},
            "userGoals": str_array,
            "stylePreferences": str_array,
        }
    )
    # No estimatedRange* here on purpose: on this path the model never
    # authors prices — priceGuidance is computed and clamped server-side.
    quote_draft_schema = closed(
        {
            "serviceType": nullable_str,
            "title": {"type": "string"},
            "homeownerSummary": {"type": "string"},
            "providerRequest": {"type": "string"},
            "scopeNotes": str_array,
            "missingDetails": str_array,
        }
    )
    visual_focus_schema = closed(
        {
            "keyframeId": {"type": "string"},
            "reason": {"type": "string"},
            "confidence": {"type": "string"},
        }
    )
    properties: dict[str, Any] = {
        "assistantMessage": {"type": "string"},
        "intent": {"type": "string"},
        "state": state_schema,
        "suggestedReplies": str_array,
        "quoteDraft": {"anyOf": [quote_draft_schema, {"type": "null"}]},
        "visualFocus": {"anyOf": [visual_focus_schema, {"type": "null"}]},
        "flowCapture": sanitize_schema(props.get("flowCapture", {"type": "object"})),
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "required": list(properties),
        "properties": properties,
    }


def _client() -> anthropic.AsyncAnthropic:
    return anthropic.AsyncAnthropic(
        api_key=settings.anthropic_api_key,
        timeout=float(settings.anthropic_request_timeout_seconds),
        max_retries=2,
    )


def convert_responses_input(responses_input: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    """Convert the OpenAI Responses-API input (system/developer/user roles,
    ``input_text``/``input_image``/``input_file`` blocks) into a Messages API
    ``system`` string plus ``messages`` list — so both providers stay
    prompt-identical by construction."""
    system_parts: list[str] = []
    user_content: list[dict[str, Any]] = []
    for item in responses_input:
        role = item.get("role")
        content = item.get("content")
        if role in {"system", "developer"} and isinstance(content, str):
            system_parts.append(content)
            continue
        if role != "user" or not isinstance(content, list):
            continue
        for block in content:
            block_type = block.get("type")
            if block_type == "input_text":
                user_content.append({"type": "text", "text": block.get("text", "")})
            elif block_type == "input_image":
                parsed = _DATA_URL.match(block.get("image_url", ""))
                if parsed:
                    user_content.append(
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": parsed.group("media"),
                                "data": parsed.group("data"),
                            },
                        }
                    )
            elif block_type == "input_file":
                parsed = _DATA_URL.match(block.get("file_data", ""))
                if parsed and parsed.group("media") == "application/pdf":
                    user_content.append(
                        {
                            "type": "document",
                            "source": {
                                "type": "base64",
                                "media_type": "application/pdf",
                                "data": parsed.group("data"),
                            },
                        }
                    )
                # Non-PDF files: the attachment summary in the JSON text block
                # already describes them; Messages API has no generic file block.
    return "\n\n".join(system_parts), [{"role": "user", "content": user_content}]


async def call_anthropic_chat(
    thread_id: str,
    responses_input: list[dict[str, Any]],
    response_schema: dict[str, Any],
) -> tuple[dict[str, Any], str]:
    """One structured chat turn. Returns (parsed model JSON, model id used)."""
    system, messages = convert_responses_input(responses_input)
    model = settings.anthropic_model.strip()
    request_id = f"lidarai-home-ai-{uuid.uuid4()}"
    logger.info(
        "Anthropic home chat request thread_id=%s model=%s effort=%s blocks=%s request_tag=%s",
        thread_id,
        model,
        settings.anthropic_effort,
        len(messages[0]["content"]) if messages else 0,
        request_id,
    )
    # Haiku-tier models reject `effort`; leave LIDARAI_ANTHROPIC_EFFORT empty
    # to omit it.
    output_config: dict[str, Any] = {
        "format": {"type": "json_schema", "schema": compact_schema(response_schema)}
    }
    if settings.anthropic_effort.strip():
        output_config["effort"] = settings.anthropic_effort.strip()
    try:
        response = await _client().messages.create(
            model=model,
            max_tokens=settings.anthropic_max_tokens,
            system=system,
            messages=messages,
            output_config=output_config,
        )
    except anthropic.RateLimitError as exc:
        raise AnthropicHomeAIError("Anthropic rate limited", status_code=429, model=model) from exc
    except anthropic.APIStatusError as exc:
        raise AnthropicHomeAIError(
            f"Anthropic API error: {exc.message}", status_code=exc.status_code, model=model
        ) from exc
    except anthropic.APIConnectionError as exc:
        raise AnthropicHomeAIError(f"Anthropic connection error: {exc}", model=model) from exc

    if response.stop_reason == "refusal":
        detail = getattr(response, "stop_details", None)
        raise AnthropicHomeAIError(
            f"Anthropic declined the request ({getattr(detail, 'category', None)})",
            model=model,
        )
    if response.stop_reason == "max_tokens":
        raise AnthropicHomeAIError("Anthropic response hit max_tokens", model=model)

    text = next((block.text for block in response.content if block.type == "text"), None)
    if not text:
        raise AnthropicHomeAIError("Anthropic response had no text block", model=model)
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise AnthropicHomeAIError(f"Anthropic response was not valid JSON: {exc}", model=model) from exc
    usage = response.usage
    logger.info(
        "Anthropic home chat success thread_id=%s model=%s request_id=%s input_tokens=%s output_tokens=%s",
        thread_id,
        response.model,
        response._request_id,
        getattr(usage, "input_tokens", None),
        getattr(usage, "output_tokens", None),
    )
    return parsed, response.model
