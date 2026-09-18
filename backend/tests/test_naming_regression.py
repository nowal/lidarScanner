"""Naming regression across two house shapes.

Every naming rule was tuned on the first real house we received (Sep 3:
19 areas, two storeys, several bathrooms). The second (Sep 4: 6 areas, one
storey, no bathroom and no bedroom in the scan) proved the rules
generalise -- and that is exactly the property that rots silently. A
change that improves one layout can quietly wreck the other.

So both shapes are pinned here as synthetic fixtures. They reproduce the
characteristics that matter -- the ambiguities, the absences, the
near-ties -- without putting a map of a client's home in the repository
(SOW section 12).

Real-house drift is caught separately by an internal snapshot script,
which diffs the resolved names of the actual exports on the machine that
has them.
"""

import json

import pytest

from app.home_index import load_bundle

IDENTITY = [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1]


def _build(tmp_path, name: str, rooms: list[dict]):
    """rooms: {storey, label, w, d, objects, windows}"""
    base = tmp_path / name
    (base / "rooms").mkdir(parents=True)
    (base / "meta.json").write_text(json.dumps({"id": name}), encoding="utf-8")
    for i, spec in enumerate(rooms, start=1):
        room_dir = base / "rooms" / f"room-{i}"
        room_dir.mkdir(parents=True)
        storey = spec.get("storey", 1)
        (room_dir / "floor.json").write_text(
            json.dumps({"floor": storey, "floorY": 0.0 if storey == 1 else 3.0}), encoding="utf-8"
        )
        w, d = spec["w"], spec["d"]
        transform = list(IDENTITY)
        transform[12] = i * 25          # keep footprints far apart
        (room_dir / "room.json").write_text(json.dumps({
            "sections": [{"label": spec["label"]}] if spec.get("label") else [],
            "floors": [{
                "polygonCorners": [[-w / 2, 0, -d / 2], [w / 2, 0, -d / 2],
                                   [w / 2, 0, d / 2], [-w / 2, 0, d / 2]],
                "transform": transform,
            }],
            "objects": [{"category": {o: {}}} for o in spec.get("objects", [])],
            "windows": [{"category": "window"} for _ in range(spec.get("windows", 0))],
            "doors": [], "walls": [], "openings": [],
        }), encoding="utf-8")
    return load_bundle(base)


# --------------------------------------------------------------------------
# Shape A — the two-storey house: several bathrooms, near-tied bedrooms,
# more than one sofa room, a laundry, and areas with nothing in them.
# --------------------------------------------------------------------------
@pytest.fixture
def two_storey(tmp_path):
    return _build(tmp_path, "two_storey", [
        {"storey": 3, "label": "bathroom", "w": 3.5, "d": 3.5,
         "objects": ["bathtub", "sink", "sink", "toilet", "storage"], "windows": 2},
        {"storey": 3, "label": "bathroom", "w": 2.6, "d": 3.0, "objects": ["toilet", "storage"]},
        {"storey": 1, "label": "bathroom", "w": 3.4, "d": 3.7,
         "objects": ["sink", "toilet", "storage"]},
        {"storey": 3, "label": "bedroom", "w": 5.0, "d": 4.4,
         "objects": ["bed", "storage", "table"], "windows": 3},
        {"storey": 3, "label": "bedroom", "w": 4.2, "d": 3.4, "objects": ["bed", "chair"]},
        {"storey": 1, "label": "bedroom", "w": 4.6, "d": 4.4, "objects": ["bed", "storage"]},
        {"storey": 3, "label": "kitchen", "w": 6.5, "d": 6.4,
         "objects": ["sink", "storage", "table", "chair"], "windows": 1},
        {"storey": 3, "label": "livingRoom", "w": 6.4, "d": 5.8,
         "objects": ["sofa", "sofa", "television", "fireplace"], "windows": 2},
        {"storey": 1, "label": "", "w": 6.0, "d": 5.7, "objects": ["sofa", "sofa", "chair"]},
        {"storey": 3, "label": "", "w": 4.5, "d": 3.4,
         "objects": ["washerDryer", "washerDryer", "storage"], "windows": 1},
        {"storey": 1, "label": "", "w": 3.0, "d": 2.9, "objects": ["storage"]},
        {"storey": 3, "label": "", "w": 1.9, "d": 1.7, "objects": ["stairs"]},
    ])


def test_primary_bathroom_is_the_one_with_the_tub(two_storey):
    room = two_storey.resolve("master bathroom")
    assert room.display_name == "primary bathroom"
    assert "bathtub" in room.name_basis and room.confident


def test_powder_room_and_plain_bathroom_are_distinguished(two_storey):
    names = {r.display_name for r in two_storey.rooms if r.role == "bathroom"}
    assert "primary bathroom" in names
    assert "powder room" in names          # toilet, no sink, no bath
    assert any(n.startswith("bathroom") for n in names)


def test_near_tied_bedrooms_leave_primary_uncertain(two_storey):
    """5.0x4.4 vs 4.6x4.4 is too close to assert."""
    primary = two_storey.resolve("primary bedroom")
    assert primary.display_name == "primary bedroom"
    assert not primary.confident


def test_only_one_living_room(two_storey):
    living = [r.display_name for r in two_storey.rooms if r.role == "living"]
    assert living.count("living room") == 1
    assert any("sitting area" in n for n in living)


def test_storey_words_appear_only_where_they_disambiguate(two_storey):
    text = two_storey.as_text()
    assert "downstairs" in text or "upstairs" in text
    # The kitchen is unique, so it needs no storey word.
    assert two_storey.resolve("kitchen").display_name == "kitchen"


def test_storey_word_beats_size_when_asked(two_storey):
    assert two_storey.resolve("the downstairs bathroom").storey == 1
    assert two_storey.resolve("upstairs bathroom").storey == 3


def test_empty_areas_are_never_invented(two_storey):
    unnamed = [r for r in two_storey.rooms if r.display_name.startswith("unnamed")]
    assert unnamed, "a storage-only mid-size area has no honest name"
    assert all(not r.confident for r in unnamed)


# --------------------------------------------------------------------------
# Shape B — the single-storey house with NO bathroom and NO bedroom in the
# scan, and one small area nothing can identify. Most of the naming logic
# keys off bathrooms and bedrooms, so their absence is the sharpest test.
# --------------------------------------------------------------------------
@pytest.fixture
def single_storey(tmp_path):
    return _build(tmp_path, "single_storey", [
        {"label": "", "w": 6.0, "d": 3.5, "objects": ["sofa", "sofa", "chair", "table"]},
        {"label": "", "w": 3.5, "d": 2.3, "objects": ["washerDryer", "storage"]},
        {"label": "kitchen", "w": 7.0, "d": 5.9,
         "objects": ["sink", "storage", "storage", "table"], "windows": 2},
        {"label": "", "w": 5.4, "d": 4.1, "objects": ["table", "chair", "chair", "chair"]},
        {"label": "", "w": 2.4, "d": 2.0, "objects": []},
        {"label": "livingRoom", "w": 6.2, "d": 4.8,
         "objects": ["sofa", "sofa", "television"], "windows": 2},
    ])


def test_a_house_with_no_bathroom_says_so(single_storey):
    """The failure mode that would matter most: inventing a bathroom
    because almost every naming rule expects one."""
    assert single_storey.resolve("master bathroom") is None
    assert single_storey.resolve("the bathroom") is None
    assert single_storey.resolve("my bedroom") is None


def test_the_real_rooms_still_resolve(single_storey):
    assert single_storey.resolve("kitchen").role == "kitchen"
    assert single_storey.resolve("living room").role == "living"
    assert single_storey.resolve("where the washer is").role == "laundry"


def test_dining_is_separated_from_living(single_storey):
    """Table and chairs with no sofa is a dining room, not another lounge."""
    dining = single_storey.resolve("dining room")
    assert dining is not None and dining.role == "dining"


def test_no_storey_words_on_a_single_storey_home(single_storey):
    for room in single_storey.rooms:
        assert "upstairs" not in room.display_name
        assert "downstairs" not in room.display_name


def test_the_unidentifiable_area_is_findable_and_nameable(single_storey):
    """The 53 sq ft room in the real house: no fixtures, no label, and only
    the homeowner knows it is a pantry."""
    small = single_storey.small_unnamed_room("what about that little room?")
    assert small is not None and not small.confident
    single_storey.rename_room(small.key, "pantry")
    assert single_storey.resolve("the pantry").key == small.key
    assert single_storey.resolve("pantry").named_by_homeowner


def test_context_text_stays_small_for_both_shapes(two_storey, single_storey):
    """Whole-home context rides on every turn; it has to stay cheap."""
    for index in (two_storey, single_storey):
        assert len(index.as_text()) < 2000
