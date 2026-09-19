"""Whole-home scans in the live flow.

The behaviour Noah asked for on Sep 1: one walk of the whole house, then
"let's do the master bathroom" and the agent knows which room that is,
stays in it, and says so plainly when a room was never scanned.
"""

import json

import pytest

import app.flow_runtime as flow_runtime
from app.config import settings
from app.flow.home_registry import _cache, ingest_bundle, load_index, save_index, forget
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


def _room(base, index, *, storey=1, label="", w=3.0, d=3.0, cx=0.0, objects=(), windows=0):
    room_dir = base / "rooms" / f"room-{index}"
    room_dir.mkdir(parents=True, exist_ok=True)
    (room_dir / "floor.json").write_text(
        json.dumps({"floor": storey, "floorY": 0.0 if storey == 1 else 3.0}), encoding="utf-8"
    )
    (room_dir / "room.json").write_text(json.dumps({
        "sections": [{"label": label}] if label else [],
        "floors": [_floor(w, d, cx)],
        "objects": [{"category": {o: {}}} for o in objects],
        "windows": [{"category": "window"} for _ in range(windows)],
        "doors": [], "walls": [], "openings": [],
    }), encoding="utf-8")


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "storage_dir", str(tmp_path / "storage"))
    _cache.clear()
    bundle = tmp_path / "bundle"
    (bundle / "rooms").mkdir(parents=True)
    (bundle / "meta.json").write_text(json.dumps({"id": "HOUSE"}), encoding="utf-8")
    _room(bundle, 1, storey=2, label="bathroom", w=3.5, d=3.5, cx=0,
          objects=["bathtub", "sink", "sink", "toilet"], windows=2)
    _room(bundle, 2, storey=2, label="bathroom", w=1.8, d=1.8, cx=10, objects=["toilet"])
    _room(bundle, 3, storey=2, label="kitchen", w=5.0, d=4.0, cx=20,
          objects=["sink", "stove"], windows=1)
    _room(bundle, 4, storey=1, label="bedroom", w=4.0, d=4.0, cx=30, objects=["bed"])
    index = ingest_bundle(bundle, "home-1")
    yield index
    _cache.clear()


def _request(message: str, home_id: str | None = "home-1") -> HomeAIChatRequest:
    return HomeAIChatRequest(threadId="t-home", message=message, homeId=home_id)


# ---------------------------------------------------------------- registry
def test_index_survives_a_restart_without_the_bundle(home, tmp_path):
    """The export is hundreds of MB and does not stay on the server; the
    resolved index is small and must be enough on its own."""
    _cache.clear()                                   # simulate a fresh process
    reloaded = load_index("home-1")
    assert reloaded is not None
    assert len(reloaded.rooms) == 4
    assert reloaded.resolve("master bathroom").key == "room-1"


def test_forget_removes_a_home(home):
    forget("home-1")
    assert load_index("home-1") is None


def test_unknown_home_id_is_not_an_error(home):
    assert load_index("no-such-home") is None
    assert load_index(None) is None


# ------------------------------------------------------------ room tracking
def test_naming_a_room_makes_it_the_subject(home):
    state = FlowState(thread_id="t-home")
    index = flow_runtime._reconcile_home(state, _request("let's redo the master bathroom"))
    assert index is not None
    assert state.active_room_key == "room-1"
    assert state.unresolved_room_phrase is None


def test_the_subject_persists_across_turns(home):
    state = FlowState(thread_id="t-home")
    flow_runtime._reconcile_home(state, _request("the master bathroom please"))
    flow_runtime._reconcile_home(state, _request("something warmer, maybe a soft green"))
    assert state.active_room_key == "room-1", "a follow-up must not lose the room"


def test_moving_rooms_moves_the_subject(home):
    state = FlowState(thread_id="t-home")
    flow_runtime._reconcile_home(state, _request("the master bathroom"))
    flow_runtime._reconcile_home(state, _request("actually let's start with the kitchen"))
    assert state.active_room_key == "room-3"


def test_a_room_that_was_never_scanned_is_flagged_not_faked(home):
    state = FlowState(thread_id="t-home")
    flow_runtime._reconcile_home(state, _request("what about the garage?"))
    assert state.unresolved_room_phrase == "garage"
    assert state.active_room_key is None


def test_an_unscanned_room_does_not_drop_the_current_one(home):
    """Asking about the garage mid-conversation is a question, not a move."""
    state = FlowState(thread_id="t-home")
    flow_runtime._reconcile_home(state, _request("the kitchen"))
    flow_runtime._reconcile_home(state, _request("do you have the garage too?"))
    assert state.active_room_key == "room-3"
    assert state.unresolved_room_phrase == "garage"


def test_a_message_with_no_room_in_it_changes_nothing(home):
    state = FlowState(thread_id="t-home")
    flow_runtime._reconcile_home(state, _request("the kitchen"))
    flow_runtime._reconcile_home(state, _request("what would that cost roughly?"))
    assert state.active_room_key == "room-3"
    assert state.unresolved_room_phrase is None


def test_switching_homes_clears_the_subject(home, tmp_path):
    state = FlowState(thread_id="t-home")
    flow_runtime._reconcile_home(state, _request("the kitchen"))
    save_index("home-2", HomeIndex([], bundle_id="OTHER"))
    flow_runtime._reconcile_home(state, _request("hello", home_id="home-2"))
    assert state.home_id == "home-2"
    assert state.active_room_key is None


def test_single_room_captures_are_untouched(home):
    """No homeId means the old behaviour, exactly."""
    state = FlowState(thread_id="t-home")
    assert flow_runtime._reconcile_home(state, _request("the kitchen", home_id=None)) is None
    assert state.active_room_key is None


# ---------------------------------------------------------------- directives
def test_directives_carry_the_room_list_and_the_active_room(home):
    state = FlowState(thread_id="t-home")
    flow_runtime._reconcile_home(state, _request("the master bathroom"))
    plan = flow_runtime._engine.plan_turn(state, "the master bathroom")
    text = flow_runtime._build_directives(
        state, plan, opening=False, price_guidance=None,
        quotes_to_present=None, home_index=home,
    )
    assert "WHOLE-HOME SCAN" in text
    assert "primary bathroom" in text and "kitchen" in text
    assert "ACTIVE ROOM" in text
    assert "never invent" in text.lower()


def test_directives_tell_the_agent_to_admit_a_missing_room(home):
    state = FlowState(thread_id="t-home")
    flow_runtime._reconcile_home(state, _request("can we do the garage"))
    plan = flow_runtime._engine.plan_turn(state, "can we do the garage")
    text = flow_runtime._build_directives(
        state, plan, opening=False, price_guidance=None,
        quotes_to_present=None, home_index=home,
    )
    assert "garage" in text
    assert "NOT" in text and "in the scan" in text


def test_no_home_means_no_home_directives(home):
    state = FlowState(thread_id="t-home")
    plan = flow_runtime._engine.plan_turn(state, "hello")
    text = flow_runtime._build_directives(
        state, plan, opening=False, price_guidance=None, quotes_to_present=None,
    )
    assert "WHOLE-HOME SCAN" not in text


def test_uncertain_room_names_are_hedged_in_the_directives(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "storage_dir", str(tmp_path / "storage"))
    _cache.clear()
    bundle = tmp_path / "b"
    (bundle / "rooms").mkdir(parents=True)
    (bundle / "meta.json").write_text("{}", encoding="utf-8")
    # Two near-identical bedrooms: "primary" is a guess, not a fact.
    _room(bundle, 1, label="bedroom", w=4.0, d=4.0, cx=0, objects=["bed"])
    _room(bundle, 2, label="bedroom", w=3.9, d=4.0, cx=10, objects=["bed"])
    index = ingest_bundle(bundle, "hedge-home")
    state = FlowState(thread_id="t")
    flow_runtime._reconcile_home(state, HomeAIChatRequest(
        threadId="t", message="the master bedroom", homeId="hedge-home"))
    plan = flow_runtime._engine.plan_turn(state, "the master bedroom")
    text = flow_runtime._build_directives(
        state, plan, opening=False, price_guidance=None,
        quotes_to_present=None, home_index=index,
    )
    assert "inferred, not certain" in text


# --------------------------------------------------------------------- wire
def test_wire_exposes_the_active_room(home):
    from app.flow.machine import GateDecision
    from app.flow.wire import FlowWire

    state = FlowState(thread_id="t-home")
    flow_runtime._reconcile_home(state, _request("the master bathroom"))
    wire = FlowWire.from_state(state, GateDecision(), "tok", None, home_index=home)
    assert wire.home is not None
    assert wire.home.homeId == "home-1"
    assert wire.home.roomCount == 4
    assert wire.home.activeRoom.name == "primary bathroom"
    assert wire.home.activeRoom.confidentName is True


def test_wire_reports_an_unscanned_room_so_the_app_can_offer_to_scan_it(home):
    from app.flow.machine import GateDecision
    from app.flow.wire import FlowWire

    state = FlowState(thread_id="t-home")
    flow_runtime._reconcile_home(state, _request("the garage"))
    wire = FlowWire.from_state(state, GateDecision(), "tok", None, home_index=home)
    assert wire.home.unresolvedRoom == "garage"
    assert wire.home.activeRoom is None


def test_wire_omits_home_for_single_room_captures(home):
    from app.flow.machine import GateDecision
    from app.flow.wire import FlowWire

    wire = FlowWire.from_state(FlowState(thread_id="t"), GateDecision(), "tok", None)
    assert wire.home is None


# ----------------------------------------------------------------- ops API
@pytest.mark.asyncio
async def test_ops_can_put_get_and_delete_a_home_index(home, monkeypatch):
    """The production ingestion path: whatever processes a scan resolves it
    and PUTs the index; the chat server never sees the export itself."""
    from httpx import ASGITransport, AsyncClient

    from app.main import app

    monkeypatch.setattr(settings, "ops_token", "ops-secret")
    headers = {"Authorization": "Bearer ops-secret"}
    payload = load_index("home-1").to_json()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        put = await http.put("/api/v1/ops/homes/uploaded", json=payload, headers=headers)
        assert put.status_code == 200
        assert put.json()["roomCount"] == 4
        assert "primary bathroom" in put.json()["rooms"]

        got = await http.get("/api/v1/ops/homes/uploaded", headers=headers)
        assert got.status_code == 200 and got.json()["roomCount"] == 4

        listing = await http.get("/api/v1/ops/homes", headers=headers)
        assert listing.status_code == 200
        uploaded = next(h for h in listing.json()["homes"] if h["homeId"] == "uploaded")
        assert uploaded["roomCount"] == 4 and any(r["name"] == "primary bathroom" for r in uploaded["rooms"])
        assert (await http.get("/api/v1/ops/homes")).status_code == 401

        gone = await http.delete("/api/v1/ops/homes/uploaded", headers=headers)
        assert gone.status_code == 200
        assert (await http.get("/api/v1/ops/homes/uploaded", headers=headers)).status_code == 404


@pytest.mark.asyncio
async def test_ops_rejects_an_empty_or_malformed_index(home, monkeypatch):
    from httpx import ASGITransport, AsyncClient

    from app.main import app

    monkeypatch.setattr(settings, "ops_token", "ops-secret")
    headers = {"Authorization": "Bearer ops-secret"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        assert (await http.put("/api/v1/ops/homes/x", json={"rooms": []}, headers=headers)).status_code == 422


@pytest.mark.asyncio
async def test_home_index_upload_requires_the_ops_token(home, monkeypatch):
    from httpx import ASGITransport, AsyncClient

    from app.main import app

    monkeypatch.setattr(settings, "ops_token", "ops-secret")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        assert (await http.put("/api/v1/ops/homes/x", json={"rooms": [{"key": "r"}]})).status_code == 401


# ------------------------------------------------- homeowner-supplied names
# Quintin's house (Sep 4) has a 53 sq ft windowless area that no rule can
# identify -- it is a pantry, and only the homeowner knows that.
def test_homeowner_naming_outranks_inference(home):
    index = load_index("home-1")
    room = index.rename_room("room-4", "Nursery")
    assert room.display_name == "nursery"
    assert room.confident and room.named_by_homeowner
    assert room.name_basis == "named by the homeowner"
    assert index.resolve("the nursery").key == "room-4"


def test_a_homeowner_name_survives_the_next_conversation(home):
    index = load_index("home-1")
    index.rename_room("room-4", "playroom")
    from app.flow.home_registry import save_index

    save_index("home-1", index)
    _cache.clear()                                   # a fresh process
    assert load_index("home-1").resolve("playroom").key == "room-4"


def test_naming_is_detected_only_for_real_room_words(home):
    state = FlowState(thread_id="t")
    state.active_room_key = "room-4"                 # a bedroom, inferred
    state.home_id = "home-1"
    index = load_index("home-1")
    index.by_key("room-4").confident = False
    assert flow_runtime._detect_room_naming(state, index, "that's the pantry") == ("room-4", "pantry")
    assert flow_runtime._detect_room_naming(state, index, "we call it the mudroom") == ("room-4", "mudroom")
    # Not a room name, so not a rename.
    assert flow_runtime._detect_room_naming(state, index, "that's the problem") is None
    assert flow_runtime._detect_room_naming(state, index, "that's annoying") is None


def test_naming_needs_a_room_in_focus(home):
    state = FlowState(thread_id="t", home_id="home-1")
    assert flow_runtime._detect_room_naming(state, load_index("home-1"), "that's the pantry") is None


def test_a_fixture_proven_room_is_not_renamed_in_passing(home):
    """A room with a bathtub and two sinks is a bathroom whatever gets said
    about it mid-sentence."""
    state = FlowState(thread_id="t", home_id="home-1", active_room_key="room-1")
    index = load_index("home-1")
    assert index.by_key("room-1").confident
    assert flow_runtime._detect_room_naming(state, index, "that's the office") is None


def test_the_small_room_is_findable_while_exactly_one_is_unnamed(home):
    index = load_index("home-1")
    for r in index.rooms:                            # make room-4 the only unnamed one
        r.confident, r.role = True, "kitchen"
    unnamed = index.by_key("room-4")
    unnamed.confident, unnamed.role = False, "unknown"
    assert index.small_unnamed_room("what about that little room?").key == "room-4"
    assert index.small_unnamed_room("tell me about the kitchen") is None
    # Ambiguous once two areas are unnamed.
    index.by_key("room-3").confident, index.by_key("room-3").role = False, "unknown"
    assert index.small_unnamed_room("the small one") is None


@pytest.mark.asyncio
async def test_naming_a_room_in_conversation_persists_it(home):
    state = FlowState(thread_id="t-home", home_id="home-1")
    index = load_index("home-1")
    # A genuinely unidentified area: no fixtures, no RoomPlan label -- which
    # is what an unnamed room actually looks like in a real export.
    unnamed = index.by_key("room-4")
    unnamed.confident, unnamed.role, unnamed.display_name = False, "unknown", "unnamed area 4"
    flow_runtime._reconcile_home(state, _request("tell me about that little room"))
    flow_runtime._reconcile_home(state, _request("that's the pantry"))
    assert state.active_room_key is not None
    named = load_index("home-1").by_key(state.active_room_key)
    assert named.display_name == "pantry" and named.named_by_homeowner
    assert state.unresolved_room_phrase is None


def test_directives_stop_hedging_once_the_homeowner_names_it(home):
    state = FlowState(thread_id="t", home_id="home-1", active_room_key="room-4")
    index = load_index("home-1")
    index.rename_room("room-4", "pantry")
    plan = flow_runtime._engine.plan_turn(state, "what would you do with it")
    text = flow_runtime._build_directives(
        state, plan, opening=False, price_guidance=None,
        quotes_to_present=None, home_index=index,
    )
    assert "told you this room is the pantry" in text
    assert "Never say it isn't labelled" in text
