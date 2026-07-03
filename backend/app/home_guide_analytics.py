from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field


HomeGuideEventType = Literal[
    "chat_started",
    "home_context_loaded",
    "home_context_missing_data",
    "user_goal_detected",
    "style_preference_detected",
    "service_interest_detected",
    "project_identified",
    "cta_quote_shown",
    "cta_quote_clicked",
    "quote_request_draft_created",
    "quote_request_sent",
    "user_objection_detected",
    "handoff_suggested",
    "chat_abandoned",
    "chat_reengaged",
]


class ConversationOutcomeScore(BaseModel):
    engagementScore: int = Field(ge=0, le=100)
    conversionScore: int = Field(ge=0, le=100)
    helpfulnessSignals: list[str] = Field(default_factory=list)
    frictionSignals: list[str] = Field(default_factory=list)
    likelyDropoffReason: str | None = None


class HomeGuideEventRecord(BaseModel):
    id: str
    conversationId: str
    userId: str | None = None
    projectId: str | None = None
    eventType: str
    payload: dict[str, Any] = Field(default_factory=dict)
    createdAt: str


def record_home_guide_turn(
    *,
    storage_dir: str,
    request: Any,
    response: Any,
    prompt_version: str,
    prompt_variant: str,
    context_quality: Any,
) -> list[HomeGuideEventRecord]:
    thread_id = str(getattr(response, "threadId", "") or "")
    if not thread_id:
        return []

    thread_dir = _thread_dir(storage_dir, thread_id)
    thread_dir.mkdir(parents=True, exist_ok=True)
    metadata_path = thread_dir / "conversation.json"
    existing = _read_json_object(metadata_path)
    is_new_conversation = not bool(existing)

    user_id = _request_attr(request, "userId") or _workflow_value(request, "userId")
    project_id = _request_attr(request, "projectId") or _workflow_value(request, "projectId")
    source_page = _request_attr(request, "sourcePage") or _workflow_value(request, "sourcePage")
    state = _to_plain(getattr(response, "state", None))
    quote_draft = _to_plain(getattr(response, "quoteDraft", None))
    cta = _to_plain(getattr(response, "cta", None))
    quality = _to_plain(context_quality)
    now = _now_iso()

    event_records: list[HomeGuideEventRecord] = []
    if is_new_conversation:
        event_records.append(_event(thread_id, user_id, project_id, "chat_started", {"sourcePage": source_page}))

    event_records.append(
        _event(
            thread_id,
            user_id,
            project_id,
            "home_context_loaded",
            {
                "quality": quality,
                "roomCount": len(_context_rooms(request)),
                "hasQuoteDraft": bool(quote_draft),
            },
        )
    )
    if quality.get("recommendedDataImprovements"):
        event_records.append(
            _event(
                thread_id,
                user_id,
                project_id,
                "home_context_missing_data",
                {"recommendedDataImprovements": quality.get("recommendedDataImprovements", [])},
            )
        )
    for goal in state.get("userGoals") or []:
        event_records.append(_event(thread_id, user_id, project_id, "user_goal_detected", {"goal": goal}))
    for preference in state.get("stylePreferences") or []:
        event_records.append(
            _event(thread_id, user_id, project_id, "style_preference_detected", {"stylePreference": preference})
        )
    for service in state.get("servicesDiscussed") or []:
        event_records.append(_event(thread_id, user_id, project_id, "service_interest_detected", {"serviceType": service}))
    if state.get("stage") in {"project_identified", "quote_ready", "quote_request_started"}:
        event_records.append(
            _event(
                thread_id,
                user_id,
                project_id,
                "project_identified",
                {
                    "stage": state.get("stage"),
                    "servicesDiscussed": state.get("servicesDiscussed") or [],
                    "roomsDiscussed": state.get("roomsDiscussed") or [],
                },
            )
        )
    if cta:
        event_records.append(_event(thread_id, user_id, project_id, "cta_quote_shown", cta))
    if quote_draft:
        event_records.append(
            _event(
                thread_id,
                user_id,
                project_id,
                "quote_request_draft_created",
                {
                    "serviceType": quote_draft.get("serviceType"),
                    "title": quote_draft.get("title"),
                },
            )
        )
    for objection in state.get("objections") or []:
        event_records.append(_event(thread_id, user_id, project_id, "user_objection_detected", {"objection": objection}))
    if state.get("stage") == "handoff_needed" or state.get("nextBestAction") == "handoff":
        event_records.append(_event(thread_id, user_id, project_id, "handoff_suggested", {"state": state}))

    for record in event_records:
        _append_jsonl(thread_dir / "events.jsonl", record.model_dump(mode="json"))
        _append_jsonl(_global_events_path(storage_dir), record.model_dump(mode="json"))

    metadata = _updated_conversation_metadata(
        existing=existing,
        now=now,
        thread_id=thread_id,
        request=request,
        response=response,
        prompt_version=prompt_version,
        prompt_variant=prompt_variant,
        source_page=source_page,
        state=state,
        quote_draft=quote_draft,
        cta=cta,
    )
    metadata_path.write_text(json.dumps(metadata, ensure_ascii=True, indent=2, sort_keys=True), encoding="utf-8")
    return event_records


def record_home_guide_event(
    *,
    storage_dir: str,
    conversation_id: str,
    event_type: str,
    payload: dict[str, Any] | None = None,
    user_id: str | None = None,
    project_id: str | None = None,
) -> HomeGuideEventRecord:
    thread_dir = _thread_dir(storage_dir, conversation_id)
    thread_dir.mkdir(parents=True, exist_ok=True)
    record = _event(conversation_id, user_id, project_id, event_type, payload or {})
    _append_jsonl(thread_dir / "events.jsonl", record.model_dump(mode="json"))
    _append_jsonl(_global_events_path(storage_dir), record.model_dump(mode="json"))

    metadata_path = thread_dir / "conversation.json"
    metadata = _read_json_object(metadata_path)
    if event_type == "quote_request_sent":
        metadata["quoteRequestSent"] = True
        if payload and payload.get("quoteRequestId"):
            metadata["quoteRequestId"] = payload["quoteRequestId"]
    if event_type == "cta_quote_clicked":
        metadata["ctaAcceptedCount"] = int(metadata.get("ctaAcceptedCount") or 0) + 1
    metadata["updatedAt"] = _now_iso()
    metadata_path.write_text(json.dumps(metadata, ensure_ascii=True, indent=2, sort_keys=True), encoding="utf-8")
    return record


def score_home_guide_conversation(
    *,
    messages: list[dict[str, Any]],
    events: list[dict[str, Any]],
    final_state: dict[str, Any],
) -> ConversationOutcomeScore:
    user_messages = [message for message in messages if message.get("role") in {"homeowner", "user"}]
    event_types = [event.get("eventType") for event in events]
    engagement = 0
    conversion = 0
    helpful: list[str] = []
    friction: list[str] = []

    if len(user_messages) >= 2:
        engagement += 25
        helpful.append("User replied after the assistant engaged.")
    if final_state.get("userGoals"):
        engagement += 15
        helpful.append("Conversation captured homeowner goals.")
    if final_state.get("stylePreferences"):
        engagement += 10
        helpful.append("Conversation captured style preferences.")
    if final_state.get("roomsDiscussed"):
        engagement += 10
        helpful.append("Conversation grounded in specific rooms.")
    if final_state.get("servicesDiscussed"):
        conversion += 20
        helpful.append("Service interest emerged.")
    if "cta_quote_clicked" in event_types:
        conversion += 25
        helpful.append("User accepted a quote CTA.")
    if "quote_request_draft_created" in event_types:
        conversion += 20
        helpful.append("Quote request draft was created.")
    if "quote_request_sent" in event_types:
        conversion += 45
        helpful.append("Quote request was sent.")

    cta_events = event_types.count("cta_quote_shown")
    project_events = event_types.count("project_identified")
    if cta_events and not project_events:
        conversion = max(0, conversion - 15)
        friction.append("CTA appeared before project intent was clearly detected.")
    if len([event for event in events if event.get("eventType") == "home_context_missing_data"]) >= 2:
        engagement = max(0, engagement - 10)
        friction.append("Repeated missing home context limited specificity.")
    if len(user_messages) <= 1 and cta_events:
        friction.append("Conversation may have dropped after an early CTA.")

    likely_dropoff = None
    if friction:
        likely_dropoff = friction[0]
    return ConversationOutcomeScore(
        engagementScore=min(100, engagement),
        conversionScore=min(100, conversion),
        helpfulnessSignals=helpful,
        frictionSignals=friction,
        likelyDropoffReason=likely_dropoff,
    )


async def summarize_home_guide_performance(
    *,
    storage_dir: str,
    start_date: datetime,
    end_date: datetime,
) -> dict[str, Any]:
    conversations = _load_conversation_metadata(storage_dir, start_date, end_date)
    grouped: dict[str, dict[str, Any]] = {}
    for conversation in conversations:
        key = (
            f"{conversation.get('promptVersion', 'unknown')}/"
            f"{conversation.get('promptVariant', 'unknown')}/"
            f"{conversation.get('sourcePage', 'unknown')}"
        )
        group = grouped.setdefault(
            key,
            {
                "conversationCount": 0,
                "quoteRequestSentCount": 0,
                "messageCount": 0,
                "ctaShownCount": 0,
                "ctaAcceptedCount": 0,
                "servicesDiscussed": {},
            },
        )
        group["conversationCount"] += 1
        group["quoteRequestSentCount"] += 1 if conversation.get("quoteRequestSent") else 0
        group["messageCount"] += int(conversation.get("messageCount") or 0)
        group["ctaShownCount"] += int(conversation.get("ctaShownCount") or 0)
        group["ctaAcceptedCount"] += int(conversation.get("ctaAcceptedCount") or 0)
        for service in conversation.get("servicesDiscussed") or []:
            services = group["servicesDiscussed"]
            services[service] = int(services.get(service) or 0) + 1

    recommendations = _performance_recommendations(grouped)
    report_lines = [
        f"Home Guide performance from {start_date.date().isoformat()} to {end_date.date().isoformat()}",
        f"Conversations analyzed: {len(conversations)}",
    ]
    for key, group in sorted(grouped.items()):
        count = max(1, int(group["conversationCount"]))
        conversion_rate = group["quoteRequestSentCount"] / count
        report_lines.append(
            f"- {key}: {group['conversationCount']} conversations, "
            f"{conversion_rate:.0%} quote sent rate, "
            f"{group['ctaAcceptedCount']} CTA accepts"
        )

    return {
        "report": "\n".join(report_lines),
        "groups": grouped,
        "recommendations": recommendations,
    }


summarizeHomeGuidePerformance = summarize_home_guide_performance


def _updated_conversation_metadata(
    *,
    existing: dict[str, Any],
    now: str,
    thread_id: str,
    request: Any,
    response: Any,
    prompt_version: str,
    prompt_variant: str,
    source_page: str | None,
    state: dict[str, Any],
    quote_draft: dict[str, Any],
    cta: dict[str, Any],
) -> dict[str, Any]:
    user_message_count = int(existing.get("userMessageCount") or 0) + 1
    assistant_message_count = int(existing.get("assistantMessageCount") or 0) + 1
    services = sorted(set((existing.get("servicesDiscussed") or []) + (state.get("servicesDiscussed") or [])))
    rooms = sorted(set((existing.get("roomsDiscussed") or []) + (state.get("roomsDiscussed") or [])))
    metadata = {
        "id": thread_id,
        "userId": existing.get("userId") or _request_attr(request, "userId") or _workflow_value(request, "userId"),
        "projectId": existing.get("projectId") or _request_attr(request, "projectId") or _workflow_value(request, "projectId"),
        "startedAt": existing.get("startedAt") or now,
        "endedAt": None,
        "promptVersion": existing.get("promptVersion") or prompt_version,
        "promptVariant": existing.get("promptVariant") or prompt_variant,
        "model": getattr(response, "model", None),
        "sourcePage": existing.get("sourcePage") or source_page,
        "initialStage": existing.get("initialStage") or state.get("stage"),
        "finalStage": state.get("stage"),
        "quoteRequestSent": bool(existing.get("quoteRequestSent") or state.get("stage") == "quote_request_sent"),
        "quoteRequestId": existing.get("quoteRequestId"),
        "messageCount": user_message_count + assistant_message_count,
        "userMessageCount": user_message_count,
        "assistantMessageCount": assistant_message_count,
        "ctaShownCount": int(existing.get("ctaShownCount") or 0) + (1 if cta else 0),
        "ctaAcceptedCount": int(existing.get("ctaAcceptedCount") or 0),
        "servicesDiscussed": services,
        "roomsDiscussed": rooms,
        "finalConversionReadiness": state.get("conversionReadiness"),
        "outcomeSummary": _outcome_summary(state, quote_draft, cta),
        "createdAt": existing.get("createdAt") or now,
        "updatedAt": now,
    }
    return metadata


def _performance_recommendations(grouped: dict[str, dict[str, Any]]) -> list[str]:
    recommendations: list[str] = []
    if not grouped:
        return ["Collect more conversations before changing the production prompt."]
    for key, group in grouped.items():
        count = max(1, int(group["conversationCount"]))
        cta_shown = int(group["ctaShownCount"] or 0)
        cta_accepted = int(group["ctaAcceptedCount"] or 0)
        quote_sent = int(group["quoteRequestSentCount"] or 0)
        if cta_shown > 0 and cta_accepted / max(1, cta_shown) < 0.15:
            recommendations.append(f"Review CTA timing and language for {key}; acceptance is below 15%.")
        if count >= 5 and quote_sent == 0:
            recommendations.append(f"Audit drop-off points for {key}; no quote requests were sent.")
    return recommendations or ["No prompt changes suggested automatically. Review sample conversations before tuning."]


def _outcome_summary(state: dict[str, Any], quote_draft: dict[str, Any], cta: dict[str, Any]) -> str:
    parts = [f"stage={state.get('stage')}", f"readiness={state.get('conversionReadiness')}"]
    if quote_draft:
        parts.append(f"draft={quote_draft.get('serviceType') or 'unknown service'}")
    if cta:
        parts.append("quote_cta_shown")
    return "; ".join(part for part in parts if part)


def _load_conversation_metadata(storage_dir: str, start_date: datetime, end_date: datetime) -> list[dict[str, Any]]:
    root = Path(storage_dir) / "ai_threads"
    if not root.exists():
        return []
    rows: list[dict[str, Any]] = []
    for path in root.glob("*/conversation.json"):
        data = _read_json_object(path)
        created = _parse_datetime(data.get("createdAt"))
        if created and start_date <= created <= end_date:
            rows.append(data)
    return rows


def _event(
    conversation_id: str,
    user_id: str | None,
    project_id: str | None,
    event_type: str,
    payload: dict[str, Any],
) -> HomeGuideEventRecord:
    return HomeGuideEventRecord(
        id=_safe_event_id(conversation_id, event_type),
        conversationId=conversation_id,
        userId=user_id,
        projectId=project_id,
        eventType=event_type,
        payload=_redact_payload(payload),
        createdAt=_now_iso(),
    )


def _safe_event_id(conversation_id: str, event_type: str) -> str:
    safe_type = re.sub(r"[^A-Za-z0-9_-]", "_", event_type)
    safe_conversation = re.sub(r"[^A-Za-z0-9_-]", "_", conversation_id)[-18:]
    return f"{safe_conversation}-{safe_type}-{uuid.uuid4()}"


def _redact_payload(payload: dict[str, Any]) -> dict[str, Any]:
    redacted = json.loads(json.dumps(payload, ensure_ascii=True, default=str))
    _redact_large_strings(redacted)
    return redacted


def _redact_large_strings(value: Any) -> None:
    if isinstance(value, dict):
        for key, child in list(value.items()):
            if key.lower() in {"jpegbase64", "image", "imagebase64"} and isinstance(child, str):
                value[key] = f"<redacted:{len(child)} chars>"
            elif isinstance(child, str) and len(child) > 1500:
                value[key] = child[:1500] + "...<truncated>"
            else:
                _redact_large_strings(child)
    elif isinstance(value, list):
        for child in value:
            _redact_large_strings(child)


def _context_rooms(request: Any) -> list[Any]:
    home_context = getattr(request, "homeContext", None)
    rooms = getattr(home_context, "rooms", None)
    return rooms if isinstance(rooms, list) else []


def _workflow_value(request: Any, key: str) -> str | None:
    workflow = getattr(request, "workflowState", None)
    raw = _to_plain(workflow)
    value = raw.get(key)
    return str(value) if value else None


def _request_attr(request: Any, key: str) -> str | None:
    value = getattr(request, key, None)
    return str(value) if value else None


def _to_plain(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    if hasattr(value, "model_dump"):
        dumped = value.model_dump(mode="json", exclude_none=True)
        return dumped if isinstance(dumped, dict) else {}
    return {}


def _thread_dir(storage_dir: str, thread_id: str) -> Path:
    safe_thread_id = re.sub(r"[^A-Za-z0-9_-]", "_", thread_id)
    return Path(storage_dir) / "ai_threads" / safe_thread_id


def _global_events_path(storage_dir: str) -> Path:
    path = Path(storage_dir) / "ai_events" / "events.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _append_jsonl(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(value, ensure_ascii=True, sort_keys=True) + "\n")


def _read_json_object(path: Path) -> dict[str, Any]:
    try:
        if not path.exists():
            return {}
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
