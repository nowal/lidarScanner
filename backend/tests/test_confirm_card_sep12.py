"""The confirm-and-send card arrived too early — sometimes before the zip was
asked for, let alone the address (Sep 12 feedback).

Two rules, tested here:

1. The card is the homeowner's to ask for. The agent offers ("want me to put
   this together for the TakeShape team?"), and nothing exists until they say
   yes. Because every client renders the card on the mere presence of
   ``quoteDraft``, the server drops a draft the model emits before that.
2. The street address no longer holds a request hostage — zip routes the lead.
   Covered on the gate side in test_flow_gates.py; here it is the submission
   path end to end.
"""

import pytest

import app.flow_runtime as flow_runtime
from app.flow import FlowEngine
from app.flow.machine import MAX_REQUEST_OFFERS
from app.flow.state import FlowState
from app.home_ai import HomeAIChatRequest

PRICING_MESSAGE = "What would it cost to paint this room and can you help me get quotes?"


def _ready_state(thread_id: str, **kwargs) -> FlowState:
    """Everything a request needs — and deliberately no address."""
    state = FlowState(thread_id=thread_id, opening_delivered=True, user_turns=4)
    state.slots.first_name = "Dana"
    state.slots.project_type = "Painting"
    state.slots.scope_options = ["walls only"]
    state.slots.zip = "37203"
    state.slots.contact_email = "dana@example.com"
    state.client_flow_aware = True
    for key, value in kwargs.items():
        setattr(state, key, value)
    return state


# ------------------------------------------------------- reading their answer
class TestAcceptance:
    def test_bare_yes_answers_the_offer(self):
        state = _ready_state("t", request_offers=1, request_offer_at_turn=4)
        assert flow_runtime._accepts_request(state, "yes please") is True
        assert flow_runtime._accepts_request(state, "yeah, do it") is True
        assert flow_runtime._accepts_request(state, "sure") is True

    def test_bare_yes_with_no_offer_outstanding_is_not_acceptance(self):
        """"Yeah" to "want to see the ceiling too?" must not authorize a lead
        package."""
        state = _ready_state("t")
        assert flow_runtime._accepts_request(state, "yes") is False

    def test_a_stale_offer_does_not_collect_a_later_yes(self):
        state = _ready_state("t", request_offers=1, request_offer_at_turn=2)
        assert flow_runtime._accepts_request(state, "yeah") is False

    def test_asking_outright_needs_no_offer(self):
        state = _ready_state("t")
        assert flow_runtime._accepts_request(state, "send it to takeshape") is True
        assert flow_runtime._accepts_request(state, "put it together") is True
        assert flow_runtime._accepts_request(state, "go ahead and submit it") is True

    def test_naming_the_request_is_an_ask(self):
        """The 2026-09-14 walk: every one of these was said to an agent that
        had everything it needed, and none of them counted."""
        state = _ready_state("t")
        for ask in (
            "Yes, please put the quote request together.",
            "Yes, go ahead and prepare the request. Everything is correct.",
            "Build the request card now please.",
            "Yes. Show me the request to review.",
            "ok draft the request",
            "send my quote request",
            "put together the living room request",
        ):
            assert flow_runtime._accepts_request(state, ask) is True, ask

    def test_naming_something_else_is_not_an_ask(self):
        state = _ready_state("t")
        for text in (
            "show me the kitchen",
            "can you prepare a shopping list",
            "build a deck off the back",
            "I want to start a project",
            "don't build the request yet",
        ):
            assert flow_runtime._accepts_request(state, text) is False, text

    def test_an_offer_past_the_budget_still_collects_the_yes(self):
        """After MAX_REQUEST_OFFERS the prompt may not offer again, but the
        model sometimes does ("I'll put it together now -- sound good?").
        The homeowner saw an offer; their yes must answer it. No budget is
        spent, so the cap still bounds how often the prompt asks."""
        state = _ready_state("t", request_offers=MAX_REQUEST_OFFERS, request_offer_at_turn=1)
        plan = flow_runtime._engine.plan_turn(state, "ok")
        assert plan.gates.can_offer_request_package is False
        flow_runtime._record_asks_and_wordings(
            state, plan,
            "Everything's set, Dana. I'll put it together as a request for the TakeShape team -- sound good?",
            opening=False,
        )
        assert state.request_offers == MAX_REQUEST_OFFERS
        assert state.request_offer_at_turn == state.user_turns
        assert flow_runtime._accepts_request(state, "yes") is True

    def test_announcing_the_draft_is_an_offer(self):
        """"I'll draft the request now for you to review" is how the model
        offered in the 2026-09-14 walk; the yes that followed went nowhere."""
        state = _ready_state("t", request_offers=MAX_REQUEST_OFFERS, request_offer_at_turn=1)
        plan = flow_runtime._engine.plan_turn(state, "ok")
        flow_runtime._record_asks_and_wordings(
            state, plan,
            "Got it, thanks Dana. I'll draft the request now for you to review before anything goes to a provider.",
            opening=False,
        )
        assert flow_runtime._accepts_request(state, "Yes, please put the quote request together.") is True
        state.request_accepted = False
        assert flow_runtime._accepts_request(state, "yes") is True

    def test_wanting_quotes_is_not_consent_to_send(self):
        """"Can you help me get quotes?" is what prompts the offer. Treating it
        as the yes is how the card got ahead of the conversation."""
        state = _ready_state("t")
        assert flow_runtime._accepts_request(state, PRICING_MESSAGE) is False
        assert flow_runtime._accepts_request(state, "i want to get quotes") is False

    def test_no_double_accept(self):
        state = _ready_state("t", request_accepted=True)
        assert flow_runtime._accepts_request(state, "yes") is False

    def test_declining_is_not_acceptance(self):
        state = _ready_state("t", request_offers=1, request_offer_at_turn=4)
        for reply in ("no thanks", "not yet", "maybe later", "hold off"):
            assert flow_runtime._accepts_request(state, reply) is False


# ------------------------------------------------------------ spending offers
def test_an_offer_in_the_reply_spends_the_budget():
    engine = FlowEngine()
    state = _ready_state("t")
    plan = engine.plan_turn(state, PRICING_MESSAGE)
    assert plan.gates.can_offer_request_package is True
    ids = flow_runtime._record_asks_and_wordings(
        state,
        plan,
        "Want me to put this together as a request for the TakeShape team to price?",
        opening=False,
    )
    assert "step9.request_offer_v1" in ids
    assert state.request_offers == 1
    assert state.request_offer_at_turn == state.user_turns


def test_a_reply_with_no_offer_in_it_spends_nothing():
    engine = FlowEngine()
    state = _ready_state("t")
    plan = engine.plan_turn(state, PRICING_MESSAGE)
    flow_runtime._record_asks_and_wordings(
        state, plan, "Satin holds up better than flat in a room that gets used.", opening=False
    )
    assert state.request_offers == 0


def test_offers_run_out():
    engine = FlowEngine()
    state = _ready_state("t", request_offers=MAX_REQUEST_OFFERS)
    plan = engine.plan_turn(state, PRICING_MESSAGE)
    assert plan.gates.can_offer_request_package is False


# ---------------------------------------------------------- the card itself
@pytest.mark.asyncio
async def test_premature_draft_is_dropped_before_it_reaches_a_client(monkeypatch, tmp_path):
    monkeypatch.setattr(flow_runtime.settings, "storage_dir", str(tmp_path))
    state = _ready_state("t-early")
    await flow_runtime.persist_flow_state(state)

    response = await flow_runtime.run_flow_turn(
        HomeAIChatRequest(threadId="t-early", message=PRICING_MESSAGE)
    )
    # Every essential is captured, so the Confirm control would work...
    assert response.flow.gates["canSubmitQuoteRequest"] is True
    # ...but they have not agreed to a request, so there is no card.
    assert response.flow.gates["canPresentRequestCard"] is False
    assert response.quoteDraft is None


@pytest.mark.asyncio
async def test_a_yes_opens_the_card_on_that_same_turn(monkeypatch, tmp_path):
    monkeypatch.setattr(flow_runtime.settings, "storage_dir", str(tmp_path))
    # An offer went out last turn.
    state = _ready_state("t-yes", request_offers=1, request_offer_at_turn=4)
    await flow_runtime.persist_flow_state(state)

    response = await flow_runtime.run_flow_turn(
        HomeAIChatRequest(threadId="t-yes", message="yes please, send it over")
    )
    assert response.flow.gates["canPresentRequestCard"] is True
    # The model did not write a draft (no model in tests), and the card still
    # came on this turn: the server builds it rather than waiting a turn.
    assert response.quoteDraft is not None
    assert response.quoteDraft.serviceType == "Painting"
    assert "walls only" in response.quoteDraft.scopeNotes
    saved = await flow_runtime.resolve_flow_state("t-yes", None)
    assert saved.request_accepted is True
    assert saved.request_card_delivered is True

    # Once delivered, the server does not append a second card every turn.
    again = await flow_runtime.run_flow_turn(
        HomeAIChatRequest(threadId="t-yes", message="what colour for the trim?")
    )
    assert again.quoteDraft is None


@pytest.mark.asyncio
async def test_no_card_on_the_opening_turn(monkeypatch, tmp_path):
    monkeypatch.setattr(flow_runtime.settings, "storage_dir", str(tmp_path))
    from app.flow_runtime import HomeAIOpeningRequest

    response = await flow_runtime.run_opening_turn(HomeAIOpeningRequest(threadId="t-open"))
    assert response.quoteDraft is None


# ----------------------------------------------------------- address optional
@pytest.mark.asyncio
async def test_submission_goes_through_without_an_address(monkeypatch, tmp_path):
    """The whole point of dropping the address requirement: a homeowner who
    won't give one still gets quotes."""
    monkeypatch.setattr(flow_runtime.settings, "storage_dir", str(tmp_path))
    from app.flow_quotes import create_quote_request

    state = _ready_state("t-noaddr", request_accepted=True)
    engine = FlowEngine()
    assert engine.missing_submission_slots(state, require_values=True) == []

    record = await create_quote_request(
        state, thread_id="t-noaddr", measurements={}, quote_draft=None
    )
    assert record.address is None
    # Ops sees a lead with no address, not a broken package.
    view = record.ops_view(include_address=True)
    assert view["address"] is None
    assert view["homeowner"]["zip"] == "37203"
    assert view["homeowner"]["contact"]["email"] == "dana@example.com"
