"""Quintin, Sep 14: "what do you think about my walls and floors?"

He named no room, and surfaces only reached the model for the active room,
so the agent said it could not see them. With no room in focus the agent now
gets floors and walls for every room the appearance pass covered, and still
admits the gap for rooms it did not.
"""

import app.flow_runtime as flow_runtime
from app.flow import home_registry
from app.flow.state import FlowState
from app.home_index import HomeIndex, Room


def _room(key, name, area):
    return Room(
        key=key, index=int(key.split("-")[-1]), storey=0, plan_label=name,
        area_sqft=area, floor_y=0.0, display_name=name, role=name, confident=True,
    )


def _home():
    return HomeIndex(
        [_room("room-1", "living room", 322), _room("room-2", "kitchen", 180)],
        bundle_id="B", storey_count=1,
    )


def _contexts(monkeypatch, by_key):
    monkeypatch.setattr(
        home_registry, "room_context_for",
        lambda home_id, room_key: by_key.get(room_key),
    )


def _text(state, index):
    return "\n".join(flow_runtime._home_directives(state, index))


LIVING = {"surfaces": {"floor": "honey-toned hardwood", "walls": "warm taupe"}}
KITCHEN = {"surfaces": {"floor": "grey porcelain tile", "walls": "white"}}


def test_whole_home_question_gets_every_enriched_room(monkeypatch):
    _contexts(monkeypatch, {"room-1": LIVING, "room-2": KITCHEN})
    text = _text(FlowState(thread_id="t", home_id="h"), _home())
    for word in ("honey-toned hardwood", "warm taupe", "grey porcelain tile"):
        assert word in text
    assert "only floor and wall materials" in text
    assert "not listed above" not in text


def test_unenriched_home_admits_it_and_names_no_material(monkeypatch):
    _contexts(monkeypatch, {"room-1": {"surfaces": {}, "coverage": "geometry_only"}})
    text = _text(FlowState(thread_id="t", home_id="h"), _home()).lower()
    assert "shapes only" in text
    assert "never describe a material" in text
    assert "hardwood" not in text and "tile" not in text


def test_partly_enriched_home_flags_the_rooms_it_cannot_see(monkeypatch):
    _contexts(monkeypatch, {"room-1": LIVING})
    text = _text(FlowState(thread_id="t", home_id="h"), _home())
    assert "honey-toned hardwood" in text
    assert "not listed above" in text


def test_rooms_past_the_cap_are_flagged_even_when_all_are_enriched(monkeypatch):
    """A fully enriched 10-room home lists 8; the other two must not read as
    if the agent had described the whole home."""
    rooms = [_room(f"room-{i}", f"room {i}", 400 - i) for i in range(1, 11)]
    _contexts(monkeypatch, {r.key: LIVING for r in rooms})
    text = _text(FlowState(thread_id="t", home_id="h"), HomeIndex(rooms, bundle_id="B", storey_count=1))
    # "room 9:" is also in the plain room list, so match the materials line.
    assert "room 8: floor" in text and "room 9: floor" not in text
    assert "not listed above" in text


def test_active_room_keeps_its_own_directive_only(monkeypatch):
    _contexts(monkeypatch, {"room-1": LIVING, "room-2": KITCHEN})
    state = FlowState(thread_id="t", home_id="h", active_room_key="room-1")
    text = _text(state, _home())
    assert "honey-toned hardwood" in text
    assert "grey porcelain tile" not in text, "other rooms stay out when one is in focus"
    assert "across the home" not in text


def test_overview_counts_only_rooms_the_pass_actually_saw():
    index = _home()
    index.rooms[0].appearance = {"coverage": "complete", "surfaces": LIVING["surfaces"]}
    index.rooms[1].appearance = {"coverage": "geometry_only", "surfaces": {}}
    overview = index.overview()
    assert overview["enrichedRooms"] == 1
    assert [r["hasMaterials"] for r in overview["rooms"]] == [True, False]
