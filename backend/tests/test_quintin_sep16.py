"""The Sep 16 demo thread (phone-quintin--mu3ctf32), four items.

- The agent told a homeowner his address only went to the provider he picked,
  then said it "was available earlier as part of your contact details". No
  address was ever given.
- A mistyped first name was permanent: "Chance I mean - typo" left the slot
  saying Chane, and the agent later denied the correction had happened.
- The turn where the phone number arrived was planned as though contact were
  still missing, so the reply announcing the card was suppressed and the card
  shipped attached to safe copy that never mentions it.
- "I'm not able to browse quotes online myself", two turns before an
  $18k-$42k ballpark, with nothing saying what a quote is here.
"""

from __future__ import annotations

import app.flow_runtime as flow_runtime
from app.flow import enforcement
from app.flow.machine import FlowEngine
from app.flow.state import FlowState, FlowStep, ScanProcessingState, ScopeIntent
from app.home_guide_prompt import build_home_guide_system_prompt


def _engaged_state() -> FlowState:
    """Everything a request needs except the contact details."""
    state = FlowState(thread_id="t-sep16")
    state.scan.state = ScanProcessingState.COMPLETE
    state.slots.first_name = "Chane"
    state.slots.zip = "45458"
    state.slots.project_type = "Interior Remodeling"
    state.slots.scope_options = ["cabinet replacement", "new appliances"]
    state.scope_intent = ScopeIntent.SINGLE_ROOM
    state.step = FlowStep.QUOTE_REQUEST
    state.user_turns = 5
    return state


# --------------------------------------------------------------- the address
def test_agent_is_told_it_has_no_address():
    state = _engaged_state()
    lines = flow_runtime._memory_directives(state)
    assert any("No street address has been captured" in line for line in lines)

    state.slots.address = "1 Example Street, Dayton OH"
    assert not any("No street address has been captured" in line
                   for line in flow_runtime._memory_directives(state))


def test_address_possession_claims_are_caught():
    gates = FlowEngine().evaluate_gates(_engaged_state(), None)
    assert gates.address_on_file is False

    for lie in (
        "An address was available earlier as part of your contact details.",
        "I have your address already, so nothing else is needed.",
        "Your address is on file with the request.",
        "I've got your address from what you shared.",
    ):
        rules = [v.rule for v in enforcement.check(lie, gates)]
        assert "address_claimed_but_absent" in rules, lie

    # The reassurance offered WHILE asking is not a possession claim, and the
    # honest sentence must survive too.
    for fine in (
        "What's your street address? It only goes to the provider you pick.",
        "I don't have an address from our conversation.",
        "Once an address is provided, the provider you pick can plan the visit.",
    ):
        rules = [v.rule for v in enforcement.check(fine, gates)]
        assert "address_claimed_but_absent" not in rules, fine

    with_address = _engaged_state()
    with_address.slots.address = "1 Example Street, Dayton OH"
    kept = FlowEngine().evaluate_gates(with_address, None)
    assert not enforcement.check("I have your address already.", kept)


def test_quote_results_promise_nothing_about_an_address_never_given():
    quotes = [{"providerName": "Brightline Painting", "priceUsd": 2450}]
    state = _engaged_state()
    plan = FlowEngine().plan_turn(state, None)

    without = flow_runtime._build_directives(
        state, plan, opening=False, price_guidance=None, quotes_to_present=quotes
    )
    assert "address is shared only with the provider" not in without
    assert "no street address" in without

    state.slots.address = "1 Example Street, Dayton OH"
    with_address = flow_runtime._build_directives(
        state, FlowEngine().plan_turn(state, None), opening=False,
        price_guidance=None, quotes_to_present=quotes,
    )
    assert "address is shared only with the provider" in with_address


# ------------------------------------------------------------------ the name
def test_a_typo_in_the_first_name_can_be_corrected():
    state = _engaged_state()
    delta = flow_runtime._apply_capture(
        state, {"firstName": "Chance"}, "Chance I mean - typo. My zip is 45458"
    )
    assert state.slots.first_name == "Chance"
    assert delta["firstName"] == "Chance"


def test_only_a_name_the_homeowner_typed_replaces_the_stored_one():
    state = _engaged_state()
    # The model inventing a different name mid-conversation is not a
    # correction, and must not overwrite what they actually said.
    flow_runtime._apply_capture(state, {"firstName": "Quintin"}, "the cabinets look dated")
    assert state.slots.first_name == "Chane"
    # Someone else's name, said in passing, is not the homeowner's.
    flow_runtime._apply_capture(
        state, {"firstName": "Dave"}, "my contractor Dave is handling the demo work"
    )
    assert state.slots.first_name == "Chane"
    # A truncated capture sitting inside the stored name is not a correction
    # either: a bare substring test used to accept it.
    flow_runtime._apply_capture(state, {"firstName": "Chan"}, "my name is Chance")
    assert state.slots.first_name == "Chane"
    # Nor is a re-capture of the same name a change worth journaling.
    delta = flow_runtime._apply_capture(state, {"firstName": "chane"}, "chane here")
    assert "firstName" not in delta


def test_the_model_is_told_how_to_make_a_correction_stick():
    state = _engaged_state()
    plan = FlowEngine().plan_turn(state, None)
    directives = flow_runtime._build_directives(
        state, plan, opening=False, price_guidance=None, quotes_to_present=None
    )
    assert "flowCapture.firstName" in directives


# ------------------------------------------------------------------ the card
def test_contact_in_the_message_opens_the_card_gate_on_the_same_turn():
    state = _engaged_state()
    state.request_accepted = True
    assert FlowEngine().evaluate_gates(state, None).can_present_request_card is False

    delta = flow_runtime._precapture_from_message(state, "509-216-0574")
    assert delta == {"contactPhone": "509-216-0574"}
    assert FlowEngine().evaluate_gates(state, None).can_present_request_card is True


def test_precapture_reads_emails_and_ignores_other_numbers():
    state = _engaged_state()
    assert flow_runtime._precapture_from_message(
        state, "reach me at quintin@example.com"
    ) == {"contactEmail": "quintin@example.com"}

    other = _engaged_state()
    for not_a_phone in ("my zip is 45458", "the room is 12 by 14", "back in 2026"):
        assert flow_runtime._precapture_from_message(other, not_a_phone) == {}
    assert other.slots.contact_captured is False


def test_precapture_takes_an_email_and_a_phone_from_the_same_message():
    state = _engaged_state()
    delta = flow_runtime._precapture_from_message(
        state, "my email is q@example.com and my cell is 509-216-0574"
    )
    assert delta == {"contactEmail": "q@example.com", "contactPhone": "509-216-0574"}


def test_precapture_refuses_details_that_are_not_the_homeowners():
    state = _engaged_state()
    for someone_else in (
        "my contractor Dave already quoted this, his number is 509-216-0574",
        # Four words: short enough to pass the "this IS the answer" shortcut,
        # which is why the third-party check runs first.
        "call Dave at 509-216-0574",
        "his number is 509-216-0574",
    ):
        assert flow_runtime._precapture_from_message(state, someone_else) == {}, someone_else
    assert state.slots.contact_captured is False


def test_an_address_the_homeowner_types_is_known_on_that_turn():
    """The slot is written after the reply is checked, so enforcement reads
    the model's own capture for the address given on this very turn."""
    gates = FlowEngine().evaluate_gates(_engaged_state(), None)
    assert gates.address_on_file is False
    thanks = "I have your address, thanks."
    assert [v.rule for v in enforcement.check(thanks, gates)] == [
        "address_claimed_but_absent"
    ]
    assert not enforcement.check(thanks, gates, address_given_this_turn=True)


def test_the_capture_is_what_says_an_address_arrived():
    def response(captured):
        return type("R", (), {"_flow_capture": captured})()

    assert flow_runtime._capture_has_address(response({"address": "1450 Pheasant Hill Drive"}))
    # Same length floor `_apply_capture` uses before it stores one.
    assert not flow_runtime._capture_has_address(response({"address": "no"}))
    assert not flow_runtime._capture_has_address(response(None))


def test_saying_the_address_is_missing_is_not_a_possession_claim():
    """The sentence `correction_instruction` asks for must not violate the
    rule that asked for it, or the turn regenerates straight into safe copy."""
    gates = FlowEngine().evaluate_gates(_engaged_state(), None)
    for honest in (
        "I don't have your address on file.",
        "Once I have your address from you, the provider can plan the visit.",
        "I never had your address from this conversation.",
    ):
        assert not enforcement.check(honest, gates), honest


# --------------------------------------------------- quote vs rough ballpark
def test_the_prompt_says_what_a_quote_is_and_where_it_comes_from():
    prompt = build_home_guide_system_prompt()
    assert "A QUOTE and a BALLPARK are two different things" in prompt
    assert "no figure you produce yourself is a quote" in prompt


def test_a_rough_range_is_labelled_a_ballpark_not_a_quote():
    from app.flow.wire import PriceGuidance

    state = _engaged_state()
    guidance = PriceGuidance(
        lowUsd=18000, highUsd=42000, basis="nothing measured yet",
        disclaimer="wide on purpose",
    )
    plan = FlowEngine().plan_turn(state, None)
    directives = flow_runtime._build_directives(
        state, plan, opening=False, price_guidance=guidance, quotes_to_present=None,
        price_asked=True,
    )
    assert "rough ballpark, never a quote" in directives

    # When the regex did NOT recognise the message as a price ask, the band is
    # still handed over -- as a conditional permission, so the model can
    # answer a phrasing no pattern caught instead of refusing.
    unasked = flow_runtime._build_directives(
        state, plan, opening=False, price_guidance=guidance, quotes_to_present=None,
        price_asked=False,
    )
    assert "IF the homeowner is asking what something costs" in unasked
    assert "never answer a cost question with a flat refusal" in unasked.lower()
    assert "If they are NOT asking about money, do not bring it up" in unasked

    # Real returned quotes ARE quotes: do not tell the model to call them a
    # ballpark just because a guidance card rode along.
    with_quotes = flow_runtime._build_directives(
        state, plan, opening=False, price_guidance=guidance,
        quotes_to_present=[{"providerName": "Brightline Painting", "priceUsd": 2450}],
    )
    assert "rough ballpark, never a quote" not in with_quotes


# ------------------------------------------------- corrections to other slots
def _settled_state() -> FlowState:
    state = _engaged_state()
    state.slots.contact_email = "quintin@exampl.com"
    state.slots.contact_phone = "509-216-0574"
    state.slots.address = "1450 Pheasant Hill Drive"
    return state


def test_every_captured_slot_takes_a_correction():
    """The name was not the only write-once slot. A wrong zip steers local
    research and provider matching invisibly; a wrong email is how a lead
    goes unreachable."""
    for capture, message, key, expected in (
        ({"zip": "45459"}, "45459 actually, I typed the wrong zip", "zip", "45459"),
        ({"contactEmail": "quintin@example.com"}, "typo - it's quintin@example.com",
         "contactEmail", "quintin@example.com"),
        ({"contactPhone": "(509) 216-9999"}, "use my other number instead, (509) 216-9999",
         "contactPhone", "(509) 216-9999"),
        ({"address": "1460 Pheasant Hill Drive"},
         "I mean 1460 Pheasant Hill Drive, not 1450", "address", "1460 Pheasant Hill Drive"),
    ):
        state = _settled_state()
        delta = flow_runtime._apply_capture(state, capture, message)
        assert delta.get(key) == expected, message


def test_a_passing_mention_is_not_a_correction():
    """Write-once is the default for a reason: a value that merely appears in
    the message must not displace one the homeowner settled earlier."""
    for capture, message, key, kept in (
        ({"zip": "45459"}, "my neighbor in 45459 used them", "zip", "45458"),
        ({"contactEmail": "spam@other.com"}, "forward it to spam@other.com as well",
         "contactEmail", "quintin@exampl.com"),
        ({"contactPhone": "509-216-9999"}, "the shop's number is 509-216-9999",
         "contactPhone", "509-216-0574"),
    ):
        state = _settled_state()
        delta = flow_runtime._apply_capture(state, capture, message)
        assert key not in delta, message
        assert getattr(state.slots, {"zip": "zip", "contactEmail": "contact_email",
                                     "contactPhone": "contact_phone"}[key]) == kept


def test_a_correction_needs_the_new_value_typed_this_turn():
    """The frame alone is not enough — the model saying "typo" over a value
    the homeowner never typed would rewrite the slot from nothing."""
    state = _settled_state()
    delta = flow_runtime._apply_capture(
        state, {"zip": "99999"}, "sorry, typo in my last message"
    )
    assert "zip" not in delta
    assert state.slots.zip == "45458"


def test_the_ledger_says_a_correction_has_to_be_captured():
    state = _settled_state()
    directives = flow_runtime._build_directives(
        state, FlowEngine().plan_turn(state, None), opening=False,
        price_guidance=None, quotes_to_present=None,
    )
    assert "record the new value in flowCapture" in directives


# ------------------------------------------- asking to look a cost up (Sep 16)
def test_the_spend_gate_recognises_how_people_actually_ask():
    """`user_asked_for_price` is the SPEND gate: true means the web search is
    worth paying for. Every one of these is a homeowner asking what something
    costs, taken from the Sep 16 battery transcripts."""
    from app.flow.pricing import user_asked_for_price

    for asking in (
        "what's that gonna run me and how long?",
        "I mostly want to know what this is gonna run me before I commit",
        "let's see what it costs first before I think about other rooms",
        "can you look for quotes online now?",
        "can you look up prices for this?",
        "shop around for prices in my area",
        "how much would that cost?",
    ):
        assert user_asked_for_price(asking), asking


def test_asking_to_look_a_price_up_is_not_asking_to_file_a_request():
    """`quote` is the flow's own word for the thing that is NOT a ballpark, so
    it stays out of the pattern. Looking a price UP is a cost question;
    putting a quote request TOGETHER is not. The verb is the whole
    difference, and getting this wrong in either direction is a bug: a miss
    is a static-table answer, a false hit spends on a search for someone who
    just said yes to filing a request."""
    from app.flow.pricing import user_asked_for_price

    for not_asking in (
        "put together a quote request",
        "draft the quote request",
        "send the quote request over",
        "yes put that quote together",
        "can you package this as a quote request?",
        "go ahead and submit the request",
        "what colour would you use in here?",
        "can you rate this design?",
    ):
        assert not user_asked_for_price(not_asking), not_asking
