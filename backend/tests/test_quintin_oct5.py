"""Quintin's Oct 5 notes, from a screened porch and a front porch.

1. Asking to replace the porch doors: "a provider would need to see them to
   give an accurate quote". No site named, same meaning to a homeowner.
2. "The AI still doesn't know measurements or how many doors there are."
   Doors and the footprint were only on the ACTIVE ROOM line, and a lone
   room was never put in focus unless it was an exterior.
3. "I scanned my front porch ... the AI said I never scanned the front
   porch." The index held areas the photos could not name; "porch" is a
   room word; it did not resolve; so the directive said it was not in the
   scan.
"""

from __future__ import annotations

from collections import Counter

import pytest

import app.flow_runtime as rt
from app.flow import enforcement, home_registry
from app.flow.machine import GateDecision
from app.flow.state import FlowState
from app.home_ai import HomeAIChatRequest, HomeAIContextPacket
from app.home_index import HomeIndex, Room

SQUARE = [(0.0, 0.0), (4.0, 0.0), (4.0, 4.0), (0.0, 4.0)]


def _porch(**kw) -> Room:
    base = dict(key="room-1", index=1, storey=0, plan_label="", area_sqft=172.0, floor_y=0.0,
                polygon=SQUARE, objects=Counter({"chair": 4, "table": 1}), window_count=0,
                door_count=2, wall_count=4, display_name="unnamed area 1", confident=False,
                measurements={"mean_wall_height_m": 2.6, "paintable_sqft": 410.0, "floor_sqft": 172.0})
    base.update(kw)
    return Room(**base)


def _request(message: str) -> HomeAIChatRequest:
    return HomeAIChatRequest(threadId="t", message=message, homeContext=HomeAIContextPacket(), homeId="h")


def _directives(state: FlowState, index: HomeIndex, message: str = "ok") -> str:
    plan = rt._engine.plan_turn(state, message)
    return rt._build_directives(state, plan, opening=False, price_guidance=None,
                                quotes_to_present=None, home_index=index)


# ------------------------------------------------------------ 1. nobody comes to look
@pytest.mark.parametrize("text,trips", [
    ("A provider would need to see the doors in person before giving an accurate quote.", True),
    ("They'll want to see them to give you an accurate number.", True),
    ("A provider needs to take a look at the screens before they can price it.", True),
    ("Providers price from the scan without needing to see it in person.", False),
    ("You can see both doors in the scan photos.", False),
    ("Let me see if I can get pricing from local companies.", False),
])
def test_needing_to_see_it_counts_as_a_site_visit(text, trips):
    rules = [v.rule for v in enforcement.check(text, GateDecision())]
    assert ("onsite_visit_suggested" in rules) is trips


# ------------------------------------------------------------ 2. doors and sizes
def test_the_index_lists_doors_and_the_footprint(monkeypatch):
    index = HomeIndex([_porch()], bundle_id="h")
    assert index.as_text() == (
        "- unnamed area 1 (name uncertain): ~172 sq ft (about 13 x 13 ft), 0 window opening(s), 2 door(s), chair, table"
    )
    monkeypatch.setattr(home_registry, "load_index", lambda home_id: index)
    monkeypatch.setattr(home_registry, "room_context_for", lambda h, k: None)
    state = FlowState(thread_id="t", client_flow_aware=True, opening_delivered=True, user_turns=1)
    rt._reconcile_home(state, _request("how many doors are there?"))
    assert state.active_room_key == "room-1", "a lone room is the room in focus"
    text = _directives(state, index)
    assert "ACTIVE ROOM: the unnamed area 1 — about 172 sq ft (about 13 x 13 ft), 0 window opening(s), 2 door(s)" in text
    assert "walls about 9 ft high, about 410 sq ft of paintable wall. These ARE measurements from the scan" in text
    assert "No specific room is in focus" not in text


def test_a_lone_area_with_no_room_in_it_is_not_given_a_size(monkeypatch):
    area = Room(key="room-1", index=1, storey=0, plan_label="", area_sqft=0.0, floor_y=0.0,
                display_name="unnamed area 1")
    index = HomeIndex([area], bundle_id="h")
    monkeypatch.setattr(home_registry, "load_index", lambda home_id: index)
    monkeypatch.setattr(home_registry, "room_context_for", lambda h, k: None)
    state = FlowState(thread_id="t", client_flow_aware=True, opening_delivered=True, user_turns=1)
    rt._reconcile_home(state, _request("hi"))
    text = _directives(state, index)
    assert "ACTIVE AREA: the unnamed area 1 — the scan found no room structure here" in text
    assert "about 0 sq ft" not in text


# ------------------------------------------------------------ 3. "you never scanned the front porch"
def test_their_word_names_the_one_area_the_photos_could_not_name(monkeypatch):
    saved = []
    index = HomeIndex([_porch(display_name="exterior", role="exterior", confident=True)], bundle_id="h")
    monkeypatch.setattr(home_registry, "load_index", lambda home_id: index)
    monkeypatch.setattr(home_registry, "save_index", lambda home_id, idx, **kw: saved.append(home_id))
    monkeypatch.setattr(home_registry, "room_context_for", lambda h, k: {"setting": "exterior", "structure": "house", "surfaces": {}, "objects": [], "style": "", "notable": []})
    state = FlowState(thread_id="t", client_flow_aware=True, opening_delivered=True, user_turns=2)
    rt._reconcile_home(state, _request("I scanned my front porch and I want the fascia replaced"))
    room = index.rooms[0]
    assert room.display_name == "front porch" and room.named_by_homeowner and room.role == "exterior"
    assert saved == ["h"] and state.active_room_key == "room-1"
    assert state.unresolved_room_phrase is None and state.unnamed_room_phrase is None
    text = _directives(state, index, "what would the fascia run?")
    assert "NOT in the scan" not in text
    assert "front porch: the outside of the building" in text
    assert "They told you this room is the front porch" in text


def test_several_unnamed_areas_are_never_denied(monkeypatch):
    rooms = [_porch(key=f"room-{n}", index=n, display_name="exterior", role="exterior", confident=True) for n in (1, 2, 3)]
    index = HomeIndex(rooms, bundle_id="h")
    monkeypatch.setattr(home_registry, "load_index", lambda home_id: index)
    monkeypatch.setattr(home_registry, "room_context_for", lambda h, k: None)
    state = FlowState(thread_id="t", client_flow_aware=True, opening_delivered=True, user_turns=2)
    rt._reconcile_home(state, _request("I scanned my front porch"))
    assert state.unnamed_room_phrase == "front porch"
    assert state.unresolved_room_phrase is None
    assert all(r.display_name == "exterior" for r in rooms), "three candidates: nothing is renamed"
    text = _directives(state, index, "the fascia needs replacing")
    assert "They call a space the front porch" in text and "it is one of those" in text
    assert "NEVER say it is not in the scan" in text and "NOT in the scan" not in text
    # The phrase is per message: a later turn without it starts clean.
    rt._reconcile_home(state, _request("ok"))
    assert state.unnamed_room_phrase is None


def test_a_room_the_home_really_lacks_is_still_said_so(monkeypatch):
    kitchen = Room(key="room-1", index=1, storey=0, plan_label="kitchen", area_sqft=180.0, floor_y=0.0,
                   polygon=SQUARE, wall_count=4, display_name="kitchen", confident=True, role="kitchen")
    index = HomeIndex([kitchen], bundle_id="h")
    monkeypatch.setattr(home_registry, "load_index", lambda home_id: index)
    monkeypatch.setattr(home_registry, "room_context_for", lambda h, k: None)
    state = FlowState(thread_id="t", client_flow_aware=True, opening_delivered=True, user_turns=2)
    rt._reconcile_home(state, _request("what about the garage?"))
    assert state.unresolved_room_phrase == "garage" and state.unnamed_room_phrase is None
    assert "NOT in the scan" in _directives(state, index)


@pytest.mark.parametrize("message,word,phrase", [
    ("I scanned my front porch and said I wanted the fascia replaced", "porch", "front porch"),
    ("the screened porch doors", "porch", "screened porch"),
    ("it's the porch", "porch", "porch"),
    ("my lovely porch", "porch", "porch"),
])
def test_the_name_keeps_its_modifier(message, word, phrase):
    assert rt._room_phrase(message, word) == phrase
