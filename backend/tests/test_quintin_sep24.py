"""Quintin's Sep 24 notes from an EXTERIOR scan on the TestFlight build.

1. "that's a nice living room" -- on a scan of the outside of the house.
2. "The request timed out" right after typing a zip -- the two research
   calls ran one after the other and could exceed the app's 90s timeout.
3. The window-replacement request had no count and no sizes.
4. Grammar: "Siding and the walkway both get power washed together well".
Plus Nathan's prototype: the appearance pass counts individual windows and
their type, because RoomPlan only counts openings.
"""

from __future__ import annotations

import asyncio
import json
import time

import pytest

import app.flow_runtime as flow_runtime
from app.config import settings
from app.flow import home_registry, ops_email
from app.flow.state import FlowState
from app.home_guide_prompt import HOME_GUIDE_PROMPT_VERSION, build_home_guide_system_prompt
from app.home_index import HomeIndex, Room, _window_openings, load_bundle
from app.room_context import validate_appearance


# ------------------------------------------------------------ window openings
def test_each_window_surface_keeps_its_size():
    surfaces = [
        {"category": "window", "dimensions": [0.9, 1.2, 0.05]},
        {"category": "window", "dimensions": [2.4, 1.5, 0.05]},   # a bank of sashes
        {"category": "window", "dimensions": []},                  # unusable, skipped
        {"category": "window", "dimensions": [0.0, 1.0, 0.05]},    # zero width, skipped
    ]
    assert _window_openings(surfaces) == [(0.9, 1.2), (2.4, 1.5)]


def test_openings_survive_the_index_round_trip():
    room = Room(key="room-1", index=1, storey=1, plan_label="", area_sqft=200.0, floor_y=0.0,
                window_count=2, window_openings=[(0.9, 1.2), (2.4, 1.5)])
    back = Room.from_json(json.loads(json.dumps(room.to_json())))
    assert back.window_openings == [(0.9, 1.2), (2.4, 1.5)]
    assert back.window_count == 2


def test_the_lead_package_lists_opening_sizes_in_feet(monkeypatch):
    from app import flow_quotes

    room = Room(key="room-1", index=1, storey=1, plan_label="", area_sqft=440.0, floor_y=0.0,
                display_name="living room", window_count=2,
                window_openings=[(0.9144, 1.2192), (2.4384, 1.524)])
    index = HomeIndex([room], bundle_id="h")
    monkeypatch.setattr(home_registry, "load_index", lambda home_id: index)
    monkeypatch.setattr(home_registry, "room_context_for", lambda h, k: {})
    state = FlowState(thread_id="t", home_id="h", active_room_key="room-1")
    measurements, key, name = flow_quotes._room_measurements(state)
    assert measurements["windowCount"] == 2
    assert measurements["windowOpenings"] == [
        {"widthFeet": 3.0, "heightFeet": 4.0},
        {"widthFeet": 8.0, "heightFeet": 5.0},
    ]


def test_the_email_prints_the_sizes_and_the_caveat():
    lines = ops_email._fmt_measurements({
        "windowCount": 2,
        "windowOpenings": [{"widthFeet": 3.0, "heightFeet": 4.0}, {"widthFeet": 8.0, "heightFeet": 5.0}],
    })
    joined = "\n".join(lines)
    assert "Window openings (scan): 2" in joined
    assert "3.0 x 4.0 ft, 8.0 x 5.0 ft" in joined
    assert "confirm the sash count" in joined


def test_the_active_room_directive_says_openings_with_sizes():
    room = Room(key="room-1", index=1, storey=1, plan_label="", area_sqft=440.0, floor_y=0.0,
                display_name="living room", window_count=2,
                window_openings=[(0.9144, 1.2192)])
    index = HomeIndex([room], bundle_id="h")
    state = FlowState(thread_id="t", active_room_key="room-1", home_id="h")
    text = "\n".join(flow_runtime._home_directives(state, index))
    assert "2 window opening(s)" in text
    assert "3.0 x 4.0 ft" in text
    assert "2 window(s)" not in text


# --------------------------------------------------------------- vision pass
def test_the_vision_pass_reports_setting_and_windows():
    raw = {
        "room": "exterior", "setting": "Exterior",
        "objects": [], "surfaces": {}, "style": "brick ranch",
        "windows": [
            {"count": 5, "type": "double-hung", "gridded": True, "where": "front, left of the door"},
            {"count": 3, "type": "Double-Hung", "gridded": True, "where": "front, right"},
            {"count": 0, "type": "casement"},            # nothing counted, dropped
            {"count": 2, "type": "porthole", "gridded": "yes"},  # unknown type, bad grid flag
        ],
    }
    doc = validate_appearance(raw, [])
    assert doc["setting"] == "exterior"
    assert [w["count"] for w in doc["windows"]] == [5, 3, 2]
    assert doc["windows"][0]["type"] == "double-hung" and doc["windows"][0]["gridded"] is True
    assert doc["windows"][2]["type"] == "unknown" and doc["windows"][2]["gridded"] is None


def test_a_response_without_the_new_fields_still_validates():
    doc = validate_appearance({"room": "kitchen", "objects": [], "surfaces": {}}, [])
    assert doc["setting"] == "" and doc["windows"] == []


def test_windows_seen_reach_the_agent_with_the_confirm_caveat(monkeypatch):
    room = Room(key="room-1", index=1, storey=1, plan_label="", area_sqft=440.0, floor_y=0.0)
    monkeypatch.setattr(home_registry, "room_context_for", lambda h, k: {
        "surfaces": {}, "objects": [], "style": "", "notable": [], "coverage": "complete",
        "windows": [{"count": 5, "type": "double-hung", "gridded": True, "where": "front left"},
                    {"count": 3, "type": "double-hung", "gridded": True, "where": "front right"}],
    })
    text = "\n".join(flow_runtime._appearance_directives("h", room))
    assert "about 8 individual windows" in text
    assert "5 double-hung, gridded (front left)" in text
    assert "ask the homeowner to confirm the count" in text


# ------------------------------------------------------------------ exterior
def test_an_exterior_capture_is_never_called_a_room(monkeypatch):
    room = Room(key="room-1", index=1, storey=1, plan_label="", area_sqft=900.0, floor_y=0.0)
    monkeypatch.setattr(home_registry, "room_context_for", lambda h, k: {
        "surfaces": {"walls": "white brick"}, "objects": [], "style": "", "notable": [],
        "coverage": "complete", "setting": "exterior",
    })
    text = "\n".join(flow_runtime._appearance_directives("h", room))
    assert "EXTERIOR OF THE HOUSE, not a room" in text
    assert "patio furniture" in text


@pytest.mark.asyncio
async def test_the_photos_saying_exterior_overrides_the_fixture_name(monkeypatch, tmp_path):
    """Two patio chairs and a sofa named a facade 'living room'. The
    appearance pass knows better, and the homeowner's own name still wins."""
    monkeypatch.setattr(settings, "storage_dir", str(tmp_path))
    patio = Room(key="room-1", index=1, storey=1, plan_label="", area_sqft=900.0, floor_y=0.0,
                 display_name="living room", role="living", name_basis="2 sofa")
    named = Room(key="room-2", index=2, storey=1, plan_label="", area_sqft=300.0, floor_y=0.0,
                 display_name="front porch", role="living", named_by_homeowner=True)
    index = HomeIndex([patio, named], bundle_id="h")
    monkeypatch.setattr(home_registry, "load_index", lambda home_id: index)
    monkeypatch.setattr(home_registry, "save_index", lambda home_id, idx: tmp_path / "x")

    async def _fake_enrich_room(bundle_dir, home_id, key, *, refresh=False):
        return {"setting": "exterior", "surfaces": {}, "objects": [], "coverage": "complete"}

    monkeypatch.setattr(home_registry, "enrich_room", _fake_enrich_room)
    await home_registry.enrich_rooms(tmp_path, "h")
    assert patio.display_name == "exterior" and patio.role == "exterior"
    assert named.display_name == "front porch", "the homeowner's word beats the photos"


# ------------------------------------------------------------- the zip turn
def test_the_turn_deadline_sits_under_the_apps_timeout():
    assert settings.turn_deadline_seconds < 90


@pytest.mark.asyncio
async def test_price_and_local_research_run_together_not_in_series(monkeypatch):
    """Each can take ~45s on a cold cache; in series that beat the app's
    timeout. The turn must overlap them."""
    async def slow_price(state, request, home_index):
        await asyncio.sleep(0.25)
        return None, False

    async def slow_local(state, request):
        await asyncio.sleep(0.25)
        return None, None

    monkeypatch.setattr(flow_runtime, "_maybe_price_guidance", slow_price)
    monkeypatch.setattr(flow_runtime, "_maybe_local_research", slow_local)
    started = time.perf_counter()
    (pg, asked), (lc, lp) = await asyncio.gather(
        flow_runtime._maybe_price_guidance(None, None, None),
        flow_runtime._maybe_local_research(None, None),
    )
    elapsed = time.perf_counter() - started
    assert elapsed < 0.45, f"ran in series ({elapsed:.2f}s)"
    # And the runtime's own call site uses gather, not two awaits in a row.
    import inspect
    source = inspect.getsource(flow_runtime.run_home_ai_turn) if hasattr(flow_runtime, "run_home_ai_turn") else inspect.getsource(flow_runtime)
    assert "asyncio.gather(\n            _maybe_price_guidance(state, request, home_index),\n            _maybe_local_research(state, request),\n        )" in source


# ------------------------------------------------------------------ grammar
def test_the_prompt_asks_for_complete_sentences_not_clipped_ones():
    prompt = build_home_guide_system_prompt("control")
    assert "Short does not mean clipped" in prompt
    assert "keep the\n  articles" in prompt or "keep the articles" in prompt
    assert '"at once"' in prompt
    assert HOME_GUIDE_PROMPT_VERSION == "home-guide-v11"


# ------------------------------------------- 3b. the single-scan packet path
# The TestFlight build sends one RoomPlan capture as a context packet, not a
# whole-home index. The sizes have to travel that route too, or the fix above
# only helps the demo homes.
_PACKET_ROOM = {
    "id": "room-1",
    "name": "Room 1",
    "type": "RoomPlan captured area",
    "floorAreaSquareMeters": 20.0,
    "wallCount": 4,
    "doorCount": 1,
    "windowCount": 2,
    "windows": [
        {"widthMeters": 0.9, "heightMeters": 1.2},
        {"widthMeters": 1.8, "heightMeters": 1.2},
        {"widthMeters": 0, "heightMeters": 1.2},
        {"widthMeters": "wide", "heightMeters": 1.2},
    ],
}


def test_the_packet_room_carries_opening_sizes_to_the_model():
    from app.home_context_builder import _compact_room

    compact = _compact_room(_PACKET_ROOM)
    assert compact["counts"]["windows"] == 2
    assert compact["windowOpeningsFeet"] == [
        {"widthFt": pytest.approx(2.95, abs=0.01), "heightFt": pytest.approx(3.94, abs=0.01)},
        {"widthFt": pytest.approx(5.91, abs=0.01), "heightFt": pytest.approx(3.94, abs=0.01)},
    ]
    # An older app that sends no sizes changes nothing in the packet.
    assert "windowOpeningsFeet" not in _compact_room({"name": "Room 1", "windowCount": 2})


def test_the_single_scan_lead_rolls_windows_up_with_sizes():
    from app.flow_api import _measurements_from_context
    from app.home_ai import HomeAIContextPacket

    packet = HomeAIContextPacket(rooms=[_PACKET_ROOM, {"name": "Room 2", "windowCount": 1, "doorCount": 2}])
    measurements = _measurements_from_context(packet)
    assert measurements["windowCount"] == 3
    assert measurements["doorCount"] == 3
    assert measurements["windowOpenings"] == [
        {"widthFeet": 3.0, "heightFeet": 3.9},
        {"widthFeet": 5.9, "heightFeet": 3.9},
    ]
    assert measurements["rooms"][0]["windowOpenings"] == measurements["windowOpenings"]

    lines = "\n".join(ops_email._fmt_measurements(measurements))
    assert "Window openings (scan): 3" in lines
    assert "Opening sizes (w x h): 3.0 x 3.9 ft, 5.9 x 3.9 ft" in lines
    assert "confirm the sash count" in lines


def test_a_packet_without_windows_adds_no_counts():
    from app.flow_api import _measurements_from_context
    from app.home_ai import HomeAIContextPacket

    measurements = _measurements_from_context(HomeAIContextPacket(rooms=[{"name": "Room 1"}]))
    assert "windowCount" not in measurements
    assert "windowOpenings" not in measurements
