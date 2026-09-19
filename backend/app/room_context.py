"""Scan bundle -> room context document, cached.

Ported from `takeshape/src/context.py`. Runs ONCE per room. Every conversation
afterwards reads the cached document and never touches an image again, which is
what keeps per-conversation cost flat no matter how long someone chats -- the
same ingest-once shape `flow/home_registry.py` already uses for the home index.

**The guarantees live in code, not in the prompt.** The system prompt asks the
model not to guess, but a prompt cannot be relied on for anything that matters:

* `measurements_from_geometry` never consults the model. Paintable area, floor
  area and perimeter come from the CapturedRoom, full stop.
* `merge_certainty` is a two-classifier gate. RoomPlan geometry and the VLM fail
  in uncorrelated ways; only where they agree does an object become assertable.
  Calibration belongs here, not in a conversation prompt that can be talked out
  of it.
* `validate_appearance` rejects the model's output structurally -- unknown
  `geometry_match` labels are dropped rather than trusted, strings are clamped,
  lists are capped. A malformed or adversarial response degrades to "we saw
  nothing", never to a confident wrong claim about someone's home.
* Frame orientation is computed from gravity in `frame_select`, so the model is
  never asked which way is up.
"""

from __future__ import annotations

import base64
import io
import json
import logging
import math
import re
from pathlib import Path
from typing import Any

from .config import settings
from .flow.keyframe_rooms import world_floor_polygon
from .frame_select import (
    ConventionError,
    DEFAULT_COUNT,
    _category,
    select_frames,
)

logger = logging.getLogger("lidarai.room_context")

SQM_TO_SQFT = 10.7639

# Payload caps. `detail: low` downsamples anyway, so full 1920x1440 frames buy
# nothing and cost upload time on every ingest.
MAX_IMAGE_EDGE = 768
MAX_IMAGES = 6

# Output caps, enforced on the model's response rather than requested of it.
MAX_OBJECTS = 40
MAX_NOTABLE = 10
MAX_TEXT = 240

CERTAINTY_HIGH = "high"
CERTAINTY_LOW = "low"
CERTAINTY_UNOBSERVED = "unobserved"

SURFACE_KEYS = ("walls", "floor", "splashback", "ceiling")

APPEARANCE_SYS = """You are analysing photos from a homeowner's LiDAR room scan for \
a home-design assistant.

Report only what you can actually see. Guessing is worse than omitting: a wrong \
claim about someone's own home destroys their trust in everything else you say.

A geometry pass has already detected some objects and will give you its label \
list. For each object you see, set "geometry_match" to the label from that list \
naming the same physical object, or null if none does. A fridge freezer is a \
"refrigerator"; a ceramic hob is a "stove". Match on what the thing IS, not on \
wording.

Return JSON only:
{
  "room": "<room type>",
  "objects": [{"class": "<noun>", "appearance": "<material, colour, condition>",
               "geometry_match": "<label from the list, or null>"}],
  "surfaces": {"walls": "", "floor": "", "splashback": "", "ceiling": ""},
  "style": "<one phrase>",
  "notable": ["<things a designer would remark on: dated elements, mismatches, \
distinctive features>"]
}
Omit any surface you cannot see. `notable` should be things the homeowner likely \
has an opinion about. Do not report dimensions, areas, or measurements of any \
kind -- those come from the geometry, not from you."""


# --------------------------------------------------------------------------
# Geometry -- never the model
# --------------------------------------------------------------------------


def _surface_area_sqm(surfaces: list[dict[str, Any]]) -> float:
    """Sum of width * height over RoomPlan surfaces (`dimensions` is
    [width/length, height, thickness])."""
    total = 0.0
    for surface in surfaces or []:
        dimensions = surface.get("dimensions") or []
        if len(dimensions) < 2:
            continue
        width, height = float(dimensions[0]), float(dimensions[1])
        if math.isfinite(width) and math.isfinite(height) and width > 0 and height > 0:
            total += width * height
    return total


def _floor_polygon_sqm(room: dict[str, Any]) -> float | None:
    """Shoelace over the floor's world polygon -- the true footprint, not the
    bounding rectangle (which overestimates L-shaped rooms).

    The world projection comes from `flow.keyframe_rooms`, which already does
    this against these bundles for keyframe-to-room assignment. Reusing it keeps
    one answer to "where is this floor": a second copy that drifted would put
    the quote and the photo assignment in different rooms.
    """
    floors = room.get("floors") or []
    if not floors:
        return None
    floor = floors[0]
    points, _y = world_floor_polygon(floor)
    total = 0.0
    for index, (x1, y1) in enumerate(points):
        x2, y2 = points[(index + 1) % len(points)]
        total += x1 * y2 - x2 * y1
    area = abs(total) / 2.0
    if area > 0:
        return area
    # A floor with no usable outline still has extents. `world_floor_polygon`
    # substitutes a bounding rectangle, but one built without a transform lies
    # edge-on in a y-up world and shoelaces to nothing -- so fall back on the
    # measured area rather than reporting a room with no floor.
    dimensions = floor.get("dimensions") or []
    if len(dimensions) >= 2:
        return abs(float(dimensions[0]) * float(dimensions[1]))
    return None


def measurements_from_geometry(room: dict[str, Any]) -> dict[str, float]:
    """RoomPlan surfaces -> paintable / floor / perimeter.

    Wall area minus door and window area is the whole calculation, exactly as the
    original stub described. These are the numbers the agent must never ask for
    and must never let a model produce.

    A key is present when the geometry supports it and absent when it does not.
    A measured zero is a real answer -- a capture whose openings swallow the
    whole wall is bad data the ops package should show as 0, not silently omit.
    """
    out: dict[str, float] = {}

    walls = room.get("walls") or []
    if walls:
        wall_area = _surface_area_sqm(walls)
        openings_area = _surface_area_sqm(room.get("doors") or []) + _surface_area_sqm(
            room.get("windows") or []
        )
        paintable = max(0.0, wall_area - openings_area)
        out["wall_m2"] = round(wall_area, 2)
        out["paintable_m2"] = round(paintable, 2)
        out["paintable_sqft"] = round(paintable * SQM_TO_SQFT, 1)
        perimeter = sum(
            float((wall.get("dimensions") or [0.0])[0]) for wall in walls
        )
        if perimeter > 0:
            out["perimeter_m"] = round(perimeter, 2)
            # Wall area over perimeter is the mean height RoomPlan measured, and
            # it is the cheapest way to see that a capture is wrong: a room open
            # to a stairwell reports storey-height walls, which inflates paintable
            # area without looking obviously broken. Surfaced so a human can
            # catch it before it reaches a quote, not used to correct anything.
            out["mean_wall_height_m"] = round(wall_area / perimeter, 2)

    floor_sqm = _floor_polygon_sqm(room)
    if floor_sqm is not None:
        out["floor_m2"] = round(floor_sqm, 2)
        out["floor_sqft"] = round(floor_sqm * SQM_TO_SQFT, 1)

    return out


def geometry_labels(room: dict[str, Any]) -> list[str]:
    """What RoomPlan says is in the room, normalised and deduplicated."""
    return sorted({_category(obj.get("category")) for obj in room.get("objects") or []})


# --------------------------------------------------------------------------
# Structural validation of the model's response
# --------------------------------------------------------------------------


def _clean_text(value: Any, limit: int = MAX_TEXT) -> str:
    if not isinstance(value, str):
        return ""
    # Collapse whitespace so a model cannot pad the document with layout.
    return re.sub(r"\s+", " ", value).strip()[:limit]


def parse_json_object(raw: str) -> dict[str, Any]:
    """The first complete JSON object in the response, or an empty dict.

    Models wrap JSON in prose or fences often enough that failing the whole
    ingest over it would be the wrong trade.
    """
    if not isinstance(raw, str):
        return {}
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        return {}
    try:
        parsed = json.loads(raw[start : end + 1])
    except (ValueError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def validate_appearance(raw: Any, allowed_labels: list[str]) -> dict[str, Any]:
    """Coerce a model response into the shape the rest of the code assumes.

    Anything unrecognised is dropped rather than passed through. In particular a
    `geometry_match` that is not in the supplied label list is discarded -- the
    join is the only thing standing between "two classifiers agreed" and "the
    model asserted agreement", so it is checked here rather than trusted.
    """
    document = raw if isinstance(raw, dict) else {}
    allowed = {label.lower().replace("_", " ") for label in allowed_labels}

    objects = []
    for entry in (document.get("objects") or [])[:MAX_OBJECTS]:
        if not isinstance(entry, dict):
            continue
        name = _clean_text(entry.get("class"), 80)
        if not name:
            continue
        match = _clean_text(entry.get("geometry_match"), 80).lower().replace("_", " ")
        objects.append(
            {
                "class": name,
                "appearance": _clean_text(entry.get("appearance")),
                "geometry_match": match if match in allowed else None,
            }
        )

    surfaces_in = document.get("surfaces")
    surfaces = {}
    if isinstance(surfaces_in, dict):
        for key in SURFACE_KEYS:
            text = _clean_text(surfaces_in.get(key))
            if text:
                surfaces[key] = text

    notable = []
    for item in (document.get("notable") or [])[:MAX_NOTABLE]:
        text = _clean_text(item)
        if text:
            notable.append(text)

    return {
        "room": _clean_text(document.get("room"), 60),
        "objects": objects,
        "surfaces": surfaces,
        "style": _clean_text(document.get("style"), 80),
        "notable": notable,
    }


def merge_certainty(
    labels: list[str], vlm_objects: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Two independent classifiers; agreement is the calibration signal.

    Geometry (RoomPlan) and the VLM fail in uncorrelated ways, and both results
    are already in hand. Agree -> assertable. Otherwise emit the observation at
    `low` and let the agent ask rather than claim.

    Geometry also knows about objects the selected frames never showed. Dropping
    those silently loses real features (a fireplace, say) and hides the coverage
    gap that gives a rescan offer an honest reason to fire.
    """
    geo = {label.lower().replace("_", " ") for label in labels}
    merged: list[dict[str, Any]] = []
    matched: set[str] = set()

    for entry in vlm_objects:
        match = entry.get("geometry_match")
        hit = bool(match) and match in geo
        if hit:
            matched.add(match)
        merged.append(
            {
                "class": entry.get("class"),
                "appearance": entry.get("appearance", ""),
                "certainty": CERTAINTY_HIGH if hit else CERTAINTY_LOW,
            }
        )

    for label in sorted(geo - matched):
        merged.append(
            {"class": label, "appearance": "", "certainty": CERTAINTY_UNOBSERVED}
        )
    return merged


def high_certainty(context: dict[str, Any]) -> list[dict[str, Any]]:
    """The only objects the agent may state as fact."""
    return [
        obj
        for obj in context.get("objects", [])
        if obj.get("certainty") == CERTAINTY_HIGH
    ]


def unobserved(context: dict[str, Any]) -> list[str]:
    """Geometry knows these are here; no selected frame showed them."""
    return [
        obj["class"]
        for obj in context.get("objects", [])
        if obj.get("certainty") == CERTAINTY_UNOBSERVED
    ]


# --------------------------------------------------------------------------
# Images
# --------------------------------------------------------------------------


def encode_frame(path: Path, turns: int) -> str | None:
    """Base64 JPEG, rotated upright and downscaled.

    Uncorrected orientation does not fail loudly -- the model describes the room
    sideways and sounds fine doing it. It also swaps floor for ceiling, which is
    the layer painting and flooring quotes are built on.
    """
    try:
        from PIL import Image
    except ImportError:  # pragma: no cover -- Pillow is in requirements.txt
        logger.warning("Pillow unavailable; skipping appearance images")
        return None
    try:
        with Image.open(path) as image:
            image = image.convert("RGB")
            if turns % 4:
                image = image.rotate(90 * (turns % 4), expand=True)
            image.thumbnail((MAX_IMAGE_EDGE, MAX_IMAGE_EDGE))
            buffer = io.BytesIO()
            image.save(buffer, format="JPEG", quality=82)
    except OSError as error:
        logger.warning("Unreadable keyframe %s: %s", path.name, error)
        return None
    return base64.standard_b64encode(buffer.getvalue()).decode()


def _image_path(room_dir: Path, frame_id: str) -> Path:
    return room_dir / "rebuild" / "images" / f"{frame_id}.jpg"


# --------------------------------------------------------------------------
# Building
# --------------------------------------------------------------------------


def _geometry_only(
    room: dict[str, Any], room_key: str, reason: str
) -> dict[str, Any]:
    """A context document with no appearance pass.

    Rooms with no detected objects (a closet, a stairway) or no usable frames
    still need measurements and a coverage posture; they just have nothing to
    look at. No model call is made.
    """
    labels = geometry_labels(room)
    return {
        "room_key": room_key,
        "room": "",
        "objects": [
            {"class": label, "appearance": "", "certainty": CERTAINTY_UNOBSERVED}
            for label in labels
        ],
        "surfaces": {},
        "style": "",
        "notable": [],
        "measurements": measurements_from_geometry(room),
        "coverage": "geometry_only",
        "coverage_reason": reason,
        "frames": [],
    }


def _room_geometry(bundle_dir: str | Path, room_key: str) -> dict[str, Any] | None:
    room_dir = Path(bundle_dir) / "rooms" / room_key
    return _load_json(room_dir / "room.json") or _load_json(room_dir / "live.json")


def geometry_context(bundle_dir: str | Path, room_key: str) -> dict[str, Any]:
    """A context document from geometry alone -- no model call, no images.

    This is what ingest can afford for every room in a whole-home export: the
    measurements are the expensive half of the answer and they cost nothing but
    arithmetic. The appearance pass is a model call per room, so it stays
    explicit and per-room rather than firing nineteen times on a walk-through.
    """
    room = _room_geometry(bundle_dir, room_key)
    if not room:
        return _geometry_only({}, room_key, "no CapturedRoom in the bundle")
    return _geometry_only(room, room_key, "appearance pass not run")


def select_context_frames(
    room: dict[str, Any], manifest: dict[str, Any], count: int = DEFAULT_COUNT
) -> list[dict[str, Any]]:
    """Frame selection with the failure modes handled rather than raised."""
    count = max(1, min(count, MAX_IMAGES))
    try:
        frames = select_frames(room, manifest, count)
    except ConventionError as error:
        # A room with no detected fixtures (a hallway, an empty spare room,
        # the "unnamed area N" cases) has nothing to score frames against,
        # yet it is exactly the room the appearance pass could name. Fall
        # back to frames spread across the walk of that area.
        from .frame_select import object_boxes, spread_frames

        if object_boxes(room):
            logger.info("No usable frames for appearance pass: %s", error)
            return []
        frames = spread_frames(manifest, count)
        logger.info("Room has no detected objects; appearance pass uses %d spread frames", len(frames))
    return frames


def build_prompt_content(
    frames: list[dict[str, Any]], room_dir: Path, labels: list[str]
) -> list[dict[str, Any]]:
    """Image blocks plus the geometry label list, in capture order."""
    content: list[dict[str, Any]] = []
    for frame in frames:
        encoded = encode_frame(_image_path(room_dir, frame["id"]), frame["turns"])
        if encoded is None:
            continue
        content.append(
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/jpeg",
                    "data": encoded,
                },
            }
        )
    if not content:
        return []
    content.append(
        {
            "type": "text",
            "text": (
                f"{len(content)} views of one room, in capture order. "
                "Describe the room as a whole, not frame by frame.\n\n"
                f"Geometry pass detected these labels: {labels}"
            ),
        }
    )
    return content


async def _call_model(content: list[dict[str, Any]]) -> str:
    import anthropic

    client = anthropic.AsyncAnthropic(
        api_key=settings.anthropic_api_key,
        timeout=float(settings.anthropic_request_timeout_seconds),
        max_retries=2,
    )
    response = await client.messages.create(
        model=settings.anthropic_model,
        max_tokens=settings.anthropic_max_tokens,
        system=APPEARANCE_SYS,
        messages=[{"role": "user", "content": content}],
    )
    return "".join(
        block.text for block in response.content if getattr(block, "type", "") == "text"
    )


async def build(
    bundle_dir: str | Path,
    room_key: str,
    *,
    count: int = DEFAULT_COUNT,
    caller: Any = None,
) -> dict[str, Any]:
    """One room's context document. `caller` is injectable for tests."""
    room_dir = Path(bundle_dir) / "rooms" / room_key
    room = _room_geometry(bundle_dir, room_key)
    if not room:
        return _geometry_only({}, room_key, "no CapturedRoom in the bundle")

    manifest = _load_json(room_dir / "rebuild" / "manifest.json") or {}
    labels = geometry_labels(room)
    frames = select_context_frames(room, manifest, count)
    if not frames:
        return _geometry_only(room, room_key, "no frame showed a detected object")

    content = build_prompt_content(frames, room_dir, labels)
    if not content:
        return _geometry_only(room, room_key, "selected frames were unreadable")

    invoke = caller or _call_model
    try:
        raw = await invoke(content)
    except Exception as error:  # noqa: BLE001 -- ingest must not die on the provider
        logger.warning("Appearance pass failed for %s: %s", room_key, error)
        return _geometry_only(room, room_key, f"appearance pass failed: {error}")

    appearance = validate_appearance(parse_json_object(raw), labels)
    objects = merge_certainty(labels, appearance["objects"])
    return {
        "room_key": room_key,
        "room": appearance["room"],
        "objects": objects,
        "surfaces": appearance["surfaces"],
        "style": appearance["style"],
        "notable": appearance["notable"],
        "measurements": measurements_from_geometry(room),
        "coverage": "partial"
        if any(obj["certainty"] == CERTAINTY_UNOBSERVED for obj in objects)
        else "complete",
        "frames": [frame["id"] for frame in frames],
    }


# --------------------------------------------------------------------------
# Cache -- same posture as flow/home_registry: local file, keyed by home
# --------------------------------------------------------------------------


def _load_json(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _safe(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "_", value)[:120]


def cache_path(home_id: str, room_key: str) -> Path:
    base = Path(settings.storage_dir) / "room_context" / _safe(home_id)
    return base / f"{_safe(room_key)}.json"


def save(home_id: str, context: dict[str, Any]) -> Path:
    path = cache_path(home_id, context["room_key"])
    # Only the write path creates directories: `load` runs on the read path
    # every turn, and a lookup for an unknown home must not leave a directory.
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(context), encoding="utf-8")
    return path


def load(home_id: str, room_key: str) -> dict[str, Any] | None:
    return _load_json(cache_path(home_id, room_key))


def forget(home_id: str) -> int:
    """Drop every stored context for a home (SOW section 12 deletion path).

    These documents describe someone's home in words, so a deletion that removed
    the index and left them behind would not be a deletion. Returns how many
    were removed, so the caller can log a real number.
    """
    base = Path(settings.storage_dir) / "room_context" / _safe(home_id)
    if not base.is_dir():
        return 0
    removed = 0
    for path in base.glob("*.json"):
        try:
            path.unlink()
            removed += 1
        except OSError:  # pragma: no cover -- best effort, the dir goes anyway
            logger.warning("Could not remove room context %s", path.name)
    try:
        base.rmdir()
    except OSError:  # pragma: no cover -- non-empty or already gone
        pass
    return removed


async def get(
    bundle_dir: str | Path,
    home_id: str,
    room_key: str,
    *,
    count: int = DEFAULT_COUNT,
    refresh: bool = False,
    caller: Any = None,
) -> dict[str, Any]:
    """Cached build. This is the entry point the enrichment path uses.

    A stored `geometry_only` document is a placeholder, not a result: ingest
    writes one for every room, so treating it as a cache hit would mean the
    appearance pass could never run on any room that had been ingested. It also
    makes a provider outage self-healing -- the next request retries instead of
    serving the degraded document forever.
    """
    if not refresh:
        cached = load(home_id, room_key)
        if cached and cached.get("coverage") != "geometry_only":
            return cached
    context = await build(bundle_dir, room_key, count=count, caller=caller)
    save(home_id, context)
    return context
