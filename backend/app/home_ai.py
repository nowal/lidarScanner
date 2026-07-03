from __future__ import annotations

import json
import logging
import re
import uuid
from pathlib import Path
from typing import Any, Literal, Optional

import httpx
from pydantic import BaseModel, Field

from .config import settings
from .home_context_builder import (
    HomeContextQuality,
    build_home_guide_model_context,
    context_payload_size_chars,
    evaluate_home_context_quality,
)
from .home_guide_analytics import (
    HomeGuideEventType,
    record_home_guide_event,
    record_home_guide_turn,
)
from .home_guide_prompt import (
    HOME_GUIDE_PROMPT_VERSION,
    HOME_GUIDE_PROMPT_VARIANTS,
    HomeGuidePromptVariantID,
    assign_home_guide_prompt_variant,
    build_home_guide_developer_prompt,
    build_home_guide_system_prompt,
)
from .home_guide_tools import (
    build_quote_cta,
    detect_service_type,
    get_service_catalog,
    project_intent_detected,
    quote_intent_detected,
)
from .models import SCHEMA_VERSION, now_utc

logger = logging.getLogger("lidarai.home_ai")


HOME_AI_SYSTEM_PROMPT = build_home_guide_system_prompt("control")
HOME_AI_DEVELOPER_PROMPT = build_home_guide_developer_prompt("control")


HOME_AI_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["assistantMessage", "intent", "state", "suggestedReplies", "quoteDraft", "visualFocus"],
    "properties": {
        "assistantMessage": {"type": "string"},
        "intent": {
            "type": "string",
            "enum": ["exploring", "design_advice", "pricing", "quote_readiness", "provider_request"],
        },
        "state": {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "stage",
                "conversionReadiness",
                "userGoals",
                "stylePreferences",
                "roomsDiscussed",
                "servicesDiscussed",
                "budgetSensitivity",
                "timeline",
                "objections",
                "nextBestAction",
                "ctaAllowed",
                "ctaReason",
                "quoteStatus",
                "requiresExplicitApproval",
                "suggestedServiceType",
                "confidence",
            ],
            "properties": {
                "stage": {
                    "type": "string",
                    "enum": [
                        "exploring",
                        "clarifying_goal",
                        "project_identified",
                        "quote_ready",
                        "quote_request_started",
                        "quote_request_sent",
                        "handoff_needed",
                    ],
                },
                "conversionReadiness": {"type": "string", "enum": ["low", "medium", "high"]},
                "userGoals": {"type": "array", "items": {"type": "string"}, "maxItems": 6},
                "stylePreferences": {"type": "array", "items": {"type": "string"}, "maxItems": 6},
                "roomsDiscussed": {"type": "array", "items": {"type": "string"}, "maxItems": 6},
                "servicesDiscussed": {"type": "array", "items": {"type": "string"}, "maxItems": 6},
                "budgetSensitivity": {"type": "string", "enum": ["unknown", "low", "medium", "high"]},
                "timeline": {"type": "string", "enum": ["unknown", "now", "soon", "someday"]},
                "objections": {"type": "array", "items": {"type": "string"}, "maxItems": 6},
                "nextBestAction": {
                    "type": "string",
                    "enum": [
                        "answer_question",
                        "ask_clarifying_question",
                        "recommend_project",
                        "offer_quote_request",
                        "start_quote_request",
                        "handoff",
                    ],
                },
                "ctaAllowed": {"type": "boolean"},
                "ctaReason": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                "quoteStatus": {
                    "type": "string",
                    "enum": ["exploring", "drafting", "awaiting_approval", "approved", "sent"],
                },
                "requiresExplicitApproval": {"type": "boolean"},
                "suggestedServiceType": {
                    "anyOf": [{"type": "string"}, {"type": "null"}],
                },
                "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
            },
        },
        "suggestedReplies": {
            "type": "array",
            "maxItems": 3,
            "items": {"type": "string"},
        },
        "quoteDraft": {
            "anyOf": [
                {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "serviceType",
                        "title",
                        "homeownerSummary",
                        "providerRequest",
                        "scopeNotes",
                        "measurementAssumptions",
                        "missingDetails",
                        "estimatedRangeLow",
                        "estimatedRangeHigh",
                        "estimateUnit",
                    ],
                    "properties": {
                        "serviceType": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                        "title": {"type": "string"},
                        "homeownerSummary": {"type": "string"},
                        "providerRequest": {"type": "string"},
                        "scopeNotes": {"type": "array", "items": {"type": "string"}},
                        "measurementAssumptions": {"type": "array", "items": {"type": "string"}},
                        "missingDetails": {"type": "array", "items": {"type": "string"}},
                        "estimatedRangeLow": {"anyOf": [{"type": "number"}, {"type": "null"}]},
                        "estimatedRangeHigh": {"anyOf": [{"type": "number"}, {"type": "null"}]},
                        "estimateUnit": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                    },
                },
                {"type": "null"},
            ],
        },
        "visualFocus": {
            "anyOf": [
                {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["keyframeId", "reason", "confidence"],
                    "properties": {
                        "keyframeId": {"type": "string"},
                        "reason": {"type": "string"},
                        "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
                    },
                },
                {"type": "null"},
            ],
        },
    },
}


class HomeAIChatMessage(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    role: Literal["homeowner", "assistant"]
    content: str
    createdAt: str = Field(default_factory=lambda: now_utc().isoformat())
    attachments: list["HomeAIAttachment"] = Field(default_factory=list)


class HomeAIAttachment(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    kind: Literal["image", "file"]
    fileName: str = Field(default="attachment", max_length=240)
    mimeType: Optional[str] = None
    byteCount: int = Field(default=0, ge=0, le=50_000_000)
    dataBase64: Optional[str] = None


class HomeAIKeyframe(BaseModel):
    id: str
    capturedAt: Optional[str] = None
    timestamp: Optional[float] = None
    cameraTransform: list[float] = Field(default_factory=list)
    imageResolution: list[int] = Field(default_factory=list)
    jpegBase64: Optional[str] = None
    note: Optional[str] = None


class HomeAIContextPacket(BaseModel):
    contextVersion: str = "home_ai_context_v1"
    userId: Optional[str] = None
    projectId: Optional[str] = None
    homeProfileId: Optional[str] = None
    sourcePage: Optional[str] = None
    initialRoomId: Optional[str] = None
    scanId: Optional[str] = None
    jobId: Optional[str] = None
    createdAt: Optional[str] = None
    roomCount: int = 0
    rooms: list[dict[str, Any]] = Field(default_factory=list)
    totals: dict[str, Any] = Field(default_factory=dict)
    floorplanSummary: Optional[str] = None
    meshSummary: dict[str, Any] = Field(default_factory=dict)
    selectedKeyframes: list[HomeAIKeyframe] = Field(default_factory=list)
    workflowState: dict[str, Any] = Field(default_factory=dict)
    notes: list[str] = Field(default_factory=list)


class HomeAIWorkflowState(BaseModel):
    quoteStatus: str = "exploring"
    selectedServiceType: Optional[str] = None
    quoteDraft: Optional[dict[str, Any]] = None
    sourcePage: Optional[str] = None
    initialRoomId: Optional[str] = None
    userGoals: list[str] = Field(default_factory=list)
    stylePreferences: list[str] = Field(default_factory=list)
    timeline: Optional[str] = None
    budgetSensitivity: Optional[str] = None


class HomeAIQuoteDraft(BaseModel):
    serviceType: Optional[str] = None
    title: str
    homeownerSummary: str
    providerRequest: str
    scopeNotes: list[str] = Field(default_factory=list)
    measurementAssumptions: list[str] = Field(default_factory=list)
    missingDetails: list[str] = Field(default_factory=list)
    estimatedRangeLow: Optional[float] = None
    estimatedRangeHigh: Optional[float] = None
    estimateUnit: Optional[str] = None


HomeGuideStage = Literal[
    "exploring",
    "clarifying_goal",
    "project_identified",
    "quote_ready",
    "quote_request_started",
    "quote_request_sent",
    "handoff_needed",
]

ConversionReadiness = Literal["low", "medium", "high"]

NextBestAction = Literal[
    "answer_question",
    "ask_clarifying_question",
    "recommend_project",
    "offer_quote_request",
    "start_quote_request",
    "handoff",
]


class HomeAIConversationState(BaseModel):
    stage: HomeGuideStage = "exploring"
    conversionReadiness: ConversionReadiness = "low"
    userGoals: list[str] = Field(default_factory=list)
    stylePreferences: list[str] = Field(default_factory=list)
    roomsDiscussed: list[str] = Field(default_factory=list)
    servicesDiscussed: list[str] = Field(default_factory=list)
    budgetSensitivity: Literal["unknown", "low", "medium", "high"] = "unknown"
    timeline: Literal["unknown", "now", "soon", "someday"] = "unknown"
    objections: list[str] = Field(default_factory=list)
    nextBestAction: NextBestAction = "answer_question"
    ctaAllowed: bool = False
    ctaReason: Optional[str] = None
    intent: Literal["exploring", "design_advice", "pricing", "quote_readiness", "provider_request"]
    quoteStatus: Literal["exploring", "drafting", "awaiting_approval", "approved", "sent"] = "exploring"
    requiresExplicitApproval: bool = False
    suggestedServiceType: Optional[str] = None
    confidence: Literal["low", "medium", "high"] = "medium"


class HomeAIVisualFocus(BaseModel):
    keyframeId: str
    reason: str = ""
    confidence: Literal["low", "medium", "high"] = "medium"


class HomeAIChatRequest(BaseModel):
    threadId: Optional[str] = None
    userId: Optional[str] = None
    projectId: Optional[str] = None
    homeProfileId: Optional[str] = None
    sourcePage: Optional[str] = None
    initialRoomId: Optional[str] = None
    message: str = Field(min_length=1, max_length=5000)
    messages: list[HomeAIChatMessage] = Field(default_factory=list)
    attachments: list[HomeAIAttachment] = Field(default_factory=list, max_length=3)
    homeContext: HomeAIContextPacket = Field(default_factory=HomeAIContextPacket)
    workflowState: HomeAIWorkflowState = Field(default_factory=HomeAIWorkflowState)


class HomeAIQuoteCTA(BaseModel):
    type: Literal["quote_request"]
    label: str
    serviceType: Optional[str] = None
    roomIds: list[str] = Field(default_factory=list)
    scopeNotes: list[str] = Field(default_factory=list)


class HomeAIChatResponse(BaseModel):
    schemaVersion: str = SCHEMA_VERSION
    threadId: str
    message: HomeAIChatMessage
    state: HomeAIConversationState
    quoteDraft: Optional[HomeAIQuoteDraft] = None
    suggestedReplies: list[str] = Field(default_factory=list)
    cta: Optional[HomeAIQuoteCTA] = None
    visualFocus: Optional[HomeAIVisualFocus] = None
    model: str
    provider: str
    usedFallback: bool = False
    promptVersion: str = HOME_GUIDE_PROMPT_VERSION
    promptVariant: HomeGuidePromptVariantID = "control"
    contextQuality: HomeContextQuality = Field(default_factory=HomeContextQuality)


class HomeAIEventRequest(BaseModel):
    threadId: str
    eventType: HomeGuideEventType
    userId: Optional[str] = None
    projectId: Optional[str] = None
    payload: dict[str, Any] = Field(default_factory=dict)


class HomeAIEventResponse(BaseModel):
    schemaVersion: str = SCHEMA_VERSION
    ok: bool = True
    eventId: str


class OpenAIHomeAIError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        model: str | None = None,
        retry_after: str | None = None,
        error_type: str | None = None,
        error_code: str | None = None,
        client_request_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.model = model
        self.retry_after = retry_after
        self.error_type = error_type
        self.error_code = error_code
        self.client_request_id = client_request_id


async def generate_home_ai_response(request: HomeAIChatRequest) -> HomeAIChatResponse:
    thread_id = request.threadId or str(uuid.uuid4())
    prompt_variant = _resolve_prompt_variant(thread_id)
    context_quality = evaluate_home_context_quality(
        request.homeContext,
        request.workflowState,
        service_catalog=get_service_catalog(None),
    )
    if settings.ai_provider == "openai" and settings.openai_api_key:
        try:
            response = await _call_openai(thread_id, request, prompt_variant=prompt_variant)
            response.promptVariant = prompt_variant
            response.contextQuality = context_quality
            _persist_and_record_turn(request, response)
            return response
        except OpenAIHomeAIError as exc:
            logger.warning(
                "OpenAI home chat failed; using local fallback thread_id=%s model=%s status=%s error_type=%s error_code=%s retry_after=%s client_request_id=%s detail=%s",
                thread_id,
                exc.model,
                exc.status_code,
                exc.error_type,
                exc.error_code,
                exc.retry_after,
                exc.client_request_id,
                str(exc),
            )
            response = _fallback_response(
                thread_id,
                request,
                prompt_variant=prompt_variant,
                context_quality=context_quality,
                fallback_reason=_openai_fallback_note(exc),
            )
            _persist_and_record_turn(request, response)
            return response
        except Exception as exc:  # noqa: BLE001
            logger.exception("OpenAI home chat failed; using local fallback", extra={"thread_id": thread_id})
            response = _fallback_response(
                thread_id,
                request,
                prompt_variant=prompt_variant,
                context_quality=context_quality,
                fallback_reason="The full AI model could not answer this turn, so I am using a lightweight local reply.",
            )
            _persist_and_record_turn(request, response)
            return response

    response = _fallback_response(
        thread_id,
        request,
        prompt_variant=prompt_variant,
        context_quality=context_quality,
    )
    _persist_and_record_turn(request, response)
    return response


async def record_home_ai_event(request: HomeAIEventRequest) -> HomeAIEventResponse:
    event = record_home_guide_event(
        storage_dir=settings.storage_dir,
        conversation_id=request.threadId,
        event_type=request.eventType,
        payload=request.payload,
        user_id=request.userId,
        project_id=request.projectId,
    )
    return HomeAIEventResponse(eventId=event.id)


async def _call_openai(
    thread_id: str,
    request: HomeAIChatRequest,
    *,
    prompt_variant: HomeGuidePromptVariantID,
) -> HomeAIChatResponse:
    primary_model = settings.openai_model.strip()
    fallback_model = settings.openai_fallback_model.strip()
    max_images = max(0, int(settings.openai_max_images_per_request or 0))
    attempts: list[tuple[str, int]] = [(primary_model, max_images)]
    if max_images > 0:
        attempts.append((primary_model, 0))
    if fallback_model and fallback_model != primary_model:
        attempts.append((fallback_model, 0))

    last_error: OpenAIHomeAIError | None = None
    for index, (model, image_limit) in enumerate(attempts):
        try:
            return await _call_openai_once(
                thread_id,
                request,
                model=model,
                image_limit=image_limit,
                prompt_variant=prompt_variant,
            )
        except OpenAIHomeAIError as exc:
            last_error = exc
            if index == len(attempts) - 1 or not _should_try_next_openai_attempt(exc):
                break
            logger.info(
                "Retrying OpenAI home chat with a lighter request thread_id=%s model=%s status=%s next_model=%s",
                thread_id,
                exc.model,
                exc.status_code,
                attempts[index + 1][0],
            )

    if last_error:
        raise last_error
    raise OpenAIHomeAIError("OpenAI model is not configured", model=primary_model)


async def _call_openai_once(
    thread_id: str,
    request: HomeAIChatRequest,
    *,
    model: str,
    image_limit: int,
    prompt_variant: HomeGuidePromptVariantID,
) -> HomeAIChatResponse:
    thread_state = _load_openai_thread_state(thread_id)
    previous_response_id = thread_state.get("previousResponseId") if thread_state.get("model") == model else None
    attached_image_count = len([frame for frame in request.homeContext.selectedKeyframes if frame.jpegBase64][: max(0, image_limit)])
    client_request_id = f"lidarai-home-ai-{uuid.uuid4()}"
    logger.info(
        "OpenAI home chat request thread_id=%s model=%s previous_response=%s room_count=%s selected_keyframes=%s image_payloads=%s image_limit=%s client_request_id=%s",
        thread_id,
        model,
        "yes" if previous_response_id else "no",
        request.homeContext.roomCount,
        len(request.homeContext.selectedKeyframes),
        attached_image_count,
        image_limit,
        client_request_id,
    )
    payload = {
        "model": model,
        "input": _responses_input(
            request,
            image_limit=image_limit,
            include_history=previous_response_id is None,
            prompt_variant=prompt_variant,
        ),
        "store": True,
        "reasoning": {"effort": settings.openai_reasoning_effort},
        "text": {
            "verbosity": "low",
            "format": {
                "type": "json_schema",
                "name": "home_ai_chat_response",
                "strict": True,
                "schema": HOME_AI_RESPONSE_SCHEMA,
            },
        },
    }
    if previous_response_id:
        payload["previous_response_id"] = previous_response_id
    headers = {
        "Authorization": f"Bearer {settings.openai_api_key}",
        "Content-Type": "application/json",
        "X-Client-Request-Id": client_request_id,
    }
    if settings.openai_organization:
        headers["OpenAI-Organization"] = settings.openai_organization
    if settings.openai_project:
        headers["OpenAI-Project"] = settings.openai_project
    timeout = httpx.Timeout(settings.openai_request_timeout_seconds)
    async with httpx.AsyncClient(timeout=timeout) as client:
        try:
            resp = await client.post("https://api.openai.com/v1/responses", headers=headers, json=payload)
        except httpx.RequestError as exc:
            raise OpenAIHomeAIError(
                f"OpenAI request failed before receiving a response: {exc}",
                model=model,
                client_request_id=client_request_id,
            ) from exc
        if resp.is_error:
            raise _openai_error_from_response(resp, model=model, client_request_id=client_request_id)
        data = resp.json()

    model_json = _extract_response_text(data)
    parsed = json.loads(model_json)
    _log_openai_success(
        thread_id=thread_id,
        model=model,
        response_id=data.get("id"),
        client_request_id=client_request_id,
        request_id=resp.headers.get("x-request-id"),
        usage=data.get("usage"),
    )
    if isinstance(data.get("id"), str):
        _save_openai_thread_state(thread_id, response_id=data["id"], model=model)
    return _response_from_model_json(
        thread_id,
        request,
        parsed,
        model=model,
        used_fallback=False,
        prompt_variant=prompt_variant,
    )


def _responses_input(
    request: HomeAIChatRequest,
    *,
    image_limit: int,
    include_history: bool,
    prompt_variant: HomeGuidePromptVariantID,
) -> list[dict[str, Any]]:
    frames_with_images = [frame for frame in request.homeContext.selectedKeyframes if frame.jpegBase64]
    included_image_ids = {frame.id for frame in frames_with_images[: max(0, image_limit)]}
    model_context = build_home_guide_model_context(
        request.homeContext,
        request.workflowState,
        included_image_ids=included_image_ids,
        service_catalog=get_service_catalog(None),
    )
    context_for_text = model_context.model_dump(mode="json", exclude_none=True)

    user_content: list[dict[str, Any]] = [
        {
            "type": "input_text",
            "text": json.dumps(
                {
                    "homeContext": context_for_text,
                    "conversationHistory": [
                        {"role": msg.role, "content": msg.content, "createdAt": msg.createdAt}
                        for msg in request.messages[-8:]
                    ] if include_history else [],
                    "workflowState": request.workflowState.model_dump(mode="json", exclude_none=True),
                    "userAttachments": [
                        _attachment_summary(attachment)
                        for attachment in request.attachments
                    ],
                    "latestHomeownerMessage": request.message,
                    "promptVersion": HOME_GUIDE_PROMPT_VERSION,
                    "promptVariant": prompt_variant,
                    "contextPayloadSizeChars": context_payload_size_chars(model_context),
                    "contextNote": (
                        "This turn is chained to the prior OpenAI response, so earlier image context may already be available."
                        if not include_history
                        else "This turn includes the local conversation history because no prior OpenAI response is available."
                    ),
                },
                ensure_ascii=True,
            ),
        }
    ]
    for frame in request.homeContext.selectedKeyframes:
        if frame.jpegBase64 and frame.id in included_image_ids:
            user_content.append({
                "type": "input_image",
                "image_url": f"data:image/jpeg;base64,{frame.jpegBase64}",
                "detail": "low",
            })
    user_content.extend(_attachment_content_blocks(request.attachments))

    return [
        {"role": "system", "content": build_home_guide_system_prompt(prompt_variant)},
        {"role": "developer", "content": build_home_guide_developer_prompt(prompt_variant)},
        {"role": "user", "content": user_content},
    ]


def _attachment_summary(attachment: HomeAIAttachment) -> dict[str, Any]:
    return {
        "id": attachment.id,
        "kind": attachment.kind,
        "fileName": attachment.fileName,
        "mimeType": _attachment_mime_type(attachment),
        "byteCount": attachment.byteCount,
        "includedAsModelInput": bool(attachment.dataBase64),
    }


def _attachment_content_blocks(attachments: list[HomeAIAttachment]) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    for attachment in attachments[:3]:
        if not attachment.dataBase64:
            continue

        mime_type = _attachment_mime_type(attachment)
        if attachment.kind == "image" or mime_type.startswith("image/"):
            blocks.append({
                "type": "input_image",
                "image_url": f"data:{mime_type};base64,{attachment.dataBase64}",
                "detail": "low",
            })
        else:
            blocks.append({
                "type": "input_file",
                "filename": _safe_attachment_filename(attachment.fileName),
                "file_data": f"data:{mime_type};base64,{attachment.dataBase64}",
            })
    return blocks


def _attachment_mime_type(attachment: HomeAIAttachment) -> str:
    raw_mime = (attachment.mimeType or "").split(";")[0].strip().lower()
    if raw_mime:
        return raw_mime[:120]

    suffix = Path(attachment.fileName).suffix.lower()
    return {
        ".csv": "text/csv",
        ".doc": "application/msword",
        ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ".heic": "image/heic",
        ".jpeg": "image/jpeg",
        ".jpg": "image/jpeg",
        ".json": "application/json",
        ".md": "text/markdown",
        ".pdf": "application/pdf",
        ".png": "image/png",
        ".ppt": "application/vnd.ms-powerpoint",
        ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        ".rtf": "application/rtf",
        ".txt": "text/plain",
        ".xls": "application/vnd.ms-excel",
        ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    }.get(suffix, "application/octet-stream")


def _safe_attachment_filename(file_name: str) -> str:
    safe_name = re.sub(r"[^A-Za-z0-9._ -]", "_", file_name).strip(" .")
    return safe_name[:180] or "attachment"


def _log_openai_success(
    *,
    thread_id: str,
    model: str,
    response_id: Any,
    client_request_id: str,
    request_id: str | None,
    usage: Any,
) -> None:
    usage_summary: dict[str, Any] = {}
    if isinstance(usage, dict):
        for key in [
            "input_tokens",
            "output_tokens",
            "total_tokens",
            "cached_input_tokens",
        ]:
            if isinstance(usage.get(key), (int, float, str)):
                usage_summary[key] = usage[key]
        for nested_key in ["input_tokens_details", "output_tokens_details"]:
            nested_value = usage.get(nested_key)
            if isinstance(nested_value, dict):
                usage_summary[nested_key] = {
                    key: value
                    for key, value in nested_value.items()
                    if isinstance(value, (int, float, str))
                }

    logger.info(
        "OpenAI home chat success thread_id=%s model=%s response_id=%s request_id=%s client_request_id=%s usage=%s",
        thread_id,
        model,
        response_id if isinstance(response_id, str) else None,
        request_id,
        client_request_id,
        json.dumps(usage_summary, ensure_ascii=True, sort_keys=True) if usage_summary else "{}",
    )


def _openai_error_from_response(
    resp: httpx.Response,
    *,
    model: str,
    client_request_id: str,
) -> OpenAIHomeAIError:
    retry_after = resp.headers.get("retry-after")
    rate_limit_detail = _openai_rate_limit_detail(resp)
    detail, error_type, error_code = _openai_error_detail(resp)
    if rate_limit_detail:
        detail = f"{detail}; {rate_limit_detail}"
    return OpenAIHomeAIError(
        f"OpenAI returned HTTP {resp.status_code}: {detail}",
        status_code=resp.status_code,
        model=model,
        retry_after=retry_after,
        error_type=error_type,
        error_code=error_code,
        client_request_id=client_request_id,
    )


def _openai_error_detail(resp: httpx.Response) -> tuple[str, str | None, str | None]:
    try:
        body = resp.json()
    except ValueError:
        text = (resp.text or "").strip()
        return text[:500] if text else "no response body", None, None

    error = body.get("error") if isinstance(body, dict) else None
    if isinstance(error, dict):
        error_type = str(error.get("type") or "").strip() or None
        error_code = str(error.get("code") or "").strip() or None
        parts = [
            str(error.get("message") or "").strip(),
            f"type={error_type}" if error_type else "",
            f"code={error_code}" if error_code else "",
        ]
        detail = "; ".join(part for part in parts if part)
        return detail[:500] if detail else "OpenAI error object had no message", error_type, error_code
    return json.dumps(body, ensure_ascii=True)[:500], None, None


def _openai_rate_limit_detail(resp: httpx.Response) -> str:
    header_names = [
        "x-request-id",
        "retry-after",
        "x-ratelimit-limit-requests",
        "x-ratelimit-remaining-requests",
        "x-ratelimit-reset-requests",
        "x-ratelimit-limit-tokens",
        "x-ratelimit-remaining-tokens",
        "x-ratelimit-reset-tokens",
    ]
    headers = {
        name: resp.headers.get(name)
        for name in header_names
        if resp.headers.get(name)
    }
    if not headers:
        return ""
    return "headers=" + json.dumps(headers, ensure_ascii=True, sort_keys=True)


def _should_try_next_openai_attempt(exc: OpenAIHomeAIError) -> bool:
    if exc.error_code == "insufficient_quota":
        return False
    if exc.status_code is None:
        return True
    return exc.status_code == 429 or exc.status_code >= 500


def _openai_fallback_note(exc: OpenAIHomeAIError) -> str:
    if exc.error_code == "insufficient_quota":
        return (
            "The full Home Guide is temporarily unavailable, so I am using a simpler reply for this turn."
        )
    if exc.status_code == 429:
        retry_text = f" Try again after about {exc.retry_after} seconds." if exc.retry_after else ""
        return (
            "The full Home Guide is busy right now, so I am using a simpler reply for this turn."
            f"{retry_text}"
        )
    if exc.status_code in {401, 403}:
        return "The full Home Guide setup needs attention, so I am using a simpler reply for this turn."
    return "The full Home Guide could not answer this turn, so I am using a simpler reply."


def _extract_response_text(data: dict[str, Any]) -> str:
    if isinstance(data.get("output_text"), str):
        return data["output_text"]

    output_chunks = []
    for item in data.get("output", []):
        for content in item.get("content", []):
            if isinstance(content, dict) and isinstance(content.get("text"), str):
                output_chunks.append(content["text"])
    if output_chunks:
        return "".join(output_chunks)

    raise ValueError("OpenAI response did not include output text")


def _response_from_model_json(
    thread_id: str,
    request: HomeAIChatRequest,
    parsed: dict[str, Any],
    *,
    model: str,
    used_fallback: bool,
    prompt_variant: HomeGuidePromptVariantID,
) -> HomeAIChatResponse:
    state_json = parsed.get("state") or {}
    quote_json = parsed.get("quoteDraft")
    quote_draft = HomeAIQuoteDraft.model_validate(quote_json) if quote_json else None
    state = _conversation_state_from_payload(
        request=request,
        parsed_intent=parsed.get("intent") or "exploring",
        state_json=state_json,
        quote_draft=quote_draft,
    )
    message = HomeAIChatMessage(
        role="assistant",
        content=_polish_homeowner_message(
            parsed.get("assistantMessage") or _fallback_message(request),
            allow_technical=_asks_about_technology(request.message),
        ),
    )
    cta = _cta_from_state_and_draft(state, quote_draft)
    return HomeAIChatResponse(
        threadId=thread_id,
        message=message,
        state=state,
        quoteDraft=quote_draft,
        suggestedReplies=[reply for reply in parsed.get("suggestedReplies", []) if isinstance(reply, str)][:3],
        cta=cta,
        visualFocus=_visual_focus_from_model_json(request, parsed.get("visualFocus")),
        model=model,
        provider=settings.ai_provider,
        usedFallback=used_fallback,
        promptVariant=prompt_variant,
        contextQuality=evaluate_home_context_quality(
            request.homeContext,
            request.workflowState,
            service_catalog=get_service_catalog(None),
        ),
    )


def _conversation_state_from_payload(
    *,
    request: HomeAIChatRequest,
    parsed_intent: str,
    state_json: dict[str, Any],
    quote_draft: HomeAIQuoteDraft | None,
) -> HomeAIConversationState:
    service_type = state_json.get("suggestedServiceType") or (quote_draft.serviceType if quote_draft else None)
    cta_allowed = bool(state_json.get("ctaAllowed"))
    if quote_draft and quote_intent_detected(request.message):
        cta_allowed = True
    payload = {
        "stage": _coerce_stage(state_json.get("stage"))
        or _stage_for_intent(parsed_intent, request.message, quote_draft),
        "conversionReadiness": _coerce_readiness(state_json.get("conversionReadiness"))
        or _conversion_readiness(parsed_intent, quote_draft),
        "userGoals": _string_list(state_json.get("userGoals")),
        "stylePreferences": _string_list(state_json.get("stylePreferences")),
        "roomsDiscussed": _string_list(state_json.get("roomsDiscussed")) or _rooms_from_context(request.homeContext),
        "servicesDiscussed": _string_list(state_json.get("servicesDiscussed")) or ([service_type] if service_type else []),
        "budgetSensitivity": _coerce_budget_sensitivity(state_json.get("budgetSensitivity"))
        or _budget_sensitivity(request.message),
        "timeline": _coerce_timeline(state_json.get("timeline")) or _timeline_from_message(request.message),
        "objections": _string_list(state_json.get("objections")),
        "nextBestAction": _coerce_next_best_action(state_json.get("nextBestAction"))
        or ("offer_quote_request" if quote_draft else "answer_question"),
        "ctaAllowed": cta_allowed,
        "ctaReason": state_json.get("ctaReason"),
        "intent": _coerce_intent(parsed_intent),
        "quoteStatus": _coerce_quote_status(state_json.get("quoteStatus"))
        or ("awaiting_approval" if quote_draft else "exploring"),
        "requiresExplicitApproval": bool(state_json.get("requiresExplicitApproval") or quote_draft),
        "suggestedServiceType": service_type,
        "confidence": _coerce_confidence(state_json.get("confidence")) or ("medium" if request.homeContext.rooms else "low"),
    }
    return HomeAIConversationState.model_validate(payload)


def _cta_from_state_and_draft(
    state: HomeAIConversationState,
    quote_draft: HomeAIQuoteDraft | None,
) -> HomeAIQuoteCTA | None:
    if not quote_draft:
        return None
    cta = build_quote_cta(
        cta_allowed=state.ctaAllowed,
        service_type=quote_draft.serviceType or state.suggestedServiceType,
        room_ids=[],
        scope_notes=quote_draft.scopeNotes,
    )
    return HomeAIQuoteCTA.model_validate(cta) if cta else None


def _visual_focus_from_model_json(request: HomeAIChatRequest, value: Any) -> HomeAIVisualFocus | None:
    if not isinstance(value, dict):
        return None

    keyframe_id = str(value.get("keyframeId") or "").strip()
    if not keyframe_id:
        return None

    valid_keyframe_ids = {frame.id for frame in request.homeContext.selectedKeyframes}
    if keyframe_id not in valid_keyframe_ids:
        logger.info("Ignoring unknown visualFocus keyframe id=%s", keyframe_id)
        return None

    confidence = value.get("confidence")
    if confidence not in {"low", "medium", "high"}:
        confidence = "medium"
    if confidence != "high" or not _message_requests_visible_detail(request.message):
        logger.info(
            "Ignoring visualFocus without a strong visible-detail match keyframe_id=%s confidence=%s",
            keyframe_id,
            confidence,
        )
        return None

    return HomeAIVisualFocus(
        keyframeId=keyframe_id,
        reason=str(value.get("reason") or "")[:240],
        confidence=confidence,
    )


def _fallback_response(
    thread_id: str,
    request: HomeAIChatRequest,
    *,
    prompt_variant: HomeGuidePromptVariantID = "control",
    context_quality: HomeContextQuality | None = None,
    fallback_reason: str | None = None,
) -> HomeAIChatResponse:
    message = request.message.strip()
    intent = _classify_intent(message)
    service_type = detect_service_type(message)
    quote_draft = _make_quote_draft(request, service_type) if intent in {"pricing", "quote_readiness", "provider_request"} else None
    assistant = _fallback_message(request, intent=intent, quote_draft=quote_draft)
    if fallback_reason:
        assistant = f"{assistant}\n\n{fallback_reason}"
    assistant = _polish_homeowner_message(
        assistant,
        allow_technical=_asks_about_technology(request.message),
    )
    state = _fallback_state(request, intent=intent, service_type=service_type, quote_draft=quote_draft)
    cta = _cta_from_state_and_draft(state, quote_draft)

    return HomeAIChatResponse(
        threadId=thread_id,
        message=HomeAIChatMessage(role="assistant", content=assistant),
        state=state,
        quoteDraft=quote_draft,
        suggestedReplies=_suggested_replies(intent, quote_draft),
        cta=cta,
        visualFocus=_fallback_visual_focus(request, intent),
        model="local-home-guide-fallback",
        provider="local",
        usedFallback=True,
        promptVariant=prompt_variant,
        contextQuality=context_quality
        or evaluate_home_context_quality(
            request.homeContext,
            request.workflowState,
            service_catalog=get_service_catalog(None),
        ),
    )


def _classify_intent(message: str) -> Literal["exploring", "design_advice", "pricing", "quote_readiness", "provider_request"]:
    text = message.lower()
    if re.search(r"\b(provider|quote|quotes|contractor|book|schedule|hire|availability|come out|send)\b", text):
        return "provider_request"
    if re.search(r"\b(cost|price|pricing|budget|estimate|how much)\b", text):
        return "pricing"
    if re.search(r"\b(idea|design|color|style|look|layout|space|room|measure|measurement|dimension|sq ft)\b", text):
        return "design_advice"
    if re.search(r"\b(done|fix|repair|replace|install|painted|cleaned|washed|refinish|renovate)\b", text):
        return "quote_readiness"
    return "exploring"


def _fallback_state(
    request: HomeAIChatRequest,
    *,
    intent: str,
    service_type: str | None,
    quote_draft: HomeAIQuoteDraft | None,
) -> HomeAIConversationState:
    goals = _goals_from_message(request.message)
    style_preferences = _style_preferences_from_message(request.message)
    rooms = _rooms_from_message(request.message) or _rooms_from_context(request.homeContext)
    services = [service_type] if service_type else []
    stage = _stage_for_intent(intent, request.message, quote_draft)
    cta_allowed = bool(quote_draft and quote_intent_detected(request.message))
    return HomeAIConversationState(
        stage=stage,
        conversionReadiness=_conversion_readiness(intent, quote_draft),
        userGoals=goals,
        stylePreferences=style_preferences,
        roomsDiscussed=rooms,
        servicesDiscussed=services,
        budgetSensitivity=_budget_sensitivity(request.message),
        timeline=_timeline_from_message(request.message),
        objections=_objections_from_message(request.message),
        nextBestAction=_next_best_action(stage, quote_draft),
        ctaAllowed=cta_allowed,
        ctaReason="User asked about pricing, providers, feasibility, or next steps." if cta_allowed else None,
        intent=_coerce_intent(intent),
        quoteStatus="awaiting_approval" if quote_draft else "exploring",
        requiresExplicitApproval=quote_draft is not None,
        suggestedServiceType=service_type,
        confidence="medium" if request.homeContext.rooms else "low",
    )


def _stage_for_intent(
    intent: str,
    message: str,
    quote_draft: HomeAIQuoteDraft | None,
) -> HomeGuideStage:
    if quote_draft and quote_intent_detected(message):
        return "quote_ready"
    if intent in {"provider_request", "pricing"}:
        return "quote_ready"
    if intent == "quote_readiness" or project_intent_detected(message):
        return "project_identified"
    if intent == "design_advice":
        return "clarifying_goal"
    return "exploring"


def _conversion_readiness(intent: str, quote_draft: HomeAIQuoteDraft | None) -> ConversionReadiness:
    if quote_draft or intent in {"provider_request", "pricing"}:
        return "high"
    if intent == "quote_readiness":
        return "medium"
    return "low"


def _next_best_action(stage: str, quote_draft: HomeAIQuoteDraft | None) -> NextBestAction:
    if quote_draft:
        return "offer_quote_request"
    if stage == "project_identified":
        return "recommend_project"
    if stage == "clarifying_goal":
        return "ask_clarifying_question"
    if stage == "handoff_needed":
        return "handoff"
    return "answer_question"


def _string_list(value: Any, *, max_items: int = 6, max_chars: int = 80) -> list[str]:
    if not isinstance(value, list):
        return []
    result = []
    for item in value:
        text = str(item).strip()
        if text:
            result.append(text[:max_chars])
    return result[:max_items]


def _rooms_from_context(context: HomeAIContextPacket) -> list[str]:
    rooms = []
    for room in context.rooms[:6]:
        name = room.get("name") or room.get("type")
        if name:
            rooms.append(str(name)[:80])
    return rooms


def _rooms_from_message(message: str) -> list[str]:
    text = message.lower()
    known_rooms = [
        "kitchen",
        "living room",
        "bedroom",
        "bathroom",
        "dining room",
        "entry",
        "hallway",
        "office",
        "basement",
        "laundry room",
        "garage",
    ]
    return [room.title() for room in known_rooms if room in text][:6]


def _goals_from_message(message: str) -> list[str]:
    text = message.lower()
    goals = []
    goal_keywords = [
        ("lighter", ["lighter", "brighter", "bright"]),
        ("cozier", ["cozy", "cozier", "warmer"]),
        ("cleaner", ["cleaner", "clean", "fresh"]),
        ("more finished", ["finished", "polished", "intentional"]),
        ("kid-friendly", ["kid", "kids", "family"]),
        ("resale-friendly", ["resale", "sell", "selling"]),
        ("budget-conscious", ["budget", "cheap", "affordable", "cost"]),
    ]
    for label, keywords in goal_keywords:
        if any(keyword in text for keyword in keywords):
            goals.append(label)
    return goals[:6]


def _style_preferences_from_message(message: str) -> list[str]:
    text = message.lower()
    styles = []
    style_keywords = [
        ("warm neutral", ["warm neutral", "warm white", "cream"]),
        ("modern", ["modern", "contemporary"]),
        ("traditional", ["traditional", "classic"]),
        ("minimal", ["minimal", "simple", "clean lines"]),
        ("colorful", ["colorful", "bold", "color"]),
        ("natural", ["natural", "wood", "organic"]),
    ]
    for label, keywords in style_keywords:
        if any(keyword in text for keyword in keywords):
            styles.append(label)
    return styles[:6]


def _budget_sensitivity(message: str) -> Literal["unknown", "low", "medium", "high"]:
    text = message.lower()
    if re.search(r"\b(cheap|tight budget|lowest|least expensive|save money|budget-conscious)\b", text):
        return "high"
    if re.search(r"\b(budget|affordable|cost|price|how much)\b", text):
        return "medium"
    return "unknown"


def _timeline_from_message(message: str) -> Literal["unknown", "now", "soon", "someday"]:
    text = message.lower()
    if re.search(r"\b(now|asap|this week|today|tomorrow|urgent)\b", text):
        return "now"
    if re.search(r"\b(soon|next month|next few weeks|before|spring|summer|fall|winter)\b", text):
        return "soon"
    if re.search(r"\b(someday|eventually|later|not ready|just thinking)\b", text):
        return "someday"
    return "unknown"


def _objections_from_message(message: str) -> list[str]:
    text = message.lower()
    objections = []
    if re.search(r"\b(expensive|too much|costly|budget)\b", text):
        objections.append("cost concern")
    if re.search(r"\b(not ready|overwhelmed|unsure|not sure|maybe later)\b", text):
        objections.append("uncertain readiness")
    if re.search(r"\b(time|busy|schedule|disruptive)\b", text):
        objections.append("timing or disruption concern")
    return objections


def _coerce_stage(value: Any) -> HomeGuideStage | None:
    allowed = {
        "exploring",
        "clarifying_goal",
        "project_identified",
        "quote_ready",
        "quote_request_started",
        "quote_request_sent",
        "handoff_needed",
    }
    return value if value in allowed else None


def _coerce_readiness(value: Any) -> ConversionReadiness | None:
    return value if value in {"low", "medium", "high"} else None


def _coerce_budget_sensitivity(value: Any) -> Literal["unknown", "low", "medium", "high"] | None:
    return value if value in {"unknown", "low", "medium", "high"} else None


def _coerce_timeline(value: Any) -> Literal["unknown", "now", "soon", "someday"] | None:
    return value if value in {"unknown", "now", "soon", "someday"} else None


def _coerce_next_best_action(value: Any) -> NextBestAction | None:
    allowed = {
        "answer_question",
        "ask_clarifying_question",
        "recommend_project",
        "offer_quote_request",
        "start_quote_request",
        "handoff",
    }
    return value if value in allowed else None


def _coerce_intent(value: Any) -> Literal["exploring", "design_advice", "pricing", "quote_readiness", "provider_request"]:
    allowed = {"exploring", "design_advice", "pricing", "quote_readiness", "provider_request"}
    return value if value in allowed else "exploring"


def _coerce_quote_status(value: Any) -> Literal["exploring", "drafting", "awaiting_approval", "approved", "sent"] | None:
    return value if value in {"exploring", "drafting", "awaiting_approval", "approved", "sent"} else None


def _coerce_confidence(value: Any) -> Literal["low", "medium", "high"] | None:
    return value if value in {"low", "medium", "high"} else None


def _fallback_design_focus(message: str) -> str:
    text = message.lower()
    if re.search(r"\b(paint|painting|color|wall|trim|ceiling)\b", text):
        return "painting"
    if re.search(r"\b(floor|flooring|rug|tile|hardwood|carpet)\b", text):
        return "flooring"
    if re.search(r"\b(layout|furniture|sofa|table|bed|desk|space)\b", text):
        return "layout"
    if re.search(r"\b(light|lighting|lamp|window|bright|dim)\b", text):
        return "lighting"
    if re.search(r"\b(measure|measurement|dimension|sq ft|square feet|size)\b", text):
        return "measurements"
    return "general"


def _message_is_status_check(message: str) -> bool:
    return bool(re.search(r"\b(hello|hi|hey|test|testing|are you there|working|can you hear)\b", message.lower()))


def _message_requests_visible_detail(message: str) -> bool:
    text = message.lower()
    return bool(
        re.search(
            r"\b(see|look|visible|notice|shown|there|where|how many|count|desk|chair|"
            r"person|people|window|door|wall|floor|cabinet|counter|sofa|table|light|fixture)\b",
            text,
        )
    )


def _asks_about_technology(message: str) -> bool:
    return bool(
        re.search(
            r"\b(scan|scanned|capture|captured|roomplan|keyframe|image|images|photo|photos|"
            r"lidar|mesh|model|data|how does|how do you know|what are you using)\b",
            message.lower(),
        )
    )


def _polish_homeowner_message(message: str, *, allow_technical: bool = False) -> str:
    if allow_technical:
        return message

    replacements = [
        (r"\bin the scan images\b", "from what I can see here"),
        (r"\bin the scanned images\b", "from what I can see here"),
        (r"\bin the captured images\b", "from what I can see here"),
        (r"\bscan images\b", "what I can see here"),
        (r"\bscanned images\b", "what I can see here"),
        (r"\bcaptured images\b", "what I can see here"),
        (r"\bselected images\b", "what I can see here"),
        (r"\bimages\b", "what I can see here"),
        (r"\bimage\b", "view"),
        (r"\bphotos\b", "what I can see here"),
        (r"\bphoto\b", "view"),
        (r"\bkeyframes\b", "views"),
        (r"\bkeyframe\b", "view"),
        (r"\bRoomPlan-derived\b", "rough"),
        (r"\bRoomPlan data itself\b", "the room details I have"),
        (r"\bRoomPlan data\b", "the room details I have"),
        (r"\bRoomPlan\b", "the room layout"),
        (r"\bscan context\b", "details I have about your home"),
        (r"\bscan summary\b", "home details"),
        (r"\bthe scan\b", "what I can see here"),
        (r"\bthis scan\b", "this space"),
        (r"\byour scan\b", "your home details"),
        (r"\bscanned room\b", "room"),
        (r"\bscanned areas\b", "areas of your home"),
        (r"\bscanned area\b", "area of your home"),
        (r"\bcaptured\b", "available"),
        (r"\bcapture\b", "record"),
        (r"\bdata packet\b", "details"),
        (r"\bmodel data\b", "home details"),
        (r"\bvisual observation\b", "what I can tell"),
    ]
    polished = message
    for pattern, replacement in replacements:
        polished = re.sub(pattern, replacement, polished, flags=re.IGNORECASE)
    polished = re.sub(r"\s+", " ", polished).strip()
    polished = polished.replace("Yes -", "Yes,")
    return polished


def _fallback_visual_focus(request: HomeAIChatRequest, intent: str) -> HomeAIVisualFocus | None:
    return None


def _fallback_message(
    request: HomeAIChatRequest,
    *,
    intent: str | None = None,
    quote_draft: HomeAIQuoteDraft | None = None,
) -> str:
    context = request.homeContext
    room_phrase = _room_phrase(context)
    measurement_phrase = _measurement_phrase(context)
    intent = intent or _classify_intent(request.message)

    if quote_draft:
        price = ""
        if quote_draft.estimatedRangeLow and quote_draft.estimatedRangeHigh:
            price = (
                f" A first planning range is about ${quote_draft.estimatedRangeLow:,.0f}-"
                f"${quote_draft.estimatedRangeHigh:,.0f}, based on {quote_draft.estimateUnit or 'the visible scope'}."
            )
        return (
            f"I can help shape this into a provider-ready request.{price}\n\n"
            f"Based on {room_phrase}{measurement_phrase}, I drafted a concise scope below. "
            "Review it before sending; nothing goes to a provider until you choose one and approve the request."
        )

    if _message_is_status_check(request.message):
        return (
            "Yes, I am here. I can help you think through your home, compare practical project options, "
            "estimate rough scope, or shape a quote request when you are ready."
        )

    if intent == "design_advice":
        focus = _fallback_design_focus(request.message)
        if focus == "painting":
            return (
                f"For painting, I would use {room_phrase}{measurement_phrase} as a planning baseline and keep the first pass simple: "
                "choose one main wall color, then decide whether trim or ceilings are part of the refresh. "
                "Warm white, soft sage, muted blue-gray, or a gentle clay tone are good starting directions, depending on the room's natural light."
            )
        if focus == "flooring":
            return (
                f"For flooring, I would treat {room_phrase}{measurement_phrase} as a rough scope and have a provider confirm before ordering materials. "
                "The most practical next choice is usually between durable luxury vinyl, warmer engineered wood, or a rug-forward refresh if the current floor is staying."
            )
        if focus == "layout":
            return (
                f"For layout, I would start from circulation: keep the main path through {room_phrase} clear, then anchor one comfortable focal area. "
                "If you want, tell me the room's purpose and I can turn that into a tighter furniture or service plan."
            )
        if focus == "lighting":
            return (
                f"For lighting, I would start with how {room_phrase} feels at night, then add layers: "
                "overhead softness, one task source, and one warmer accent so the space feels intentional in the evening."
            )
        if focus == "measurements":
            if measurement_phrase:
                return (
                    f"I have {room_phrase}{measurement_phrase}. That is useful for planning, "
                    "but a provider should confirm measurements before bidding or ordering materials."
                )
            return (
                "I do not have enough reliable room dimensions here to quote measurements confidently. "
                "I can still help frame options and mark what a provider should verify."
            )
        return (
            f"Based on {room_phrase}{measurement_phrase}, I would start with calm, practical moves: "
            "freshen the main wall plane, simplify the trim contrast, and choose one warmer texture so the room feels more intentional. "
            "If the room has good natural light, soft warm whites, muted greens, or a gentle clay tone could work beautifully. "
            "If the room is dimmer, I would keep walls lighter and bring depth through textiles, wood, or hardware."
        )

    return (
        f"I can help you think through this space. Based on {room_phrase}{measurement_phrase}, "
        "we can explore design direction, rough project cost, or turn the idea into a quote request when you are ready."
    )


def _room_phrase(context: HomeAIContextPacket) -> str:
    if context.roomCount == 0:
        return "the home details I have"
    if context.roomCount == 1:
        room = context.rooms[0] if context.rooms else {}
        label = room.get("name") or room.get("type") or "this room"
        return str(label)
    return f"{context.roomCount} areas of your home"


def _measurement_phrase(context: HomeAIContextPacket) -> str:
    area = _total_area_square_feet(context)
    if area:
        return f" and roughly {area:.0f} sq ft"
    if context.rooms:
        return " and the rough measurements available"
    return ""


def _make_quote_draft(request: HomeAIChatRequest, service_type: str | None) -> HomeAIQuoteDraft:
    context = request.homeContext
    service_type = service_type or request.workflowState.selectedServiceType
    low, high, unit = _estimate_range(context, service_type)
    summary = f"Homeowner is asking about: {request.message.strip()}"
    provider_request = _provider_request_text(context, request.message, service_type)
    assumptions = ["Measurements are rough planning details and should be field-verified."]
    if context.selectedKeyframes:
        assumptions.append("Visible room details can help clarify finishes and condition.")
    if not service_type:
        assumptions.append("Service type was inferred loosely; confirm the trade before sending.")

    return HomeAIQuoteDraft(
        serviceType=service_type,
        title=f"{service_type or 'Home project'} request",
        homeownerSummary=summary,
        providerRequest=provider_request,
        scopeNotes=_scope_notes(context, service_type),
        measurementAssumptions=assumptions,
        missingDetails=_missing_details(service_type),
        estimatedRangeLow=low,
        estimatedRangeHigh=high,
        estimateUnit=unit,
    )


def _provider_request_text(context: HomeAIContextPacket, message: str, service_type: str | None) -> str:
    parts = [
        f"I would like a quote for {service_type.lower() if service_type else 'a home project'} based on my TakeShape home details.",
        f"Homeowner notes: {message.strip()}",
    ]
    area = _total_area_square_feet(context)
    if area:
        parts.append(f"Approximate floor area: {area:.0f} sq ft.")
    if context.rooms:
        room_lines = []
        for room in context.rooms[:4]:
            label = room.get("name") or room.get("type") or "Scanned area"
            area_m2 = room.get("floorAreaSquareMeters")
            if isinstance(area_m2, (int, float)) and area_m2 > 0:
                room_lines.append(f"{label}: {area_m2 * 10.7639:.0f} sq ft")
            else:
                room_lines.append(str(label))
        parts.append("Rooms/areas: " + "; ".join(room_lines) + ".")
    parts.append("Please field-verify measurements and provide recommended scope, timing, and pricing.")
    return "\n".join(parts)


def _scope_notes(context: HomeAIContextPacket, service_type: str | None) -> list[str]:
    notes = []
    if service_type:
        notes.append(f"Requested service: {service_type}.")
    area = _total_area_square_feet(context)
    if area:
        notes.append(f"Approximate floor area: about {area:.0f} sq ft.")
    opening_count = sum(int(room.get("openingCount") or 0) for room in context.rooms)
    window_count = sum(int(room.get("windowCount") or 0) for room in context.rooms)
    if opening_count or window_count:
        notes.append(f"Visible openings/windows: {opening_count} openings, {window_count} windows.")
    return notes or ["Scope should be confirmed with the homeowner before provider dispatch."]


def _missing_details(service_type: str | None) -> list[str]:
    common = ["Preferred timing", "Any access constraints or pets"]
    if service_type == "Painting":
        return ["Paint color/finish", "Whether ceilings, trim, or repairs are included"] + common
    if service_type == "Flooring":
        return ["Preferred flooring material", "Whether existing flooring removal is needed"] + common
    if service_type == "Interior Cleaning":
        return ["Cleaning level", "Any delicate surfaces or priority areas"] + common
    if service_type == "Decking":
        return ["Desired material", "Repair vs new build"] + common
    if service_type == "Window Cleaning":
        return ["Interior, exterior, or both", "Approximate window count to field-verify"] + common
    if service_type == "Power Washing":
        return ["Surfaces to wash", "Water access details"] + common
    return ["Preferred service type", "Desired outcome"] + common


def _estimate_range(context: HomeAIContextPacket, service_type: str | None) -> tuple[float | None, float | None, str | None]:
    area = _total_area_square_feet(context)
    if not area:
        return None, None, None

    if service_type == "Painting":
        return max(450, area * 2.5), max(950, area * 6.5), "rough interior painting range"
    if service_type == "Flooring":
        return max(800, area * 7), max(1600, area * 18), "installed flooring range"
    if service_type == "Interior Cleaning":
        return max(160, area * 0.18), max(360, area * 0.45), "deep-cleaning range"
    return max(300, area * 1.5), max(850, area * 5.0), "rough planning range"


def _total_area_square_feet(context: HomeAIContextPacket) -> float | None:
    total = context.totals.get("floorAreaSquareMeters")
    if isinstance(total, (int, float)) and total > 0:
        return float(total) * 10.7639

    area_m2 = 0.0
    for room in context.rooms:
        value = room.get("floorAreaSquareMeters")
        if isinstance(value, (int, float)) and value > 0:
            area_m2 += float(value)
    return area_m2 * 10.7639 if area_m2 > 0 else None


def _suggested_replies(intent: str, quote_draft: HomeAIQuoteDraft | None) -> list[str]:
    if quote_draft:
        return ["Find providers", "Edit the draft", "What assumptions did you use?"]
    if intent == "design_advice":
        return ["Show me paint ideas", "What would this cost?", "Draft a quote request"]
    return ["What should I do with this space?", "What would painting cost?", "Help me get quotes"]


def _openai_thread_state_path(thread_id: str) -> Path:
    safe_thread_id = re.sub(r"[^A-Za-z0-9_-]", "_", thread_id)
    return Path(settings.storage_dir) / "ai_threads" / safe_thread_id / "openai_state.json"


def _load_openai_thread_state(thread_id: str) -> dict[str, Any]:
    try:
        state_path = _openai_thread_state_path(thread_id)
        if not state_path.exists():
            return {}
        data = json.loads(state_path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not load OpenAI home AI thread state: %s", exc)
        return {}


def _save_openai_thread_state(thread_id: str, *, response_id: str, model: str) -> None:
    try:
        state_path = _openai_thread_state_path(thread_id)
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(
            json.dumps(
                {
                    "previousResponseId": response_id,
                    "model": model,
                    "updatedAt": now_utc().isoformat(),
                },
                ensure_ascii=True,
            ),
            encoding="utf-8",
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not save OpenAI home AI thread state: %s", exc)


def _resolve_prompt_variant(thread_id: str) -> HomeGuidePromptVariantID:
    try:
        path = _prompt_assignment_path(thread_id)
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            variant = data.get("promptVariant")
            if variant in HOME_GUIDE_PROMPT_VARIANTS:
                return variant

        variant = assign_home_guide_prompt_variant(thread_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "promptVersion": HOME_GUIDE_PROMPT_VERSION,
                    "promptVariant": variant,
                    "assignedAt": now_utc().isoformat(),
                },
                ensure_ascii=True,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        return variant
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not resolve home guide prompt variant: %s", exc)
        return "control"


def _prompt_assignment_path(thread_id: str) -> Path:
    safe_thread_id = re.sub(r"[^A-Za-z0-9_-]", "_", thread_id)
    return Path(settings.storage_dir) / "ai_threads" / safe_thread_id / "prompt_assignment.json"


def _persist_and_record_turn(request: HomeAIChatRequest, response: HomeAIChatResponse) -> None:
    _persist_turn(request, response)
    try:
        record_home_guide_turn(
            storage_dir=settings.storage_dir,
            request=request,
            response=response,
            prompt_version=response.promptVersion,
            prompt_variant=response.promptVariant,
            context_quality=response.contextQuality,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not record home guide analytics: %s", exc)


def _persist_turn(request: HomeAIChatRequest, response: HomeAIChatResponse) -> None:
    try:
        thread_dir = Path(settings.storage_dir) / "ai_threads" / response.threadId
        thread_dir.mkdir(parents=True, exist_ok=True)
        request_json = request.model_dump(mode="json", exclude_none=True)
        for frame in request_json.get("homeContext", {}).get("selectedKeyframes", []):
            if "jpegBase64" in frame:
                frame["jpegBase64"] = f"<redacted:{len(frame['jpegBase64'])} chars>"
        for attachment in request_json.get("attachments", []):
            if "dataBase64" in attachment:
                attachment["dataBase64"] = f"<redacted:{len(attachment['dataBase64'])} chars>"
        for message in request_json.get("messages", []):
            for attachment in message.get("attachments", []):
                if "dataBase64" in attachment:
                    attachment["dataBase64"] = f"<redacted:{len(attachment['dataBase64'])} chars>"
        turn_path = thread_dir / "messages.jsonl"
        with turn_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps({
                "createdAt": now_utc().isoformat(),
                "request": request_json,
                "response": response.model_dump(mode="json", exclude_none=True),
            }) + "\n")
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not persist home AI turn: %s", exc)
