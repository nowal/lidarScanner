"""The flow engine: decides, in code, what the agent is allowed to do on a
given turn. The prompt describes *how* to ask; this module decides *whether*.

Ordering rules implemented here (SOW §2/§3, API_CONTRACT_V1 §8):

- The first-name ask happens once, in the opening turn (steps 1–2).
- Zip is asked only after real engagement, with a sticky wording variant so
  answer rates per wording can be measured.
- The agent NEVER suggests additional scanning until the first scan's
  processing is complete — and "complete" prefers the server's own job store
  over anything the client claims.
- The address is asked only once a quote request is underway, but it never
  blocks a request: submission needs project, scope, zip and contact, plus the
  homeowner's yes to the request being put together, plus explicit confirmation
  on the card itself.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .state import FlowState, FlowStep, ScanProcessingState, ScanStatus, ScopeIntent

# A user turn counts as "engaged" once this many substantive messages have
# arrived after the opener. SOW §2 ties the zip ask to "after initial
# engagement"; 2 keeps the first ask small without feeling withheld.
ENGAGED_USER_TURNS = 2

MAX_FIRST_NAME_ASKS = 2   # opener + at most one gentle retry
MAX_ZIP_ASKS = 2          # never badger; §2 wants answer-rate data, not pressure
MAX_ADDRESS_ASKS = 3
MAX_SCOPE_ASKS = 2        # scope is asked lightly, at most twice (docs/SCAN_SCOPE.md)
# Under single_room / selected_rooms, "anything else you'd want to include?"
# is offered at most this many times in the whole conversation.
MAX_EXTENSION_OFFERS = 1
# "Want me to put this together for the TakeShape team?" — asked at most
# twice. The card only appears after they say yes, so the offer is the whole
# path to a request; two tries, then drop it.
MAX_REQUEST_OFFERS = 2

# How step-6 extension prompting is constrained this turn, once (and only
# once) the SOW §3 gate is open. See docs/SCAN_SCOPE.md.
EXTENSION_CLOSED = "closed"            # gate shut: no scan prompt of any kind
EXTENSION_OPEN = "open"                # whole_home / undecided: current behaviour
EXTENSION_NAMED_ROOMS = "named_rooms"  # selected_rooms: only rooms they named (+ one generic offer)
EXTENSION_GENERIC_ONCE = "generic_once"  # single_room: one "anything else?" offer left
EXTENSION_NONE = "none"                # single_room / selected_rooms with the offer spent


@dataclass
class GateDecision:
    """Boolean gates with reasons, for logging and for the journal."""

    scan_processing_complete: bool = False
    can_prompt_additional_scan: bool = False
    # Scope-shaped extension prompting (never wider than the gate above).
    extension_prompt_mode: str = EXTENSION_CLOSED
    extension_generic_offer_available: bool = False
    scope_rooms: list[str] = field(default_factory=list)
    can_ask_scope: bool = False
    can_ask_first_name: bool = False
    can_ask_zip: bool = False
    can_ask_address: bool = False
    can_offer_quote_request: bool = False
    can_submit_quote_request: bool = False
    # Whether a street address has actually been captured. The agent may ask
    # for one and may explain the privacy policy while asking; what it may
    # never do is claim to already hold an address it was never given.
    address_on_file: bool = False
    # May the agent ask "want me to put this together for TakeShape?" — the
    # offer the homeowner has to accept before any card exists.
    can_offer_request_package: bool = False
    # May a request card be put in front of the homeowner at all. Everything
    # submission needs, plus their yes.
    can_present_request_card: bool = False
    # True only once ops-returned quotes exist for this homeowner — the sole
    # situation where the model may put a dollar figure in front of them
    # (client decision, Sep 1). Set by the runtime, which knows the quote
    # record; the engine's default is the safe one.
    can_state_prices: bool = False
    # When prices were unlocked by a server-computed guidance card rather than
    # by real returned quotes, the model may state ONLY that card's numbers —
    # a card of $2.4k-$6.8k must not become "around $15,000" in the prose.
    # None means unbounded (ops-returned quotes, which are real figures).
    allowed_price_range: tuple[float, float] | None = None
    reasons: dict[str, str] = field(default_factory=dict)

    def client_view(self) -> dict:
        return {
            "scanProcessingComplete": self.scan_processing_complete,
            "canPromptAdditionalScan": self.can_prompt_additional_scan,
            "canRequestQuote": self.can_offer_quote_request,
            # True only when every submission slot is captured — the app
            # should enable its Confirm control off this, so a tap can't
            # bounce off the server's 409 (API_CONTRACT_V1 §5).
            "canSubmitQuoteRequest": self.can_submit_quote_request,
            # Whether a card should be on screen. False until the homeowner
            # has agreed to the request being put together, which is why a
            # draft can be absent even with canSubmitQuoteRequest true.
            "canPresentRequestCard": self.can_present_request_card,
            # How far a scan invitation may go this turn given the stated
            # scope; "closed" whenever canPromptAdditionalScan is false.
            "extensionPromptMode": self.extension_prompt_mode,
        }


@dataclass
class TurnPlan:
    """What the prompt builder is permitted to include this turn."""

    gates: GateDecision
    step: FlowStep
    zip_wording_id: str | None = None  # set only when the zip ask is permitted
    scope_wording_id: str | None = None  # set only when the scope ask is permitted


class FlowEngine:
    """Pure logic; no I/O. The endpoint layer feeds it the reconciled scan
    status and persists the state it returns."""

    # ------------------------------------------------------------ scan state
    @staticmethod
    def reconcile_scan(
        state: FlowState,
        client_state: ScanProcessingState,
        client_job_id: str | None,
        client_scan_id: str | None,
        client_progress: float | None,
        server_job_state: ScanProcessingState | None,
    ) -> ScanStatus:
        """Merge the client's claim with the server's job store. The server
        wins whenever it has an opinion (SOW §3). When the job store has no
        record — the normal state after a redeploy or terminal-job pruning —
        the client's processing flag is the authority the SOW names, so it is
        honored, recorded as ``server_verified=False``, and warned about by
        the runtime. A completion the server once confirmed stays confirmed
        (``verified_complete`` persists in the durable flow state)."""
        scan = ScanStatus(
            scan_id=client_scan_id or state.scan.scan_id,
            job_id=client_job_id or state.scan.job_id,
            state=client_state,
            progress=client_progress,
            verified_complete=state.scan.verified_complete,
        )
        if server_job_state is not None:
            scan.state = server_job_state
            scan.server_verified = True
            if server_job_state is ScanProcessingState.COMPLETE:
                scan.verified_complete = True
        elif client_state is ScanProcessingState.UNKNOWN:
            # Legacy client, no server job to check: keep the last known state
            # rather than regressing to unknown.
            scan.state = state.scan.state
        if scan.state is ScanProcessingState.COMPLETE and scan.verified_complete:
            scan.server_verified = True
        return scan

    # ------------------------------------------------------------ gate logic
    def evaluate_gates(self, state: FlowState, user_message: str | None) -> GateDecision:
        g = GateDecision()
        slots = state.slots

        # --- SOW §3 hard constraint -------------------------------------
        # Scope never widens this: it only narrows what an OPEN gate allows.
        g.scan_processing_complete = state.scan.state is ScanProcessingState.COMPLETE
        g.can_prompt_additional_scan = g.scan_processing_complete
        if not g.can_prompt_additional_scan:
            g.reasons["can_prompt_additional_scan"] = (
                f"first scan processing state is '{state.scan.state}'"
                + (" (server-verified)" if state.scan.server_verified else " (client-reported)")
                + f" [signal: {state.scan.signal}]"
            )
        g.extension_prompt_mode = extension_prompt_mode(state, g.can_prompt_additional_scan)
        g.extension_generic_offer_available = (
            g.can_prompt_additional_scan and state.extension_offers < MAX_EXTENSION_OFFERS
        )
        g.scope_rooms = list(state.scope_rooms)
        if g.can_prompt_additional_scan and g.extension_prompt_mode != EXTENSION_OPEN:
            g.reasons["extension_prompt_mode"] = (
                f"scope is {state.scope_intent}; generic offers made: {state.extension_offers}"
            )

        # --- scope intent (step 3) ---------------------------------------
        # Asked lightly inside the design conversation, once the homeowner
        # has given a name and the variant's placement turn is reached;
        # never on a turn that already carries the zip ask (one question per
        # reply), never after a quote request exists.
        placement_ok = True
        if state.scope_wording_id:
            from .wording import wording_by_id

            wording = wording_by_id(state.scope_wording_id)
            placement_ok = wording is None or state.user_turns >= wording.min_user_turns
        scope_recently_asked = (
            state.scope_asks > 0 and state.user_turns - state.scope_last_asked_at_turn < 2
        )

        # --- first name (step 2) ----------------------------------------
        g.can_ask_first_name = (
            slots.first_name is None and state.first_name_asks < MAX_FIRST_NAME_ASKS
        )

        # --- zip (step 4) ------------------------------------------------
        # Placement is part of the trial (SOW §2: "wordings and placements"):
        # the assigned wording may wait for a later substantive turn.
        zip_turns = ENGAGED_USER_TURNS
        if state.zip_wording_id:
            from .wording import wording_by_id

            zip_wording = wording_by_id(state.zip_wording_id)
            if zip_wording is not None:
                zip_turns = max(ENGAGED_USER_TURNS, zip_wording.min_user_turns)
        engaged = state.user_turns >= zip_turns
        pricing_signal = _mentions_local_or_pricing(user_message)
        # One-turn cooldown: a homeowner who ignored the zip ask last turn
        # shouldn't hear it again immediately (observed in scene testing).
        recently_asked = (
            state.zip_asks > 0
            and state.user_turns - state.zip_last_asked_at_turn < 2
        )
        # After one ignored ask, only re-ask when the homeowner themselves
        # raises pricing/providers — otherwise it reads as badgering.
        context_ok = (
            (engaged or pricing_signal) if state.zip_asks == 0 else pricing_signal
        )
        g.can_ask_zip = (
            slots.zip is None
            and state.zip_asks < MAX_ZIP_ASKS
            and not recently_asked
            and context_ok
        )
        if not g.can_ask_zip and slots.zip is None:
            g.reasons["can_ask_zip"] = (
                "zip ask budget spent"
                if state.zip_asks >= MAX_ZIP_ASKS
                else f"engagement not reached ({state.user_turns}/{ENGAGED_USER_TURNS} turns)"
            )
        g.can_ask_scope = (
            state.scope_intent is ScopeIntent.UNDECIDED
            and slots.first_name is not None
            and state.user_turns >= 1
            and placement_ok
            and state.scope_asks < MAX_SCOPE_ASKS
            and not scope_recently_asked
            and not g.can_ask_zip
            and state.quote_request is None
            # Someone who has asked for the request wants it, not a planning
            # question about how many rooms (Sep 15, #80).
            and not state.request_accepted
        )
        if not g.can_ask_scope and state.scope_intent is ScopeIntent.UNDECIDED:
            g.reasons["can_ask_scope"] = (
                "scope ask budget spent" if state.scope_asks >= MAX_SCOPE_ASKS
                else "zip ask takes this turn" if g.can_ask_zip
                else "placement turn not reached" if not placement_ok
                else "cooldown" if scope_recently_asked
                else "not engaged yet"
            )

        # --- quote offer / gathering (step 7) ----------------------------
        # Offering a quote is allowed once a concrete project is on the table;
        # conversational pressure control lives in the prompt, the gate only
        # prevents premature mechanics.
        g.can_offer_quote_request = slots.project_type is not None

        # --- address (step 8) --------------------------------------------
        # Zip comes first (SOW step 4 precedes step 8): asking for a street
        # address before even a zip is captured reads pushy and skips the
        # wording trial. Live smoke runs confirmed the model will jump ahead
        # without this ordering.
        quote_underway = state.step >= FlowStep.QUOTE_GATHERING and (
            slots.project_type is not None and len(slots.scope_options) > 0
        )
        g.address_on_file = slots.address_captured
        g.can_ask_address = (
            not slots.address_captured
            and quote_underway
            and slots.zip is not None
            and state.address_asks < MAX_ADDRESS_ASKS
        )
        if not g.can_ask_address and not slots.address_captured:
            g.reasons["can_ask_address"] = (
                "zip not captured yet" if quote_underway and slots.zip is None
                else "quote request not underway"
            )

        # --- submission (step 9) -----------------------------------------
        # Contact is part of the lead package (SOW §2 step 9): a verified
        # homeowner identity supplies it, otherwise it must be captured in
        # conversation before the Confirm control can work.
        #
        # The street address is NOT required (client decision, Sep 12): zip is
        # enough for ops to route a lead, and holding the request hostage to an
        # address the homeowner doesn't want to give loses the lead entirely.
        # `can_ask_address` still tries for it, and it still rides the package
        # when captured.
        has_contact = bool(state.has_identity or slots.contact_captured)
        g.can_submit_quote_request = bool(
            slots.project_type is not None
            and slots.scope_options
            and slots.zip is not None
            and has_contact
        )
        if not g.can_submit_quote_request:
            missing = self.missing_submission_slots(state)
            g.reasons["can_submit_quote_request"] = "missing: " + ", ".join(missing)
        # The card is offered before it is shown: an unannounced card reads as
        # the agent shipping their details on its own (Sep 12 feedback).
        g.can_present_request_card = g.can_submit_quote_request and state.request_accepted
        g.can_offer_request_package = bool(
            g.can_submit_quote_request
            and not state.request_accepted
            and state.request_offers < MAX_REQUEST_OFFERS
        )
        if not g.can_present_request_card:
            g.reasons["can_present_request_card"] = (
                "homeowner has not agreed to the request yet"
                if g.can_submit_quote_request
                else g.reasons.get("can_submit_quote_request", "")
            )
        return g

    @staticmethod
    def missing_submission_slots(state: FlowState, *, require_values: bool = False) -> list[str]:
        """``require_values=True`` is the submission-time check: a token's
        captured/linked flags satisfy the conversational gates, but the lead
        package needs the actual values (merged in from the durable store by
        ``resolve_flow_state``)."""
        slots = state.slots
        missing = []
        if slots.project_type is None:
            missing.append("projectType")
        if not slots.scope_options:
            missing.append("scopeOptions")
        if slots.zip is None:
            missing.append("zip")
        # No address check: it is opportunistic, not required (see evaluate_gates).
        if require_values:
            contact_ok = bool(
                (not state.homeowner_is_guest and (state.homeowner_id or state.homeowner_auth_sub))
                or slots.contact_email
                or slots.contact_phone
            )
        else:
            contact_ok = bool(state.has_identity or slots.contact_captured)
        if not contact_ok:
            missing.append("contact")
        return missing

    # ------------------------------------------------------------ turn plan
    def plan_turn(self, state: FlowState, user_message: str | None) -> TurnPlan:
        gates = self.evaluate_gates(state, user_message)
        plan = TurnPlan(gates=gates, step=self._derive_step(state))
        if gates.can_ask_zip:
            plan.zip_wording_id = state.zip_wording_id
        if gates.can_ask_scope:
            plan.scope_wording_id = state.scope_wording_id
        return plan

    @staticmethod
    def _derive_step(state: FlowState) -> FlowStep:
        """The 'current step' surfaced to clients and the journal. The journey
        is not strictly linear (design conversation continues throughout), so
        this is the furthest meaningful position, not a lock."""
        slots = state.slots
        if state.quote_request is not None:
            return (
                FlowStep.RESULTS_RETURN
                if state.quote_request.quotes_returned_count > 0
                else FlowStep.QUOTE_REQUEST
            )
        if slots.address_captured or state.request_accepted:
            return FlowStep.QUOTE_REQUEST
        if slots.project_type is not None and slots.scope_options:
            return FlowStep.ADDRESS_COLLECTION
        if slots.project_type is not None:
            return FlowStep.QUOTE_GATHERING
        if slots.zip is not None:
            return FlowStep.SCAN_HANDOFF if not _scan_done(state) else FlowStep.SCAN_EXTENSION
        if not state.opening_delivered:
            return FlowStep.RECOGNITION
        if slots.first_name is None:
            return FlowStep.ENGAGEMENT
        if state.user_turns < ENGAGED_USER_TURNS:
            return FlowStep.DESIGN_CONVERSATION
        return FlowStep.ZIP_COLLECTION

    # ------------------------------------------------------------ accounting
    @staticmethod
    def record_user_turn(state: FlowState, message: str) -> None:
        if _is_substantive(message):
            state.user_turns += 1

    @staticmethod
    def record_asks(
        state: FlowState,
        *,
        asked_first_name: bool = False,
        asked_zip: bool = False,
        asked_address: bool = False,
        asked_scope: bool = False,
        offered_extension: bool = False,
        offered_request: bool = False,
    ) -> None:
        if asked_first_name:
            state.first_name_asks += 1
        if asked_zip:
            state.zip_asks += 1
            state.zip_last_asked_at_turn = state.user_turns
        if asked_address:
            state.address_asks += 1
        if asked_scope:
            state.scope_asks += 1
            state.scope_last_asked_at_turn = state.user_turns
        if offered_extension:
            state.extension_offers += 1
        if offered_request:
            state.request_offers += 1
            state.request_offer_at_turn = state.user_turns


def extension_prompt_mode(state: FlowState, gate_open: bool) -> str:
    """What kind of scan invitation the stated scope permits -- and nothing
    at all while the SOW §3 gate is shut, whatever the scope says."""
    if not gate_open:
        return EXTENSION_CLOSED
    if state.scope_intent in (ScopeIntent.WHOLE_HOME, ScopeIntent.UNDECIDED):
        return EXTENSION_OPEN
    offer_left = state.extension_offers < MAX_EXTENSION_OFFERS
    if state.scope_intent is ScopeIntent.SELECTED_ROOMS and state.scope_rooms:
        return EXTENSION_NAMED_ROOMS
    return EXTENSION_GENERIC_ONCE if offer_left else EXTENSION_NONE


def _scan_done(state: FlowState) -> bool:
    return state.scan.state is ScanProcessingState.COMPLETE


def _is_substantive(message: str) -> bool:
    return len(message.strip()) >= 2


_LOCAL_PRICING_HINTS = (
    "price", "cost", "quote", "estimate", "budget", "how much",
    "near me", "in my area", "local", "around here", "contractor", "provider",
)


def _mentions_local_or_pricing(message: str | None) -> bool:
    if not message:
        return False
    lowered = message.lower()
    return any(hint in lowered for hint in _LOCAL_PRICING_HINTS)
