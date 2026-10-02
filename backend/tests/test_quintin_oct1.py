"""Quintin's Oct 1 notes: an exterior scan the agent could not see or measure.

His thread, in order: the opener said "I don't actually have any room views
loaded yet for this space"; after "I scanned the outside of my house and
driveway" it still spoke of "the room"; asked for a price it said "nothing's
been measured yet"; and then "a provider will measure on site" -- to someone
using the app to avoid exactly that. Separately: the scope question asked
twice back to back, and a dropped word in an opener.

What was actually wrong, reproduced against the staged-upload path:
- the phone stops sending photos in the chat packet once the staged upload
  starts, so a chat opened before the server has read that upload sees
  nothing, and that blind opener is cached for the life of the thread;
- an area RoomPlan found no room in printed as "unnamed area 1: ~0 sq ft"
  under "they walked their WHOLE HOME" whenever the photo pass had not run;
- an exterior has no RoomPlan measurements at all, so nothing was measured.
"""

from __future__ import annotations

import asyncio

import pytest

import app.flow_runtime as rt
from app.config import settings
from app.flow import enforcement, home_registry, ops_email, scan_uploads, supabase_store
from app.flow.capture import surfaces_feet
from app.flow.machine import GateDecision
from app.flow.pricing import compute_price_guidance
from app.flow.state import FlowState, ScopeIntent
from app.home_ai import HomeAIChatRequest, HomeAIContextPacket
from app.home_index import HomeIndex, Room

BOUNDS = {"widthMeters": 28.0, "lengthMeters": 31.0, "heightMeters": 6.2}
SURFACES = {"uprightSquareMeters": 107.0, "groundSquareMeters": 84.0, "levelSquareMeters": 120.0, "heightMeters": 5.5}


def _packet(*, rooms: int = 0, surfaces: bool = True) -> HomeAIContextPacket:
    mesh = {"boundsMeters": dict(BOUNDS)}
    if surfaces:
        mesh["surfaces"] = dict(SURFACES)
    return HomeAIContextPacket(roomCount=rooms, rooms=[{"name": "Room 1"}] * rooms, meshSummary=mesh)


def _request(message: str = "hi", *, packet: HomeAIContextPacket | None = None, home_id: str | None = "h") -> HomeAIChatRequest:
    return HomeAIChatRequest(threadId="t", message=message, homeContext=packet or _packet(), homeId=home_id)


def _area(n: int = 1, **kw) -> Room:
    return Room(key=f"room-{n}", index=n, storey=0, plan_label="", area_sqft=0.0, floor_y=0.0,
                display_name=kw.pop("display_name", f"unnamed area {n}"), **kw)


def _directives(state, *, opening=False, index=None, message="ok", **kw) -> str:
    plan = rt._engine.plan_turn(state, None if opening else message)
    return rt._build_directives(state, plan, opening=opening, price_guidance=None,
                                quotes_to_present=None, home_index=index, **kw)


# ------------------------------------------------------------ the blind opener
def test_an_opener_with_nothing_to_see_says_so_instead_of_naming_a_room():
    state = FlowState(thread_id="t", client_flow_aware=True)
    request = _request()
    assert rt._scan_visible(state, request, None) is False
    text = _directives(state, opening=True, scan_visible=False)
    assert "You cannot see this scan yet" in text
    assert "Do not call it a room" in text
    assert "naming the room you can see" not in text


@pytest.mark.asyncio
async def test_the_opening_waits_for_a_staged_upload_that_is_still_being_read(monkeypatch):
    index = HomeIndex([_area()], bundle_id="h")
    calls = iter([
        {"progress": {"status": "queued"}},
        {"progress": {"status": "running"}},
        {"progress": {"status": "done"}, "contextIndex": index.to_json()},
    ])
    seen = []

    async def fake_transition(home_id, action, **_):
        seen.append(action)
        return next(calls)

    async def fake_load(home_id, **_):
        return index

    monkeypatch.setattr(supabase_store, "enabled", lambda: True)
    monkeypatch.setattr(scan_uploads, "transition", fake_transition)
    monkeypatch.setattr(home_registry, "load_index_async", fake_load)
    monkeypatch.setattr(rt, "_STAGED_POLL_SECONDS", 0.01)
    assert await rt._await_staged_index("h", 5.0) is index
    assert seen == ["read", "read", "read"]


@pytest.mark.asyncio
async def test_the_wait_gives_up_quietly(monkeypatch):
    monkeypatch.setattr(rt, "_STAGED_POLL_SECONDS", 0.01)
    monkeypatch.setattr(supabase_store, "enabled", lambda: False)
    assert await rt._await_staged_index("h", 5.0) is None          # nothing to wait on locally
    monkeypatch.setattr(supabase_store, "enabled", lambda: True)

    async def none(home_id, action, **_):
        return None

    monkeypatch.setattr(scan_uploads, "transition", none)
    assert await rt._await_staged_index("h", 5.0) is None          # no upload known

    async def failed(home_id, action, **_):
        return {"progress": {"status": "failed"}}

    monkeypatch.setattr(scan_uploads, "transition", failed)
    assert await rt._await_staged_index("h", 5.0) is None          # waiting will not help

    async def boom(home_id, action, **_):
        raise RuntimeError("db down")

    monkeypatch.setattr(scan_uploads, "transition", boom)
    assert await rt._await_staged_index("h", 5.0) is None

    async def forever(home_id, action, **_):
        return {"progress": {"status": "running"}}

    monkeypatch.setattr(scan_uploads, "transition", forever)
    started = asyncio.get_event_loop().time()
    assert await rt._await_staged_index("h", 0.05) is None          # bounded
    assert asyncio.get_event_loop().time() - started < 1.0
    assert await rt._await_staged_index(None, 5.0) is None


def test_a_thread_that_opened_blind_says_when_the_scan_arrives():
    state = FlowState(thread_id="t", client_flow_aware=True, opening_delivered=True, user_turns=1)
    arrived = _directives(state, scan_visible=True, scan_arrived=True)
    assert "THE SCAN HAS NOW COME THROUGH" in arrived
    state.scan_mesh_bounds = {**BOUNDS, "roomCount": 0}
    still = _directives(state, scan_visible=False)
    assert "YOU STILL CANNOT SEE THIS SCAN" in still and "you DO have the measurements above" in still
    assert "Never say the scan cannot measure" in still
    normal = _directives(state)
    assert "CANNOT SEE THIS SCAN" not in normal and "COME THROUGH" not in normal


# ------------------------------------------------------------ what it is, without photos
def test_the_homeowners_word_makes_a_capture_exterior_on_the_packet_path():
    state = FlowState(thread_id="t", client_flow_aware=True, opening_delivered=True, user_turns=1)
    request = _request("Quintin\n\nAnd I scanned the outside of my house and driveway.", home_id=None)
    rt._reconcile_home(state, request)
    assert state.scan_appearance == {"setting": "exterior", "source": "homeowner", "structure": ""}
    text = _directives(state, scan_visible=False)
    assert "THIS CAPTURE IS THE EXTERIOR OF THE HOUSE, not a room" in text
    # A capture with RoomPlan rooms is not relabelled on a passing mention.
    inside = FlowState(thread_id="t2")
    rt._reconcile_home(inside, _request("it's bright like the outside", packet=_packet(rooms=1), home_id=None))
    assert inside.scan_appearance is None


def test_an_indexed_area_with_no_room_in_it_becomes_exterior_on_their_word(monkeypatch):
    index = HomeIndex([_area()], bundle_id="h")
    monkeypatch.setattr(home_registry, "load_index", lambda home_id: index)
    monkeypatch.setattr(home_registry, "room_context_for", lambda h, k: None)
    state = FlowState(thread_id="t", client_flow_aware=True, opening_delivered=True, user_turns=1)

    before = _directives(state, index=index)
    assert "SCANNED AREAS" in before and "no room structure found in this area" in before
    assert "WHOLE-HOME SCAN" not in before and "~0 sq ft" not in before

    request = _request("It's the exterior of the garage that I scanned")
    assert rt._reconcile_home(state, request) is index
    assert state.active_room_key == "room-1", "the lone exterior capture is put in focus"
    rt._remember_mesh_bounds(state, request)
    rt._remember_scan_surfaces(state, request)
    text = _directives(state, index=index)
    assert "EXTERIOR CAPTURE. This homeowner scanned the outside of their property." in text
    assert "ACTIVE CAPTURE: the exterior — the outside of the building, not a room" in text
    assert "the scan spans about 92 x 102 ft" in text
    assert "MEASURED FROM THE SCAN" in text
    assert "SHAPE ONLY" not in text and "~0 sq ft" not in text and "WHOLE-HOME SCAN" not in text

    # A real room is never relabelled by the phrase.
    kitchen = Room(key="room-1", index=1, storey=0, plan_label="kitchen", area_sqft=180.0, floor_y=0.0,
                   polygon=[(0, 0), (4, 0), (4, 4), (0, 4)], wall_count=4, display_name="kitchen")
    real = HomeIndex([kitchen], bundle_id="h2")
    monkeypatch.setattr(home_registry, "load_index", lambda home_id: real)
    other = FlowState(thread_id="t3")
    rt._reconcile_home(other, _request("I scanned the outside too", home_id="h2"))
    assert other.scan_appearance is None


def test_an_index_with_no_room_in_it_does_not_open_as_a_whole_home(monkeypatch):
    index = HomeIndex([_area()], bundle_id="h")
    monkeypatch.setattr(home_registry, "room_context_for", lambda h, k: None)
    text = _directives(FlowState(thread_id="t", client_flow_aware=True, home_id="h"), opening=True, index=index)
    assert "there is no room layout in it" in text and "ask what they scanned" in text
    assert "They walked their WHOLE HOME" not in text and "naming the room you can see" not in text


def test_several_exterior_areas_are_one_exterior_capture_not_a_whole_home(monkeypatch):
    rooms = [_area(1, display_name="exterior", role="exterior"), _area(2, display_name="exterior", role="exterior")]
    index = HomeIndex(rooms, bundle_id="h")
    monkeypatch.setattr(home_registry, "room_context_for", lambda h, k: {
        "setting": "exterior", "structure": "house", "surfaces": {}, "objects": [], "style": "", "notable": []})
    assert index.as_text() == (
        "- exterior (area 1): the outside of the building\n- exterior (area 2): the outside of the building"
    )
    state = FlowState(thread_id="t", client_flow_aware=True, home_id="h")
    text = _directives(state, opening=True, index=index)
    assert "EXTERIOR CAPTURE. This homeowner scanned the outside of their property, in 2 areas." in text
    assert "They walked their WHOLE HOME" not in text
    assert "the outside of their house" in text


# ------------------------------------------------------------ measurements from the mesh
def test_scanned_surfaces_reach_the_agent_as_measurements():
    state = FlowState(thread_id="t", client_flow_aware=True, opening_delivered=True, user_turns=1)
    request = _request(home_id=None)
    rt._remember_mesh_bounds(state, request)
    rt._remember_scan_surfaces(state, request)
    assert state.scan_surfaces["uprightSquareMeters"] == 107.0
    feet = surfaces_feet(state)
    assert round(feet["upright"]) == 1152 and round(feet["ground"]) == 904 and round(feet["height"]) == 18
    text = _directives(state)
    assert "MEASURED FROM THE SCAN (LiDAR mesh, what the walk actually covered): about 1,152 sq ft of upright surface" in text
    assert "about 904 sq ft of level ground" in text
    assert "NEVER say nothing was measured" in text
    assert "say 'the scan covers', never 'the house is'" in text
    assert "NEVER tell the homeowner that a provider will have to measure" in text

    # An interior capture keeps RoomPlan's numbers; the mesh sum is not shown.
    inside = FlowState(thread_id="t2")
    request = _request(packet=_packet(rooms=2), home_id=None)
    rt._remember_mesh_bounds(inside, request)
    rt._remember_scan_surfaces(inside, request)
    assert surfaces_feet(inside) is None
    # Garbage from the client is ignored.
    junk = FlowState(thread_id="t3")
    bad = HomeAIContextPacket(meshSummary={"surfaces": {"uprightSquareMeters": -4, "groundSquareMeters": "x"}})
    rt._remember_scan_surfaces(junk, _request(packet=bad, home_id=None))
    assert junk.scan_surfaces is None


@pytest.mark.asyncio
async def test_power_washing_is_priced_from_the_scanned_surface(monkeypatch):
    monkeypatch.setattr(settings, "agent_price_guidance_enabled", True)
    monkeypatch.setattr(settings, "price_research_enabled", False)
    state = FlowState(thread_id="t", client_flow_aware=True, opening_delivered=True, user_turns=3)
    state.slots.project_type = "Power Washing"
    request = _request("About how much would it cost?", home_id=None)
    typical, asked = await rt._maybe_price_guidance(state, request, None)
    assert asked and "nothing measured yet" in typical.basis

    rt._remember_mesh_bounds(state, request)
    rt._remember_scan_surfaces(state, request)
    measured, _ = await rt._maybe_price_guidance(state, request, None)
    assert "≈2,060 sq ft of surface measured from the scan" in measured.basis
    assert "≈1,152 upright" in measured.basis and "≈904 level ground" in measured.basis
    assert "nothing measured" not in measured.basis
    assert 150 <= measured.lowUsd < measured.highUsd <= 2056 * 0.45 * 1.2
    again, _ = await rt._maybe_price_guidance(state, request, None)
    assert (again.lowUsd, again.highUsd) == (measured.lowUsd, measured.highUsd), "pinned"
    # A trade not priced by surface is untouched by the measurement.
    assert compute_price_guidance("Interior Painting", None, surface_sqft=2000) is not None
    assert "surface measured" not in compute_price_guidance("Interior Painting", None, surface_sqft=2000).basis


def test_the_lead_package_and_the_ops_email_carry_the_scanned_surfaces():
    from app.flow_api import _add_capture_measurements
    from app.home_context_builder import _compact_mesh_summary

    state = FlowState(thread_id="t", scan_appearance={"setting": "exterior", "structure": "house"},
                      scan_mesh_bounds={**BOUNDS, "roomCount": 0}, scan_surfaces=dict(SURFACES))
    measurements: dict = {}
    _add_capture_measurements(state, measurements)
    assert measurements["scannedSurfacesSquareFeet"] == {"upright": 1152.0, "ground": 904.0, "heightFeet": 18.0}
    lines = "\n".join(ops_email._fmt_measurements(measurements))
    assert "Scanned surfaces (LiDAR mesh, approx.): upright (walls, siding, fences) ~1,152 sq ft, up to 18.0 ft high" in lines
    assert "level ground (drive, walks, patio, any lawn scanned) ~904 sq ft" in lines
    assert "Scan extent (LiDAR mesh, approx.)" in lines
    compact = _compact_mesh_summary({"surfaces": dict(SURFACES)})
    assert round(compact["scannedSurfaces"]["uprightSqFt"]) == 1152
    assert "scannedSurfaces" not in _compact_mesh_summary({"rawAnchorCount": 2})


# ------------------------------------------------------------ never send them a site visit
@pytest.mark.parametrize("text,trips", [
    ("A provider will measure on site for the real number.", True),
    ("They'll want an in-person visit before quoting.", True),
    ("Someone can come out to measure next week.", True),
    ("Submitting the request doesn't require an in-home visit, providers price it from the details you've shared.", False),
    ("Since providers already get the scan, we can ask for pricing without starting with an in-home estimate.", False),
    ("You're not being asked for an on-site estimate.", False),
])
def test_a_reply_never_sends_a_provider_out_to_measure(text, trips):
    rules = [v.rule for v in enforcement.check(text, GateDecision())]
    assert ("onsite_visit_suggested" in rules) is trips
    if trips:
        assert "on site or in person" in enforcement.correction_instruction(
            [v for v in enforcement.check(text, GateDecision()) if v.rule == "onsite_visit_suggested"])


# ------------------------------------------------------------ the scope question, asked once
@pytest.mark.parametrize("message,intent", [
    ("I mainly just wanna focus on these screened in porch, but I am not sure what else I should add", ScopeIntent.SINGLE_ROOM),
    ("Just the porch", ScopeIntent.SINGLE_ROOM),
    ("only the kitchen for now", ScopeIntent.SINGLE_ROOM),
    ("just the kitchen and the hall bath", None),
    ("mostly the whole house really", ScopeIntent.WHOLE_HOME),
    ("I just love the light in here", None),
])
def test_a_named_space_answers_the_scope_question(message, intent):
    assert rt._detect_scope_intent(message) is intent


def test_a_repeated_scope_question_is_removed_and_an_uninvited_one_is_counted():
    reply = ("A dark wood console under the TV would ground the space nicely against the wicker. "
             "Are we keeping this project just to the porch, or do you want other rooms folded in too?")
    settled = FlowState(thread_id="t", opening_delivered=True, user_turns=4, scope_intent=ScopeIntent.SINGLE_ROOM)
    plan = rt._engine.plan_turn(settled, "Just the porch")
    assert rt._strip_repeat_scope_question(settled, plan, reply) == (
        "A dark wood console under the TV would ground the space nicely against the wicker.")

    open_question = FlowState(thread_id="t2", opening_delivered=True, user_turns=4)
    plan = rt._engine.plan_turn(open_question, "ok")
    assert rt._strip_repeat_scope_question(open_question, plan, reply) == reply, "a first ask stays"
    # Asked without the gate inviting it: it still spends the budget.
    plan.gates.can_ask_scope = False
    rt._record_asks_and_wordings(open_question, plan, reply, opening=False)
    assert open_question.scope_asks == 1
    plan = rt._engine.plan_turn(open_question, "hmm")
    assert rt._strip_repeat_scope_question(open_question, plan, reply).endswith("against the wicker.")
    # A reply that is only the question is left alone rather than emptied.
    only = "Are we keeping this to the porch, or are other rooms part of the plan too?"
    assert rt._strip_repeat_scope_question(settled, plan, only) == only


# ------------------------------------------------------------ a dropped word in a reply
OPENER = ("I'm TakeShape's AI assistant for your home, and I've walked through what you available, "
          "two spaces totaling around 850 square feet, including your living room with its sofa and stairs.")
FIXED = OPENER.replace("what you available", "what you have available")


def test_a_proofread_may_only_make_small_grammatical_repairs():
    from app.flow.proofread import accept

    assert accept(OPENER, FIXED)
    assert accept("Siding and the walkway both get get washed in one visit.",
                  "Siding and the walkway both get washed in one visit.")
    assert accept("Two spaces total around 850 square feet.", "Two spaces totaling around 850 square feet.")
    assert not accept(OPENER, OPENER), "no change is nothing to apply"
    assert not accept(OPENER, FIXED.replace("850", "950")), "numbers are not grammar"
    assert not accept(OPENER, FIXED.replace("sofa", "sectional")), "content words stay"
    assert not accept(OPENER, FIXED + " It is a beautiful space."), "nothing is added"
    assert not accept(OPENER, "I have walked through the home that you have made available to me."), "not a rewrite"
    assert not accept("One line.\nTwo lines here.", "One line. Two lines are here."), "line breaks stay"


@pytest.mark.asyncio
async def test_the_opener_gets_a_second_read_and_nothing_else_does_by_default(monkeypatch):
    from app.flow import proofread as pr

    monkeypatch.setattr(settings, "anthropic_api_key", "k")
    monkeypatch.setattr(settings, "proofread_scope", "opening")
    calls = []

    async def fixes(text):
        calls.append(text)
        return f"<message>\n{FIXED}\n</message>"

    assert await pr.proofread(OPENER, opening=True, caller=fixes) == FIXED
    assert await pr.proofread(OPENER, opening=False, caller=fixes) == OPENER and len(calls) == 1
    monkeypatch.setattr(settings, "proofread_scope", "all")
    assert await pr.proofread(OPENER, opening=False, caller=fixes) == FIXED

    async def rewrites(text):
        return "Welcome! Your home is 950 square feet and lovely."

    assert await pr.proofread(OPENER, opening=True, caller=rewrites) == OPENER, "a rewrite is refused"

    async def boom(text):
        raise RuntimeError("provider down")

    assert await pr.proofread(OPENER, opening=True, caller=boom) == OPENER
    monkeypatch.setattr(settings, "proofread_scope", "off")
    assert await pr.proofread(OPENER, opening=True, caller=fixes) == OPENER
    monkeypatch.setattr(settings, "proofread_scope", "all")
    monkeypatch.setattr(settings, "anthropic_api_key", "")
    assert await pr.proofread(OPENER, opening=True, caller=fixes) == OPENER, "no key, no call"


def test_switching_homes_cannot_reuse_the_previous_exterior_measurements(monkeypatch):
    monkeypatch.setattr(home_registry, "load_index", lambda home_id: None)
    state = FlowState(thread_id="switch", home_id="old-home", scan_surfaces=dict(SURFACES))
    rt._reconcile_home(state, _request(home_id="new-home", packet=HomeAIContextPacket()))
    assert state.home_id == "new-home"
    assert state.scan_surfaces is None


@pytest.mark.parametrize("original,edited", [
    ("A provider will not need an on-site visit.", "A provider will need an on-site visit."),
    ("We can request quotes using this scan.", "We will request quotes using this scan."),
    ("That paint looks worn in the scan.", "That painful looks worn in the scan."),
    ("No, no visit is required.", "No, visit is required."),
    ("This doesn't require a visit.", "This does require a visit."),
])
def test_proofreading_cannot_change_negation_promises_or_content(original, edited):
    from app.flow.proofread import accept
    assert not accept(original, edited)
