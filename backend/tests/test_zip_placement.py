"""SOW §2: the zip request is trialled across wordings AND placements, and
the journal records which produced an answer."""

from app.flow.machine import ENGAGED_USER_TURNS, FlowEngine
from app.flow.state import FlowState, Slots
from app.flow.wording import ZIP_WORDINGS, assign_zip_wording, wording_by_id


def _state(wording_id: str, turns: int) -> FlowState:
    return FlowState(thread_id="t", opening_delivered=True, zip_wording_id=wording_id,
                     slots=Slots(first_name="Dana"), user_turns=turns)


def test_zip_wordings_carry_two_placements():
    placements = {w.id: w.min_user_turns for w in ZIP_WORDINGS}
    assert set(placements.values()) == {ENGAGED_USER_TURNS, ENGAGED_USER_TURNS + 1}
    assert all(wording_by_id(w.id) is w for w in ZIP_WORDINGS)


def test_the_later_placement_waits_one_more_turn():
    engine = FlowEngine()
    early = next(w for w in ZIP_WORDINGS if w.min_user_turns == ENGAGED_USER_TURNS)
    late = next(w for w in ZIP_WORDINGS if w.min_user_turns > ENGAGED_USER_TURNS)
    assert engine.evaluate_gates(_state(early.id, ENGAGED_USER_TURNS), "nice room").can_ask_zip is True
    assert engine.evaluate_gates(_state(late.id, ENGAGED_USER_TURNS), "nice room").can_ask_zip is False
    assert engine.evaluate_gates(_state(late.id, ENGAGED_USER_TURNS + 1), "nice room").can_ask_zip is True
    # A pricing question still brings the ask forward, whatever the placement.
    assert engine.evaluate_gates(_state(late.id, 1), "what would this cost near me?").can_ask_zip is True
    # A state with no assigned wording (older threads) keeps the original threshold.
    assert engine.evaluate_gates(_state(None, ENGAGED_USER_TURNS), "nice room").can_ask_zip is True


def test_assignment_is_sticky_and_covers_every_variant():
    assert assign_zip_wording("thread-x") is assign_zip_wording("thread-x")
    assert {assign_zip_wording(f"t{i}").id for i in range(80)} == {w.id for w in ZIP_WORDINGS}
