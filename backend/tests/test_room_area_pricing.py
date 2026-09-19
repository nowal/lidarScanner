"""Price guidance is for the room they are standing in, not the whole house.

Found on the deployed demo the day the wide-band quote shipped (Sep 12).
Guidance took its area from the context packet's whole-home total and never
looked at the room in focus, so on a whole-home walk every single-room ask
got the price of the entire house: an 85 sq ft laundry room and a 322 sq ft
living room both came back $2,700-$4,900, the band for all 1,373 sq ft.

Every home on the demo is a whole-home scan, so this was the normal case
rather than an edge one.
"""

import asyncio

import pytest

import app.flow_runtime as flow_runtime
from app.config import settings
from app.flow.state import FlowState, Slots
from app.home_ai import HomeAIChatRequest
from app.home_index import HomeIndex, Room


# Synthetic house: never a client's real scan (SOW section 12). Two rooms an
# order of magnitude apart, so a whole-home band cannot masquerade as either.
def _house() -> HomeIndex:
    def room(key, name, floor_sqft):
        r = Room(
            key=key, index=int(key.split("-")[1]), storey=0, plan_label=name,
            area_sqft=floor_sqft, floor_y=0.0, display_name=name,
            role=name, confident=True,
        )
        r.measurements = {"floor_sqft": floor_sqft}
        return r

    return HomeIndex(
        [room("room-1", "laundry room", 80.0), room("room-2", "kitchen", 450.0)],
        bundle_id="synthetic-house",
    )


WHOLE_HOME = {
    "contextVersion": "home_ai_context_v1", "scanId": "synthetic-house",
    "roomCount": 2, "rooms": [],
    # 130 m2 ~ 1400 sq ft, far larger than either room
    "totals": {"floorAreaSquareMeters": 130, "roomCount": 2},
    "floorplanSummary": "2 spaces",
    "meshSummary": {"photorealStatus": "processing", "keyframeCount": 4},
    "selectedKeyframes": [], "notes": [],
}


@pytest.fixture(autouse=True)
def _guidance_on(monkeypatch):
    monkeypatch.setattr(settings, "agent_price_guidance_enabled", True)
    # Static table only: a web call would make these assertions non-deterministic
    # and would spend on the provider key from a test run.
    monkeypatch.setattr(settings, "price_research_enabled", False)


def _guidance(active_room_key, index=None, context=None):
    state = FlowState(thread_id="t", slots=Slots(project_type="Painting", zip="37203"))
    state.home_id = "synthetic-house"
    state.active_room_key = active_room_key
    request = HomeAIChatRequest(
        threadId="t", message="roughly what does that cost?",
        homeContext=context if context is not None else WHOLE_HOME,
    )
    return asyncio.run(flow_runtime._maybe_price_guidance(state, request, index))[0]


def test_the_room_in_focus_sets_the_price_not_the_house():
    house = _house()
    laundry = _guidance("room-1", house)
    kitchen = _guidance("room-2", house)
    whole = _guidance(None, house)

    assert laundry is not None and kitchen is not None and whole is not None
    # The failure this guards: all three identical.
    assert (laundry.lowUsd, laundry.highUsd) != (kitchen.lowUsd, kitchen.highUsd)
    assert (laundry.lowUsd, laundry.highUsd) != (whole.lowUsd, whole.highUsd)
    # A 450 sq ft room costs more to paint than an 80 sq ft one.
    assert kitchen.highUsd > laundry.highUsd
    # And neither room is priced as if it were the whole 1,400 sq ft house.
    assert kitchen.highUsd < whole.highUsd


def test_the_basis_names_the_room_it_measured():
    """On a whole-home scan an unqualified "320 sq ft" reads as a mistake."""
    kitchen = _guidance("room-2", _house())
    assert "kitchen" in kitchen.basis
    assert "450 sq ft" in kitchen.basis or "450 sq ft" in kitchen.basis.replace(",", "")


def test_no_room_in_focus_still_uses_the_whole_scan():
    """The previous behaviour is right when they have not named a room."""
    whole = _guidance(None, _house())
    assert whole is not None
    assert "1400 sq ft" in whole.basis.replace(",", "")
    assert "(" not in whole.basis.split(",")[0]  # no room label


def test_a_home_with_no_index_is_unaffected():
    """Single-room captures and pre-scan asks have no index at all."""
    guidance = _guidance(None, None)
    assert guidance is not None
    assert guidance.lowUsd > 0


def test_an_active_room_the_index_does_not_have_falls_back():
    """State can name a room the current index lost (a re-ingest, a switch)."""
    guidance = _guidance("room-99", _house())
    assert guidance is not None
    assert "1400 sq ft" in guidance.basis.replace(",", "")


def test_moving_rooms_reprices_rather_than_reusing_the_pinned_band():
    """The band is pinned so it cannot drift mid-conversation, but walking
    into a different room is a real change and has to re-price."""
    house = _house()
    state = FlowState(thread_id="t", slots=Slots(project_type="Painting", zip="37203"))
    state.home_id = "synthetic-house"
    request = HomeAIChatRequest(
        threadId="t", message="roughly what does that cost?", homeContext=WHOLE_HOME
    )

    state.active_room_key = "room-1"
    first = asyncio.run(flow_runtime._maybe_price_guidance(state, request, house))[0]
    assert state.price_guidance_snapshot["areaLabel"] == "laundry room"

    state.active_room_key = "room-2"
    second = asyncio.run(flow_runtime._maybe_price_guidance(state, request, house))[0]
    assert second.highUsd != first.highUsd
    assert "kitchen" in second.basis


def test_the_pin_still_holds_when_nothing_moved():
    house = _house()
    state = FlowState(thread_id="t", slots=Slots(project_type="Painting", zip="37203"))
    state.home_id = "synthetic-house"
    state.active_room_key = "room-2"
    request = HomeAIChatRequest(
        threadId="t", message="roughly what does that cost?", homeContext=WHOLE_HOME
    )
    first = asyncio.run(flow_runtime._maybe_price_guidance(state, request, house))[0]
    again = asyncio.run(flow_runtime._maybe_price_guidance(state, request, house))[0]
    assert (first.lowUsd, first.highUsd) == (again.lowUsd, again.highUsd)


# ------------------------------------------------ the helper on its own
def test_measured_floor_area_beats_the_bounding_footprint():
    """`area_sqft` is the polygon's extent; `measurements.floor_sqft` is the
    shoelace area of the real outline. Prefer the measured one."""
    house = _house()
    room = house.by_key("room-2")
    room.area_sqft = 999.0                     # bounding box, too generous
    room.measurements = {"floor_sqft": 450.0}  # the real floor
    state = FlowState(thread_id="t")
    state.active_room_key = "room-2"
    area, label = flow_runtime._active_room_area_sqft(state, house)
    assert area == 450.0
    assert label == "kitchen"


def test_a_room_with_no_measurements_uses_its_footprint():
    house = _house()
    room = house.by_key("room-1")
    room.measurements = {}
    state = FlowState(thread_id="t")
    state.active_room_key = "room-1"
    area, label = flow_runtime._active_room_area_sqft(state, house)
    assert area == 80.0
    assert label == "laundry room"


@pytest.mark.parametrize("index,key", [(None, "room-1"), (_house(), None)])
def test_no_index_or_no_room_means_no_room_area(index, key):
    state = FlowState(thread_id="t")
    state.active_room_key = key
    assert flow_runtime._active_room_area_sqft(state, index) == (None, None)
