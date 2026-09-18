"""Build a `HomeAIContextPacket`-shaped dict from a raw RoomPlan
``CapturedRoom`` JSON export — the same fields, units, and bounding-box
approximations the iOS `HomeAIContextBuilder` produces
(`HomeAIChatView.swift:264-348`), so smoke tests exercise the agent with
exactly the geometry shape production sends.

Known real fixture: ``upstream/takeshape-mobile/LidarAITests/captured-room.json``
(a genuine scanned living space: 8.8m x 6.0m floor, 10 walls, 2 windows,
9 recognized furniture objects).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

SQM_TO_SQFT = 10.7639


def _category_name(entity: dict[str, Any]) -> str:
    category = entity.get("category")
    if isinstance(category, dict) and category:
        return next(iter(category.keys()))
    return "object"


def _confidence_name(entity: dict[str, Any]) -> str | None:
    confidence = entity.get("confidence")
    if isinstance(confidence, dict) and confidence:
        return next(iter(confidence.keys()))
    return None


def polygon_area(corners: list[list[float]]) -> float:
    """Shoelace area over the first two coordinates of RoomPlan polygon
    corners — the true footprint, vs the bounding-box rectangle the iOS
    builder approximates with."""
    if not corners or len(corners) < 3:
        return 0.0
    area = 0.0
    n = len(corners)
    for i in range(n):
        x1, y1 = corners[i][0], corners[i][1]
        x2, y2 = corners[(i + 1) % n][0], corners[(i + 1) % n][1]
        area += x1 * y2 - x2 * y1
    return abs(area) / 2.0


def _section_label(data: dict[str, Any]) -> str | None:
    """RoomPlan's own room-type classification (iOS 17+):
    livingRoom/kitchen/diningRoom/bedroom/bathroom/unidentified."""
    labels = [
        str(s.get("label", "")).strip()
        for s in data.get("sections", [])
        if isinstance(s, dict)
    ]
    named = [label for label in labels if label and label != "unidentified"]
    if not named:
        return None
    import re

    # camelCase → words ("livingRoom" → "living room")
    return re.sub(r"(?<=[a-z])(?=[A-Z])", " ", named[0]).lower()


def context_from_captured_room(path: str | Path, *, room_index: int = 1) -> dict[str, Any]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    walls = data.get("walls", [])
    doors = data.get("doors", [])
    windows = data.get("windows", [])
    openings = data.get("openings", [])
    objects = data.get("objects", [])
    floors = data.get("floors", [])

    # iOS computes an axis-aligned bounding box of the surface footprint;
    # the RoomPlan floor surface's own dimensions are that bbox already.
    if floors:
        width = float(floors[0]["dimensions"][0])
        length = float(floors[0]["dimensions"][1])
    else:  # fall back to wall extents
        width = max((float(w["dimensions"][0]) for w in walls), default=0.0)
        length = width
    height = max((float(w["dimensions"][1]) for w in walls), default=0.0)
    perimeter = 2 * (width + length)
    # True footprint from the floor polygon when present (the untapped
    # RoomPlan data — bbox overestimates any non-rectangular room).
    bbox_area = width * length
    true_area = polygon_area(floors[0].get("polygonCorners", [])) if floors else 0.0
    floor_area = true_area if true_area > 0 else bbox_area
    section_label = _section_label(data)
    stories = {int(s.get("story", 0)) for s in (*floors, *walls) if isinstance(s, dict)}

    def _round(value: float) -> float:
        return round(value, 2)

    room = {
        "id": f"room-{room_index}",
        "name": section_label.title() if section_label else f"Room {room_index}",
        "type": section_label or "RoomPlan captured area",
        "boundingWidthMeters": _round(width),
        "boundingLengthMeters": _round(length),
        "estimatedHeightMeters": _round(height),
        "floorAreaSquareMeters": _round(floor_area),
        "perimeterMeters": _round(perimeter),
        "wallCount": len(walls),
        "openingCount": len(openings),
        "doorCount": len(doors),
        "windowCount": len(windows),
        "objectCount": len(objects),
        "objects": [
            {
                "category": _category_name(obj),
                "widthMeters": _round(float(obj["dimensions"][0])),
                "heightMeters": _round(float(obj["dimensions"][1])),
                "depthMeters": _round(float(obj["dimensions"][2])),
                **(
                    {"confidence": _confidence_name(obj)}
                    if _confidence_name(obj)
                    else {}
                ),
            }
            for obj in objects[:12]
        ],
        # Untapped-RoomPlan extras (see docs/ARKIT_DATA_OPPORTUNITIES.md):
        "floorAreaIsTrueFootprint": true_area > 0,
        "story": min(stories) if stories else 0,
        "windowSizes": [
            {
                "widthMeters": _round(float(w["dimensions"][0])),
                "heightMeters": _round(float(w["dimensions"][1])),
            }
            for w in windows[:6]
        ],
    }
    totals = {
        "floorAreaSquareMeters": room["floorAreaSquareMeters"],
        "roomCount": 1,
        "wallCount": room["wallCount"],
        "openingCount": room["openingCount"],
        "doorCount": room["doorCount"],
        "windowCount": room["windowCount"],
        "objectCount": room["objectCount"],
    }
    return {
        "contextVersion": "home_ai_context_v1",
        "roomCount": 1,
        "rooms": [room],
        "totals": totals,
        "floorplanSummary": (
            f"1 RoomPlan captured area, about {round(floor_area * SQM_TO_SQFT)} sq ft."
        ),
        "meshSummary": {"photorealStatus": "processing", "keyframeCount": 0},
        "selectedKeyframes": [],
        "notes": [
            "Context derived from a real RoomPlan capture; no keyframe photos "
            "are available in this test fixture, so ground on the geometry "
            "and recognized objects only."
        ],
    }
