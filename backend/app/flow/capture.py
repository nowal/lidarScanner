"""What a capture is, when RoomPlan cannot say.

An exterior walk has no rooms: no floor polygon, no walls, no area. Three
things can still say what it is and how big -- the photos (the appearance
pass), the homeowner's own words, and the LiDAR mesh. These helpers are the
one place that reads them, so the runtime, the lead package and the price
band agree (Quintin, Oct 1: "the AI is saying it can't measure or see
anything that was scanned").
"""

from __future__ import annotations

import math
import re
from typing import Any

SQM_TO_SQFT = 10.7639
M_TO_FT = 3.28084

# Their own word for what they scanned. Only the unambiguous phrasings.
SAID_EXTERIOR = re.compile(
    r"(?i)\b(?:scann?ed|scan|captured|walked|it'?s|it\s+is|this\s+is|that'?s)\b[^.!?\n]{0,40}?\b(?:outside|exterior)\b"
    r"|\b(?:outside|exterior)\s+of\s+(?:my|the|our)\s+(?:house|home|garage|building|shed|barn|property)\b"
    r"|\bscann?ed\s+(?:my|the|our)\s+(?:driveway|yard|siding|roof|deck|patio|front\s+yard|back\s*yard)\b"
)


def geometry_less(room: Any) -> bool:
    """RoomPlan found no room in this area: no floor plan, no walls, no area."""
    return not room.polygon and not room.wall_count and not room.area_sqft


def is_exterior(state: Any, room: Any) -> bool:
    """The photos said so, or the homeowner did and the scan has no room
    geometry to contradict them. A name the homeowner gave a room still wins."""
    if room.role == "exterior":
        return True
    said = (getattr(state, "scan_appearance", None) or {}).get("setting") == "exterior"
    return bool(said and geometry_less(room) and not room.named_by_homeowner)


def surfaces_feet(state: Any) -> dict[str, float] | None:
    """Upright and level-ground surface the mesh covered, in square feet, when
    the capture is one RoomPlan could not measure. None otherwise: an interior
    room's own numbers are better than a mesh sum that includes its furniture."""
    raw = getattr(state, "scan_surfaces", None) or {}
    exterior = (getattr(state, "scan_appearance", None) or {}).get("setting") == "exterior"
    no_rooms = not (getattr(state, "scan_mesh_bounds", None) or {}).get("roomCount")
    if not raw or not (exterior or no_rooms):
        return None
    out: dict[str, float] = {}
    for key, name, factor in (
        ("uprightSquareMeters", "upright", SQM_TO_SQFT),
        ("groundSquareMeters", "ground", SQM_TO_SQFT),
        ("heightMeters", "height", M_TO_FT),
    ):
        value = raw.get(key)
        if isinstance(value, (int, float)) and math.isfinite(value) and value > 0:
            out[name] = float(value) * factor
    if out.get("upright", 0) < 20 and out.get("ground", 0) < 20:
        return None
    return out
