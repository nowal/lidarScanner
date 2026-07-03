from __future__ import annotations

import json
import re
from typing import Any

from pydantic import BaseModel, Field


SQUARE_METERS_TO_SQUARE_FEET = 10.7639
METERS_TO_FEET = 3.28084


class HomeContextQuality(BaseModel):
    hasImages: bool = False
    hasFloorPlan: bool = False
    hasRoomNames: bool = False
    hasMeasurements: bool = False
    hasUserGoals: bool = False
    hasServiceCatalog: bool = False
    recommendedDataImprovements: list[str] = Field(default_factory=list)


class HomeGuideModelContext(BaseModel):
    contextVersion: str = "home_guide_model_context_v1"
    projectRefs: dict[str, Any] = Field(default_factory=dict)
    rooms: list[dict[str, Any]] = Field(default_factory=list)
    totals: dict[str, Any] = Field(default_factory=dict)
    floorplanSummary: str | None = None
    meshSummary: dict[str, Any] = Field(default_factory=dict)
    selectedKeyframes: list[dict[str, Any]] = Field(default_factory=list)
    workflowState: dict[str, Any] = Field(default_factory=dict)
    notes: list[str] = Field(default_factory=list)
    serviceCatalog: list[dict[str, Any]] = Field(default_factory=list)
    quoteStatus: dict[str, Any] = Field(default_factory=dict)
    constraintsAndUnknowns: list[str] = Field(default_factory=list)
    quality: HomeContextQuality = Field(default_factory=HomeContextQuality)


DEFAULT_SERVICE_CATALOG: list[dict[str, Any]] = [
    {
        "serviceType": "Painting",
        "description": "Interior wall, trim, ceiling, and room refresh painting.",
    },
    {
        "serviceType": "Flooring",
        "description": "Floor replacement, repair, refinishing, or material planning.",
    },
    {
        "serviceType": "Interior Cleaning",
        "description": "Deep cleaning and move-in or project-prep cleaning.",
    },
    {
        "serviceType": "Decking",
        "description": "Deck repair, refresh, replacement, or outdoor surface work.",
    },
    {
        "serviceType": "Window Cleaning",
        "description": "Interior and exterior window cleaning.",
    },
    {
        "serviceType": "Power Washing",
        "description": "Pressure washing for exterior surfaces.",
    },
]


def build_home_guide_model_context(
    home_context: Any,
    workflow_state: Any | None = None,
    *,
    included_image_ids: set[str] | None = None,
    service_catalog: list[dict[str, Any]] | None = None,
    max_rooms: int = 6,
    max_objects_per_room: int = 8,
) -> HomeGuideModelContext:
    raw = _to_plain(home_context)
    workflow = _to_plain(workflow_state)
    service_catalog = service_catalog or DEFAULT_SERVICE_CATALOG
    included_image_ids = included_image_ids or set()

    rooms = [
        _compact_room(room, max_objects=max_objects_per_room)
        for room in _as_list(raw.get("rooms"))[:max_rooms]
    ]
    totals = _compact_totals(raw.get("totals") or {}, rooms)
    selected_keyframes = [
        _compact_keyframe(frame, included_image_ids)
        for frame in _as_list(raw.get("selectedKeyframes"))[:6]
    ]
    mesh_summary = _compact_mesh_summary(raw.get("meshSummary") or {})
    quote_status = {
        "status": workflow.get("quoteStatus") or _nested_get(raw, ["workflowState", "quoteStatus"]) or "exploring",
        "selectedServiceType": workflow.get("selectedServiceType"),
        "hasQuoteDraft": bool(workflow.get("quoteDraft")),
    }
    project_refs = {
        key: value
        for key, value in {
            "scanId": raw.get("scanId"),
            "jobId": raw.get("jobId"),
            "createdAt": raw.get("createdAt"),
            "projectId": raw.get("projectId"),
            "homeProfileId": raw.get("homeProfileId"),
            "sourcePage": raw.get("sourcePage"),
        }.items()
        if value
    }
    notes = [
        _naturalize_context_text(str(note), 280)
        for note in _as_list(raw.get("notes"))[:8]
        if str(note).strip()
    ]
    constraints = _constraints_and_unknowns(raw, rooms, selected_keyframes, totals)
    quality = evaluate_home_context_quality(
        raw,
        workflow,
        rooms=rooms,
        selected_keyframes=selected_keyframes,
        service_catalog=service_catalog,
    )

    return HomeGuideModelContext(
        projectRefs=project_refs,
        rooms=rooms,
        totals=totals,
        floorplanSummary=_naturalize_context_text(raw.get("floorplanSummary"), 420),
        meshSummary=mesh_summary,
        selectedKeyframes=selected_keyframes,
        workflowState=_compact_workflow(workflow),
        notes=notes,
        serviceCatalog=service_catalog,
        quoteStatus=quote_status,
        constraintsAndUnknowns=constraints,
        quality=quality,
    )


def evaluate_home_context_quality(
    home_context: Any,
    workflow_state: Any | None = None,
    *,
    rooms: list[dict[str, Any]] | None = None,
    selected_keyframes: list[dict[str, Any]] | None = None,
    service_catalog: list[dict[str, Any]] | None = None,
) -> HomeContextQuality:
    raw = _to_plain(home_context)
    workflow = _to_plain(workflow_state)
    rooms = rooms if rooms is not None else [_compact_room(room) for room in _as_list(raw.get("rooms"))]
    selected_keyframes = selected_keyframes if selected_keyframes is not None else [
        _compact_keyframe(frame, set()) for frame in _as_list(raw.get("selectedKeyframes"))
    ]

    has_images = bool(selected_keyframes)
    has_floorplan = bool(raw.get("floorplanSummary")) or bool(rooms)
    has_room_names = any(_clean_text(room.get("name"), 80) for room in rooms)
    has_measurements = bool(_total_area_square_feet(raw, rooms)) or any(
        room.get("dimensionsFeet") or room.get("floorAreaSquareFeet") for room in rooms
    )
    has_user_goals = bool(workflow.get("userGoals")) or any("goal" in str(note).lower() for note in _as_list(raw.get("notes")))
    has_service_catalog = bool(service_catalog)

    improvements: list[str] = []
    if not has_images:
        improvements.append("Attach a small set of representative room views.")
    if not has_floorplan:
        improvements.append("Include a floorplan summary for spatial grounding.")
    if not has_room_names:
        improvements.append("Let the homeowner name rooms or confirm room types.")
    if not has_measurements:
        improvements.append("Include room dimensions or floor area when available.")
    if not has_user_goals:
        improvements.append("Capture explicit homeowner goals before optimizing recommendations.")
    if not has_service_catalog:
        improvements.append("Pass the available service catalog for this homeowner's area.")

    return HomeContextQuality(
        hasImages=has_images,
        hasFloorPlan=has_floorplan,
        hasRoomNames=has_room_names,
        hasMeasurements=has_measurements,
        hasUserGoals=has_user_goals,
        hasServiceCatalog=has_service_catalog,
        recommendedDataImprovements=improvements,
    )


def context_payload_size_chars(context: HomeGuideModelContext) -> int:
    return len(json.dumps(context.model_dump(mode="json", exclude_none=True), ensure_ascii=True))


def _compact_room(room: Any, *, max_objects: int = 8) -> dict[str, Any]:
    raw = _to_plain(room)
    dimensions = {
        key: _round(value)
        for key, value in {
            "widthFt": _meters_to_feet(raw.get("boundingWidthMeters")),
            "lengthFt": _meters_to_feet(raw.get("boundingLengthMeters")),
            "heightFt": _meters_to_feet(raw.get("estimatedHeightMeters")),
        }.items()
        if value is not None
    }
    result = {
        "id": raw.get("id"),
        "name": _naturalize_context_text(raw.get("name"), 80),
        "type": _naturalize_context_text(raw.get("type"), 80),
        "dimensionsFeet": dimensions or None,
        "floorAreaSquareFeet": _square_meters_to_feet(raw.get("floorAreaSquareMeters")),
        "perimeterFeet": _meters_to_feet(raw.get("perimeterMeters")),
        "counts": {
            "walls": raw.get("wallCount") or 0,
            "openings": raw.get("openingCount") or 0,
            "doors": raw.get("doorCount") or 0,
            "windows": raw.get("windowCount") or 0,
            "objects": raw.get("objectCount") or 0,
        },
        "visibleObjects": [
            _compact_object(obj)
            for obj in _as_list(raw.get("objects"))[:max_objects]
        ],
    }
    return {key: value for key, value in result.items() if value not in (None, "", [], {})}


def _compact_object(obj: Any) -> dict[str, Any]:
    raw = _to_plain(obj)
    dimensions = {
        key: _round(value)
        for key, value in {
            "widthFt": _meters_to_feet(raw.get("widthMeters")),
            "heightFt": _meters_to_feet(raw.get("heightMeters")),
            "depthFt": _meters_to_feet(raw.get("depthMeters")),
        }.items()
        if value is not None
    }
    return {
        key: value
        for key, value in {
            "category": _clean_text(raw.get("category"), 80),
            "dimensionsFeet": dimensions or None,
        }.items()
        if value not in (None, "", [], {})
    }


def _compact_totals(totals: Any, rooms: list[dict[str, Any]]) -> dict[str, Any]:
    raw = _to_plain(totals)
    total_area = _square_meters_to_feet(raw.get("floorAreaSquareMeters"))
    if total_area is None:
        room_area = sum(float(room.get("floorAreaSquareFeet") or 0) for room in rooms)
        total_area = _round(room_area) if room_area > 0 else None
    result = {
        "floorAreaSquareFeet": total_area,
        "roomCount": raw.get("roomCount") or len(rooms),
        "wallCount": raw.get("wallCount"),
        "openingCount": raw.get("openingCount"),
        "doorCount": raw.get("doorCount"),
        "windowCount": raw.get("windowCount"),
        "objectCount": raw.get("objectCount"),
    }
    return {key: value for key, value in result.items() if value not in (None, "", [], {})}


def _compact_mesh_summary(mesh_summary: Any) -> dict[str, Any]:
    raw = _to_plain(mesh_summary)
    allowed = [
        "rawAnchorCount",
        "rawVertexCount",
        "rawFaceCount",
        "keyframeCount",
        "depthFrameCount",
        "photorealStatus",
    ]
    return {key: raw.get(key) for key in allowed if raw.get(key) not in (None, "", [], {})}


def _compact_keyframe(frame: Any, included_image_ids: set[str]) -> dict[str, Any]:
    raw = _to_plain(frame)
    frame_id = str(raw.get("id") or "")
    result = {
        "id": frame_id,
        "capturedAt": raw.get("capturedAt"),
        "timestamp": raw.get("timestamp"),
        "imageResolution": raw.get("imageResolution"),
        "note": _clean_text(raw.get("note"), 220),
        "imageIncludedThisTurn": frame_id in included_image_ids,
        "hasImageReference": bool(raw.get("jpegBase64") or raw.get("imageResolution")),
    }
    return {key: value for key, value in result.items() if value not in (None, "", [], {})}


def _compact_workflow(workflow: Any) -> dict[str, Any]:
    raw = _to_plain(workflow)
    allowed = [
        "quoteStatus",
        "selectedServiceType",
        "sourcePage",
        "initialRoomId",
        "userGoals",
        "stylePreferences",
        "timeline",
        "budgetSensitivity",
    ]
    compact = {key: raw.get(key) for key in allowed if raw.get(key) not in (None, "", [], {})}
    if raw.get("quoteDraft"):
        compact["hasQuoteDraft"] = True
    return compact


def _constraints_and_unknowns(
    raw_context: dict[str, Any],
    rooms: list[dict[str, Any]],
    selected_keyframes: list[dict[str, Any]],
    totals: dict[str, Any],
) -> list[str]:
    constraints = [
        "Rough room measurements are useful for planning but should be field-verified before bidding or ordering materials.",
        "Do not infer hidden condition issues, exact material quality, or provider availability from the scan alone.",
    ]
    if not selected_keyframes:
        constraints.append("No representative room views were included in this turn.")
    if not rooms:
        constraints.append("No named rooms or room summaries were included.")
    if not totals.get("floorAreaSquareFeet"):
        constraints.append("No reliable total floor area was included.")
    if not raw_context.get("serviceOpportunities"):
        constraints.append("No precomputed service opportunities are available yet.")
    return constraints


def _total_area_square_feet(raw_context: dict[str, Any], rooms: list[dict[str, Any]]) -> float | None:
    totals = _to_plain(raw_context.get("totals"))
    area = _square_meters_to_feet(totals.get("floorAreaSquareMeters"))
    if area is not None:
        return area
    room_area = sum(float(room.get("floorAreaSquareFeet") or 0) for room in rooms)
    return _round(room_area) if room_area > 0 else None


def _to_plain(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    if hasattr(value, "model_dump"):
        dumped = value.model_dump(mode="json", exclude_none=True)
        return dumped if isinstance(dumped, dict) else {}
    if hasattr(value, "__dict__"):
        return dict(value.__dict__)
    return {}


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _clean_text(value: Any, max_len: int) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return text[:max_len]


def _naturalize_context_text(value: Any, max_len: int) -> str | None:
    text = _clean_text(value, max_len)
    if not text:
        return None
    replacements = [
        (r"\bRoomPlan captured area\b", "room area"),
        (r"\bRoomPlan-derived\b", "rough"),
        (r"\bRoomPlan\b", "floorplan"),
        (r"\bscan viewpoints\b", "room views"),
        (r"\bselected keyframes\b", "representative room views"),
        (r"\bkeyframes\b", "room views"),
        (r"\bscan context\b", "home context"),
        (r"\bscan\b", "home"),
    ]
    naturalized = text
    for pattern, replacement in replacements:
        naturalized = re.sub(pattern, replacement, naturalized, flags=re.IGNORECASE)
    return naturalized[:max_len]


def _nested_get(value: dict[str, Any], keys: list[str]) -> Any:
    current: Any = value
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _square_meters_to_feet(value: Any) -> float | None:
    if isinstance(value, (int, float)) and value > 0:
        return _round(float(value) * SQUARE_METERS_TO_SQUARE_FEET)
    return None


def _meters_to_feet(value: Any) -> float | None:
    if isinstance(value, (int, float)) and value > 0:
        return _round(float(value) * METERS_TO_FEET)
    return None


def _round(value: float) -> float:
    return round(value, 2)
