"""Quintin's Sep 16 thread: issues #95-#99.

#95 window cleaning priced at $1,300-$5,300 for 15 windows
#96 quotes re-presented with Choose after one was chosen (demo card; the
    directive side is here)
#97 after a choice the agent hands off instead of owning the next steps
#98 the agent could not vouch for a provider ("I don't have access")
#99 the agent speaks of TakeShape as a separate organization
"""

from __future__ import annotations

from collections import Counter

import pytest

import app.flow_runtime as flow_runtime
from app.config import settings
from app.flow import partners
from app.flow.pricing import compute_price_guidance, priced_by_floor_area, priced_per_window
from app.flow.state import FlowState, QuoteRequestRef
from app.flow_quotes import EXPECTATIONS, QuoteRequestRecord, ReturnedQuote, quote_store
from app.home_guide_prompt import build_home_guide_system_prompt
from app.home_index import HomeIndex, Room


# ---------------------------------------------------------------- #95
class _Researched:
    low_per_sqft, high_per_sqft, region_label, fetched_at = 1.0, 2.0, "Nashville", 0.0


def test_window_cleaning_is_priced_per_window_never_per_square_foot():
    g = compute_price_guidance("Window Cleaning", 2300.0, researched=_Researched(), window_count=15)
    assert 100 <= g.lowUsd <= 150 and 300 <= g.highUsd <= 400, (g.lowUsd, g.highUsd)
    assert "15 window openings" in g.basis and "per-window" in g.basis
    # No count yet: the typical band, still never thousands.
    g = compute_price_guidance("Window Cleaning", 2300.0, researched=_Researched())
    assert g.highUsd <= 600 and "nothing measured" in g.basis


def test_exterior_and_job_priced_trades_ignore_floor_area():
    assert not priced_by_floor_area("Power Washing") and not priced_by_floor_area("Gutter Cleaning")
    assert priced_by_floor_area("Painting") and priced_per_window("Window Cleaning")
    g = compute_price_guidance("Power Washing", 2300.0, researched=_Researched())
    assert g.highUsd <= 800, "a 2,300 sq ft floor must not price a power wash"


def _house() -> HomeIndex:
    def room(key, name, windows):
        return Room(key=key, index=int(key[-1]), storey=0, plan_label=name, area_sqft=200.0,
                    floor_y=0.0, polygon=[], objects=Counter(), window_count=windows,
                    display_name=name, confident=True)
    return HomeIndex(rooms=[room("room-1", "kitchen", 3), room("room-2", "living room", 5)], bundle_id="t")


def test_window_count_follows_the_room_in_focus_then_the_home():
    house = _house()
    assert flow_runtime._window_count(FlowState(thread_id="t", active_room_key="room-2"), house, None) == 5
    assert flow_runtime._window_count(FlowState(thread_id="t"), house, None) == 8
    assert flow_runtime._window_count(FlowState(thread_id="t"), None, None) is None


# ---------------------------------------------------------------- #98
def _rows():
    return [{
        "name": "Nash Painting", "relationship": "partner", "website": "nashpainting.com",
        "ratingLabel": "4.8 stars (52 Google reviews)",
        "presences": [{"platform": "google", "rating": 4.8, "reviewCount": 52,
                       "profileUrl": "https://maps.google.com/?cid=1"}],
    }]


def test_a_returned_quote_carries_what_is_on_file_about_the_provider(monkeypatch):
    monkeypatch.setattr(partners, "_load_rows", _rows)
    view = ReturnedQuote(providerName="nash painting", priceUsd=1850).homeowner_view()
    assert view["provider"]["relationship"] == "partner"
    assert view["provider"]["googleRating"] == 4.8 and view["provider"]["googleReviewCount"] == 52
    assert view["provider"]["website"] == "nashpainting.com"
    assert ReturnedQuote(providerName="Nobody Ltd", priceUsd=100).homeowner_view()["provider"] is None


@pytest.mark.asyncio
async def test_the_agent_is_told_what_is_on_file_after_presentation(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "storage_dir", str(tmp_path))
    monkeypatch.setattr(partners, "_load_rows", _rows)
    quote = ReturnedQuote(providerName="Nash Painting", priceUsd=1850)
    record = QuoteRequestRecord(id="qr_file01", createdAt="2026-09-16T00:00:00+00:00", threadId="t",
                                status="presented", serviceType="Painting", quotes=[quote])
    on_file = flow_runtime._quotes_on_file(record)
    assert on_file[0]["providerName"] == "Nash Painting" and on_file[0]["provider"]["relationship"] == "partner"
    state = FlowState(thread_id="t", opening_delivered=True, user_turns=3, client_flow_aware=True,
                      quote_request=QuoteRequestRef(id="qr_file01", status="presented"))
    plan = flow_runtime._engine.plan_turn(state, "are they any good?")
    text = flow_runtime._build_directives(
        state, plan, opening=False, price_guidance=None, quotes_to_present=None, quotes_on_file=on_file,
    )
    assert "WHAT IS ON FILE about each provider" in text
    assert "Never say you have no access to their reviews" in text


# ---------------------------------------------------------------- #97 / #96
@pytest.mark.asyncio
async def test_after_a_choice_the_agent_owns_the_next_steps(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "storage_dir", str(tmp_path))
    state = FlowState(thread_id="t", opening_delivered=True, user_turns=3, client_flow_aware=True,
                      quote_request=QuoteRequestRef(id="qr_x", status="presented"))
    plan = flow_runtime._engine.plan_turn(state, "when will I hear from them?")
    text = flow_runtime._build_directives(
        state, plan, opening=False, price_guidance=None, quotes_to_present=None,
        selected_quote={"providerName": "Brightline Painting", "price": "$2,450"},
    )
    assert "YOU own what happens next" in text
    assert "Brightline Painting reaches out using the contact details" in text
    assert "deposit" in text and "before the crew arrives" in text
    assert "do not re-present the quotes" in text
    assert "TakeShape team" not in text


# ---------------------------------------------------------------- #99
def test_the_assistant_speaks_as_takeshape_in_the_first_person():
    prompt = build_home_guide_system_prompt("control")
    assert "speak AS TakeShape, in the first person" in prompt
    # Sep 17 (#101) rewrote these two so the agent owns the action; the
    # first-person voice is what this test is about and it survives.
    assert EXPECTATIONS["copy"].startswith("Sounds good — I'm getting your request")
    assert "I'll bring them to you here" in EXPECTATIONS["copy"]
    assert "I won't guess at one" in flow_runtime._SAFE_NO_PRICE_COPY
    assert "I'll package what we've discussed" in flow_runtime._SAFE_NO_PRICE_COPY
    # An offer phrased the new way still counts as an offer.
    assert flow_runtime._REQUEST_OFFER.search("Want me to put it together as a request for my team to price?")


@pytest.mark.asyncio
async def test_submitted_directives_never_name_the_team_as_a_third_party(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "storage_dir", str(tmp_path))
    state = FlowState(thread_id="t", opening_delivered=True, user_turns=3, client_flow_aware=True,
                      quote_request=QuoteRequestRef(id="qr_y"))
    plan = flow_runtime._engine.plan_turn(state, "is it in?")
    text = flow_runtime._build_directives(state, plan, opening=False, price_guidance=None, quotes_to_present=None)
    assert "HAS been submitted" in text
    assert "the TakeShape team" not in text
    # Sep 17 (#101): the directive keeps the agent as the subject and
    # explicitly forbids handing the sentence to the people behind it.
    assert "YOU are getting it in front of local providers" in text
    assert "a person on my team reviews every request" in text  # named as the thing NOT to say
    assert "do\n            NOT make" in text or "NOT make the people working behind you the story" in text
