"""Whole-home index: turn a walked-home export into named, queryable rooms.

The product problem (Noah, Sep 1): a homeowner walks the whole house in one
capture and then says *"let's do the master bathroom"*. The export has no
such name in it. RoomPlan labels roughly half the areas (``livingRoom``,
``bathroom``, ``bedroom``, ``kitchen``) and leaves the rest ``unidentified``;
it never distinguishes the primary bathroom from the powder room, and its
area numbering is capture order, not anything a person would say.

This module resolves that. For every area it computes a real footprint,
reads the fixtures RoomPlan detected, and derives a name a homeowner would
recognise -- with the evidence for it recorded, because a name the agent
cannot justify is a name it should not use. It then maps every captured
frame to the room it was taken in, so "show me the master bathroom" has
photos behind it.

Deliberately model-free: pure geometry and fixture evidence, so it is
cheap, deterministic, testable, and identical on every run.

Bundle layout (TakeShape export):
    meta.json, project.json, structure-floor-K.json
    rooms/room-N/{floor,room,live}.json          area structure
    rooms/room-N/rebuild/manifest.json           frames + camera poses
"""

from __future__ import annotations

import math

import json
import logging
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .flow import regional_naming as _regional_naming

logger = logging.getLogger("lidarai.home_index")

SQFT_PER_SQM = 10.7639


def _category(raw: Any) -> str:
    """RoomPlan emits a category as "sink" or {"sink": {}} depending on
    version; both appear in real exports."""
    if isinstance(raw, str):
        return raw
    if isinstance(raw, dict) and raw:
        return next(iter(raw.keys()))
    return "unknown"


def _transform_point(t: list[float], p: Iterable[float]) -> tuple[float, float, float]:
    """Apply a column-major 4x4 (ARKit simd_float4x4) to a 3D point."""
    coords = list(p) + [0.0, 0.0, 0.0]
    x, y, z = coords[0], coords[1], coords[2]
    return (
        t[0] * x + t[4] * y + t[8] * z + t[12],
        t[1] * x + t[5] * y + t[9] * z + t[13],
        t[2] * x + t[6] * y + t[10] * z + t[14],
    )


def _polygon_world(surface: dict) -> list[tuple[float, float]]:
    """Footprint polygon on the ground plane, in world space."""
    corners = surface.get("polygonCorners") or []
    transform = surface.get("transform") or []
    if not corners or len(transform) < 16:
        return []
    return [(w[0], w[2]) for w in (_transform_point(transform, c) for c in corners)]


def _polygon_area_sqft(points: list[tuple[float, float]]) -> float:
    if len(points) < 3:
        return 0.0
    total = 0.0
    for i, (x1, z1) in enumerate(points):
        x2, z2 = points[(i + 1) % len(points)]
        total += x1 * z2 - x2 * z1
    return abs(total) / 2.0 * SQFT_PER_SQM


def _point_in_polygon(x: float, z: float, poly: list[tuple[float, float]]) -> bool:
    inside = False
    for i, (x1, z1) in enumerate(poly):
        x2, z2 = poly[(i + 1) % len(poly)]
        if (z1 > z) != (z2 > z):
            t = (z - z1) / (z2 - z1) if z2 != z1 else 0.0
            if x < x1 + t * (x2 - x1):
                inside = not inside
    return inside


def _centroid(poly: list[tuple[float, float]]) -> tuple[float, float]:
    if not poly:
        return (0.0, 0.0)
    return (sum(p[0] for p in poly) / len(poly), sum(p[1] for p in poly) / len(poly))


def _segment_gap(p1, p2, q1, q2) -> float:
    """Minimum distance between two segments in the plane."""
    def _point_to_segment(p, a, b) -> float:
        ax, ay = a; bx, by = b; px, py = p
        dx, dy = bx - ax, by - ay
        length_sq = dx * dx + dy * dy
        if length_sq == 0:
            return math.hypot(px - ax, py - ay)
        t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / length_sq))
        return math.hypot(px - (ax + t * dx), py - (ay + t * dy))

    def _orient(a, b, c) -> float:
        return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])

    # Proper intersection -> distance zero.
    d1, d2 = _orient(q1, q2, p1), _orient(q1, q2, p2)
    d3, d4 = _orient(p1, p2, q1), _orient(p1, p2, q2)
    if ((d1 > 0) != (d2 > 0)) and ((d3 > 0) != (d4 > 0)) and d1 and d2 and d3 and d4:
        return 0.0
    return min(
        _point_to_segment(p1, q1, q2), _point_to_segment(p2, q1, q2),
        _point_to_segment(q1, p1, p2), _point_to_segment(q2, p1, p2),
    )


def polygon_gap(a: list[tuple[float, float]], b: list[tuple[float, float]]) -> float:
    """Minimum distance between the boundaries of two polygons (metres in
    the walk's world frame). ``inf`` when either is empty."""
    if len(a) < 2 or len(b) < 2:
        return math.inf
    best = math.inf
    for i in range(len(a)):
        p1, p2 = a[i], a[(i + 1) % len(a)]
        for j in range(len(b)):
            q1, q2 = b[j], b[(j + 1) % len(b)]
            best = min(best, _segment_gap(p1, p2, q1, q2))
            if best == 0.0:
                return 0.0
    return best


@dataclass
class Room:
    key: str                       # "room-19" -- the export's own id
    index: int
    storey: int
    plan_label: str                # what RoomPlan called it, verbatim
    area_sqft: float
    floor_y: float
    polygon: list[tuple[float, float]] = field(default_factory=list)
    objects: Counter = field(default_factory=Counter)
    window_count: int = 0
    door_count: int = 0
    wall_count: int = 0
    frame_ids: list[str] = field(default_factory=list)
    display_name: str = ""
    name_basis: str = ""           # why it got that name -- auditable
    confident: bool = False        # False -> the agent should hedge or ask
    role: str = "unknown"          # bathroom/bedroom/kitchen/... for queries
    storey_word: str = ""          # "upstairs"/"downstairs" for queries
    named_by_homeowner: bool = False   # their word beats any inference
    # Geometry-only measurements (paintable wall area, floor area, perimeter)
    # computed at ingest by ``room_context.measurements_from_geometry``. They
    # ride on the index because the index is the durable copy: the bundle is
    # gone after ingest and the host's disk is wiped on every redeploy.
    measurements: dict = field(default_factory=dict)
    # The on-device textured bake for this area (``rooms/room-N/model.usdz``),
    # when the export carried one: ``file`` (path inside the export), ``bytes``,
    # and after ingest either ``object`` (its Supabase Storage path, the copy a
    # lead package links to) or ``skipped`` (why it was not stored -- usually
    # the storage plan's file-size cap). Absent = the phone had not finished
    # texturing this area when the homeowner exported.
    model: dict = field(default_factory=dict)
    # The appearance pass's document for this room (surfaces, per-object
    # descriptions, style, notable details) when one has been run. It rides
    # on the index for the same reason measurements do: the pass runs on the
    # machine that holds the export, the index is the copy that reaches the
    # server, and the host's disk is wiped on every redeploy.
    appearance: dict = field(default_factory=dict)

    @property
    def centroid(self) -> tuple[float, float]:
        return _centroid(self.polygon)

    @property
    def has_materials(self) -> bool:
        """The appearance pass ran and saw surfaces. A failed or keyless pass
        still leaves a ``geometry_only`` document here, which does not count."""
        return bool(self.appearance) and self.appearance.get("coverage") != "geometry_only"

    def summary(self) -> dict:
        return {
            "key": self.key,
            "name": self.display_name,
            "storey": self.storey,
            "areaSqFt": round(self.area_sqft),
            "planLabel": self.plan_label,
            "fixtures": [f"{n}x {c}" for c, n in self.objects.most_common(5)],
            "windows": self.window_count,
            "doors": self.door_count,
            "photoCount": len(self.frame_ids),
            "nameBasis": self.name_basis,
            "confidentName": self.confident,
            "hasModel": bool(self.model),
            "hasMaterials": self.has_materials,
            # Footprint in the scan's world frame (x, z in metres), so an
            # operations or demo surface can draw the plan and place the
            # active room on it. Six-ish points per room; no photos, no mesh.
            "polygon": [[round(x, 3), round(z, 3)] for x, z in self.polygon],
            "floorY": round(self.floor_y, 3),
        }

    def to_json(self) -> dict:
        """Everything needed to rebuild this room without the bundle -- the
        export is hundreds of megabytes and does not stay on the server."""
        return {
            "key": self.key, "index": self.index, "storey": self.storey,
            "planLabel": self.plan_label, "areaSqFt": self.area_sqft,
            "floorY": self.floor_y, "polygon": [list(p) for p in self.polygon],
            "objects": dict(self.objects), "windows": self.window_count,
            "doors": self.door_count, "walls": self.wall_count,
            "frameIds": self.frame_ids, "name": self.display_name,
            "nameBasis": self.name_basis, "confident": self.confident,
            "role": self.role, "storeyWord": self.storey_word,
            "namedByHomeowner": self.named_by_homeowner,
            "measurements": dict(self.measurements),
            "model": dict(self.model),
            "appearance": dict(self.appearance),
        }

    @classmethod
    def from_json(cls, data: dict) -> "Room":
        return cls(
            key=data["key"], index=int(data.get("index", 0)),
            storey=int(data.get("storey", 0)), plan_label=data.get("planLabel", ""),
            area_sqft=float(data.get("areaSqFt", 0.0)),
            floor_y=float(data.get("floorY", 0.0)),
            polygon=[(p[0], p[1]) for p in data.get("polygon", [])],
            objects=Counter(data.get("objects", {})),
            window_count=int(data.get("windows", 0)),
            door_count=int(data.get("doors", 0)),
            wall_count=int(data.get("walls", 0)),
            frame_ids=list(data.get("frameIds", [])),
            display_name=data.get("name", ""), name_basis=data.get("nameBasis", ""),
            confident=bool(data.get("confident", False)),
            role=data.get("role", "unknown"), storey_word=data.get("storeyWord", ""),
            named_by_homeowner=bool(data.get("namedByHomeowner", False)),
            measurements=dict(data.get("measurements") or {}),
            appearance=dict(data.get("appearance") or {}),
            model=dict(data.get("model") or {}),
        )


def _model_record(path: Path, rel: str) -> dict:
    """What the index remembers about a baked model file: where it sat in the
    export and how big it is. Never the bytes -- the index stays small."""
    try:
        return {"file": rel, "bytes": path.stat().st_size} if path.is_file() else {}
    except OSError:
        return {}


# --------------------------------------------------------------------------
# Naming
# --------------------------------------------------------------------------
def _storey_words(storeys: list[int]) -> dict[int, str]:
    """Human words for storeys. Exports number by detected elevation, not by
    the words people use, and they skip numbers -- the first real house we
    received is numbered 1 and 3."""
    ordered = sorted(set(storeys))
    if len(ordered) <= 1:
        return {s: "" for s in ordered}
    if len(ordered) == 2:
        return {ordered[0]: "downstairs", ordered[1]: "upstairs"}
    words: dict[int, str] = {}
    for i, s in enumerate(ordered):
        if i == 0:
            words[s] = "downstairs"
        elif i == len(ordered) - 1:
            words[s] = "top floor"
        else:
            words[s] = f"floor {s}"
    return words


def _name_rooms(rooms: list[Room]) -> None:
    """Assign homeowner-recognisable names, recording the evidence.

    Order matters: the most specific, most defensible signals first. A room
    only gets a confident name when a fixture proves it -- a label with an
    empty room behind it stays hedged, so the agent asks instead of
    asserting.
    """
    words = _storey_words([r.storey for r in rooms])

    def label(r: Room) -> str:
        return (r.plan_label or "").lower().replace(" ", "")

    def has(r: Room, *cats: str) -> bool:
        return any(r.objects.get(c, 0) for c in cats)

    roles: dict[str, str] = {}
    for r in rooms:
        if has(r, "washerDryer"):
            roles[r.key] = "laundry"
        elif "kitchen" in label(r) or has(r, "stove", "oven", "refrigerator", "dishwasher"):
            roles[r.key] = "kitchen"
        elif "bathroom" in label(r) or has(r, "toilet", "bathtub", "shower"):
            roles[r.key] = "bathroom"
        elif "bedroom" in label(r) or has(r, "bed"):
            roles[r.key] = "bedroom"
        elif "livingroom" in label(r) or r.objects.get("sofa", 0) >= 2 or has(r, "television", "fireplace"):
            roles[r.key] = "living"
        elif r.objects.get("table", 0) >= 1 and r.objects.get("chair", 0) >= 2:
            roles[r.key] = "dining"
        elif has(r, "stairs") and r.area_sqft < 120:
            roles[r.key] = "stairs"
        elif r.area_sqft and r.area_sqft < 30:
            roles[r.key] = "closet"
        else:
            roles[r.key] = "unknown"

    by_role: dict[str, list[Room]] = {}
    for r in rooms:
        r.role = roles[r.key]
        r.storey_word = words.get(r.storey, "")
        by_role.setdefault(roles[r.key], []).append(r)

    def suffix(r: Room, role: str) -> str:
        """Storey word only when it actually disambiguates."""
        same = by_role.get(role, [])
        if len({x.storey for x in same}) <= 1:
            return ""
        word = words.get(r.storey, "")
        return f" ({word})" if word else ""

    # --- bathrooms: the distinction the whole feature exists for ---
    baths = sorted(by_role.get("bathroom", []), key=lambda r: -r.area_sqft)
    primary_bath = next(
        (r for r in baths if has(r, "bathtub") or r.objects.get("sink", 0) >= 2),
        baths[0] if baths else None,
    )
    numbered = 0
    for r in baths:
        if r is primary_bath and len(baths) > 1:
            why = []
            if has(r, "bathtub"):
                why.append("has the bathtub")
            if r.objects.get("sink", 0) >= 2:
                why.append(f"{r.objects['sink']} sinks")
            if not why:
                why.append("largest bathroom")
            r.display_name = "primary bathroom"
            r.name_basis = "; ".join(why) + f"; {round(r.area_sqft)} sq ft"
            r.confident = bool(has(r, "bathtub") or r.objects.get("sink", 0) >= 2)
        elif has(r, "toilet") and not has(r, "bathtub", "shower") and not r.objects.get("sink", 0):
            r.display_name = "powder room"
            r.name_basis = "toilet, no bath or shower"
            r.confident = True
        else:
            numbered += 1
            plain = [
                x for x in baths
                if x is not primary_bath
                and not (has(x, "toilet") and not has(x, "bathtub", "shower") and not x.objects.get("sink", 0))
            ]
            base = f"bathroom {numbered}" if len(plain) > 1 else "bathroom"
            r.display_name = base + suffix(r, "bathroom")
            r.name_basis = (
                f"RoomPlan label '{r.plan_label}'" if "bathroom" in label(r) else "toilet/sink present"
            )
            r.confident = has(r, "toilet", "sink")

    # --- bedrooms ---
    beds = sorted(by_role.get("bedroom", []), key=lambda r: -r.area_sqft)
    numbered = 0
    for i, r in enumerate(beds):
        if i == 0 and len(beds) > 1:
            r.display_name = "primary bedroom"
            r.name_basis = f"largest bedroom, {round(r.area_sqft)} sq ft"
            # Only confident when it is clearly the biggest -- two similar
            # bedrooms make "primary" a guess, and the agent should say so.
            r.confident = has(r, "bed") and r.area_sqft > beds[1].area_sqft * 1.15
        else:
            numbered += 1
            base = f"bedroom {numbered}" if len(beds) > 2 else "bedroom"
            r.display_name = base + suffix(r, "bedroom")
            r.name_basis = "bed detected" if has(r, "bed") else f"RoomPlan label '{r.plan_label}'"
            r.confident = has(r, "bed")

    # Living spaces: a house has one living room. Other sofa-bearing rooms
    # are dens/sitting areas -- calling four rooms "living room 1..4" is
    # how you get an agent confidently discussing the wrong space.
    living = sorted(by_role.get("living", []), key=lambda r: -r.area_sqft)
    for i, r in enumerate(living):
        if i == 0:
            r.display_name = "living room"
            r.name_basis = f"largest living space, {round(r.area_sqft)} sq ft, sofas / television"
            r.confident = True
        else:
            rest = living[1:]
            base = "sitting area" if len(rest) == 1 else f"sitting area {i}"
            r.display_name = base + suffix(r, "living")
            r.name_basis = "sofas, but not the main living room"
            r.confident = False

    simple = {
        "kitchen": ("kitchen", "kitchen fixtures", True),
        "laundry": ("laundry room", "washer/dryer detected", True),
        "dining": ("dining room", "table and chairs, no sofa", False),
        "stairs": ("stairway", "stairs, small footprint", True),
        "closet": ("closet", "very small, storage only", False),
    }
    for role, (base, basis, confident) in simple.items():
        group = sorted(by_role.get(role, []), key=lambda r: -r.area_sqft)
        for i, r in enumerate(group):
            name = base if len(group) == 1 else f"{base} {i + 1}"
            r.display_name = name + suffix(r, role)
            r.name_basis = basis
            r.confident = confident

    for r in by_role.get("unknown", []):
        word = words.get(r.storey, "")
        r.display_name = f"unnamed area {r.index}" + (f" ({word})" if word else "")
        r.name_basis = "no fixtures and no RoomPlan label - needs the homeowner to say what it is"
        r.confident = False


# --------------------------------------------------------------------------
# Index
# --------------------------------------------------------------------------
def _load_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not read %s: %s", path, exc)
        return None


_SYNONYMS = {
    "master bathroom": "primary bathroom",
    "master bath": "primary bathroom",
    "ensuite": "primary bathroom",
    "en suite": "primary bathroom",
    "master bedroom": "primary bedroom",
    "main bedroom": "primary bedroom",
    "half bath": "powder room",
    "guest bath": "powder room",
    "washroom": "bathroom",
    "restroom": "bathroom",
    "lounge": "living room",
    "family room": "living room",
    "den": "living room",
    "utility room": "laundry room",
    "laundry": "laundry room",
    "washer": "laundry room",
    "dryer": "laundry room",
}

# Regional words for the same room ("front room", "keeping room", "lower
# level"). Kept in one place with the regional directive so the list the
# model is told about and the list resolution understands cannot drift
# apart. Local entries win on a clash, since they were tuned against the
# real houses.
_SYNONYMS = {**_regional_naming.SYNONYMS, **_SYNONYMS}

# Rooms a home might have whether or not this scan contains them: naming one
# the index cannot resolve is a signal in itself ("we never scanned the
# garage"), not silence.
_ROOM_WORDS = {
    "bathroom", "bath", "bedroom", "kitchen", "laundry", "closet", "garage",
    "basement", "attic", "office", "study", "den", "dining", "living",
    "hallway", "hall", "patio", "deck", "porch", "nursery", "pantry",
    "mudroom", "foyer", "entry", "stairway", "stairs", "landing", "powder",
    "ensuite", "washroom", "restroom", "lounge", "playroom", "sunroom",
}

_STOPWORDS = re.compile(
    r"\b(the|my|our|a|an|in|of|please|lets|let s|do|redo|paint|painting|want|to|for|about)\b"
)


class HomeIndex:
    """Named rooms for one scanned home, and the queries the agent needs."""

    def __init__(self, rooms: list[Room], bundle_id: str = "", storey_count: int = 1,
                 home_model: dict | None = None):
        # The whole-home bake (top-level ``model.usdz``), same shape as Room.model.
        self.home_model: dict = dict(home_model or {})
        self.rooms = rooms
        self.bundle_id = bundle_id
        self.storey_count = storey_count

    def by_key(self, key: str) -> Room | None:
        return next((r for r in self.rooms if r.key == key), None)

    def resolve(self, phrase: str) -> Room | None:
        """Match what a homeowner typed to a room, or return None.

        None is a real answer: the agent should ask which room rather than
        silently pick one and then describe the wrong space.
        """
        if not phrase:
            return None
        q = re.sub(r"[^a-z0-9 ]+", " ", phrase.lower())
        q = _STOPWORDS.sub(" ", q)
        q = re.sub(r"\s+", " ", q).strip()
        if not q:
            return None
        for word, canonical in _SYNONYMS.items():
            if word in q:
                q = q.replace(word, canonical)
        q = re.sub(r"\s+", " ", q).strip()

        for r in self.rooms:
            if r.display_name.lower() == q:
                return r

        # A storey word narrows the field before anything else: "the
        # upstairs bathroom" must never land on a downstairs one, even
        # though "bathroom" matches both.
        pool = self.rooms
        storey_asked = next(
            (w for w in ("upstairs", "downstairs", "top floor") if w in q), None
        )
        if storey_asked:
            on_storey = [r for r in self.rooms if r.storey_word == storey_asked]
            if on_storey:
                pool = on_storey
                q = re.sub(r"\s+", " ", q.replace(storey_asked, " ")).strip()
                if not q:
                    return max(pool, key=lambda r: r.area_sqft)
                for r in pool:
                    if r.display_name.lower().split(" (")[0] == q:
                        return r
                # "upstairs bathroom" -> whichever upstairs room IS a bathroom
                role_hits = [r for r in pool if r.role == q or q in r.role]
                if role_hits:
                    return max(role_hits, key=lambda r: r.area_sqft)

        contained = [
            r for r in pool
            if r.display_name.lower() in q or q in r.display_name.lower()
        ]
        if len(contained) == 1:
            return contained[0]
        if contained:
            return max(contained, key=lambda r: r.area_sqft)

        role_hits = [r for r in pool if r.role != "unknown" and r.role in q]
        if role_hits:
            return max(role_hits, key=lambda r: r.area_sqft)

        if not pool:
            return None
        tokens = set(q.split())
        scored = [(len(tokens & set(r.display_name.lower().split())), r) for r in pool]
        count, room = max(scored, key=lambda s: (s[0], s[1].area_sqft))
        return room if count else None

    def mentioned_room(self, message: str) -> tuple["Room | None", str | None]:
        """Did the homeowner name a room, and which one?

        Returns ``(room, None)`` when a named room resolves, ``(None,
        phrase)`` when they clearly named a room this home does not have
        (so the agent can say so instead of describing a different one),
        and ``(None, None)`` when no room was mentioned at all.

        Resolution is only attempted when a room word is actually present:
        running every message through the fuzzy matcher would eventually
        drag an unrelated sentence onto some room.
        """
        if not message:
            return None, None
        text = re.sub(r"[^a-z0-9 ]+", " ", message.lower())
        words = set(text.split())

        vocabulary = set(_ROOM_WORDS)
        for room in self.rooms:
            for token in room.display_name.lower().split():
                if token not in {"room", "area", "unnamed"} and not token.isdigit():
                    vocabulary.add(token.strip("()"))
            if room.role != "unknown":
                vocabulary.add(room.role)

        phrase_hit = next((p for p in _SYNONYMS if p in text), None)
        word_hit = next((w for w in vocabulary if w in words), None)
        if not phrase_hit and not word_hit:
            return None, None

        room = self.resolve(message)
        if room is not None:
            return room, None
        return None, (phrase_hit or word_hit)

    def small_unnamed_room(self, message: str) -> "Room | None":
        """"that little room" / "the small one" -- unambiguous only while a
        single area is still unnamed, which is when they ask."""
        if not re.search(r"(?i)\b(small|little|tiny)\b", message or ""):
            return None
        unnamed = [r for r in self.rooms if not r.confident and r.role == "unknown"]
        return unnamed[0] if len(unnamed) == 1 else None

    def rename_room(self, key: str, name: str) -> "Room | None":
        """Record what the homeowner calls a room.

        RoomPlan leaves plenty of areas `unidentified`, and the fixture
        rules can only go so far — a 53 sq ft windowless space is a pantry,
        a closet or a mudroom depending on the house. The homeowner knows.
        Once they say it, it is the most authoritative name we have, so it
        outranks anything inferred and never gets second-guessed again.
        """
        room = self.by_key(key)
        if room is None:
            return None
        clean = re.sub(r"\s+", " ", name.strip().lower())[:40]
        if not clean:
            return None
        room.display_name = clean
        room.name_basis = "named by the homeowner"
        room.confident = True
        room.named_by_homeowner = True
        return room

    def overview(self) -> dict:
        return {
            "bundleId": self.bundle_id,
            "models": {
                "rooms": sum(1 for r in self.rooms if r.model),
                "stored": sum(1 for r in self.rooms if r.model.get("object")),
                "home": bool(self.home_model),
            },
            "roomCount": len(self.rooms),
            "storeys": self.storey_count,
            "totalAreaSqFt": round(sum(r.area_sqft for r in self.rooms)),
            "photoCount": sum(len(r.frame_ids) for r in self.rooms),
            "namedConfidently": sum(1 for r in self.rooms if r.confident),
            "enrichedRooms": sum(1 for r in self.rooms if r.has_materials),
            "rooms": [r.summary() for r in self.rooms],
        }

    def to_json(self) -> dict:
        return {
            "bundleId": self.bundle_id,
            "storeys": self.storey_count,
            "rooms": [r.to_json() for r in self.rooms],
            "homeModel": dict(self.home_model),
        }

    @classmethod
    def from_json(cls, data: dict) -> "HomeIndex":
        return cls(
            [Room.from_json(r) for r in data.get("rooms", [])],
            bundle_id=data.get("bundleId", ""),
            storey_count=int(data.get("storeys", 1) or 1),
            home_model=dict(data.get("homeModel") or {}),
        )

    # ------------------------------------------------------------- layout
    # Adjacency and level changes from the walk's own geometry. Quintin
    # (Sep 15) asked which room sits next to the laundry room and whether
    # the sitting area is a few steps down; the agent said it had no
    # adjacency information. It did: every room's floor polygon is in one
    # world frame, and its floor height is on the index.
    ADJACENT_GAP_M = 0.6          # inner-face polygons sit a wall's thickness apart
    LEVEL_CHANGE_M = 0.15         # one riser is ~0.18 m; below this it is noise

    def neighbours(self, room: "Room") -> list[tuple["Room", float, float]]:
        """Rooms on the same storey whose floor polygon comes within a wall's
        thickness of this one: ``(room, gap_m, floor_delta_m)``, nearest
        first. Rooms without a polygon (unnamed areas with no plan) never
        pair, so nothing is claimed about them."""
        if not room.polygon:
            return []
        out = []
        for other in self.rooms:
            if other.key == room.key or other.storey != room.storey or not other.polygon:
                continue
            gap = polygon_gap(room.polygon, other.polygon)
            if gap <= self.ADJACENT_GAP_M:
                out.append((other, gap, other.floor_y - room.floor_y))
        return sorted(out, key=lambda t: t[1])

    def layout_text(self, room: "Room") -> str:
        """One sentence for the agent: who borders this room, and any level
        change worth a step. Empty when the walk gives no answer."""
        parts = []
        for other, _gap, delta in self.neighbours(room):
            phrase = f"the {other.display_name}"
            if abs(delta) >= self.LEVEL_CHANGE_M:
                direction = "lower" if delta < 0 else "higher"
                phrase += f" (its floor is about {round(abs(delta) * 100)} cm {direction}, a step or two {'down' if delta < 0 else 'up'})"
            parts.append(phrase)
        if not parts:
            return ""
        joined = parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1]
        return f"The {room.display_name} shares a wall with {joined}."

    def layout_overview(self, limit: int = 12) -> str:
        """The home's adjacencies as compact pairs, nearest first, for turns
        with no room in focus."""
        seen = set()
        pairs = []
        for room in self.rooms:
            for other, gap, _delta in self.neighbours(room):
                key = tuple(sorted((room.key, other.key)))
                if key in seen:
                    continue
                seen.add(key)
                pairs.append((gap, f"{room.display_name} - {other.display_name}"))
        pairs.sort()
        return "; ".join(text for _gap, text in pairs[:limit])

    def as_text(self) -> str:
        """Compact home description for the agent's context: one line per
        room, cheap in tokens, no coordinates, uncertainty marked."""
        lines = []
        for r in sorted(self.rooms, key=lambda x: (x.storey, -x.area_sqft)):
            fixtures = ", ".join(c for c, _ in r.objects.most_common(4))
            hedge = "" if r.confident else " (name uncertain)"
            line = f"- {r.display_name}{hedge}: ~{round(r.area_sqft)} sq ft, {r.window_count} window(s)"
            lines.append(line + (f", {fixtures}" if fixtures else ""))
        return "\n".join(lines)


def load_bundle(bundle_dir: str | Path) -> HomeIndex:
    """Read an export directory into a named index.

    Tolerates the degenerate case -- a walk with no RoomPlan structure at
    all, which is what the first single-room export we received looks like.
    Those areas stay unnamed rather than invented.
    """
    base = Path(bundle_dir)
    meta = _load_json(base / "meta.json") or {}
    rooms: list[Room] = []

    rooms_dir = base / "rooms"
    room_dirs = sorted(
        (p for p in rooms_dir.glob("room-*") if p.is_dir()),
        key=lambda p: int(p.name.split("-")[-1]),
    ) if rooms_dir.exists() else []

    for room_dir in room_dirs:
        index = int(room_dir.name.split("-")[-1])
        floor_info = _load_json(room_dir / "floor.json") or {}
        structure = _load_json(room_dir / "room.json")
        if structure is None:
            structure = _load_json(room_dir / "live.json") or {}

        objects: Counter = Counter()
        for obj in structure.get("objects", []) or []:
            cat = _category(obj.get("category"))
            if cat != "unknown":
                objects[cat] += 1

        floors = structure.get("floors") or []
        polygon = _polygon_world(floors[0]) if floors else []
        sections = [s.get("label", "") for s in (structure.get("sections") or [])]

        rooms.append(Room(
            key=room_dir.name,
            index=index,
            storey=int(floor_info.get("floor", 0) or 0),
            plan_label=next((s for s in sections if s and s != "unidentified"), ""),
            area_sqft=_polygon_area_sqft(polygon),
            floor_y=float(floor_info.get("floorY", 0.0) or 0.0),
            polygon=polygon,
            objects=objects,
            window_count=len(structure.get("windows") or []),
            door_count=len(structure.get("doors") or []),
            wall_count=len(structure.get("walls") or []),
            model=_model_record(room_dir / "model.usdz", f"rooms/{room_dir.name}/model.usdz"),
        ))

    _assign_frames(base, rooms)
    _name_rooms(rooms)
    return HomeIndex(
        rooms,
        bundle_id=str(meta.get("id", base.name)),
        storey_count=len({r.storey for r in rooms}) or 1,
        home_model=_model_record(base / "model.usdz", "model.usdz"),
    )


def _assign_frames(base: Path, rooms: list[Room]) -> None:
    """Map every captured frame to the room it was taken in.

    Frames live under the area that captured them, but a walk-through
    crosses rooms -- so camera position decides, and the owning area is only
    the fallback. This is what lets "show me the primary bathroom" return
    photos that were captured while walking past it.
    """
    by_key = {r.key: r for r in rooms}
    for room in rooms:
        manifest = _load_json(base / "rooms" / room.key / "rebuild" / "manifest.json")
        if not manifest:
            continue
        for frame in manifest.get("frames", []) or []:
            fid = frame.get("id")
            transform = frame.get("cameraTransform") or []
            if not fid or len(transform) < 16:
                continue
            x, y, z = transform[12], transform[13], transform[14]

            def inside(candidate: Room) -> bool:
                if not candidate.polygon:
                    return False
                # Storey band: a camera one floor up is not in this room.
                if not (candidate.floor_y - 0.6) <= y <= (candidate.floor_y + 3.2):
                    return False
                return _point_in_polygon(x, z, candidate.polygon)

            # The capturing area wins when the frame is genuinely inside it:
            # RoomPlan footprints overlap at thresholds and in open plans, and
            # without this a room can lose every photo it actually took.
            own = by_key[room.key]
            placed = own if inside(own) else next((c for c in rooms if inside(c)), None)
            (placed or own).frame_ids.append(fid)


def _cli() -> None:  # pragma: no cover -- operator convenience
    import sys

    if len(sys.argv) < 2:
        print("usage: python -m app.home_index <bundle_dir> [query]")
        raise SystemExit(2)
    index = load_bundle(sys.argv[1])
    ov = index.overview()
    print(f"{ov['roomCount']} areas | {ov['storeys']} storey(s) | {ov['totalAreaSqFt']} sq ft | "
          f"{ov['photoCount']} photos | {ov['namedConfidently']} confidently named\n")
    for r in sorted(index.rooms, key=lambda x: (x.storey, -x.area_sqft)):
        mark = " " if r.confident else "?"
        print(f"{mark} {r.display_name:<26} {round(r.area_sqft):>5} sqft  storey {r.storey}  "
              f"{len(r.frame_ids):>3} photos  [{r.key}] {r.name_basis}")
    for q in sys.argv[2:]:
        hit = index.resolve(q)
        print(f"\nresolve({q!r}) -> {hit.display_name if hit else 'NO MATCH (agent should ask)'}")


if __name__ == "__main__":  # pragma: no cover
    _cli()
