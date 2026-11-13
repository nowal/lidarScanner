"""Find pending deliveries without hydrating the entire quote history.

These are read-only candidate queries. Delivery still re-reads the record and
uses the existing claims, receipts, and idempotency keys before sending mail.
"""
import logging
from pathlib import Path

from ..config import settings
from . import acceptances, supabase_store

logger = logging.getLogger("lidarai.flow.email_recovery")
PAGE_SIZE = 200


async def _rows(path: str, params: dict[str, str]) -> list[dict]:
    # Finish discovery before sending anything: offset pagination must not be
    # interleaved with our own updates to the pending-delivery predicates.
    rows = []
    while True:
        response = await supabase_store._rest().get(path, params={
            **params, "limit": str(PAGE_SIZE), "offset": str(len(rows)),
        })
        response.raise_for_status()
        page = response.json()
        rows.extend(page)
        if len(page) < PAGE_SIZE:
            return rows


def _record(raw):
    from ..flow_quotes import QuoteRequestRecord
    try:
        return QuoteRequestRecord.model_validate(raw)
    except ValueError:
        logger.warning("Skipping invalid email recovery record")
        return None


async def pending_leads(transport_ready: bool):
    from ..flow_quotes import quote_store

    if supabase_store.enabled():
        params = {
            "select": "record", "order": "created_at.asc,id.asc",
            "status": "eq.submitted",
            "record->>opsEmailQueuedAt": "not.is.null",
            "record->>opsEmailDeliveredAt": "is.null",
        }
        if not transport_ready:
            params["record->>opsEmailCapturedAt"] = "is.null"
        rows = await _rows("/flow_quote_requests", params)
        records = [record for row in rows if (record := _record(row["record"]))]
    else:
        records = await quote_store.list("submitted", include_provider_responses=False)

    candidates = {record.id: record for record in records
                  if record.opsEmailQueuedAt and not record.opsEmailDeliveredAt
                  and (transport_ready or not record.opsEmailCapturedAt)}
    # Old builds incorrectly stamped a disk capture as delivered. Only inspect
    # IDs with surviving lead outbox files; don't scan all delivered history.
    for path in (Path(settings.storage_dir) / "ops_outbox").glob("qr_*.json"):
        if path.stem in candidates:
            continue
        record = await quote_store.get(path.stem, include_provider_responses=False)
        if (record and record.status == "submitted" and record.opsEmailQueuedAt and record.opsEmailDeliveredAt
                and not record.opsEmailCapturedAt):
            candidates[record.id] = record
    # Preserve the old eligibility rule: immutable bids can advance a stale
    # submitted parent to quotes_ready. Hydrate only pending candidates.
    records = [await quote_store._with_provider_responses(record) for record in candidates.values()]
    return [record for record in records if record.status == "submitted"]


def _pending_acceptance(record, transport_ready: bool) -> bool:
    quote = record.quote_by_id(record.selectedQuoteId) if record.selectedQuoteId else None
    return bool(quote and quote.decision == "approved" and (
        (quote.acceptanceNotificationQueuedAt and not quote.acceptanceNotificationDeliveredAt
         and (transport_ready or not quote.acceptanceNotificationCapturedAt))
        or (quote.homeownerConfirmationQueuedAt and not quote.homeownerConfirmationDeliveredAt
            and (transport_ready or not quote.homeownerConfirmationCapturedAt))))


async def pending_acceptances(transport_ready: bool):
    from ..flow_quotes import quote_store

    if not supabase_store.enabled():
        records = await quote_store.list(include_provider_responses=False)
        # The SQLite receipt is authoritative even when the cached parent is
        # stale, just as it is in the production query below.
        records = [acceptances.apply(record, await acceptances.read(record.id))
                   for record in records]
    else:
        predicates = []
        for channel, queued in (("provider", "acceptanceNotificationQueuedAt"),
                                ("homeowner", "homeownerConfirmationQueuedAt")):
            predicate = f"and(quote->>{queued}.not.is.null,{channel}_delivered.is.null"
            if not transport_ready:
                predicate += f",{channel}_captured.is.null"
            predicates.append(predicate + ")")
        rows = await _rows("/flow_quote_acceptances", {
            "select": "*,flow_quote_requests!inner(record)",
            "order": "request_id.asc", "or": "(" + ",".join(predicates) + ")",
        })
        records = [acceptances.apply(record, row) for row in rows
                   if (record := _record(row["flow_quote_requests"]["record"]))]
        # Preserve recovery for older approvals without an immutable receipt.
        # The anti-join excludes delivered receipts, including stale parents.
        legacy = await _rows("/flow_quote_requests", {
            "select": "record,flow_quote_acceptances()",
            "flow_quote_acceptances": "is.null",
            "record->>selectedQuoteId": "not.is.null",
            "record->quotes": 'cs.[{"decision":"approved"}]',
            "order": "created_at.asc,id.asc",
        })
        records.extend(record for row in legacy if (record := _record(row["record"])))
    return [record for record in records if _pending_acceptance(record, transport_ready)]
