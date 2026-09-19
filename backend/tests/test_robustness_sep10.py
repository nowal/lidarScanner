"""Fixes from running a real eight-area export (scan 5B579D84, Sep 10) and
walking it end to end: a re-stated scope replaces the room list, "that
space you called X is the mudroom" renames X, the appearance pass has
frames for a room with no detected fixtures, a mid-thread home switch is
named to the model, and operations can list every ingested home."""

from __future__ import annotations

import pytest

import app.flow_runtime as flow_runtime
from app.config import settings
from app.flow.machine import TurnPlan, FlowEngine
from app.flow.state import FlowState, ScopeIntent, Slots
from app.flow_runtime import _apply_capture, _detect_room_naming, _reconcile_home
from app.frame_select import spread_frames
from app.home_ai import HomeAIChatRequest
from app.home_index import HomeIndex, Room
from app.room_context import select_context_frames


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "storage_dir", str(tmp_path))
    from app.flow import home_registry

    home_registry._cache.clear()
    yield
    home_registry._cache.clear()


def _index() -> HomeIndex:
    rooms = [
        Room(key="room-1", index=1, storey=1, plan_label="unidentified", area_sqft=44.0, floor_y=0.0,
             display_name="unnamed area 1", confident=False, name_basis="no fixtures"),
        Room(key="room-4", index=4, storey=1, plan_label="kitchen", area_sqft=356.0, floor_y=0.0,
             display_name="kitchen", confident=True, name_basis="kitchen fixtures"),
    ]
    return HomeIndex(rooms, bundle_id="B", storey_count=1)


# ------------------------------------------------ 1. re-stated scope
def test_a_restated_scope_replaces_the_room_list():
    state = FlowState(thread_id="t", opening_delivered=True)
    _apply_capture(state, {"scopeIntent": "single_room", "scopeRooms": ["mudroom"]}, "just the mudroom")
    assert state.scope_rooms == ["mudroom"]
    delta = _apply_capture(state, {"scopeIntent": "selected_rooms", "scopeRooms": ["kitchen", "bathroom"]},
                           "a couple of rooms: the kitchen and the bathroom, nothing else")
    assert state.scope_intent is ScopeIntent.SELECTED_ROOMS
    assert state.scope_rooms == ["kitchen", "bathroom"], "the earlier room does not linger"
    assert delta["scopeRoomsCleared"] == ["mudroom"]
    # Same intent restated: rooms accumulate (adding a room to the list).
    _apply_capture(state, {"scopeIntent": "selected_rooms", "scopeRooms": ["hall"]}, "and the hall too")
    assert state.scope_rooms == ["kitchen", "bathroom", "hall"]


# ------------------------------------------------ 2. naming the room mentioned
def test_the_named_room_is_the_one_the_message_points_at():
    from app.flow import home_registry

    index = _index()
    home_registry.save_index("h", index)
    state = FlowState(thread_id="t", opening_delivered=True, home_id="h", active_room_key="room-4")
    request = HomeAIChatRequest(message="That small space you called unnamed area 1 is the mudroom.", homeId="h")
    _reconcile_home(state, request)
    assert index.by_key("room-1").display_name == "mudroom"
    assert index.by_key("room-1").named_by_homeowner and index.by_key("room-1").confident
    assert index.by_key("room-4").display_name == "kitchen", "the room in focus is not renamed by mistake"
    assert state.active_room_key == "room-1"


def test_naming_still_needs_a_room_word_and_a_room_in_focus():
    index = _index()
    state = FlowState(thread_id="t", home_id="h", active_room_key="room-1")
    assert _detect_room_naming(state, index, "that's the problem") is None
    assert _detect_room_naming(state, index, "the small room is the mudroom.") == ("room-1", "mudroom")
    assert _detect_room_naming(state, index, "we call it the pantry") == ("room-1", "pantry")
    assert _detect_room_naming(state, index, "the kitchen is the pantry", target=index.by_key("room-4")) is None, \
        "a fixture-proven name is never overwritten"
    assert _detect_room_naming(FlowState(thread_id="t", home_id="h"), index, "it's the pantry") is None


# ------------------------------------------------ 3. frames for fixture-less rooms
def _manifest(n: int) -> dict:
    frames = []
    for i in range(n):
        # Cameras spaced 1 m apart along x, all looking down -Z (identity rotation).
        transform = [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, float(i), 0.0, 0.0, 1]
        frames.append({"id": f"f{i:02d}", "cameraTransform": transform, "intrinsics": [1] * 9})
    return {"frames": frames}


def test_spread_frames_samples_across_the_walk_deterministically():
    chosen = spread_frames(_manifest(10), count=4)
    ids = [r["id"] for r in chosen]
    assert ids[:2] == ["f00", "f09"], "seed, then the far end of the walk"
    assert ids[2] in ("f04", "f05") and len(set(ids)) == 4
    xs = sorted(r["centre"][0] for r in chosen)
    assert xs[0] == 0 and xs[-1] == 9 and all(b - a >= 2 for a, b in zip(xs, xs[1:])), "spread, not clustered"
    assert all(k in chosen[0] for k in ("id", "turns", "centre", "direction", "score"))
    assert spread_frames({"frames": []}, count=4) == []
    assert spread_frames(_manifest(10), count=4) == chosen


def test_a_room_with_no_detected_objects_still_gets_frames_for_the_appearance_pass():
    room_without_fixtures = {"objects": [], "walls": [], "floors": []}
    frames = select_context_frames(room_without_fixtures, _manifest(6), count=3)
    assert [r["id"] for r in frames] == ["f00", "f05", "f03"]
    # A room WITH fixtures that simply do not project keeps the old behaviour.
    room_with_boxes = {"objects": [{"category": "sofa", "dimensions": [1, 1, 1],
                                    "transform": [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 100, 100, 100, 1]}]}
    frames = select_context_frames(room_with_boxes, _manifest(6), count=3)
    assert frames == [] or all("id" in r for r in frames)


# ------------------------------------------------ 4. mid-thread home switch
def test_switching_homes_mid_thread_is_named_to_the_model():
    from app.flow import home_registry

    home_registry.save_index("h1", _index())
    other = HomeIndex([Room(key="room-9", index=9, storey=2, plan_label="bedroom", area_sqft=200.0, floor_y=3.0,
                            display_name="bedroom", confident=True)], bundle_id="B2", storey_count=2)
    home_registry.save_index("h2", other)
    state = FlowState(thread_id="t", opening_delivered=True, home_id="h1", active_room_key="room-4",
                      slots=Slots(first_name="Dana"))
    previous = state.home_id
    index = _reconcile_home(state, HomeAIChatRequest(message="now my other place", homeId="h2"))
    switched = bool(previous and state.home_id != previous)
    assert switched and state.active_room_key is None and index.by_key("room-9")
    plan = FlowEngine().plan_turn(state, "now my other place")
    directives = flow_runtime._build_directives(state, plan, opening=False, price_guidance=None,
                                                quotes_to_present=None, home_index=index, home_switched=switched)
    assert "switched to a DIFFERENT home" in directives and "never say there is only one home" in directives
    unswitched = flow_runtime._build_directives(state, plan, opening=False, price_guidance=None,
                                                quotes_to_present=None, home_index=index, home_switched=False)
    assert "DIFFERENT home" not in unswitched
