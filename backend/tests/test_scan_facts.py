"""The ledger and the code-side gates on what the agent may claim.

The point of these tests is that the guarantees hold without a prompt: a
`low`-certainty observation must be structurally unable to reach the model, not
merely discouraged from being repeated.
"""

from __future__ import annotations

import pytest

from app import scan_facts as sf


def context(**overrides):
    base = {
        "room_key": "room-15",
        "room": "kitchen",
        "objects": [
            {"class": "butler sink", "appearance": "white ceramic", "certainty": "high"},
            {"class": "kettle", "appearance": "chrome", "certainty": "low"},
            {"class": "fireplace", "appearance": "", "certainty": "unobserved"},
        ],
        "surfaces": {"walls": "matt cream", "floor": "worn oak"},
        "style": "shaker",
        "notable": ["dated splashback"],
        "measurements": {"paintable_m2": 14.5, "floor_m2": 41.9, "perimeter_m": 18.2},
        "coverage": "partial",
    }
    base.update(overrides)
    return base


# --------------------------------------------------------------------------
# What the scan answered is never asked
# --------------------------------------------------------------------------


def test_scan_measurements_land_in_the_ledger() -> None:
    state = sf.prefill_from_context(sf.new_state("home-1", "room-15"), context())
    assert state["facts"]["paintable_m2"] == 14.5
    assert state["facts"]["floor_m2"] == 41.9
    assert state["facts"]["perimeter_m"] == 18.2


def test_prefilled_facts_are_never_asked_for() -> None:
    """This is what the ingest bought: measurements are `scan`-sourced rows, so
    they can never surface as a question even before they are filled in."""
    state = sf.enter_intake(sf.new_state("home-1"), "painting")
    sf.prefill_from_context(state, context())
    for _ in range(20):
        ask = sf.next_ask(state)
        if ask is None:
            break
        assert sf.FACTS[ask][0] == "conversation"
        sf.note_ask(state, ask)


def test_observed_surface_condition_becomes_a_fact() -> None:
    state = sf.prefill_from_context(sf.new_state("home-1"), context())
    assert state["facts"]["wall_condition"] == "matt cream"
    assert state["facts"]["floor_condition"] == "worn oak"


def test_a_geometry_only_document_records_no_condition() -> None:
    """No appearance pass ran, so there is nothing observed. Recording an empty
    condition would suppress the question permanently for a room nobody looked
    at."""
    document = context(coverage="geometry_only", surfaces={})
    state = sf.prefill_from_context(sf.new_state("home-1"), document)
    assert "wall_condition" not in state["facts"]
    # Measurements came from geometry and must survive regardless.
    assert state["facts"]["paintable_m2"] == 14.5


def test_rooms_covered_accumulates_without_duplicates() -> None:
    state = sf.new_state("home-1")
    sf.prefill_from_context(state, context())
    sf.prefill_from_context(state, context())
    assert state["scan"]["rooms_covered"] == ["kitchen"]


# --------------------------------------------------------------------------
# The gates -- guarantees, not guidance
# --------------------------------------------------------------------------


def test_only_agreed_objects_are_assertable() -> None:
    assert [o["class"] for o in sf.assertable_objects(context())] == ["butler sink"]


def test_the_agent_view_cannot_express_an_unconfirmed_object() -> None:
    """A claim the agent cannot support is a claim it was never shown. This is
    the difference between asking a model to be careful and making carelessness
    impossible to express."""
    view = sf.agent_view(context())
    rendered = repr(view)
    assert "butler sink" in rendered
    assert "kettle" not in rendered
    assert "fireplace" not in rendered


def test_the_agent_view_keeps_measurements_because_they_are_geometry() -> None:
    view = sf.agent_view(context())
    assert view["measurements"]["paintable_m2"] == 14.5


def test_the_agent_view_survives_an_empty_document() -> None:
    view = sf.agent_view({})
    assert view["objects"] == []
    assert view["measurements"] == {}
    assert view["coverage"] == "unknown"


def test_rescan_candidates_are_only_the_unphotographed() -> None:
    """Something demonstrably there and demonstrably unphotographed is an honest
    reason to offer a rescan; a `low` sighting is not."""
    assert sf.rescan_candidates(context()) == ["fireplace"]


# --------------------------------------------------------------------------
# The ledger's own behaviour
# --------------------------------------------------------------------------


def test_required_facts_depend_on_the_job() -> None:
    assert "tenure" in sf.required("flooring")
    assert "tenure" not in sf.required("painting")
    assert "job_type" in sf.required("painting")


def test_next_ask_is_none_until_intake() -> None:
    state = sf.new_state("home-1")
    assert sf.next_ask(state) is None


def test_next_ask_takes_the_highest_priority_unknown() -> None:
    state = sf.enter_intake(sf.new_state("home-1"), "painting")
    assert sf.next_ask(state) == "scope"  # job_type is already recorded


def test_next_ask_is_deterministic_between_equal_priorities() -> None:
    """Two facts of equal priority must not depend on set iteration order for
    which one gets asked -- the same conversation would otherwise diverge."""
    state = sf.enter_intake(sf.new_state("home-1"), "structural")
    for key in ("scope", "rooms", "budget_range", "timeline", "tenure"):
        sf.record(state, key, "x")
    assert sf.next_ask(state) == sf.next_ask(state)
    assert sf.next_ask(state) in ("occupied", "property_type")


def test_a_fact_is_never_asked_twice() -> None:
    state = sf.enter_intake(sf.new_state("home-1"), "painting")
    first = sf.next_ask(state)
    sf.note_ask(state, first)
    assert sf.next_ask(state) != first


def test_declining_a_topic_is_sticky() -> None:
    """Re-raising a declined topic is the ham-fisted failure."""
    state = sf.enter_intake(sf.new_state("home-1"), "painting")
    sf.decline(state, "budget_range")
    sf.decline(state, "budget_range")
    assert state["declined"] == ["budget_range"]
    assert "budget_range" not in sf.missing(state)


def test_services_are_not_discussed_in_companion_mode() -> None:
    state = sf.new_state("home-1")
    assert sf.may_discuss_services(state) is False
    sf.enter_intake(state, "painting")
    assert sf.may_discuss_services(state) is True


def test_condition_is_never_volunteered_unprompted() -> None:
    """The agent may call a room handsome; it may not call the carpet worn
    unless the homeowner asked what it thought."""
    state = sf.new_state("home-1")
    assert sf.may_volunteer_condition(state, user_asked_opinion=False) is False
    assert sf.may_volunteer_condition(state, user_asked_opinion=True) is True


def test_address_is_only_requested_once_the_rest_is_known() -> None:
    state = sf.enter_intake(sf.new_state("home-1"), "painting")
    assert sf.may_request_address(state) is False
    sf.prefill_from_context(state, context())
    for key in sf.required("painting"):
        if key not in state["facts"]:
            sf.record(state, key, "x")
    assert sf.may_request_address(state) is True


def test_a_quote_needs_contact_details_and_fires_once() -> None:
    state = sf.enter_intake(sf.new_state("home-1"), "painting")
    customer = {"address": "1 High St", "contact": "a@b.co"}
    assert sf.may_submit_quote(state, customer) is True
    assert sf.may_submit_quote(state, {"address": "1 High St"}) is False
    state["quote_requested"] = True
    assert sf.may_submit_quote(state, customer) is False


def test_events_are_logged_with_their_fields() -> None:
    state = sf.new_state("home-1")
    sf.log_event(state, "scan_processed", room="kitchen")
    assert state["log"] == [{"kind": "scan_processed", "room": "kitchen"}]


@pytest.mark.parametrize("key", sf.SCAN_MEASUREMENTS)
def test_every_scan_measurement_has_a_ledger_row(key: str) -> None:
    """A measurement with no row would be computed and then silently dropped."""
    assert sf.FACTS[key][0] == "scan"
