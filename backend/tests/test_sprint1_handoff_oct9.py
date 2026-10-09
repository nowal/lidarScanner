"""The provider handoff, as agreed on the Oct 9 call (Sprint 1 close-out).

Quintin's last round (Oct 5-8) kept hitting the same wall: the agent said a
provider "has to see the space", asked for a size it could not use once
given ("my driveway is about 15 feet wide" changed nothing), and borrowed
a Power Washing range for a concrete job. The handoff agreed:

1. Nobody comes to look. Providers take their measurements from the scan;
   the homeowner has nothing more to measure. Every directive and the safe
   copy say that, none says "see the space".
2. A missing measurement gets a clause and a rough range, not another
   question. Self-measuring on the 3D model is mentioned only when that
   tool exists (it does not yet).
3. Sizes the homeowner types are kept, count as measured, go on the
   request, and drive the surface band.
4. A trade outside the catalog gets no range at all, borrowed or otherwise.
5. The rubrics stay off the directive this sprint (fewer questions).
"""

from __future__ import annotations

import pytest

import app.flow_runtime as rt
from app.config import settings
from app.flow_quotes import EXPECTATIONS
from app.flow.pricing import area_from_measurements, compute_price_guidance, parse_measurements
from app.flow.state import FlowState
from app.home_ai import HomeAIChatRequest, HomeAIContextPacket
from app.home_guide_tools import detect_service_type
from app.home_index import HomeIndex

SEE_IT = ("see the space", "seen the space", "see the area", "see it in person", "come out and",
          "has to see", "needs to see", "need to see", "walk through", "walkthrough with")


def _sends_someone_to_look(text: str) -> list[str]:
    """Lines that affirm a visit; the NEVER rules that forbid one are not it."""
    hits = []
    for line in text.splitlines():
        low = line.lower()
        if "never" in low or " no " in f" {low} " or "without" in low:
            continue
        hits += [p for p in SEE_IT if p in low]
    return hits


def _request(message: str = "hi") -> HomeAIChatRequest:
    return HomeAIChatRequest(threadId="t", message=message, homeContext=HomeAIContextPacket(), homeId=None)


def _state(**kw) -> FlowState:
    base = dict(thread_id="t", client_flow_aware=True, opening_delivered=True, user_turns=3)
    base.update(kw)
    return FlowState(**base)


def _directives(state: FlowState, message: str = "ok", **kw) -> str:
    plan = rt._engine.plan_turn(state, message)
    return rt._build_directives(state, plan, opening=False, price_guidance=kw.pop("price_guidance", None),
                                quotes_to_present=kw.pop("quotes_to_present", None),
                                home_index=kw.pop("home_index", HomeIndex([], bundle_id="h")), **kw)


# --------------------------------------------------------- 1. nobody comes to look
def test_no_directive_and_no_safe_copy_sends_a_provider_to_look():
    copy = EXPECTATIONS["copy"]
    for text in (rt._SAFE_NO_PRICE_COPY, copy):
        assert not _sends_someone_to_look(text), text
    assert "pricing it from your scan" in rt._SAFE_NO_PRICE_COPY
    assert "measurements from your scan" in copy and "nothing more for you to measure" in copy


def test_the_standing_rules_say_providers_measure_from_the_scan():
    state = _state()
    state.slots.project_type = "Painting"
    text = _directives(state)
    assert "WHEN A MEASUREMENT OR DETAIL IS MISSING" in text
    assert "take their measurements from the 3D scan they already made" in text
    assert "no tape measure and no walkthrough for them" in text
    assert "never more than one question per reply" in text
    assert not _sends_someone_to_look(text)


def test_self_measuring_is_offered_as_an_option_and_can_be_hidden(monkeypatch):
    """Noah's two parts (Oct 9): providers measure from the model, and the
    homeowner is welcome to measure it themselves if they want more accuracy."""
    state = _state()
    state.slots.project_type = "Painting"
    assert settings.model_measure_tool_available is True
    text = _directives(state)
    assert "welcome to measure the 3D model themselves; mention it once, as an option" in text
    assert "take their measurements from the 3D scan they already made" in text
    monkeypatch.setattr(settings, "model_measure_tool_available", False)
    assert "measure the 3D model themselves" not in _directives(state)


def test_the_price_directive_points_at_the_scan_not_a_visit(monkeypatch):
    monkeypatch.setattr(settings, "agent_price_guidance_enabled", True)
    state = _state()
    state.slots.project_type = "Painting"
    guidance = compute_price_guidance("Painting", None)
    assert guidance is not None and "nothing measured yet" in guidance.basis
    # Caught by the price regex, or left to the model's judgement: same handoff.
    for asked in (True, False):
        text = _directives(state, "what would this run me?", price_guidance=guidance, price_asked=asked)
        assert "prices it from their scan for a true price" in text
        assert "providers take their measurements from the 3D scan" in text
        assert "If they volunteer a rough size, use it; do not ask for one" in text
        assert not _sends_someone_to_look(text)


def test_the_request_offer_and_the_presented_draft_carry_the_handoff():
    """Noah (Oct 9): the handoff must be clear in the quote request process."""
    import dataclasses
    state = _state()
    state.slots.project_type = "Painting"
    state.slots.scope_options = ["walls"]
    state.slots.zip = "37203"
    state.slots.contact_email = "h@example.com"
    plan = rt._engine.plan_turn(state, "ok")
    handoff = "providers take their measurements from the 3D scan, so there is nothing more for them to measure"
    # The offer ("shall I put this together?") before they have said yes.
    offered = dataclasses.replace(plan, gates=dataclasses.replace(plan.gates, can_offer_request_package=True))
    text = rt._build_directives(state, offered, opening=False, price_guidance=None,
                                quotes_to_present=None, home_index=HomeIndex([], bundle_id="h"))
    assert handoff in text, text
    # The draft card, once they have.
    state.request_accepted = True
    text = _directives(state)
    assert handoff in text, text


# ---------------------------------------------------- 3. sizes they typed count
@pytest.mark.parametrize("message,phrases,area", [
    ("my driveway is about 15 feet wide", ["about 15 feet wide"], None),
    ("it's 20 by 40 feet", ["20 by 40 feet"], 800.0),
    ("roughly 20' x 40' of concrete", ["20' x 40'"], 800.0),
    ("the slab is 1,200 sq ft", ["1,200 sq ft"], 1200.0),
    ("it is about 15 feet wide and 60 feet long", ["about 15 feet wide", "60 feet long"], None),
    ("no idea how big it is", [], None),
])
def test_sizes_are_parsed_as_typed(message, phrases, area):
    assert parse_measurements(message) == phrases
    assert area_from_measurements(phrases) == area


def test_a_typed_size_is_kept_on_the_thread_and_shown_to_the_agent():
    state = _state()
    state.slots.project_type = "Power Washing"
    rt._precapture_measurements(state, "my driveway is about 15 feet wide")
    rt._precapture_measurements(state, "and about 15 FEET wide, 60 feet long")
    assert state.slots.homeowner_measurements == ["about 15 feet wide", "60 feet long"]
    text = _directives(state)
    assert "MEASUREMENTS THEY GAVE YOU, in their words: about 15 feet wide; 60 feet long" in text
    assert "never say you cannot size it" in text


@pytest.mark.asyncio
async def test_a_typed_area_drives_the_surface_band(monkeypatch):
    monkeypatch.setattr(settings, "agent_price_guidance_enabled", True)
    monkeypatch.setattr(settings, "price_research_enabled", False)
    state = _state()
    state.slots.project_type = "Power Washing"
    typical, _ = await rt._maybe_price_guidance(state, _request("what does it cost?"), None)
    assert "nothing measured yet" in typical.basis

    rt._precapture_measurements(state, "the driveway is 20 by 40 feet")
    given, _ = await rt._maybe_price_guidance(state, _request("what does it cost?"), None)
    assert "≈800 sq ft, the size they gave you" in given.basis
    assert "nothing measured" not in given.basis
    assert 150 <= given.lowUsd < given.highUsd <= 800 * 0.45 * 1.2


# --------------------------------------------- 4. no borrowed range off-catalog
@pytest.mark.parametrize("message,service", [
    ("I want to replace the concrete on my driveway", None),
    ("repave the driveway and widen the walkway", None),
    ("pour a new driveway", None),
    ("power wash the driveway", "Power Washing"),
    ("clean the walkway", "Power Washing"),
    ("the driveway", "Power Washing"),
])
def test_concrete_work_is_not_a_wash(message, service):
    assert detect_service_type(message) == service


def test_an_off_catalog_trade_gets_no_range_at_all(monkeypatch):
    monkeypatch.setattr(settings, "agent_price_guidance_enabled", True)
    assert compute_price_guidance("Concrete", None) is None
    state = _state()
    state.slots.project_type = "Concrete"
    text = _directives(state, "what would a new driveway run?")
    assert "You have NO pricing data for Concrete" in text
    assert "Never borrow a range from another trade" in text
    assert "$" not in text.split("NO pricing data")[1].split("\n")[0]


# ---------------------------------------------------- 5. rubrics stay off
def test_the_rubric_directive_is_off_by_default():
    assert settings.rubric_directive_enabled is False
    state = _state()
    state.slots.project_type = "Painting"
    assert "SERVICE RUBRIC" not in _directives(state)
