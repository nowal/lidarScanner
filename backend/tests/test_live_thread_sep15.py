"""The live thread of Sep 15 (phone-quintin--mu1yxebm), eighteen turns that
never produced a request card. Each test replays the homeowner's and the
agent's actual words.

#77  the offer and the yes were both missed, so no card ever came
#78  "blue is the color direction" turned an upholstery job into Painting
#79  the zip ask promised local trends that nothing could deliver
#80  a request missing work scope got a which-rooms question instead
"""

from types import SimpleNamespace

import pytest

import app.flow_runtime as flow_runtime
from app.flow import FlowEngine
from app.flow.state import FlowState, ScopeIntent
from app.home_ai import HomeAIChatRequest


def _state(thread_id: str = "t", **kwargs) -> FlowState:
    """Where the thread stood from turn 8: everything but the work scope."""
    state = FlowState(thread_id=thread_id, opening_delivered=True, user_turns=10)
    state.slots.first_name = "Chance"
    state.slots.project_type = "Painting"
    state.slots.zip = "45458"
    state.slots.contact_email = "homeowner@example.com"
    state.client_flow_aware = True
    for key, value in kwargs.items():
        setattr(state, key, value)
    return state


# ------------------------------------------------------------------- #77
class TestTheCardComes:
    @pytest.mark.parametrize(
        "offer",
        [
            "Right, my miss. Living room it is, walls and trim. I can put that "
            "together as a request now if you'd like to see it.",
            "Want me to put together the living room walls and trim request now, "
            "so it's ready for you to review?",
            "Want me to go ahead and draft the request now for the living room "
            "walls and trim?",
        ],
    )
    def test_the_offers_in_the_thread_are_offers(self, offer):
        assert flow_runtime._REQUEST_OFFER.search(offer)

    def test_asking_to_generate_a_request_is_asking_for_it(self):
        assert flow_runtime._accepts_request(
            _state(), "can you generate a request for me to confirm?"
        )

    def test_a_yes_to_an_offer_made_before_scope_was_in_counts(self):
        engine = FlowEngine()
        state = _state()   # no scope yet: the offer gate is shut
        plan = engine.plan_turn(state, "living room - i said that earlier")
        assert plan.gates.can_offer_request_package is False
        flow_runtime._record_asks_and_wordings(
            state, plan,
            "I can put that together as a request now if you'd like to see it.",
            opening=False,
        )
        assert state.request_offers == 0   # no budget spent
        assert flow_runtime._accepts_request(state, "yes; let's do that")

        state.request_accepted = True
        state.slots.scope_options = ["walls and trim"]
        assert engine.evaluate_gates(state, "walls and trim").can_present_request_card

    def test_a_yes_to_an_unrelated_question_is_still_not_acceptance(self):
        state = _state()
        flow_runtime._record_asks_and_wordings(
            state, FlowEngine().plan_turn(state, "hm"),
            "Want a deeper navy or a teal-blue?", opening=False,
        )
        assert not flow_runtime._accepts_request(state, "yes")


@pytest.mark.asyncio
async def test_asking_for_the_request_brings_the_card_that_turn(monkeypatch, tmp_path):
    monkeypatch.setattr(flow_runtime.settings, "storage_dir", str(tmp_path))
    state = _state("t-gen")
    state.slots.scope_options = ["walls and trim"]
    await flow_runtime.persist_flow_state(state)

    response = await flow_runtime.run_flow_turn(
        HomeAIChatRequest(threadId="t-gen", message="can you generate a request for me to confirm?")
    )
    assert response.quoteDraft is not None
    assert response.flow.gates["canPresentRequestCard"] is True


# ------------------------------------------------------------------- #78
class TestNotPainting:
    def test_a_colour_does_not_make_upholstery_a_paint_job(self):
        state = FlowState()
        state.slots.project_type = "upholstery replacement"
        delta = flow_runtime._apply_capture(state, {}, "blue is the color direction")
        assert state.slots.project_type == "upholstery replacement"
        assert "projectType" not in delta

    def test_first_capture_ignores_weak_words_in_the_message(self):
        state = FlowState()
        flow_runtime._apply_capture(
            state, {"projectType": "sofa refresh"}, "fresh upholstery, blue color"
        )
        assert state.slots.project_type == "sofa refresh"

    def test_a_named_trade_still_upgrades(self):
        state = FlowState()
        state.slots.project_type = "bedroom refresh"
        flow_runtime._apply_capture(state, {}, "paint, new bed")
        assert state.slots.project_type == "Painting"


# ------------------------------------------------------------------- #79
def _zip_directives(monkeypatch, research_on: bool) -> str:
    monkeypatch.setattr(flow_runtime.settings, "local_context_enabled", research_on)
    state = FlowState(
        thread_id="t", opening_delivered=True, user_turns=3,
        zip_wording_id="step4.zip.local_styles_v1",
    )
    plan = FlowEngine().plan_turn(state, "fresh upholstery")
    assert plan.gates.can_ask_zip
    return flow_runtime._build_directives(
        state, plan, opening=False, price_guidance=None, quotes_to_present=None
    )


def test_the_zip_ask_promises_no_local_trends_without_research(monkeypatch):
    text = _zip_directives(monkeypatch, research_on=False)
    assert "what's popular" not in text
    assert "no local trend data" in text


def test_the_zip_ask_keeps_its_local_pitch_when_research_is_on(monkeypatch):
    assert "what's popular" in _zip_directives(monkeypatch, research_on=True)


@pytest.mark.asyncio
async def test_a_zip_in_the_same_message_reaches_local_research(monkeypatch):
    from app.flow import local_research

    monkeypatch.setattr(flow_runtime.settings, "local_context_enabled", True)
    seen = []

    async def lookup(service, zip_code):
        seen.append(zip_code)
        return SimpleNamespace(region_label="Dayton, OH", style_notes=["x"], practical_notes=[])

    monkeypatch.setattr(local_research, "lookup_local_context", lookup)
    state = FlowState(thread_id="t")
    state.slots.project_type = "upholstery replacement"
    context, _ = await flow_runtime._maybe_local_research(
        state, HomeAIChatRequest(threadId="t", message="45458 - yeah what's trending in my area")
    )
    assert seen == ["45458"]
    assert context is not None


@pytest.mark.asyncio
async def test_a_budget_figure_is_not_a_zip(monkeypatch):
    from app.flow import local_research

    monkeypatch.setattr(flow_runtime.settings, "local_context_enabled", True)
    seen = []

    async def lookup(service, zip_code):
        seen.append(zip_code)

    monkeypatch.setattr(local_research, "lookup_local_context", lookup)
    state = FlowState(thread_id="t")
    state.slots.project_type = "Painting"
    await flow_runtime._maybe_local_research(
        state, HomeAIChatRequest(threadId="t", message="what's popular around here for a 15000 budget?")
    )
    assert seen == []


# ------------------------------------------------------ PR #81 review
def test_offering_something_else_does_not_arm_the_yes():
    state = _state()
    assert not flow_runtime._REQUEST_OFFER.search("Want me to put together a color palette?")
    flow_runtime._record_asks_and_wordings(
        state, FlowEngine().plan_turn(state, "hm"),
        "Want me to put together a color palette?", opening=False,
    )
    assert not flow_runtime._accepts_request(state, "yes")


@pytest.mark.parametrize(
    "message",
    ["Can I make a request? I'd rather avoid gray", "don't generate a request yet"],
)
def test_idioms_and_negations_are_not_asking_for_the_request(message):
    assert not flow_runtime._accepts_request(_state(), message)


# ------------------------------------------------------------------- #80
def test_no_rooms_question_once_they_asked_for_the_request():
    state = _state(request_accepted=True)
    assert state.scope_intent is ScopeIntent.UNDECIDED
    assert FlowEngine().evaluate_gates(state, "can you generate a request?").can_ask_scope is False


def test_missing_work_scope_is_named_as_work_not_rooms():
    state = _state()
    plan = FlowEngine().plan_turn(state, "can you generate a request for me to confirm?")
    text = flow_runtime._build_directives(
        state, plan, opening=False, price_guidance=None, quotes_to_present=None
    )
    assert "what the work itself covers (parts of the job, not which rooms)" in text
