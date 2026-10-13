"""Review of PR #10, Oct 9. Five blockers: wrong numbers to a homeowner,
state written off a guess, a fix that wasn't there, and a validation
regression.

1. `footprint_feet` was a world-axis bounding box, so a rotated room was
   reported wrong and contradicted the area quoted beside it, under "These
   ARE measurements from the scan".
2. An unresolvable room word permanently renamed and saved the one unnamed
   area, with no confirmation: "did you get the attic?" relabelled an
   interior area forever.
3. The multi-candidate branch never cleared `unresolved_room_phrase`, so
   one prompt carried "NEVER say it is not in the scan" next to "which is
   NOT in the scan" about the same space.
4. `recipient_list` skipped every blank entry, which also dropped the
   rejection of a to-list with no valid mailbox.
5. The Power Washing suppression did not return, so "crack repair on the
   driveway" became Handyman and still got a band off the wrong table.
"""

from __future__ import annotations

import math
from collections import Counter

import pytest

import app.flow_runtime as rt
from app.flow import home_registry
from app.flow.ops_email import recipient_list
from app.flow.state import FlowState
from app.home_ai import HomeAIChatRequest, HomeAIContextPacket
from app.home_guide_tools import detect_service_type
from app.home_index import HomeIndex, Room


def _rotate(points, degrees):
    t = math.radians(degrees)
    return [(x * math.cos(t) - z * math.sin(t), x * math.sin(t) + z * math.cos(t))
            for x, z in points]


def _room(polygon, **kw) -> Room:
    base = dict(key="room-1", index=1, storey=0, plan_label="", floor_y=0.0,
                polygon=polygon, objects=Counter(), window_count=0, door_count=2,
                wall_count=4, display_name="unnamed area 1", confident=False,
                area_sqft=_sqft(polygon))
    base.update(kw)
    return Room(**base)


def _sqft(points) -> float:
    """Shoelace, in square feet."""
    area = abs(sum(points[i][0] * points[(i + 1) % len(points)][1]
                   - points[(i + 1) % len(points)][0] * points[i][1]
                   for i in range(len(points)))) / 2.0
    return area * 10.7639


def _request(message: str) -> HomeAIChatRequest:
    return HomeAIChatRequest(threadId="t", message=message,
                             homeContext=HomeAIContextPacket(), homeId="h")


def _directives(state: FlowState, index: HomeIndex, message: str = "ok") -> str:
    plan = rt._engine.plan_turn(state, message)
    return rt._build_directives(state, plan, opening=False, price_guidance=None,
                                quotes_to_present=None, home_index=index)


# --------------------------------------------------- 1. the footprint is the room's
RECT = [(0.0, 0.0), (6.0, 0.0), (6.0, 3.0), (0.0, 3.0)]  # 18 m2, 194 sq ft


def test_an_axis_aligned_room_still_reads_its_own_size():
    size = HomeIndex.footprint_feet(_room(RECT))
    assert size is not None
    assert round(size[0]) == 20 and round(size[1]) == 10


@pytest.mark.parametrize("degrees", [15, 30, 45, 60, 85, 120])
def test_a_rotated_room_is_not_reported_bigger_than_it_is(degrees):
    """The bug: at 30 degrees a 194 sq ft room came out 22 x 18 ft (403)."""
    room = _room(_rotate(RECT, degrees))
    size = HomeIndex.footprint_feet(room)
    assert size is not None, f"{degrees} degrees: a rectangle has a footprint"
    assert round(size[0]) == 20 and round(size[1]) == 10
    # The pair no longer contradicts the area quoted next to it.
    assert size[0] * size[1] <= room.area_sqft * 1.02


def test_an_l_shaped_room_is_given_no_width_by_length():
    """No rectangle is honest here, so the callers quote the area alone."""
    ell = [(0.0, 0.0), (6.0, 0.0), (6.0, 3.0), (3.0, 3.0), (3.0, 6.0), (0.0, 6.0)]
    assert HomeIndex.footprint_feet(_room(ell)) is None


def test_a_degenerate_polygon_has_no_footprint():
    assert HomeIndex.footprint_feet(_room([(0.0, 0.0), (4.0, 0.0)])) is None
    assert HomeIndex.footprint_feet(_room([])) is None


def test_the_active_room_line_quotes_a_rotated_room_consistently(monkeypatch):
    room = _room(_rotate(RECT, 30), display_name="screened porch", confident=True,
                 measurements={"mean_wall_height_m": 2.6, "floor_sqft": 194.0})
    index = HomeIndex([room], bundle_id="h")
    monkeypatch.setattr(home_registry, "load_index", lambda home_id: index)
    monkeypatch.setattr(home_registry, "room_context_for", lambda h, k: None)
    state = FlowState(thread_id="t", client_flow_aware=True, opening_delivered=True, user_turns=2)
    rt._reconcile_home(state, _request("what would it cost to paint the screened porch?"))
    text = _directives(state, index, "what would it cost to paint the screened porch?")
    assert "about 20 x 10 ft" in text
    assert "22 x 18" not in text


# --------------------------------------------------- 2. a question is not a naming
def _unnamed_interior():
    return _room(RECT, display_name="unnamed area 1", confident=False, area_sqft=194.0)


def test_asking_whether_an_area_was_scanned_renames_nothing(monkeypatch):
    saved = []
    room = _unnamed_interior()
    index = HomeIndex([room], bundle_id="h")
    monkeypatch.setattr(home_registry, "load_index", lambda home_id: index)
    monkeypatch.setattr(home_registry, "save_index", lambda h, idx, **kw: saved.append(h))
    monkeypatch.setattr(home_registry, "room_context_for", lambda h, k: None)
    state = FlowState(thread_id="t", client_flow_aware=True, opening_delivered=True, user_turns=2)

    rt._reconcile_home(state, _request("did you get the attic?"))

    assert room.display_name == "unnamed area 1", "a question writes no name"
    assert not room.named_by_homeowner
    assert saved == [], "nothing is persisted off an inference"
    # It is still their word for the space, and still the subject.
    assert state.unnamed_room_phrase == "attic"
    assert state.unnamed_room_key == "room-1" and state.active_room_key == "room-1"
    text = _directives(state, index, "did you get the attic?")
    assert "which is NOT in the scan" not in text
    assert "you don't have that space" not in text
    assert "they call a space the attic" in text.lower()
    assert "the only one it can be" in text


def test_telling_us_what_the_space_is_still_names_it(monkeypatch):
    """The Oct 5 report: their word names the area, and it keeps."""
    saved = []
    room = _unnamed_interior()
    index = HomeIndex([room], bundle_id="h")
    monkeypatch.setattr(home_registry, "load_index", lambda home_id: index)
    monkeypatch.setattr(home_registry, "save_index", lambda h, idx, **kw: saved.append(h))
    monkeypatch.setattr(home_registry, "room_context_for", lambda h, k: None)
    state = FlowState(thread_id="t", client_flow_aware=True, opening_delivered=True, user_turns=2)

    rt._reconcile_home(state, _request("I scanned my front porch and want the fascia replaced"))

    assert room.display_name == "front porch" and room.named_by_homeowner
    assert saved == ["h"] and state.active_room_key == "room-1"
    assert state.unnamed_room_phrase is None and state.unnamed_room_key is None


@pytest.mark.parametrize("message,names", [
    ("did you get the attic?", False),
    ("is the attic in the scan?", False),
    ("what about the attic", False),
    ("do you have the attic", False),
    ("I scanned my attic", True),
    ("we walked the attic too", True),
    ("it's the attic", True),
    ("that is the attic", True),
])
def test_which_messages_may_write_a_name(message, names):
    assert bool(rt._ROOM_NAMING.search(message)) is names


# --------------------------------------------------- 3. one prompt, one story
def test_the_prompt_never_both_denies_and_affirms_the_space(monkeypatch):
    """Three candidates: the phrase resolves to no single area, and the
    stale `unresolved_room_phrase` from the turn before must not survive."""
    rooms = [_room(RECT, key=f"room-{n}", index=n, display_name="exterior",
                   role="exterior", confident=True, area_sqft=194.0) for n in (1, 2, 3)]
    index = HomeIndex(rooms, bundle_id="h")
    monkeypatch.setattr(home_registry, "load_index", lambda home_id: index)
    monkeypatch.setattr(home_registry, "room_context_for", lambda h, k: None)
    state = FlowState(thread_id="t", client_flow_aware=True, opening_delivered=True, user_turns=2)

    # A denial left on the thread by an earlier turn, correct when it was set.
    state.unresolved_room_phrase = "wine cellar"
    state.unresolved_room_turns = 1

    # Now a word that IS one of the unnamed areas. The denial must go.
    rt._reconcile_home(state, _request("I scanned my front porch"))
    assert state.unnamed_room_phrase == "front porch"
    assert state.unresolved_room_phrase is None
    assert state.unresolved_room_turns == 0
    text = _directives(state, index, "the fascia needs replacing")
    assert "NEVER say it is not in the scan" in text
    assert "which is NOT in the scan" not in text
    assert "wine cellar" not in text


# --------------------------------------------------- 4. an empty header vs a bad one
def test_no_recipients_is_not_an_error():
    """Python 3.12 parses '' as ('', ''); asking for no cc is legitimate."""
    assert recipient_list("") == []
    assert recipient_list([]) == []
    assert recipient_list("   ") == []


@pytest.mark.parametrize("header", [
    "Ops <>",
    ", ,",
    "undisclosed-recipients:;",
    "alice@example.com, Bob <>",
    "alice@example.com, not-an-address",
])
def test_a_header_with_no_mailbox_is_still_rejected(header):
    """The regression: these returned [] or silently dropped a recipient."""
    with pytest.raises(ValueError, match="Invalid email recipient"):
        recipient_list(header)


def test_real_recipients_still_parse():
    assert recipient_list("Ops <ops@example.com>") == ["ops@example.com"]
    assert recipient_list(["a@x.com", "B <b@y.com>"]) == ["a@x.com", "b@y.com"]
    assert recipient_list("a@x.com, A@X.com") == ["a@x.com"], "deduped, case-insensitively"
    with pytest.raises(ValueError):
        recipient_list("a@x.com\nbcc: evil@x.com")


# --------------------------------------------------- 5. hardscape is not in the catalog
@pytest.mark.parametrize("message", [
    "replace the concrete on my driveway",
    "crack repair on the driveway",
    "I want to repave the driveway",
    "pour a new walkway",
    "widen the driveway",
])
def test_hardscape_work_gets_no_trade_at_all(message):
    """Quintin, Oct 8: priced as Power Washing ($150-600) off the word
    "driveway". Falling through to the weak words made it Handyman, which
    is the same wrong price under another name."""
    assert detect_service_type(message) is None
    assert detect_service_type(message, strong_only=True) is None


@pytest.mark.parametrize("message,service", [
    ("power wash the driveway", "Power Washing"),
    ("can you clean the driveway", "Power Washing"),
    ("wash the walkway", "Power Washing"),
])
def test_actually_washing_the_driveway_is_still_power_washing(message, service):
    assert detect_service_type(message) == service
