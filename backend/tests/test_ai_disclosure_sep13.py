"""The guide never said what it was (#54).

There was no self-disclosure anywhere in the prompt, so "are you a real
person?" got whatever the model improvised that turn. Now it introduces itself
as TakeShape's AI assistant in the opening, answers the question straight, and
an enforcement rule catches the one sentence it may never write.

Speaking as part of TakeShape is fine and stays fine: the agent IS TakeShape's
assistant. The line is only claiming to be a person. That makes the negations
the interesting case, because the CORRECT sentence ("I'm not a real person,
I'm TakeShape's AI assistant") contains the same words as the wrong one.
"""

import pytest

from app.flow import enforcement
from app.flow.machine import GateDecision
from app.flow.state import FlowState
from app.flow_runtime import _build_directives
from app.flow.wording import SAFE_IMPERSONATION_COPY
from app.home_guide_prompt import (
    HOME_GUIDE_PROMPT_VARIANTS,
    assign_home_guide_prompt_variant,
    build_home_guide_system_prompt,
)


def _check(text: str) -> list[str]:
    return [v.rule for v in enforcement.check(text, GateDecision(), card_on_screen=True)]


class TestTheRule:
    @pytest.mark.parametrize(
        "text",
        [
            "I'm a real person, I promise.",
            "I am a real human, just here to help with the room.",
            "I'm human, and I love what you've done with the light in here.",
            "I'm a person on the design side of things.",
            "Honestly, I'm real.",
            "No, I'm not an AI.",
            "I'm not a bot, don't worry.",
            "I am not a chatbot.",
            "I'm not a robot if that's what you're asking.",
            "As a human, I would paint that wall first.",
        ],
    )
    def test_claiming_to_be_human_trips(self, text):
        assert _check(text) == ["human_impersonation"]

    @pytest.mark.parametrize(
        "text",
        [
            "I'm TakeShape's AI assistant for your home.",
            "I'm not a real person, I'm TakeShape's AI assistant for your home.",
            "I'm an AI, not a human, but I'm good on rooms.",
            "I'm not human, I'm the AI that helps with your projects.",
            "The TakeShape team will review this and a person looks at every request.",
            "A person on the team reviews every request before quotes come back.",
            "I'm really glad you asked about the trim.",
            SAFE_IMPERSONATION_COPY,
        ],
    )
    def test_the_honest_sentences_do_not_trip(self, text):
        assert _check(text) == []

    def test_the_correction_names_the_rule(self):
        instruction = enforcement.correction_instruction(
            [enforcement.Violation("human_impersonation", "I'm a real person")]
        )
        assert "AI assistant" in instruction
        assert "human" in instruction


class TestTheDisclosure:
    def test_the_system_prompt_says_what_it_is(self):
        prompt = build_home_guide_system_prompt()
        assert "TakeShape's AI assistant" in prompt
        assert "Never say or imply you are a human" in prompt

    @pytest.mark.parametrize("with_home_index", [False, True])
    def test_the_opening_turn_introduces_itself(self, with_home_index, monkeypatch):
        class _Index:
            rooms = []

            def __getattr__(self, name):
                return None

        monkeypatch.setattr(
            "app.flow_runtime._home_directives", lambda state, index: []
        )
        directives = _build_directives(
            FlowState(thread_id="t1"),
            _plan(),
            opening=True,
            price_guidance=None,
            quotes_to_present=None,
            home_index=_Index() if with_home_index else None,
        )
        assert "OPENING turn" in directives
        assert "TakeShape's AI assistant" in directives


class TestTheVariant:
    """more_direct told the model to push the quote request. It is gone."""

    def test_only_two_variants_remain(self):
        assert set(HOME_GUIDE_PROMPT_VARIANTS) == {"control", "more_design_led"}

    def test_nobody_is_assigned_the_pushy_variant(self):
        assigned = {assign_home_guide_prompt_variant(f"user-{i}") for i in range(200)}
        assert assigned == {"control", "more_design_led"}


def _plan():
    from app.flow.machine import TurnPlan
    from app.flow.state import FlowStep

    return TurnPlan(gates=GateDecision(), step=FlowStep.RECOGNITION)
