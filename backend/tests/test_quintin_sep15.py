"""Quintin's Sep 15 thread, three items.

- "The AI told me it didn't know which rooms were beside each other."
  The walk's floor polygons share one world frame; adjacency and level
  changes are computed from them and handed to the agent.
- "I'd prefer it to be much softer than 'I can't do that'." A voice rule
  in the system prompt, and the one scripted "I can't" copy rewritten.
- "Make it sound more like the AI itself is retrieving the quotes." The
  agent is the one who brings the quotes back; a person still prices them.
"""

from __future__ import annotations

import math
from collections import Counter

import app.flow_runtime as flow_runtime
from app.flow.state import FlowState
from app.flow_quotes import EXPECTATIONS
from app.home_guide_prompt import build_home_guide_system_prompt
from app.home_index import HomeIndex, Room, polygon_gap


def _room(key: str, name: str, x0: float, z0: float, w: float, d: float, *, storey: int = 0,
          floor_y: float = 0.0, polygon: bool = True) -> Room:
    return Room(
        key=key, index=int(key.split("-")[-1]), storey=storey, plan_label=name,
        area_sqft=w * d * 10.7639, floor_y=floor_y,
        polygon=[(x0, z0), (x0 + w, z0), (x0 + w, z0 + d), (x0, z0 + d)] if polygon else [],
        objects=Counter(), display_name=name, confident=True,
    )


def _house() -> HomeIndex:
    # Laundry room and sitting area share a wall (0.2 m apart); the sitting
    # area's floor is 0.4 m lower. The kitchen touches the laundry room's
    # other side. The office is across the house; the bedroom is upstairs
    # directly above the laundry room.
    return HomeIndex(
        rooms=[
            _room("room-1", "laundry room", 0.0, 0.0, 3.0, 2.5),
            _room("room-2", "sitting area", 3.2, 0.0, 4.0, 4.0, floor_y=-0.4),
            _room("room-3", "kitchen", -4.2, 0.0, 4.0, 3.0),
            _room("room-4", "office", 20.0, 20.0, 3.0, 3.0),
            _room("room-5", "bedroom", 0.0, 0.0, 3.0, 2.5, storey=1, floor_y=3.0),
            _room("room-6", "unnamed area", 0.0, 0.0, 0.0, 0.0, polygon=False),
        ],
        bundle_id="test", storey_count=2,
    )


# ------------------------------------------------------------- geometry
def test_polygon_gap_touching_separated_and_empty():
    a = [(0, 0), (1, 0), (1, 1), (0, 1)]
    b = [(1.2, 0), (2, 0), (2, 1), (1.2, 1)]
    assert math.isclose(polygon_gap(a, b), 0.2)
    assert polygon_gap(a, [(5, 5), (6, 5), (6, 6), (5, 6)]) > 4
    assert polygon_gap(a, []) == math.inf
    overlapping = [(0.5, 0.5), (1.5, 0.5), (1.5, 1.5), (0.5, 1.5)]
    assert polygon_gap(a, overlapping) == 0.0


def test_neighbours_are_same_storey_within_a_wall():
    house = _house()
    laundry = house.by_key("room-1")
    names = [r.display_name for r, _gap, _delta in house.neighbours(laundry)]
    assert names == ["sitting area", "kitchen"]
    # Upstairs, far away, and polygon-less rooms never pair.
    assert "bedroom" not in names and "office" not in names and "unnamed area" not in names
    assert house.neighbours(house.by_key("room-6")) == []


def test_layout_text_names_neighbours_and_a_level_change():
    house = _house()
    text = house.layout_text(house.by_key("room-1"))
    assert text.startswith("The laundry room shares a wall with the sitting area")
    assert "about 40 cm lower, a step or two down" in text
    assert text.endswith("and the kitchen.")
    assert "kitchen (" not in text, "no level change is claimed where floors are level"
    assert house.layout_text(house.by_key("room-4")) == ""


def test_layout_overview_lists_each_pair_once():
    overview = _house().layout_overview()
    assert overview.count("laundry room - sitting area") + overview.count("sitting area - laundry room") == 1
    assert "kitchen" in overview and "office" not in overview


# ----------------------------------------------------------- directives
def test_active_room_directive_carries_the_layout():
    house = _house()
    state = FlowState(thread_id="t", home_id="test", active_room_key="room-1")
    text = "\n".join(flow_runtime._home_directives(state, house))
    assert "LAYOUT, from the walk's floor plan" in text
    assert "shares a wall with the sitting area" in text
    assert "rather than that you cannot tell" in text


def test_a_room_with_no_captured_neighbour_says_so_and_asks():
    house = _house()
    state = FlowState(thread_id="t", home_id="test", active_room_key="room-4")
    text = "\n".join(flow_runtime._home_directives(state, house))
    assert "did not capture a wall this room shares" in text


def test_whole_home_turns_get_the_overview():
    state = FlowState(thread_id="t", home_id="test")
    text = "\n".join(flow_runtime._home_directives(state, _house()))
    assert "LAYOUT, rooms that share a wall" in text


# ---------------------------------------------------------------- voice
def test_the_prompt_bans_i_cant_for_legitimate_questions():
    prompt = build_home_guide_system_prompt("control")
    assert 'Never tell the homeowner "I can\'t"' in prompt
    assert "finds the answer or the way to" in prompt


def test_scripted_copy_no_longer_says_i_cant():
    assert "can't" not in flow_runtime._SAFE_NO_PRICE_COPY
    assert "won't guess" in flow_runtime._SAFE_NO_PRICE_COPY


# -------------------------------------------------------------- conduit
def test_expectations_copy_has_the_agent_bringing_the_quotes_back():
    copy = EXPECTATIONS["copy"]
    # The agent does the sending and the bringing back. Sep 17 (#101)
    # dropped the "a person reviews every request" clause: naming the
    # people behind the agent made them the subject and the agent a
    # messenger. What stays is that the agent never claims to have priced
    # the work itself and promises no turnaround.
    assert "I'm getting your request in front of local providers" in copy
    assert "I'll bring them to you here" in copy
    assert "my team" not in copy
    assert not any(word in copy.lower() for word in ("hour", "day", "24", "48"))
