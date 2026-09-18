"""The agent said the card was there and it wasn't (Sep 13):

    agent: I'll put together the request for the theater room now, and you
           can confirm it on the card when it's ready.
    homeowner: is it ready?
    agent: It's on the request card now, just waiting on your tap of Confirm.

No card had ever been rendered. The gates decide whether a card MAY exist;
nothing checked the sentences ABOUT the card against whether one does. Now
`state.request_card_delivered` records the card that actually shipped, and
the enforcement pass treats a claim without one as a violation.
"""

import pytest

import app.flow_runtime as flow_runtime
from app.flow import enforcement
from app.flow.machine import GateDecision
from app.flow.state import FlowState, Slots
from app.home_ai import (
    HomeAIChatMessage,
    HomeAIChatRequest,
    HomeAIChatResponse,
    HomeAIConversationState,
)

CLAIM = "It's on the request card now, just waiting on your tap of Confirm."


class TestTheRule:
    def test_the_claim_trips_when_no_card_is_on_screen(self):
        v = enforcement.check(CLAIM, GateDecision(), card_on_screen=False)
        assert [x.rule for x in v] == ["request_card_claimed_but_absent"]

    def test_promising_a_card_trips_too(self):
        v = enforcement.check(
            "I'll put this together and you can confirm it on the card when it's ready.",
            GateDecision(),
            card_on_screen=False,
        )
        assert [x.rule for x in v] == ["request_card_claimed_but_absent"]

    def test_the_same_sentence_is_fine_with_a_card_on_screen(self):
        assert enforcement.check(CLAIM, GateDecision(), card_on_screen=True) == []

    def test_offering_to_put_a_request_together_is_not_a_card_claim(self):
        assert (
            enforcement.check(
                "Want me to put this together for the TakeShape team to price?",
                GateDecision(),
                card_on_screen=False,
            )
            == []
        )


def _state(thread_id: str) -> FlowState:
    """Mid-conversation, nothing agreed to, so no card can exist."""
    return FlowState(
        thread_id=thread_id,
        opening_delivered=True,
        user_turns=4,
        client_flow_aware=True,
        slots=Slots(project_type="Painting", scope_options=["walls only"]),
    )


@pytest.mark.asyncio
async def test_a_card_claim_with_no_card_never_reaches_the_homeowner(monkeypatch, tmp_path):
    monkeypatch.setattr(flow_runtime.settings, "storage_dir", str(tmp_path))
    calls = []

    async def claiming_generate(request, flow_directives=None, max_images_override=None):
        calls.append(flow_directives)
        return HomeAIChatResponse(
            threadId=request.threadId,
            message=HomeAIChatMessage(role="assistant", content=CLAIM),
            state=HomeAIConversationState(intent="quote_readiness"),
            model="stub",
            provider="stub",
        )

    monkeypatch.setattr(flow_runtime, "generate_home_ai_response", claiming_generate)
    await flow_runtime.persist_flow_state(_state("t-claim"))

    response = await flow_runtime.run_flow_turn(
        HomeAIChatRequest(threadId="t-claim", message="is it ready?")
    )
    # One regeneration with the correction, then deterministic safe copy.
    assert len(calls) == 2
    assert "no request card" in calls[1].lower()
    assert response.message.content == flow_runtime._SAFE_NO_CARD_COPY
