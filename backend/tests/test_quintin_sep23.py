"""Quintin's Sep 23 notes from the TestFlight build.

1. "The AI is still talking in terms of 'my team will get pricing'" -- the
   request-offer directive literally told the model to say "a request for my
   team to price". The #101 sweep missed it because it does not match the
   "a person on my team" pattern.
2. "it mentioned 2 Windows, but there's actually 8" -- RoomPlan counts window
   surfaces; a bank of side-by-side sashes is one. The number is a floor,
   never a count of windows.
3. "over 30 minutes and no email" -- on a deployment with an ops address but
   no mail transport, every lead is composed and written to the outbox on
   disk, and /health said nothing about it.
"""

from __future__ import annotations

import pytest

import app.flow_runtime as flow_runtime
from app.config import settings
from app.flow.pricing import compute_price_guidance
from app.flow.state import FlowState
from app.main import config_problems


def _directives(state: FlowState, message: str) -> str:
    plan = flow_runtime._engine.plan_turn(state, message)
    return flow_runtime._build_directives(
        state, plan, opening=False, price_guidance=None, quotes_to_present=None
    )


# ---------------------------------------------------------------- 1. voice
def test_the_request_offer_directive_keeps_the_agent_as_the_one_getting_pricing():
    state = FlowState(thread_id="t", opening_delivered=True, user_turns=4, client_flow_aware=True)
    state.slots.first_name = "Quintin"
    state.slots.project_type = "Painting"
    state.slots.scope_options = ["trim only"]
    state.slots.zip = "37130"
    state.slots.contact_phone = "555-0100"
    text = _directives(state, "ok")
    assert "get real pricing from local companies" in text
    assert "for my team to price" not in text


def test_no_directive_hands_the_pricing_or_the_vetting_to_a_team(monkeypatch, tmp_path):
    """Rendered directives for the states the homeowner actually reaches:
    offer pending, request submitted, quotes on file. The one permitted
    'my team' is inside the quoted example of what NOT to say."""
    from app.flow.state import QuoteRequestRef

    monkeypatch.setattr(settings, "storage_dir", str(tmp_path))
    example = "no 'a person on my team reviews every request'"

    submitted = FlowState(
        thread_id="t2", opening_delivered=True, user_turns=5, client_flow_aware=True,
        quote_request=QuoteRequestRef(id="qr_x"),
    )
    plan = flow_runtime._engine.plan_turn(submitted, "is it in?")
    text = flow_runtime._build_directives(
        submitted, plan, opening=False, price_guidance=None, quotes_to_present=None,
        quotes_on_file=[{"providerName": "Brightline", "provider": {"relationship": "prospect"}}],
    )
    assert example in text
    assert "my team" not in text.replace(example, "")


# ------------------------------------------------------------- 2. windows
def test_window_counts_are_called_openings_and_the_homeowner_is_asked():
    state = FlowState(thread_id="t", opening_delivered=True, user_turns=2, client_flow_aware=True)
    text = _directives(state, "how many windows do I have?")
    assert "WINDOW COUNTS FROM THE SCAN ARE OPENINGS, NOT WINDOWS" in text
    assert "ask the homeowner how many windows are in those sections" in text
    assert "individual count not confirmed" in text


def test_the_lead_email_labels_the_scan_number_as_openings():
    from app.flow import ops_email

    lines = ops_email._fmt_measurements({"windowCount": 2, "doorCount": 1, "floorAreaSquareFeet": 440})
    joined = "\n".join(lines)
    assert "Window openings (scan): 2" in joined
    assert "Windows: 2" not in joined


def test_per_window_price_basis_says_openings():
    class _R:
        low_per_sqft, high_per_sqft, region_label, fetched_at = 1.0, 2.0, "Nashville", 0.0

    g = compute_price_guidance("Window Cleaning", 440.0, researched=_R(), window_count=2)
    assert "window openings counted in the walk" in g.basis
    assert " windows counted" not in g.basis


# --------------------------------------------------------- 3. mail transport
def test_health_warns_when_ops_email_has_no_way_to_send(monkeypatch):
    monkeypatch.setattr(settings, "ops_email", "quintin@example.com")
    monkeypatch.setattr(settings, "resend_api_key", "")
    monkeypatch.setattr(settings, "smtp_host", "")
    monkeypatch.setattr(settings, "smtp_username", "")
    monkeypatch.setattr(settings, "smtp_password", "")
    problems = config_problems()
    assert any("no mail transport" in p and "captured to the outbox" in p for p in problems)


def test_health_is_quiet_when_resend_is_configured(monkeypatch):
    monkeypatch.setattr(settings, "ops_email", "quintin@example.com")
    monkeypatch.setattr(settings, "resend_api_key", "re_test")
    assert not any("no mail transport" in p for p in config_problems())


def test_health_is_quiet_when_smtp_is_configured(monkeypatch):
    monkeypatch.setattr(settings, "ops_email", "quintin@example.com")
    monkeypatch.setattr(settings, "resend_api_key", "")
    monkeypatch.setattr(settings, "smtp_host", "smtp.example.com")
    monkeypatch.setattr(settings, "smtp_username", "u")
    monkeypatch.setattr(settings, "smtp_password", "p")
    assert not any("no mail transport" in p for p in config_problems())


def test_health_is_quiet_when_no_ops_email_at_all(monkeypatch):
    """No address means no expectation of mail; that case already logs itself."""
    monkeypatch.setattr(settings, "ops_email", "")
    monkeypatch.setattr(settings, "resend_api_key", "")
    assert not any("no mail transport" in p for p in config_problems())
