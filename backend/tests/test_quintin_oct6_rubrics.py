"""Quintin's service quote rubrics (Oct 6): where they sit and how they are read.

The document (docs/SERVICE_RUBRICS.md) is condensed into flow/service_rubrics.
Three readers: the directives for the service in play, the lead package the
provider gets, and the homeowner when quotes come back.
"""

from __future__ import annotations

import pytest

import app.flow_runtime as rt
from app.flow import ops_email, service_rubrics
from app.flow.service_rubrics import HOMEOWNER_CONCEALED_WORDING, PROVIDER_CONCEALED_WORDING, RUBRICS
from app.flow.state import FlowState
from app.flow_quotes import QuoteRequestRecord
from app.home_ai import _missing_details
from app.home_guide_tools import SCAN_SUPPORT as SERVICE_COVERAGE


def test_every_catalog_service_with_a_rubric_resolves_and_decking_does_not():
    for service in SERVICE_COVERAGE:
        rubric = service_rubrics.rubric_for(service, [])
        assert (rubric is None) == (service == "Decking"), service
    assert len(RUBRICS) == 16


@pytest.mark.parametrize("service,options,text,key", [
    ("Roofing & Siding", ["gutter install"], "", "Gutter Installation"),
    ("Roofing & Siding", ["siding"], "", "Siding Installation"),
    ("Roofing & Siding", [], "the roof is leaking", "Roofing"),
    ("Roofing & Siding", [], "", "Roofing"),
    ("Window & Door Install", [], "replace the patio door", "Door Installation"),
    ("Window & Door Install", ["window replacement"], "", "Window Installation"),
    ("Power Washing", [], "", "Power Washing"),
])
def test_combined_catalog_services_pick_the_trade_from_the_scope(service, options, text, key):
    assert service_rubrics.rubric_for(service, options, text).key == key


def _directives(state: FlowState, index=None, message: str = "ok") -> str:
    plan = rt._engine.plan_turn(state, message)
    return rt._build_directives(state, plan, opening=False, price_guidance=None,
                                quotes_to_present=None, home_index=index)


def test_the_agent_gets_the_rubric_for_the_service_in_play():
    state = FlowState(thread_id="t", client_flow_aware=True, opening_delivered=True, user_turns=2)
    assert "SERVICE RUBRIC" not in _directives(state)
    state.slots.project_type = "Painting"
    text = _directives(state)
    assert "SERVICE RUBRIC (Painting)" in text
    assert "Scope and surfaces; Measurements; Openings and details" in text
    assert "which rooms and surfaces: walls, ceilings, trim, doors (which sides), cabinets, or exterior" in text
    assert "never ask them to remeasure a dimension the scan supplies" in text
    assert "Never ask them to climb" in text
    assert "a fact they have not given is unknown, not a guess" in text


def test_an_exterior_trade_on_an_indoor_scan_says_the_scan_cannot_supply_it():
    state = FlowState(thread_id="t", client_flow_aware=True, opening_delivered=True, user_turns=2)
    state.slots.project_type = "Power Washing"
    inside = _directives(state)
    assert "nothing usable for this trade: it needs exterior surfaces" in inside
    assert "never convert indoor floor area into this trade's quantity" in inside
    # The same trade on an exterior capture with mesh surfaces reads the numbers.
    state.scan_appearance = {"setting": "exterior", "structure": "house"}
    state.scan_mesh_bounds = {"widthMeters": 20.0, "lengthMeters": 30.0, "heightMeters": 6.0, "roomCount": 0}
    state.scan_surfaces = {"uprightSquareMeters": 100.0, "groundSquareMeters": 80.0}
    outside = _directives(state)
    assert "From the scan: the upright surface (walls, siding, fences) and the level ground" in outside
    assert "whether that level ground is all driveway or includes lawn or walkway" in outside
    assert "nothing usable for this trade" not in outside


def test_quotes_come_with_what_they_cover():
    state = FlowState(thread_id="t", client_flow_aware=True, opening_delivered=True, user_turns=4)
    plan = rt._engine.plan_turn(state, "any news?")
    text = rt._build_directives(state, plan, opening=False, price_guidance=None,
                                quotes_to_present=[{"providerName": "Brightline", "priceUsd": 900}])
    assert "say what a quote does and does not cover" in text
    assert "The scan cannot show conditions beneath flooring, behind walls" in text


def test_the_lead_package_carries_the_fields_the_scan_and_the_unknowns():
    section = service_rubrics.package(
        "Painting", ["walls and trim"], ["eggshell"],
        {"floorAreaSquareFeet": 180.0, "paintableWallSquareFeet": 410.0, "windowCount": 2, "doorCount": 1,
         "windowOpenings": [{"widthFeet": 3.0, "heightFeet": 4.0}]},
    )
    assert section["service"] == "Painting"
    assert section["fromScan"] == [
        "floor area ~180 sq ft", "paintable wall ~410 sq ft", "2 window opening(s)",
        "opening sizes 3.0 x 4.0 ft", "1 door(s)",
    ]
    assert section["fromHomeowner"] == ["walls and trim", "eggshell"]
    assert section["concealedConditions"] == PROVIDER_CONCEALED_WORDING
    assert service_rubrics.package("Decking", [], [], {}) == {}

    record = QuoteRequestRecord(id="qr_1", createdAt="2026-10-06T00:00:00+00:00", threadId="t",
                                serviceType="Painting", zip="37130", firstName="Quintin",
                                scopeOptions=["walls and trim"], materials=["eggshell"],
                                measurements={"paintableWallSquareFeet": 410.0}, rubric=section)
    _, body = ops_email.build_ops_email(record, [], None, None, None)
    assert "PROVIDER FIELDS (Painting)" in body
    assert "  - Scope and surfaces: interior or exterior" in body
    assert "From the scan: floor area ~180 sq ft" in body
    assert "From the homeowner: walls and trim; eggshell (and the synopsis above)." in body
    assert "Anything not covered above is UNKNOWN: ask through the app rather than assume." in body
    assert "Concealed conditions: This scope uses the attached scan measurements" in body
    assert "In your quote, please state: your business name" in body
    html = ops_email.build_ops_email_html(record, [], None, None, None)
    assert "Provider fields (Painting)" in html and "Concealed conditions:" in html

    bare = QuoteRequestRecord(id="qr_2", createdAt="2026-10-06T00:00:00+00:00", threadId="t", serviceType="Decking")
    _, body = ops_email.build_ops_email(bare, [], None, None, None)
    assert "PROVIDER FIELDS" not in body


def test_an_exterior_lead_prints_the_mesh_surfaces_it_has():
    section = service_rubrics.package(
        "Power Washing", ["driveway"], [],
        {"capture": "exterior of a house (from the scan photos)",
         "scannedSurfacesSquareFeet": {"upright": 1152.0, "ground": 904.0, "heightFeet": 18.0}},
    )
    assert "upright surface ~1,152 sq ft (walls, siding, fences the walk passed)" in section["fromScan"]
    assert "level ground ~904 sq ft (drive, walks, patio, any lawn scanned)" in section["fromScan"]
    assert section["fromScan"][-1] == "exterior of a house (from the scan photos)"
    empty = service_rubrics.package("Gutter Cleaning", [], [], {})
    assert "nothing measured for this trade" in "\n".join(service_rubrics.package_lines(empty))


def test_the_quote_draft_asks_the_rubrics_questions():
    assert _missing_details("Power Washing")[0].startswith("Which surfaces and the outcome they want")
    assert _missing_details("Decking") == ["Desired material", "Repair vs new build", "Preferred timing", "Any access constraints or pets"]


def test_the_homeowner_wording_is_quintins():
    assert HOMEOWNER_CONCEALED_WORDING.startswith("Your quote covers the work and conditions described in the TakeShape scope.")
    assert "for your approval before additional work proceeds" in HOMEOWNER_CONCEALED_WORDING
