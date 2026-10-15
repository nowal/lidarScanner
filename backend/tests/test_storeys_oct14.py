"""Quintin, Oct 14: a two-storey walk, and an agent that could not find the
upstairs or the foyer.

His export had three areas. The app had done its part: the upstairs was its
own area, on floor 2. The server named every area after one fixture, so the
whole ground floor (kitchen, dining, half bath, laundry, stairs) became
"laundry room" and the entire upstairs became "bathroom" because it had a
toilet in it. Nothing in the agent's room list said which floor anything was
on, so it told him four times that it had no upstairs, put it down to "a
syncing gap", and argued that his stairs were in the laundry room.

The fixture mirrors that export's structure (floors, section labels,
fixtures), not its data.
"""

import json

import pytest

import app.flow_runtime as flow_runtime
from app.config import settings
from app.flow.home_registry import _cache, ingest_bundle
from app.flow.state import FlowState
from app.home_ai import HomeAIChatRequest
from app.home_index import HomeIndex, load_bundle

IDENTITY = [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1]


def _floor(width: float, depth: float, cx: float = 0.0) -> dict:
    hw, hd = width / 2, depth / 2
    t = list(IDENTITY)
    t[12] = cx
    return {
        "polygonCorners": [[-hw, 0, -hd], [hw, 0, -hd], [hw, 0, hd], [-hw, 0, hd]],
        "transform": t,
    }


def _area(base, index, *, storey, labels, objects, w, d, cx):
    room_dir = base / "rooms" / f"room-{index}"
    room_dir.mkdir(parents=True, exist_ok=True)
    (room_dir / "floor.json").write_text(json.dumps({
        "floor": storey, "floorY": -1.6 if storey == 1 else 1.5,
        "source": "manual" if storey == 1 else "detected",
    }), encoding="utf-8")
    (room_dir / "room.json").write_text(json.dumps({
        "sections": [{"label": label} for label in labels],
        "floors": [_floor(w, d, cx)],
        "objects": [{"category": {o: {}}} for o in objects],
        "windows": [], "doors": [], "walls": [], "openings": [],
    }), encoding="utf-8")


def _two_storey(base):
    (base / "rooms").mkdir(parents=True)
    (base / "meta.json").write_text(json.dumps({"id": "OCT14"}), encoding="utf-8")
    _area(base, 1, storey=1, labels=["livingRoom"], w=7.6, d=5.6, cx=0,
          objects=["sofa"] * 4 + ["television", "fireplace", "table", "chair", "chair"])
    _area(base, 2, storey=1, labels=["kitchen", "unidentified", "diningRoom", "unidentified", "bathroom"],
          w=10.0, d=8.3, cx=12,
          objects=["stove", "oven", "refrigerator", "dishwasher", "washerDryer", "washerDryer",
                   "toilet", "sink", "sink", "stairs", "stairs", "table", "sofa", "television"])
    _area(base, 3, storey=2, labels=["livingRoom", "bathroom", "unidentified"], w=8.0, d=6.5, cx=5,
          objects=["sofa", "sofa", "television", "toilet", "sink", "sink", "stairs"])


@pytest.fixture
def house(tmp_path):
    bundle = tmp_path / "bundle"
    _two_storey(bundle)
    return load_bundle(bundle)


# ------------------------------------------------------------------ naming
def test_an_undivided_area_is_named_for_what_it_takes_in(house):
    ground = house.by_key("room-2")
    assert ground.role == "open_plan"
    assert ground.display_name == "kitchen, dining, bathroom and laundry area"
    assert ground.contains == ["kitchen", "dining", "bathroom", "laundry", "stairs"]
    assert ground.confident


def test_the_only_area_on_a_floor_is_named_for_the_floor(house):
    upstairs = house.by_key("room-3")
    assert upstairs.display_name == "upstairs"
    assert upstairs.storey_word == "upstairs"
    assert "bathroom" in upstairs.contains and "living" in upstairs.contains


def test_a_plain_room_keeps_its_name(house):
    # A table and two chairs in a living room do not make it a dining room.
    assert house.by_key("room-1").display_name == "living room"
    assert house.by_key("room-1").role == "living"


def test_one_fixture_no_longer_names_the_whole_area(house):
    names = {r.display_name for r in house.rooms}
    assert "laundry room" not in names
    assert "bathroom" not in names


# ------------------------------------------------------------- resolution
@pytest.mark.parametrize("said", [
    "upstairs",
    "the second floor",
    "what about the 2nd floor",
    "Can you see anything off the upstairs and the stair well?",
    "the upstairs bathroom",
])
def test_the_upstairs_is_found(house, said):
    assert house.resolve(said).key == "room-3"
    room, missing = house.mentioned_room(said)
    assert room is not None and room.key == "room-3" and missing is None


@pytest.mark.parametrize("said", ["the laundry room", "the kitchen", "the stairs", "the stairwell", "main floor"])
def test_what_an_open_area_holds_finds_it(house, said):
    assert house.resolve(said).key == "room-2"


def test_stairs_are_not_the_upstairs(house):
    # "stairs" sits inside "upstairs"; the old substring match took it there.
    assert house.resolve("stairs").key != "room-3"


def test_a_real_room_beats_an_area_that_merely_holds_one(tmp_path):
    bundle = tmp_path / "b"
    (bundle / "rooms").mkdir(parents=True)
    (bundle / "meta.json").write_text("{}", encoding="utf-8")
    _area(bundle, 1, storey=1, labels=["kitchen", "bathroom"], w=8, d=6, cx=0,
          objects=["stove", "toilet"])
    _area(bundle, 2, storey=1, labels=["bathroom"], w=2, d=2, cx=10, objects=["toilet", "sink"])
    index = load_bundle(bundle)
    assert index.resolve("the bathroom").key == "room-2"
    assert index.by_key("room-1").display_name == "kitchen and bathroom area"


# --------------------------------------------------------- the agent's list
def test_the_room_list_is_grouped_by_floor(house):
    text = house.as_text()
    lines = text.splitlines()
    assert lines[0] == "Downstairs (floor 1 of 2):"
    assert "Upstairs (floor 2 of 2):" in lines
    assert lines.index("Upstairs (floor 2 of 2):") > lines.index(
        next(l for l in lines if l.startswith("- living room"))
    )
    upstairs = next(l for l in lines if l.startswith("- upstairs:"))
    assert "did not divide into rooms" in upstairs
    assert "a living area, a bathroom and the stairs" in upstairs


def test_a_single_storey_list_has_no_floor_headings(tmp_path):
    bundle = tmp_path / "b"
    (bundle / "rooms").mkdir(parents=True)
    (bundle / "meta.json").write_text("{}", encoding="utf-8")
    _area(bundle, 1, storey=1, labels=["kitchen", "diningRoom"], w=6, d=5, cx=0, objects=["stove"])
    text = load_bundle(bundle).as_text()
    assert not text.splitlines()[0].endswith(":")
    assert text.startswith("- kitchen and dining area: one open area")


# ------------------------------------------------- indexes saved before Oct 14
def test_a_saved_index_is_renamed_on_load(house):
    saved = house.to_json()
    # What the server wrote for his home on Oct 14.
    for room, (name, role) in zip(saved["rooms"], [("living room", "living"),
                                                   ("laundry room", "laundry"),
                                                   ("bathroom", "bathroom")]):
        room.update(name=name, role=role, sections=[], contains=[])
    saved["rooms"][2]["storeyWord"] = "upstairs"
    reloaded = HomeIndex.from_json(saved)
    assert reloaded.by_key("room-3").display_name == "upstairs"
    assert reloaded.by_key("room-2").display_name.endswith("laundry area")
    assert reloaded.by_key("room-1").display_name == "living room"
    assert reloaded.resolve("the second floor").key == "room-3"


def test_a_name_the_homeowner_gave_survives_the_upgrade(house):
    saved = house.to_json()
    saved["rooms"][1].update(name="main floor", role="laundry", namedByHomeowner=True,
                             sections=[], contains=[])
    reloaded = HomeIndex.from_json(saved)
    assert reloaded.by_key("room-2").display_name == "main floor"


# ------------------------------------------------------------------- flow
@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "storage_dir", str(tmp_path / "storage"))
    _cache.clear()
    bundle = tmp_path / "bundle"
    _two_storey(bundle)
    index = ingest_bundle(bundle, "home-oct14")
    yield index
    _cache.clear()


def _turn(state, message):
    return flow_runtime._reconcile_home(
        state, HomeAIChatRequest(threadId="t-oct14", message=message, homeId="home-oct14")
    )


def test_asking_about_upstairs_puts_it_in_focus(home):
    state = FlowState(thread_id="t-oct14")
    _turn(state, "Can you see anything off the upstairs and the stair well?")
    assert state.active_room_key == "room-3"
    assert state.unresolved_room_phrase is None


def test_the_foyer_is_part_of_an_open_area_not_missing(home):
    state = FlowState(thread_id="t-oct14")
    index = _turn(state, "Can you separate the foyer out from the laundry room?")
    # "laundry room" resolves; ask about the foyer alone.
    index = _turn(state, "Is the foyer in there?")
    assert state.part_room_phrase == "foyer"
    assert state.unresolved_room_phrase is None
    text = "\n".join(flow_runtime._home_directives(state, index))
    assert "They mentioned the foyer" in text
    assert "part of one of the open areas above" in text
    assert "which is NOT in the scan" not in text
    # The rename path never fires: the foyer is part of the area, not all of it.
    assert index.by_key("room-2").display_name.endswith("laundry area")


def test_directives_carry_floors_and_no_syncing_excuse(home):
    state = FlowState(thread_id="t-oct14")
    index = _turn(state, "hi")
    text = "\n".join(flow_runtime._home_directives(state, index))
    assert "Upstairs (floor 2 of 2):" in text
    assert "- FLOORS:" in text
    assert "NEVER put a missing space down to syncing" in text
    assert "accept it at once" in text


def test_a_never_scanned_space_is_still_missing(home):
    state = FlowState(thread_id="t-oct14")
    index = _turn(state, "can we do the garage?")
    assert state.part_room_phrase is None
    assert state.unresolved_room_phrase == "garage"
    assert "NOT in the scan" in "\n".join(flow_runtime._home_directives(state, index))
