"""Approve / decline with comments, and telling the homeowner the lead landed.

Quintin, Sep 17 (via Nathan): the homeowner should be able to approve or
deny a returned quote and say why, in the conversation with the agent, and
should be told in the chat when their request actually reaches the people
who line up providers.

Approving is the same act as selecting -- it is the address-release moment
(SOW §12). Declining releases nothing, and the reason that comes with it is
the part operations needs.
"""

from __future__ import annotations

import pytest

import app.flow_runtime as flow_runtime
from app.config import settings
from app.flow import ops_email
from app.flow.state import FlowState, QuoteRequestRef
from app.flow_api import _record_decision
from app.flow_quotes import QuoteRequestRecord, ReturnedQuote, quote_store
from app.models import now_utc


def _record(**kw) -> QuoteRequestRecord:
    base = dict(
        id="qr_dec",
        threadId="t-dec",
        createdAt=now_utc().isoformat(),
        status="quotes_ready",
        serviceType="Painting",
        address="12 Elm St",
        quotes=[
            ReturnedQuote(id="q1", providerName="Brightline Painting", priceUsd=2450),
            ReturnedQuote(id="q2", providerName="Harbor Coatings", priceLowUsd=2100, priceHighUsd=2900),
        ],
    )
    base.update(kw)
    return QuoteRequestRecord(**base)


@pytest.fixture(autouse=True)
def _quiet_ops_email(monkeypatch):
    """Every decision notifies operations; capture instead of sending."""
    sent: list[tuple[str, str]] = []

    async def _capture(subject, body, html=None, *, outbox_key="message"):
        sent.append((subject, body))
        return "sent"

    monkeypatch.setattr(ops_email, "send_ops_message", _capture)
    return sent


# ---------------------------------------------------------------- decisions
@pytest.mark.asyncio
async def test_approving_records_the_verdict_and_releases_the_address(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "storage_dir", str(tmp_path))
    record = _record()
    await quote_store.save(record)

    await _record_decision(record, "q1", "approved", "Looks right, let's go")

    quote = record.quote_by_id("q1")
    assert quote.decision == "approved"
    assert quote.decisionNote == "Looks right, let's go"
    assert quote.decidedAt
    # Approving IS selecting: the address is released to that provider.
    assert record.selectedQuoteId == "q1"


@pytest.mark.asyncio
async def test_declining_records_the_reason_and_releases_nothing(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "storage_dir", str(tmp_path))
    record = _record()
    await quote_store.save(record)

    await _record_decision(record, "q2", "declined", "Too high for the scope we discussed")

    quote = record.quote_by_id("q2")
    assert quote.decision == "declined"
    assert quote.decisionNote == "Too high for the scope we discussed"
    assert record.selectedQuoteId is None, "a decline must never release the address"


@pytest.mark.asyncio
async def test_declining_the_approved_quote_takes_the_approval_back(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "storage_dir", str(tmp_path))
    record = _record()
    await quote_store.save(record)

    await _record_decision(record, "q1", "approved", None)
    assert record.selectedQuoteId == "q1"
    await _record_decision(record, "q1", "declined", "Changed my mind")

    assert record.selectedQuoteId is None, (
        "leaving the selection on a declined quote would tell the agent the "
        "address is out after the homeowner changed their mind"
    )


@pytest.mark.asyncio
async def test_an_empty_comment_is_stored_as_no_comment(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "storage_dir", str(tmp_path))
    record = _record()
    await quote_store.save(record)
    await _record_decision(record, "q1", "approved", "   ")
    assert record.quote_by_id("q1").decisionNote is None


@pytest.mark.asyncio
async def test_the_decision_reaches_the_homeowner_view(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "storage_dir", str(tmp_path))
    record = _record()
    await quote_store.save(record)
    await _record_decision(record, "q2", "declined", "Too high")

    view = record.homeowner_view()
    declined = next(q for q in view["quotes"] if q["id"] == "q2")
    assert declined["decision"] == "declined"
    assert declined["decisionNote"] == "Too high"
    assert declined["decidedAt"]


# ---------------------------------------------------------- operations mail
@pytest.mark.asyncio
async def test_operations_is_told_about_an_approval_with_the_address(
    monkeypatch, tmp_path, _quiet_ops_email
):
    monkeypatch.setattr(settings, "storage_dir", str(tmp_path))
    record = _record()
    await quote_store.save(record)

    await _record_decision(record, "q1", "approved", "Looks right")

    subject, body = _quiet_ops_email[-1]
    assert subject.startswith("APPROVED: Brightline Painting")
    assert "qr_dec" in subject
    assert "12 Elm St" in body, "approval is the address-release moment"
    assert "Looks right" in body


@pytest.mark.asyncio
async def test_operations_is_told_about_a_decline_without_the_address(
    monkeypatch, tmp_path, _quiet_ops_email
):
    monkeypatch.setattr(settings, "storage_dir", str(tmp_path))
    record = _record()
    await quote_store.save(record)

    await _record_decision(record, "q2", "declined", "Too high for the scope")

    subject, body = _quiet_ops_email[-1]
    assert subject.startswith("DECLINED: Harbor Coatings")
    assert "Too high for the scope" in body
    assert "12 Elm St" not in body, "a decline releases nothing"
    assert "No address was released" in body


@pytest.mark.asyncio
async def test_a_mail_failure_never_breaks_the_decision(monkeypatch, tmp_path):
    """The homeowner's verdict is recorded even when ops mail is down."""
    monkeypatch.setattr(settings, "storage_dir", str(tmp_path))

    async def _boom(*a, **k):
        raise RuntimeError("smtp down")

    monkeypatch.setattr(ops_email, "send_decision_email", _boom)
    record = _record()
    await quote_store.save(record)

    await _record_decision(record, "q1", "approved", None)
    assert record.quote_by_id("q1").decision == "approved"


# ------------------------------------------------------------- the agent
@pytest.mark.asyncio
async def test_the_agent_is_told_what_was_declined_and_why(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "storage_dir", str(tmp_path))
    record = _record()
    record.quotes[1].decision = "declined"
    record.quotes[1].decisionNote = "Too high for the scope"
    state = FlowState(
        thread_id="t-dec", opening_delivered=True, user_turns=3,
        client_flow_aware=True, quote_request=QuoteRequestRef(id="qr_dec"),
    )
    plan = flow_runtime._engine.plan_turn(state, "what happened with the other one?")
    text = flow_runtime._build_directives(
        state, plan, opening=False, price_guidance=None, quotes_to_present=None,
        declined_quotes=flow_runtime._declined_quotes(record),
    )
    assert "DECLINED: Harbor Coatings" in text
    assert "Too high for the scope" in text
    assert "do not talk them back into it" in text
    assert "address did NOT go to anyone they declined" in text


@pytest.mark.asyncio
async def test_no_decline_directive_when_nothing_was_declined(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "storage_dir", str(tmp_path))
    state = FlowState(
        thread_id="t-dec", opening_delivered=True, user_turns=3,
        client_flow_aware=True, quote_request=QuoteRequestRef(id="qr_dec"),
    )
    plan = flow_runtime._engine.plan_turn(state, "any news?")
    text = flow_runtime._build_directives(
        state, plan, opening=False, price_guidance=None, quotes_to_present=None,
        declined_quotes=flow_runtime._declined_quotes(_record()),
    )
    assert "DECLINED" not in text


# --------------------------------------------------- the lead landed (chat)
@pytest.mark.asyncio
async def test_the_agent_may_confirm_the_lead_landed_once_it_has(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "storage_dir", str(tmp_path))
    state = FlowState(
        thread_id="t-dec", opening_delivered=True, user_turns=3,
        client_flow_aware=True, quote_request=QuoteRequestRef(id="qr_dec"),
    )
    plan = flow_runtime._engine.plan_turn(state, "did that actually go anywhere?")
    delivered = flow_runtime._build_directives(
        state, plan, opening=False, price_guidance=None, quotes_to_present=None,
        lead_delivered=True,
    )
    assert "has now LANDED" in delivered
    assert "without naming anyone behind" in delivered

    undelivered = flow_runtime._build_directives(
        state, plan, opening=False, price_guidance=None, quotes_to_present=None,
        lead_delivered=False,
    )
    assert "LANDED" not in undelivered, "never claim delivery we cannot see"


@pytest.mark.asyncio
async def test_the_welcome_back_says_the_request_landed(monkeypatch, tmp_path):
    from app.flow_runtime import HomeAIOpeningRequest

    monkeypatch.setattr(settings, "storage_dir", str(tmp_path))
    record = _record(status="submitted", quotes=[])
    record.opsEmailDeliveredAt = now_utc().isoformat()
    await quote_store.save(record)
    state = FlowState(
        thread_id="t-dec", opening_delivered=True, user_turns=4,
        client_flow_aware=True, quote_request=QuoteRequestRef(id="qr_dec"),
        slots=FlowState().slots,
    )
    state.slots.project_type = "Painting"
    await flow_runtime.persist_flow_state(state)

    response = await flow_runtime.run_opening_turn(HomeAIOpeningRequest(threadId="t-dec"))
    content = response.message.content
    assert "has landed" in content
    assert "I've got it in front of local providers" in content
    # #101: the people behind the agent stay out of it.
    assert "my team" not in content and "a person" not in content
