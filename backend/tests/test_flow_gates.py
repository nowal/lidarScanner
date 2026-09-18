"""Gate tests. The ones marked SOW §3 are the contractual hard constraint:
the agent must never suggest additional scanning until the first scan's
processing completes, and a client cannot talk the server out of it.
"""

import pytest

from app.flow import (
    FlowEngine,
    FlowState,
    FlowStep,
    FlowTokenCodec,
    InvalidFlowToken,
    ScanProcessingState,
)
from app.flow import enforcement
from app.flow.state import ScanStatus
from app.flow.wording import assign_zip_wording, ZIP_WORDINGS


@pytest.fixture
def engine():
    return FlowEngine()


def make_state(**kwargs) -> FlowState:
    state = FlowState(thread_id="t-1", opening_delivered=True)
    for key, value in kwargs.items():
        setattr(state, key, value)
    return state


# ---------------------------------------------------------------- SOW §3 gate
class TestScanGate:
    @pytest.mark.parametrize(
        "scan_state",
        [
            ScanProcessingState.UNKNOWN,
            ScanProcessingState.IDLE,
            ScanProcessingState.PREPARING_UPLOAD,
            ScanProcessingState.UPLOADING,
            ScanProcessingState.PROCESSING,
            ScanProcessingState.DOWNLOADING,
            ScanProcessingState.FAILED,
        ],
    )
    def test_no_scan_prompt_unless_complete(self, engine, scan_state):
        state = make_state(scan=ScanStatus(state=scan_state))
        gates = engine.evaluate_gates(state, "what else should I do?")
        assert gates.can_prompt_additional_scan is False

    def test_scan_prompt_allowed_when_complete(self, engine):
        state = make_state(scan=ScanStatus(state=ScanProcessingState.COMPLETE))
        gates = engine.evaluate_gates(state, "hi")
        assert gates.can_prompt_additional_scan is True

    def test_server_overrides_client_claim(self, engine):
        """A client claiming 'complete' does not open the gate when the
        server's job store says the job is still running."""
        state = make_state()
        scan = engine.reconcile_scan(
            state,
            client_state=ScanProcessingState.COMPLETE,
            client_job_id="job-1",
            client_scan_id="scan-1",
            client_progress=1.0,
            server_job_state=ScanProcessingState.PROCESSING,
        )
        assert scan.state is ScanProcessingState.PROCESSING
        assert scan.server_verified is True

    def test_legacy_client_keeps_last_known_state(self, engine):
        state = make_state(scan=ScanStatus(state=ScanProcessingState.COMPLETE))
        scan = engine.reconcile_scan(
            state,
            client_state=ScanProcessingState.UNKNOWN,
            client_job_id=None,
            client_scan_id=None,
            client_progress=None,
            server_job_state=None,
        )
        assert scan.state is ScanProcessingState.COMPLETE

    def test_photoreal_status_mapping(self):
        assert ScanProcessingState.from_photoreal_status("ready") is ScanProcessingState.COMPLETE
        assert ScanProcessingState.from_photoreal_status("processing") is ScanProcessingState.PROCESSING
        assert ScanProcessingState.from_photoreal_status("not_started") is ScanProcessingState.IDLE
        assert ScanProcessingState.from_photoreal_status(None) is ScanProcessingState.UNKNOWN
        assert ScanProcessingState.from_photoreal_status("garbage") is ScanProcessingState.UNKNOWN


# ------------------------------------------------------------- ordering rules
class TestOrderingGates:
    def test_zip_not_asked_before_engagement(self, engine):
        state = make_state(user_turns=1)
        gates = engine.evaluate_gates(state, "I like this couch")
        assert gates.can_ask_zip is False

    def test_zip_asked_after_engagement(self, engine):
        state = make_state(user_turns=2)
        gates = engine.evaluate_gates(state, "what color should the walls be?")
        assert gates.can_ask_zip is True

    def test_pricing_signal_unlocks_zip_early(self, engine):
        state = make_state(user_turns=0)
        gates = engine.evaluate_gates(state, "how much would repainting cost?")
        assert gates.can_ask_zip is True

    def test_zip_ask_budget(self, engine):
        state = make_state(user_turns=5, zip_asks=2)
        gates = engine.evaluate_gates(state, "sure")
        assert gates.can_ask_zip is False

    def test_zip_ask_cooldown_one_turn(self, engine):
        state = make_state(user_turns=3)
        engine.record_asks(state, asked_zip=True)  # asked at turn 3
        gates = engine.evaluate_gates(state, "hmm let me think")
        assert gates.can_ask_zip is False  # same turn count → cooling down
        state.user_turns = 4
        gates = engine.evaluate_gates(state, "ok")
        assert gates.can_ask_zip is False  # next turn still cooling
        state.user_turns = 5
        gates = engine.evaluate_gates(state, "ok")
        # Cooldown elapsed but no pricing signal: a second ask needs the
        # homeowner to raise cost/providers themselves.
        assert gates.can_ask_zip is False
        gates = engine.evaluate_gates(state, "what would that cost?")
        assert gates.can_ask_zip is True

    def test_zip_not_asked_once_captured(self, engine):
        state = make_state(user_turns=5)
        state.slots.zip = "37203"
        gates = engine.evaluate_gates(state, "ok")
        assert gates.can_ask_zip is False

    def test_address_not_asked_without_quote_underway(self, engine):
        state = make_state(user_turns=6)
        gates = engine.evaluate_gates(state, "the kitchen needs work")
        assert gates.can_ask_address is False

    def test_address_asked_when_quote_underway(self, engine):
        state = make_state(user_turns=6)
        state.slots.project_type = "Painting"
        state.slots.scope_options = ["walls only"]
        state.slots.zip = "37203"
        state.step = FlowStep.QUOTE_GATHERING
        gates = engine.evaluate_gates(state, "yes let's get quotes")
        assert gates.can_ask_address is True

    def test_address_waits_for_zip(self, engine):
        state = make_state(user_turns=6)
        state.slots.project_type = "Painting"
        state.slots.scope_options = ["walls only"]
        state.step = FlowStep.QUOTE_GATHERING
        gates = engine.evaluate_gates(state, "yes let's get quotes")
        assert gates.can_ask_address is False
        assert gates.reasons["can_ask_address"] == "zip not captured yet"

    def test_submission_requires_full_slots(self, engine):
        state = make_state()
        state.slots.project_type = "Painting"
        state.slots.scope_options = ["walls only"]
        gates = engine.evaluate_gates(state, "submit it")
        assert gates.can_submit_quote_request is False
        assert engine.missing_submission_slots(state) == ["zip", "contact"]
        state.slots.zip = "37203"
        gates = engine.evaluate_gates(state, "submit it")
        # Zip alone is not enough: ops needs a way to reach them.
        assert gates.can_submit_quote_request is False
        assert engine.missing_submission_slots(state) == ["contact"]
        state.slots.contact_email = "dana@example.com"
        gates = engine.evaluate_gates(state, "submit it")
        assert gates.can_submit_quote_request is True

    def test_submission_does_not_require_an_address(self, engine):
        """Holding a request hostage to an address the homeowner won't give
        loses the lead; zip routes it well enough (client decision, Sep 12)."""
        state = make_state()
        state.slots.project_type = "Painting"
        state.slots.scope_options = ["walls only"]
        state.slots.zip = "37203"
        state.slots.contact_email = "dana@example.com"
        assert state.slots.address is None
        gates = engine.evaluate_gates(state, "submit it")
        assert gates.can_submit_quote_request is True
        assert engine.missing_submission_slots(state) == []
        assert engine.missing_submission_slots(state, require_values=True) == []

    def test_homeowner_identity_satisfies_contact(self, engine):
        state = make_state(homeowner_id="ho-123")
        state.slots.project_type = "Painting"
        state.slots.scope_options = ["walls only"]
        state.slots.zip = "37203"
        gates = engine.evaluate_gates(state, "submit it")
        assert gates.can_submit_quote_request is True

    def test_card_withheld_until_the_homeowner_agrees(self, engine):
        """The complaint that started this (Sep 12): a confirm-and-send card
        appearing before anyone asked for one."""
        state = make_state()
        state.slots.project_type = "Painting"
        state.slots.scope_options = ["walls only"]
        state.slots.zip = "37203"
        state.slots.contact_email = "dana@example.com"
        gates = engine.evaluate_gates(state, "what would that run?")
        assert gates.can_submit_quote_request is True
        assert gates.can_present_request_card is False
        assert gates.can_offer_request_package is True
        assert gates.reasons["can_present_request_card"] == (
            "homeowner has not agreed to the request yet"
        )
        state.request_accepted = True
        gates = engine.evaluate_gates(state, "yes please")
        assert gates.can_present_request_card is True
        # Nothing left to offer once they've said yes.
        assert gates.can_offer_request_package is False

    def test_no_card_offer_before_the_slots_are_there(self, engine):
        state = make_state()
        state.slots.project_type = "Painting"
        gates = engine.evaluate_gates(state, "i want this painted")
        assert gates.can_offer_request_package is False
        # An early yes does not conjure a card out of missing slots.
        state.request_accepted = True
        gates = engine.evaluate_gates(state, "yes do it")
        assert gates.can_present_request_card is False

    def test_request_offer_budget(self, engine):
        from app.flow.machine import MAX_REQUEST_OFFERS

        state = make_state(request_offers=MAX_REQUEST_OFFERS)
        state.slots.project_type = "Painting"
        state.slots.scope_options = ["walls only"]
        state.slots.zip = "37203"
        state.slots.contact_email = "dana@example.com"
        gates = engine.evaluate_gates(state, "hmm")
        assert gates.can_offer_request_package is False
        assert gates.can_present_request_card is False

    def test_record_offer_stamps_the_turn(self, engine):
        state = make_state(user_turns=4)
        engine.record_asks(state, offered_request=True)
        assert state.request_offers == 1
        assert state.request_offer_at_turn == 4

    def test_first_name_ask_budget(self, engine):
        state = make_state(first_name_asks=2)
        gates = engine.evaluate_gates(state, "hello")
        assert gates.can_ask_first_name is False


# -------------------------------------------------------------- address privacy
class TestAddressPrivacy:
    def test_address_never_in_client_view(self):
        state = make_state()
        state.slots.address = "123 Main St, Nashville TN"
        view = state.client_view()
        assert "123 Main" not in str(view)
        assert view["slots"]["addressCaptured"] is True


# ----------------------------------------------------------------- enforcement
class TestEnforcement:
    GATED = FlowEngine().evaluate_gates(
        make_state(scan=ScanStatus(state=ScanProcessingState.PROCESSING)), "hi"
    )
    OPEN = FlowEngine().evaluate_gates(
        make_state(
            scan=ScanStatus(state=ScanProcessingState.COMPLETE),
            user_turns=5,
        ),
        "how much would it cost?",
    )

    @pytest.mark.parametrize(
        "text",
        [
            "You could scan another room to give me a fuller picture.",
            "Try capturing the hallway next so we can see the flow.",
            "Feel free to add more rooms of the house when you have a minute.",
            "I'd suggest you rescan the kitchen for better detail.",
            "Once you extend the scan to the bathroom, we can plan there too.",
            "Maybe walk through the rest of the house with your phone.",
        ],
    )
    def test_scan_suggestions_blocked_while_processing(self, text):
        violations = enforcement.check(text, self.GATED)
        assert any(v.rule == "scan_suggestion_while_processing" for v in violations)

    def test_scan_suggestion_allowed_when_complete(self):
        text = "You could scan another room to give me a fuller picture."
        assert enforcement.check(text, self.OPEN) == []

    def test_innocent_text_passes(self):
        text = (
            "A warm off-white would open this room up, and swapping the rug "
            "for something lighter would balance the wood tones."
        )
        assert enforcement.check(text, self.GATED) == []

    def test_premature_address_ask_blocked(self):
        violations = enforcement.check(
            "Could you share your address so we can check drive times?", self.GATED
        )
        assert any(v.rule == "premature_address_ask" for v in violations)

    def test_correction_instruction_mentions_rule(self):
        violations = enforcement.check("Please scan another room.", self.GATED)
        note = enforcement.correction_instruction(violations)
        assert "still being prepared" in note


# ------------------------------------------------------------------- tokens
class TestFlowToken:
    def test_round_trip(self):
        codec = FlowTokenCodec("secret-1")
        state = make_state(user_turns=3)
        state.slots.first_name = "Dana"
        decoded = codec.decode(codec.encode(state))
        assert decoded.user_turns == 3
        assert decoded.slots.first_name == "Dana"

    def test_tamper_detected(self):
        codec = FlowTokenCodec("secret-1")
        token = codec.encode(make_state())
        payload, sig = token.split(".")
        forged = payload[:-2] + ("AA" if not payload.endswith("AA") else "BB") + "." + sig
        with pytest.raises(InvalidFlowToken):
            codec.decode(forged)

    def test_wrong_secret_rejected(self):
        token = FlowTokenCodec("secret-1").encode(make_state())
        with pytest.raises(InvalidFlowToken):
            FlowTokenCodec("secret-2").decode(token)

    def test_garbage_rejected(self):
        with pytest.raises(InvalidFlowToken):
            FlowTokenCodec("secret-1").decode("not-a-token")


# ------------------------------------------------------------------- wording
class TestWording:
    def test_zip_wording_sticky_and_deterministic(self):
        a = assign_zip_wording("thread-123")
        b = assign_zip_wording("thread-123")
        assert a.id == b.id

    def test_zip_wordings_distribute(self):
        seen = {assign_zip_wording(f"t-{i}").id for i in range(50)}
        assert seen == {w.id for w in ZIP_WORDINGS}
