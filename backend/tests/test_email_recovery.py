"""Recovery discovers pending work without downloading/hydrating all history."""
import asyncio

import httpx
import pytest

from app.config import settings
from app.flow import acceptances, email_recovery, ops_email, supabase_store
from app.flow_quotes import ApprovalContact, ReturnedQuote, quote_store
from test_ops_email import _record


@pytest.fixture(autouse=True)
def storage(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "storage_dir", str(tmp_path))


@pytest.fixture
def remote(monkeypatch):
    def install(handler):
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://db.test/rest/v1")
        monkeypatch.setattr(supabase_store, "enabled", lambda: True)
        monkeypatch.setattr(supabase_store, "_rest", lambda: client)
        return client
    return install


def accepted_record(id="qr_accepted", **overrides):
    quote = ReturnedQuote(
        id="q", providerName="Provider", priceUsd=10, decision="approved",
        approvalMethod="callback", approvalContact=ApprovalContact(
            fullName="Test Owner", email="owner@example.test", phone="6155550199",
            address="123 Test Street, Nashville, TN 37203"),
        acceptanceNotificationQueuedAt="2026-11-01T00:00:00Z",
        homeownerConfirmationQueuedAt="2026-11-01T00:00:00Z", **overrides,
    )
    return _record(id=id, selectedQuoteId=quote.id, quotes=[quote])


@pytest.mark.asyncio
async def test_idle_workers_use_three_filtered_queries_and_no_per_quote_reads(remote, monkeypatch):
    requests = []
    def handle(request):
        requests.append(request)
        assert request.method == "GET"
        return httpx.Response(200, json=[])
    client = remote(handle)
    monkeypatch.setattr(ops_email, "_queue", asyncio.Queue())
    async with client:
        assert await ops_email.requeue_undelivered() == 0
        assert await ops_email.retry_pending_acceptances() == 0
    assert len(requests) == 3
    lead, receipt, legacy = requests
    assert lead.url.params["status"] == "eq.submitted"
    assert lead.url.params["record->>opsEmailQueuedAt"] == "not.is.null"
    assert lead.url.params["record->>opsEmailDeliveredAt"] == "is.null"
    assert lead.url.params["record->>opsEmailCapturedAt"] == "is.null"
    assert receipt.url.path.endswith("/flow_quote_acceptances")
    assert "provider_delivered.is.null" in receipt.url.params["or"]
    assert "homeowner_delivered.is.null" in receipt.url.params["or"]
    assert "provider_captured.is.null" in receipt.url.params["or"]
    assert legacy.url.params["flow_quote_acceptances"] == "is.null"
    assert legacy.url.params["record->>selectedQuoteId"] == "not.is.null"
    assert all("flow_provider_responses" not in str(r.url) for r in requests)


@pytest.mark.asyncio
async def test_pending_lead_discovery_paginates_without_missing_backlog(remote, monkeypatch):
    monkeypatch.setattr(email_recovery, "PAGE_SIZE", 2)
    backlog = [_record(id=f"qr_{i}", opsEmailQueuedAt="queued").model_dump(mode="json") for i in range(5)]
    offsets = []
    def handle(request):
        if not request.url.path.endswith("/flow_quote_requests"):
            return httpx.Response(200, json=[])
        offset = int(request.url.params["offset"])
        offsets.append(offset)
        assert "record->>opsEmailCapturedAt" not in request.url.params
        return httpx.Response(200, json=[{"record": r} for r in backlog[offset:offset + 2]])
    async with remote(handle):
        records = await email_recovery.pending_leads(True)
    assert [r.id for r in records] == [r["id"] for r in backlog]
    assert offsets == [0, 2, 4]


@pytest.mark.asyncio
async def test_query_failure_does_not_send_stale_local_work(remote, monkeypatch):
    await quote_store.save(_record(opsEmailQueuedAt="queued"))
    monkeypatch.setattr(ops_email, "_queue", asyncio.Queue())
    async with remote(lambda request: httpx.Response(503)):
        assert await ops_email.requeue_undelivered() == 0
        with pytest.raises(httpx.HTTPStatusError):
            await ops_email.retry_pending_acceptances()
    assert ops_email._queue.empty()


@pytest.mark.asyncio
async def test_receipt_overrides_stale_parent_and_legacy_approval_remains_recoverable(remote):
    current = accepted_record()
    # Parent says sent, but the authoritative homeowner receipt still needs work.
    current.quotes[0].homeownerConfirmationDeliveredAt = "stale-parent-stamp"
    legacy = accepted_record("qr_legacy")
    receipt = dict(request_id=current.id, quote=current.quotes[0].model_dump(mode="json"),
                   provider_delivered="sent", homeowner_delivered=None,
                   flow_quote_requests={"record": current.model_dump(mode="json")})
    def handle(request):
        if request.url.path.endswith("/flow_quote_acceptances"):
            assert "_captured.is.null" not in request.url.params["or"]
            return httpx.Response(200, json=[receipt])
        return httpx.Response(200, json=[{"record": legacy.model_dump(mode="json")}])
    async with remote(handle):
        records = await email_recovery.pending_acceptances(True)
    assert {r.id for r in records} == {current.id, legacy.id}
    quote = records[0].quote_by_id("q")
    assert quote.homeownerConfirmationDeliveredAt is None
    assert quote.acceptanceNotificationDeliveredAt == "sent"


@pytest.mark.asyncio
@pytest.mark.parametrize("channel", ["provider", "homeowner"])
async def test_retry_sends_only_failed_recipient_then_stops(channel, monkeypatch):
    record = accepted_record()
    await quote_store.save(record)
    await acceptances.commit(record, record.quotes[0])
    other = "provider" if channel == "homeowner" else "homeowner"
    assert await acceptances.delivery(record.id, other, "prior")
    assert await acceptances.delivery(record.id, other, "prior", "sent")
    sent = []
    async def provider(*args): sent.append("provider"); return "sent"
    async def homeowner(*args): sent.append("homeowner"); return "sent"
    monkeypatch.setattr(ops_email, "send_decision_email", provider)
    monkeypatch.setattr(ops_email, "send_homeowner_confirmation", homeowner)
    assert await ops_email.retry_pending_acceptances() == 1
    assert sent == [channel]
    assert await ops_email.retry_pending_acceptances() == 0
    assert sent == [channel]


@pytest.mark.asyncio
async def test_capture_retries_only_when_transport_is_ready():
    record = accepted_record(acceptanceNotificationCapturedAt="captured", homeownerConfirmationCapturedAt="captured")
    await quote_store.save(record)
    await acceptances.commit(record, record.quotes[0])
    assert await email_recovery.pending_acceptances(False) == []
    assert [r.id for r in await email_recovery.pending_acceptances(True)] == [record.id]


@pytest.mark.asyncio
async def test_unqueued_and_delivered_history_is_not_recovered():
    for record in (
        _record(id="qr_not_queued"),
        _record(id="qr_delivered", opsEmailQueuedAt="queued", opsEmailDeliveredAt="sent"),
        _record(id="qr_captured", opsEmailQueuedAt="queued", opsEmailCapturedAt="captured"),
        _record(id="qr_advanced", status="ops_received", opsEmailQueuedAt="queued"),
        _record(id="qr_pending", opsEmailQueuedAt="queued"),
    ):
        await quote_store.save(record)
    assert [r.id for r in await email_recovery.pending_leads(False)] == ["qr_pending"]
    assert {r.id for r in await email_recovery.pending_leads(True)} == {"qr_captured", "qr_pending"}


@pytest.mark.asyncio
async def test_legacy_acceptance_is_retried_without_discovering_new_bids(monkeypatch):
    record = accepted_record("qr_old")
    record.quotes[0].homeownerConfirmationQueuedAt = None
    await quote_store.save(record)
    sent = []
    async def send(*args): sent.append("provider"); return "sent"
    monkeypatch.setattr(ops_email, "send_decision_email", send)
    assert await ops_email.retry_pending_acceptances() == 1
    assert sent == ["provider"]
    assert await ops_email.retry_pending_acceptances() == 0


@pytest.mark.asyncio
async def test_lead_with_late_immutable_bid_keeps_existing_recovery_eligibility(monkeypatch):
    from app.flow import provider_responses
    await quote_store.save(_record(opsEmailQueuedAt="queued"))
    async def bids(request_id):
        return [ReturnedQuote(id="late", providerName="Provider", priceUsd=10)]
    monkeypatch.setattr(provider_responses, "responses", bids)
    assert await email_recovery.pending_leads(True) == []
