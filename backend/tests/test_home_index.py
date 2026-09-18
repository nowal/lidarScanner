"""Whole-home room resolution.

Fixtures here are synthetic on purpose: a real export is a map of somebody's
house, and that does not belong in the repository (SOW section 12). The
shapes mirror the real TakeShape export exactly -- column-major transforms,
RoomPlan's ``{"sink": {}}`` category form, per-area rebuild manifests -- so
these tests exercise the same paths the real bundles do.
"""

import json
from pathlib import Path

import pytest

from app.home_index import (
    _point_in_polygon,
    _polygon_area_sqft,
    _polygon_world,
    load_bundle,
)

IDENTITY = [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1]


def _floor_surface(width: float, depth: float, cx: float = 0.0, cz: float = 0.0) -> dict:
    """A rectangular floor, corners in local space, placed by transform."""
    hw, hd = width / 2, depth / 2
    transform = list(IDENTITY)
    transform[12], transform[13], transform[14] = cx, 0.0, cz
    return {
        "polygonCorners": [[-hw, 0, -hd], [hw, 0, -hd], [hw, 0, hd], [-hw, 0, hd]],
        "transform": transform,
        "dimensions": [width, depth, 0],
    }


def _camera(x: float, y: float, z: float) -> list[float]:
    t = list(IDENTITY)
    t[12], t[13], t[14] = x, y, z
    return t


def _write_room(
    base: Path,
    index: int,
    *,
    storey: int = 1,
    label: str = "",
    width: float = 3.0,
    depth: float = 3.0,
    cx: float = 0.0,
    cz: float = 0.0,
    objects: list[str] | None = None,
    windows: int = 0,
    frames: list[tuple[str, tuple[float, float, float]]] | None = None,
    structure: bool = True,
) -> None:
    room_dir = base / "rooms" / f"room-{index}"
    (room_dir / "rebuild").mkdir(parents=True, exist_ok=True)
    (room_dir / "floor.json").write_text(
        json.dumps({"floor": storey, "floorY": 0.0 if storey == 1 else 3.0}), encoding="utf-8"
    )
    if structure:
        (room_dir / "room.json").write_text(json.dumps({
            "sections": [{"label": label, "story": storey}] if label else [],
            "floors": [_floor_surface(width, depth, cx, cz)],
            # The real export uses the dict form of a category.
            "objects": [{"category": {name: {}}} for name in (objects or [])],
            "windows": [{"category": "window"} for _ in range(windows)],
            "doors": [], "walls": [], "openings": [],
        }), encoding="utf-8")
    if frames:
        (room_dir / "rebuild" / "manifest.json").write_text(json.dumps({
            "frames": [{"id": fid, "cameraTransform": _camera(*pos)} for fid, pos in frames]
        }), encoding="utf-8")


@pytest.fixture
def house(tmp_path) -> Path:
    """A two-storey house with the ambiguities that matter: two bathrooms
    upstairs (one with a tub, one toilet-only), bathrooms on both storeys,
    two similar-sized bedrooms, and two sofa rooms."""
    base = tmp_path / "bundle"
    (base / "rooms").mkdir(parents=True)
    (base / "meta.json").write_text(json.dumps({"id": "TEST-HOME"}), encoding="utf-8")

    # --- downstairs ---
    _write_room(base, 1, storey=1, label="bathroom", width=2.0, depth=2.0, cx=0, cz=0,
                objects=["toilet", "sink"], frames=[("d-bath", (0, 1.2, 0))])
    _write_room(base, 2, storey=1, label="livingRoom", width=6.0, depth=5.0, cx=20, cz=0,
                objects=["sofa", "sofa", "television"], windows=2,
                frames=[("d-living", (20, 1.2, 0))])
    _write_room(base, 3, storey=1, width=4.0, depth=4.0, cx=40, cz=0,
                objects=["sofa", "sofa"], frames=[("d-den", (40, 1.2, 0))])
    # --- upstairs ---
    _write_room(base, 4, storey=2, label="bathroom", width=3.5, depth=3.5, cx=0, cz=0,
                objects=["bathtub", "sink", "sink", "toilet"], windows=1,
                frames=[("u-primary-bath", (0, 4.2, 0))])
    _write_room(base, 5, storey=2, label="bathroom", width=1.8, depth=1.8, cx=10, cz=0,
                objects=["toilet"], frames=[("u-powder", (10, 4.2, 0))])
    _write_room(base, 6, storey=2, label="bedroom", width=5.0, depth=4.4, cx=20, cz=0,
                objects=["bed", "storage"], windows=3,
                frames=[("u-bed-big", (20, 4.2, 0))])
    _write_room(base, 7, storey=2, label="bedroom", width=4.6, depth=4.4, cx=30, cz=0,
                objects=["bed"], frames=[("u-bed-2", (30, 4.2, 0))])
    _write_room(base, 8, storey=2, width=3.0, depth=3.0, cx=40, cz=0,
                objects=["washerDryer"], frames=[("u-laundry", (40, 4.2, 0))])
    _write_room(base, 9, storey=2, width=2.5, depth=2.5, cx=50, cz=0,
                objects=[], frames=[("u-mystery", (50, 4.2, 0))])
    return base


# ------------------------------------------------------------------ geometry
def test_polygon_area_matches_known_rectangle():
    surface = _floor_surface(3.0, 4.0)          # 12 m2
    area = _polygon_area_sqft(_polygon_world(surface))
    assert area == pytest.approx(12 * 10.7639, rel=0.01)


def test_polygon_respects_the_transform_translation():
    poly = _polygon_world(_floor_surface(2.0, 2.0, cx=5.0, cz=-3.0))
    xs = [p[0] for p in poly]
    zs = [p[1] for p in poly]
    assert min(xs) == pytest.approx(4.0) and max(xs) == pytest.approx(6.0)
    assert min(zs) == pytest.approx(-4.0) and max(zs) == pytest.approx(-2.0)


def test_point_in_polygon():
    square = [(0, 0), (4, 0), (4, 4), (0, 4)]
    assert _point_in_polygon(2, 2, square)
    assert not _point_in_polygon(5, 2, square)
    assert not _point_in_polygon(-1, -1, square)


# -------------------------------------------------------------------- naming
def test_primary_bathroom_is_chosen_by_fixtures_not_size(house):
    index = load_bundle(house)
    primary = index.resolve("primary bathroom")
    assert primary is not None
    assert primary.key == "room-4"
    assert "bathtub" in primary.name_basis and "2 sinks" in primary.name_basis
    assert primary.confident


def test_powder_room_is_distinguished_from_a_full_bath(house):
    index = load_bundle(house)
    powder = index.resolve("powder room")
    assert powder is not None and powder.key == "room-5"
    assert powder.confident


def test_similar_sized_bedrooms_make_primary_a_hedge(house):
    """4.6x4.4 vs 5.0x4.4 is not a confident 'primary' -- the agent should
    ask rather than assert."""
    index = load_bundle(house)
    primary = index.resolve("primary bedroom")
    assert primary is not None and primary.key == "room-6"
    assert not primary.confident


def test_one_living_room_the_rest_are_sitting_areas(house):
    index = load_bundle(house)
    names = sorted(r.display_name for r in index.rooms if r.role == "living")
    assert names == ["living room", "sitting area"]


def test_laundry_named_from_the_appliance(house):
    index = load_bundle(house)
    laundry = index.resolve("laundry room")
    assert laundry is not None and laundry.key == "room-8" and laundry.confident


def test_a_room_with_no_evidence_is_never_invented(house):
    index = load_bundle(house)
    mystery = index.by_key("room-9")
    assert mystery.display_name.startswith("unnamed area")
    assert not mystery.confident
    assert "needs the homeowner" in mystery.name_basis


# ------------------------------------------------------------------ resolving
@pytest.mark.parametrize("phrase", [
    "master bathroom", "the master bath", "let's redo the master bathroom",
    "ensuite", "our en suite",
])
def test_master_bathroom_synonyms(house, phrase):
    assert load_bundle(house).resolve(phrase).key == "room-4"


def test_storey_word_wins_over_size(house):
    """'the downstairs bathroom' must not land on the bigger upstairs one."""
    index = load_bundle(house)
    assert index.resolve("the downstairs bathroom").key == "room-1"
    assert index.resolve("upstairs bathroom").key == "room-4"


def test_unknown_room_returns_none_rather_than_guessing(house):
    index = load_bundle(house)
    assert index.resolve("the garage") is None
    assert index.resolve("") is None


def test_resolves_by_appliance_phrasing(house):
    assert load_bundle(house).resolve("where the washer is").key == "room-8"


# -------------------------------------------------------------------- frames
def test_frames_stay_with_the_room_that_captured_them(house):
    index = load_bundle(house)
    assert index.by_key("room-4").frame_ids == ["u-primary-bath"]
    assert index.by_key("room-1").frame_ids == ["d-bath"]


def test_a_frame_shot_inside_another_room_is_reassigned(tmp_path):
    """A walk-through crosses rooms: a frame captured while standing in the
    kitchen belongs to the kitchen, whichever area recorded it."""
    base = tmp_path / "bundle"
    (base / "rooms").mkdir(parents=True)
    (base / "meta.json").write_text("{}", encoding="utf-8")
    _write_room(base, 1, label="kitchen", width=4.0, depth=4.0, cx=0, cz=0,
                objects=["sink", "stove"])
    # Area 2 is far away but its walk recorded a frame standing in area 1.
    _write_room(base, 2, label="bedroom", width=4.0, depth=4.0, cx=30, cz=0,
                objects=["bed"], frames=[("stray", (0, 1.2, 0)), ("home", (30, 1.2, 0))])
    index = load_bundle(base)
    assert index.resolve("kitchen").frame_ids == ["stray"]
    assert index.resolve("bedroom").frame_ids == ["home"]


def test_storey_band_keeps_frames_off_the_floor_below(tmp_path):
    base = tmp_path / "bundle"
    (base / "rooms").mkdir(parents=True)
    (base / "meta.json").write_text("{}", encoding="utf-8")
    # Same footprint, one directly above the other.
    _write_room(base, 1, storey=1, label="kitchen", width=4.0, depth=4.0, objects=["sink"])
    _write_room(base, 2, storey=2, label="bedroom", width=4.0, depth=4.0, objects=["bed"],
                frames=[("upstairs-frame", (0, 4.2, 0))])
    index = load_bundle(base)
    assert index.resolve("bedroom").frame_ids == ["upstairs-frame"]
    assert index.resolve("kitchen").frame_ids == []


# ------------------------------------------------------------------- degenerate
def test_a_walk_with_no_roomplan_structure_does_not_crash(tmp_path):
    """The first real single-room export had frames and nothing else."""
    base = tmp_path / "bundle"
    (base / "rooms").mkdir(parents=True)
    (base / "meta.json").write_text(json.dumps({"id": "RAW"}), encoding="utf-8")
    _write_room(base, 1, structure=False, frames=[("f1", (0, 1.2, 0))])
    index = load_bundle(base)
    assert len(index.rooms) == 1
    assert index.rooms[0].display_name.startswith("unnamed area")
    assert index.rooms[0].frame_ids == ["f1"]
    assert index.resolve("kitchen") is None


def test_empty_bundle_is_survivable(tmp_path):
    base = tmp_path / "bundle"
    base.mkdir()
    index = load_bundle(base)
    assert index.rooms == [] and index.resolve("kitchen") is None
    assert index.overview()["roomCount"] == 0


# ---------------------------------------------------------------- agent output
def test_context_text_is_compact_and_marks_uncertainty(house):
    text = load_bundle(house).as_text()
    assert "primary bathroom" in text
    assert "(name uncertain)" in text          # hedged names are visible
    assert "sq ft" in text
    # A whole house must not cost a fortune in context.
    assert len(text) < 2000
    assert "polygon" not in text and "transform" not in text


def test_overview_counts(house):
    ov = load_bundle(house).overview()
    assert ov["roomCount"] == 9
    assert ov["storeys"] == 2
    assert ov["photoCount"] == 9
    assert ov["totalAreaSqFt"] > 0
    assert ov["namedConfidently"] >= 5
