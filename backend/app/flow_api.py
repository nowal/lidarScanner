"""Flow endpoints (API_CONTRACT_V1 §4–§7): the opening turn, quote-request
submission with the ops lead package, and the operations results-return API.

Mounted from ``main.py``. Homeowner-facing routes use the existing service
bearer token plus the optional ``X-Homeowner-Token``; ops routes use a
separate ``LIDARAI_OPS_TOKEN`` and refuse to exist until it is configured.
"""

from __future__ import annotations

import asyncio
import hmac
import html as html_mod
import logging
import re
from pathlib import Path
from typing import Any, Literal, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

from .config import settings
from .flow import FlowEngine
from .flow.identity import resolve_homeowner_token
from .flow_quotes import (
    EXPECTATIONS,
    VALID_STATUSES,
    ReturnedQuote,
    create_quote_request,
    deliver_to_ops,
    quote_store,
)
from .flow import ops_email
from .flow.ops_email import send_ops_email, verify_entry_signature
from .flow_runtime import (
    HomeAIOpeningRequest,
    _attach_identity,
    encode_flow_token,
    persist_flow_state,
    resolve_flow_state,
    run_opening_turn,
)
from .home_ai import HomeAIChatResponse, HomeAIContextPacket
from .models import SCHEMA_VERSION, now_utc

logger = logging.getLogger("lidarai.flow.api")

router = APIRouter(prefix=settings.api_prefix)
_engine = FlowEngine()

# Public brand asset for ops emails (email clients fetch images over HTTP;
# a logo is public branding, so no auth here).
_LOGO_PATH = Path(__file__).resolve().parent.parent / "demo_assets" / "takeshape-logo.png"


@router.get("/assets/takeshape-logo.png", include_in_schema=False)
async def takeshape_logo() -> FileResponse:
    return FileResponse(
        _LOGO_PATH, media_type="image/png", headers={"Cache-Control": "public, max-age=86400"}
    )


def _tokens_match(provided: str | None, expected: str) -> bool:
    return hmac.compare_digest(provided or "", expected)


def require_token(
    authorization: Optional[str] = Header(default=None),
    x_service_token: Optional[str] = Header(default=None),
) -> None:
    if not settings.auth_token:
        return
    if _tokens_match(x_service_token, settings.auth_token):
        return
    if not _tokens_match(authorization, f"Bearer {settings.auth_token}"):
        raise HTTPException(status_code=401, detail="Invalid auth token")


def require_ops_token(authorization: Optional[str] = Header(default=None)) -> None:
    """Ops routes are separate-audience: no configured ops token means the
    surface is closed, not open."""
    if not settings.ops_token:
        raise HTTPException(status_code=503, detail="Ops API is not configured")
    if not _tokens_match(authorization, f"Bearer {settings.ops_token}"):
        raise HTTPException(status_code=401, detail="Invalid ops token")


async def homeowner_id_from_header(x_homeowner_token: Optional[str]) -> str | None:
    return await resolve_homeowner_token(
        x_homeowner_token,
        supabase_url=settings.supabase_url,
        api_key=settings.supabase_service_role_key,
        jwt_secret=settings.supabase_jwt_secret,
    )


# --------------------------------------------------------------------------
# Opening turn (steps 1–2)
# --------------------------------------------------------------------------
@router.post(
    "/ai/home-chat/opening",
    response_model=HomeAIChatResponse,
    dependencies=[Depends(require_token)],
)
async def home_ai_opening(
    request_body: HomeAIOpeningRequest,
    x_homeowner_token: Optional[str] = Header(default=None),
) -> HomeAIChatResponse:
    return await run_opening_turn(
        request_body, homeowner_id=await homeowner_id_from_header(x_homeowner_token)
    )


# --------------------------------------------------------------------------
# Quote requests (step 9)
# --------------------------------------------------------------------------
class QuoteRequestSubmission(BaseModel):
    threadId: str
    flowToken: Optional[str] = None
    confirm: bool = False
    # Optional context snapshot so the lead package carries measurements.
    homeContext: Optional[HomeAIContextPacket] = None


@router.post("/ai/quote-requests", dependencies=[Depends(require_token)], status_code=201)
async def submit_quote_request(
    body: QuoteRequestSubmission,
    x_homeowner_token: Optional[str] = Header(default=None),
) -> JSONResponse:
    if not body.confirm:
        raise HTTPException(
            status_code=400,
            detail="Quote requests require explicit confirmation (confirm: true)",
        )
    state = await resolve_flow_state(body.threadId, body.flowToken)
    await _attach_identity(state, await homeowner_id_from_header(x_homeowner_token))

    # Submission needs the real values (the token carries captured-flags
    # only; resolve_flow_state merges values back from the durable store).
    missing = _engine.missing_submission_slots(state, require_values=True)
    if missing:
        return JSONResponse(
            status_code=409,
            content={
                "schemaVersion": SCHEMA_VERSION,
                "error": "missing_slots",
                "missingSlots": missing,
            },
        )
    # A lead package needs someone ops can reach: a verified homeowner
    # identity or contact details captured in conversation.
    if not (
        (not state.homeowner_is_guest and (state.homeowner_id or state.homeowner_auth_sub))
        or state.slots.contact_email
        or state.slots.contact_phone
    ):
        raise HTTPException(
            status_code=401,
            detail="A homeowner identity (X-Homeowner-Token) or captured contact info is required",
        )
    if state.quote_request is not None:
        existing = await quote_store.get(state.quote_request.id)
        if existing is not None:
            return JSONResponse(
                status_code=200,
                content={
                    "schemaVersion": SCHEMA_VERSION,
                    "quoteRequestId": existing.id,
                    "status": existing.status,
                    "expectations": EXPECTATIONS,
                },
            )

    if state.home_id:
        from .flow.home_registry import load_index_async
        index = await load_index_async(state.home_id)
        if index and index.upload and not index.upload.get("modelsReady"):
            return JSONResponse(status_code=409, content={
                "schemaVersion": SCHEMA_VERSION, "error": "scan_upload_pending",
                "missingSlots": ["final_model_upload"],
            })
    measurements = _measurements_from_context(body.homeContext)
    record = await create_quote_request(
        state,
        thread_id=body.threadId,
        measurements=measurements,
        quote_draft=None,
    )
    # The submission advanced the flow; bump the revision so the durable copy
    # outranks any pre-submission token a client might echo, and hand back a
    # fresh token for clients that adopt it.
    state.revision += 1
    await persist_flow_state(state)
    delivered = await deliver_to_ops(record)
    # The ops email runs provider research that can take a minute or two —
    # never on the homeowner's submit. Fire it in the background; the
    # package stays retrievable via the ops API (and resendable) regardless.
    emailed = "scheduled" if _schedule_ops_email(record) else "disabled"
    if emailed == "scheduled":
        # Durable "queued, not yet delivered": the worker re-drives it after
        # a restart (see ops_email.requeue_undelivered).
        record.opsEmailQueuedAt = now_utc().isoformat()
        await quote_store.save(record)
    logger.info(
        "Quote request %s created (thread=%s, webhook_delivered=%s, ops_email=%s)",
        record.id,
        body.threadId,
        delivered,
        emailed,
    )
    return JSONResponse(
        status_code=201,
        content={
            "schemaVersion": SCHEMA_VERSION,
            "quoteRequestId": record.id,
            "status": record.status,
            "expectations": EXPECTATIONS,
            "flowToken": encode_flow_token(state),
        },
    )


def _schedule_ops_email(record) -> bool:
    """Hand the lead to the startup-owned worker.

    Not a task spawned here: a task created inside a request handler dies
    with the request's context, which silently lost a lead package when the
    submission arrived through the demo proxy.
    """
    from .flow.ops_email import queue_ops_email

    return queue_ops_email(record)


def _measurements_from_context(context: HomeAIContextPacket | None) -> dict[str, Any]:
    if context is None:
        return {}
    totals = context.totals or {}
    area_m2 = totals.get("floorAreaSquareMeters")
    measurements: dict[str, Any] = {
        "note": "Approximate (bounding-box) measurements from the home capture.",
    }
    if isinstance(area_m2, (int, float)) and area_m2 > 0:
        measurements["floorAreaSquareFeet"] = round(float(area_m2) * 10.7639, 1)
    rooms = []
    for room in (context.rooms or [])[:8]:
        if not isinstance(room, dict):
            continue
        entry: dict[str, Any] = {"name": room.get("name")}
        area = room.get("floorAreaSquareMeters")
        if isinstance(area, (int, float)) and area > 0:
            entry["floorAreaSquareFeet"] = round(float(area) * 10.7639, 1)
        for key in ("wallCount", "doorCount", "windowCount"):
            if isinstance(room.get(key), int):
                entry[key] = room[key]
        for window in room.get("windows") or []:
            if not isinstance(window, dict):
                continue
            width, height = window.get("widthMeters"), window.get("heightMeters")
            if isinstance(width, (int, float)) and isinstance(height, (int, float)) and width > 0 and height > 0:
                entry.setdefault("windowOpenings", []).append(
                    {"widthFeet": round(float(width) * 3.28084, 1), "heightFeet": round(float(height) * 3.28084, 1)}
                )
        rooms.append(entry)
    if rooms:
        measurements["rooms"] = rooms
        # A single-scan lead used to list its rooms by area only, so the
        # window-replacement request reached ops with no count and no sizes
        # (Quintin, Sep 24). The scan is the job here: roll the rooms up.
        for key in ("windowCount", "doorCount"):
            total = sum(int(r.get(key) or 0) for r in rooms)
            if total:
                measurements[key] = total
        openings = [o for r in rooms for o in r.get("windowOpenings") or []]
        if openings:
            measurements["windowOpenings"] = openings[:12]
    return measurements


async def _require_homeowner_access(record, x_homeowner_token: Optional[str]) -> None:
    """Quote requests attributed to a homeowner are theirs alone: the shared
    service token (present in every app install) is not enough to read one or
    to trigger the §12 address release. Anonymous records (contact captured
    in conversation, no identity) have nothing to bind to and stay
    service-token-gated."""
    if not record.homeownerId:
        return
    sub = await homeowner_id_from_header(x_homeowner_token)
    if not sub:
        raise HTTPException(
            status_code=403,
            detail="This quote request belongs to a signed-in homeowner; send X-Homeowner-Token",
        )
    if sub == record.homeownerId:
        return
    from .flow import supabase_store

    row = await supabase_store.resolve_homeowner(sub)
    if row and row.get("id") == record.homeownerId:
        return
    raise HTTPException(status_code=403, detail="Not your quote request")


@router.get("/ai/quote-requests/{request_id}", dependencies=[Depends(require_token)])
async def get_quote_request(
    request_id: str,
    x_homeowner_token: Optional[str] = Header(default=None),
) -> JSONResponse:
    record = await quote_store.get(request_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Quote request not found")
    await _require_homeowner_access(record, x_homeowner_token)
    return JSONResponse(record.homeowner_view())


class QuoteSelection(BaseModel):
    quoteId: str


class QuoteDecision(BaseModel):
    """The homeowner's verdict on one returned quote (Quintin, Sep 17).

    ``approved`` is the same act as selecting: it is the address-release
    moment (SOW §12). ``declined`` is a first-class answer -- the note that
    comes with it is what lets operations go back to that provider or find
    another one, and silence tells them nothing."""

    quoteId: str
    decision: Literal["approved", "declined"]
    note: Optional[str] = Field(default=None, max_length=2000)


@router.post("/ai/quote-requests/{request_id}/select", dependencies=[Depends(require_token)])
async def select_quote(
    request_id: str,
    body: QuoteSelection,
    x_homeowner_token: Optional[str] = Header(default=None),
) -> JSONResponse:
    """The homeowner picks a quote — this is the moment the address is
    released to operations/provider (SOW §12)."""
    record = await quote_store.get(request_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Quote request not found")
    await _require_homeowner_access(record, x_homeowner_token)
    if not any(q.id == body.quoteId for q in record.quotes):
        raise HTTPException(status_code=404, detail="Quote not found on this request")
    await _record_decision(record, body.quoteId, "approved", None)
    return JSONResponse(record.homeowner_view())


async def _record_decision(
    record: Any, quote_id: str, decision: str, note: str | None
) -> None:
    """Write one verdict and tell operations about it.

    Approving releases the address, which is what selecting has always
    done. Declining the quote that was approved takes the approval back:
    leaving ``selectedQuoteId`` pointing at a declined quote would tell the
    agent the address is out when the homeowner has just changed their
    mind."""
    quote = record.quote_by_id(quote_id)
    quote.decision = decision
    quote.decisionNote = (note or "").strip() or None
    quote.decidedAt = now_utc().isoformat()
    if decision == "approved":
        record.selectedQuoteId = quote_id
    elif record.selectedQuoteId == quote_id:
        record.selectedQuoteId = None
    await quote_store.save(record)
    # Operations hears about it on the same durable path as a lead, so a
    # decision is never lost to a mail outage.
    try:
        await ops_email.send_decision_email(record, quote)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Decision email for %s failed: %s", record.id, exc)


@router.post("/ai/quote-requests/{request_id}/decide", dependencies=[Depends(require_token)])
async def decide_quote(
    request_id: str,
    body: QuoteDecision,
    x_homeowner_token: Optional[str] = Header(default=None),
) -> JSONResponse:
    """Approve or decline one returned quote, with an optional comment."""
    record = await quote_store.get(request_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Quote request not found")
    await _require_homeowner_access(record, x_homeowner_token)
    if record.quote_by_id(body.quoteId) is None:
        raise HTTPException(status_code=404, detail="Quote not found on this request")
    await _record_decision(record, body.quoteId, body.decision, body.note)
    return JSONResponse(record.homeowner_view())


# --------------------------------------------------------------------------
# Operations API (step 10)
# --------------------------------------------------------------------------
@router.get("/ops/quote-requests", dependencies=[Depends(require_ops_token)])
async def ops_list_quote_requests(status: Optional[str] = None) -> JSONResponse:
    records = await quote_store.list(status=status)
    return JSONResponse(
        {
            "schemaVersion": SCHEMA_VERSION,
            "quoteRequests": [r.ops_view() for r in records],
        }
    )


@router.get("/ops/quote-requests/{request_id}", dependencies=[Depends(require_ops_token)])
async def ops_get_quote_request(request_id: str) -> JSONResponse:
    record = await quote_store.get(request_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Quote request not found")
    if record.status == "submitted":
        record.status = "ops_received"
        await quote_store.save(record)
    return JSONResponse(record.ops_view())


class OpsQuotesUpload(BaseModel):
    quotes: list[ReturnedQuote] = Field(min_length=1)


def _provider_key(name: str) -> str:
    return " ".join(name.split()).casefold()


def _upsert_returned_quotes(record, quotes: list[ReturnedQuote]) -> int:
    """Upsert by quote id, then by provider name.

    A retried curl (the documented ops workflow) must not show the homeowner
    the same quote twice, and re-sending a quote id corrects the earlier
    entry. The entry page mints a fresh id on every submission, so a
    corrected price for the same company used to stack a duplicate (seen
    2026-09-14: "Nash Painting" twice at two prices); a re-stated quote for a
    provider already on the request now replaces it, keeping the original
    quote id so a selection made against it still points at that provider.
    """
    by_id = {q.id: i for i, q in enumerate(record.quotes)}
    by_provider = {_provider_key(q.providerName): i for i, q in enumerate(record.quotes)}
    added = 0
    for quote in quotes:
        index = by_id.get(quote.id)
        if index is None:
            index = by_provider.get(_provider_key(quote.providerName))
            if index is not None:
                quote.id = record.quotes[index].id
        if index is not None:
            record.quotes[index] = quote
        else:
            by_id[quote.id] = len(record.quotes)
            by_provider[_provider_key(quote.providerName)] = len(record.quotes)
            record.quotes.append(quote)
            added += 1
    record.status = "quotes_ready"
    # A company that answered is worth remembering (provider promotion).
    from .flow.partners import note_quoted

    note_quoted(record, quotes)
    return added


@router.post("/ops/quote-requests/{request_id}/quotes", dependencies=[Depends(require_ops_token)])
async def ops_upload_quotes(request_id: str, body: OpsQuotesUpload) -> JSONResponse:
    record = await quote_store.get(request_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Quote request not found")
    if record.status == "closed":
        raise HTTPException(status_code=409, detail="Quote request is closed")
    added = _upsert_returned_quotes(record, body.quotes)
    await quote_store.save(record)
    logger.info(
        "Ops uploaded %d quote(s) for %s (%d new, %d replaced)",
        len(body.quotes), request_id, added, len(body.quotes) - added,
    )
    return JSONResponse(record.ops_view())


@router.delete(
    "/ops/quote-requests/{request_id}/quotes/{quote_id}",
    dependencies=[Depends(require_ops_token)],
)
async def ops_delete_quote(request_id: str, quote_id: str) -> JSONResponse:
    """Remove a mistakenly uploaded quote (before or after the homeowner saw it)."""
    record = await quote_store.get(request_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Quote request not found")
    remaining = [q for q in record.quotes if q.id != quote_id]
    if len(remaining) == len(record.quotes):
        raise HTTPException(status_code=404, detail="Quote not found on this request")
    record.quotes = remaining
    if record.selectedQuoteId == quote_id:
        record.selectedQuoteId = None
    await quote_store.save(record)
    return JSONResponse(record.ops_view())


class OpsStatusUpdate(BaseModel):
    status: str


# Lifecycle guard: ops can move a request forward or close it; a closed
# request stays closed (reopen deliberately via in_progress is not offered —
# create a new request instead). "presented" is written by the agent when the
# homeowner actually saw the quotes.
_STATUS_TRANSITIONS: dict[str, set[str]] = {
    "submitted": {"ops_received", "in_progress", "quotes_ready", "closed"},
    "ops_received": {"in_progress", "quotes_ready", "closed"},
    "in_progress": {"ops_received", "quotes_ready", "closed"},
    "quotes_ready": {"in_progress", "presented", "closed"},
    "presented": {"in_progress", "quotes_ready", "closed"},
    "closed": set(),
}


@router.post("/ops/quote-requests/{request_id}/status", dependencies=[Depends(require_ops_token)])
async def ops_update_status(request_id: str, body: OpsStatusUpdate) -> JSONResponse:
    if body.status not in VALID_STATUSES:
        raise HTTPException(status_code=400, detail=f"Status must be one of {sorted(VALID_STATUSES)}")
    record = await quote_store.get(request_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Quote request not found")
    if body.status != record.status and body.status not in _STATUS_TRANSITIONS.get(record.status, set()):
        raise HTTPException(
            status_code=409,
            detail=f"Cannot move status from '{record.status}' to '{body.status}'",
        )
    record.status = body.status
    await quote_store.save(record)
    return JSONResponse(record.ops_view())


@router.post(
    "/ops/quote-requests/{request_id}/resend-email",
    dependencies=[Depends(require_ops_token)],
)
async def ops_resend_email(request_id: str) -> JSONResponse:
    """Re-send the lead email for a request (lost email, changed ops
    address, or a delivery failure at submission time).

    Queued, not awaited: composing one now includes provider research that
    runs 45-150s, which outran the platform's HTTP timeout and made a
    successful resend look like a failure.
    """
    record = await quote_store.get(request_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Quote request not found")
    queued = _schedule_ops_email(record)
    return JSONResponse(
        status_code=202 if queued else 200,
        content={
            "schemaVersion": SCHEMA_VERSION,
            "quoteRequestId": record.id,
            "email": "queued" if queued else "disabled",
        },
    )


# --------------------------------------------------------------------------
# Home index ingestion (whole-home scans)
#
# The walked-home export is hundreds of megabytes and never comes near the
# chat server. Whatever processes a scan resolves it into an index (named
# rooms, footprints, fixtures, frame ids — tens of KB, no photos or depth)
# and PUTs that here; the conversation reads it by `homeId` from then on.
# --------------------------------------------------------------------------
@router.put("/ops/homes/{home_id}", dependencies=[Depends(require_ops_token)])
async def ops_put_home_index(home_id: str, body: dict) -> JSONResponse:
    from .flow.home_registry import save_index
    from .home_index import HomeIndex

    try:
        index = HomeIndex.from_json(body)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=422, detail=f"Unreadable home index: {exc}") from exc
    if not index.rooms:
        raise HTTPException(status_code=422, detail="A home index needs at least one room")
    save_index(home_id, index)
    ov = index.overview()
    logger.info("Stored home index %s (%d rooms)", home_id, ov["roomCount"])
    return JSONResponse({
        "schemaVersion": SCHEMA_VERSION,
        "homeId": home_id,
        "roomCount": ov["roomCount"],
        "namedConfidently": ov["namedConfidently"],
        "rooms": [r["name"] for r in ov["rooms"]],
    })


class HomeIngestRequest(BaseModel):
    """Where the app put the export: the bucket it uploads to and the object
    path it got back. ``enrich`` runs the appearance pass when a model key is
    configured (one call per room)."""

    bucket: str = Field(default="metashape-exports", max_length=100)
    objectPath: str = Field(min_length=1, max_length=500)
    enrich: bool = True


_HOME_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,119}$")


def _start_ingest(home_id: str, body: HomeIngestRequest, background: BackgroundTasks) -> JSONResponse:
    from .flow.home_registry import ingest_from_storage, ingest_status

    if not _HOME_ID_RE.match(home_id):
        raise HTTPException(status_code=422, detail="Invalid home id")
    if not body.objectPath.lower().endswith(".zip") or ".." in body.objectPath:
        raise HTTPException(status_code=422, detail="objectPath must name a .zip export")
    current = ingest_status(home_id)
    if current and current.get("status") == "running":
        return JSONResponse(status_code=202, content={
            "schemaVersion": SCHEMA_VERSION, "homeId": home_id, "status": "running",
        })
    background.add_task(ingest_from_storage, home_id, body.bucket, body.objectPath, enrich=body.enrich)
    logger.info("Ingest queued for %s from %s/%s", home_id, body.bucket, body.objectPath)
    return JSONResponse(status_code=202, content={
        "schemaVersion": SCHEMA_VERSION, "homeId": home_id, "status": "queued",
    })


@router.post("/ops/homes/{home_id}/ingest", dependencies=[Depends(require_ops_token)], status_code=202)
async def ops_ingest_home(home_id: str, body: HomeIngestRequest, background: BackgroundTasks) -> JSONResponse:
    """Operations: ingest an export already in storage (any path)."""
    return _start_ingest(home_id, body, background)


@router.get("/ops/homes/{home_id}/ingest", dependencies=[Depends(require_ops_token)])
async def ops_ingest_status(home_id: str) -> JSONResponse:
    from .flow.home_registry import ingest_status, load_index_async

    status = ingest_status(home_id) or {}
    index = await load_index_async(home_id)
    return JSONResponse({
        "schemaVersion": SCHEMA_VERSION,
        "homeId": home_id,
        "ingested": index is not None,
        "roomCount": len(index.rooms) if index is not None else 0,
        **{k: v for k, v in status.items() if k != "bucket"},
    })


@router.post("/ai/homes/{home_id}/ingest", dependencies=[Depends(require_token)], status_code=202)
async def app_ingest_home(home_id: str, body: HomeIngestRequest, background: BackgroundTasks) -> JSONResponse:
    """The app, right after it uploads an export: the object path it was
    given must be that scan's own upload -- the path's scan folder is the
    home id -- so an install cannot point the server at someone else's
    export."""
    parts = [p for p in body.objectPath.split("/") if p]
    if len(parts) < 2 or parts[-2].lower() != home_id.lower():
        raise HTTPException(
            status_code=403,
            detail="objectPath must be this scan's own upload (…/<homeId>/<file>.zip)",
        )
    return _start_ingest(home_id.lower(), body, background)


@router.get("/ops/homes", dependencies=[Depends(require_ops_token)])
async def ops_list_homes() -> JSONResponse:
    """Every ingested home, local and durable, with its overview -- so
    operations (and the dev console) can see what the agent can talk about."""
    from .flow.home_registry import list_home_ids_async, load_index_async

    homes = []
    for home_id in await list_home_ids_async():
        index = await load_index_async(home_id)
        if index is None:
            continue
        ov = index.overview()
        homes.append({
            "homeId": home_id,
            "bundleId": index.bundle_id,
            "roomCount": ov["roomCount"],
            "namedConfidently": ov["namedConfidently"],
            "totalAreaSqFt": ov.get("totalAreaSqFt"),
            "storeyCount": index.storey_count,
            "rooms": ov["rooms"],
        })
    return JSONResponse({"schemaVersion": SCHEMA_VERSION, "homes": homes})


@router.get("/ops/homes/{home_id}", dependencies=[Depends(require_ops_token)])
async def ops_get_home_index(home_id: str) -> JSONResponse:
    from .flow.home_registry import load_index_async

    index = await load_index_async(home_id)
    if index is None:
        raise HTTPException(status_code=404, detail="No home index for that id")
    return JSONResponse(index.overview())


@router.delete("/ops/homes/{home_id}", dependencies=[Depends(require_ops_token)])
async def ops_delete_home_index(home_id: str) -> JSONResponse:
    """Homeowner deletion path (SOW §12)."""
    from .flow.home_registry import forget

    forget(home_id)
    return JSONResponse({"schemaVersion": SCHEMA_VERSION, "homeId": home_id, "deleted": True})


# --------------------------------------------------------------------------
# Ops quote-entry page (provider-finder loop, Sep 1) — the link inside the
# lead email. Auth is the HMAC signature over (request id, expiry): no ops
# token in anyone's inbox, and the link grants exactly one request's
# view-lead + enter-quote. Address stays withheld here like every ops view.
# --------------------------------------------------------------------------
async def _entry_record(request_id: str, exp: int, sig: str):
    if not verify_entry_signature(request_id, exp, sig):
        raise HTTPException(status_code=403, detail="This link is invalid or has expired")
    record = await quote_store.get(request_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Quote request not found")
    return record


# Path variant first: exp/sig in the path survive email quoted-printable
# encoding, which mangles "=" in query strings (observed Resend → Gmail).
# The query variant stays for compatibility with already-sent links.
@router.get("/ops/entry/{request_id}/{exp}/{sig}", response_class=HTMLResponse)
@router.get("/ops/entry/{request_id}", response_class=HTMLResponse)
async def ops_entry_page(request_id: str, exp: int = 0, sig: str = "") -> HTMLResponse:
    record = await _entry_record(request_id, exp, sig)
    if record.status == "submitted":
        record.status = "ops_received"
        await quote_store.save(record)
    e = html_mod.escape
    view = record.ops_view()
    project = view["project"]
    model_url = (view.get("model") or {}).get("url")
    rows = [
        ("Homeowner", f"{view['homeowner']['firstName'] or '—'}"),
        ("Contact", f"{view['homeowner']['contact']['email'] or '—'} · {view['homeowner']['contact']['phone'] or '—'}"),
        ("Zip", view["homeowner"]["zip"] or "—"),
        ("Address", "withheld until the homeowner selects a quote"),
        ("Service", project["serviceType"] or "—"),
        ("Room", project.get("room") or "—"),
        ("Scope", "; ".join(project["scopeOptions"]) or "—"),
        ("Materials", ", ".join(project["materials"]) or "—"),
        ("Quotes already entered", str(view["quotesUploaded"])),
    ]
    rows_html = "".join(
        f"<tr><th>{e(k)}</th><td>{e(v)}</td></tr>" for k, v in rows
    )
    model_html = (
        f'<p><a href="{e(model_url)}" target="_blank">Open the 3D model</a></p>'
        if model_url
        else "<p class='muted'>3D model link not available for this request.</p>"
    )
    page = f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>Quote entry — {e(record.id)}</title>
<style>
  body {{ font-family: 'Segoe UI', system-ui, sans-serif; background:#0F151C; color:#E9EEF3;
         margin:0; padding:24px; display:flex; justify-content:center; }}
  main {{ max-width:640px; width:100%; }}
  h1 {{ font-size:20px; }} h2 {{ font-size:15px; margin:22px 0 8px; color:#8FC4A2; }}
  table {{ border-collapse:collapse; width:100%; font-size:14px; }}
  th {{ text-align:left; color:#97A5B2; font-weight:600; padding:4px 12px 4px 0; white-space:nowrap; vertical-align:top; }}
  td {{ padding:4px 0; }}
  .syn {{ font-size:14px; color:#C6D0DA; background:#171F29; border-radius:10px; padding:12px 14px; }}
  label {{ display:block; font-size:13px; color:#97A5B2; margin:12px 0 4px; }}
  input, textarea {{ width:100%; box-sizing:border-box; background:#171F29; color:#E9EEF3;
    border:1px solid #2A3642; border-radius:8px; padding:9px 11px; font-size:14px; }}
  .pair {{ display:flex; gap:10px; }} .pair > div {{ flex:1; }}
  button {{ margin-top:18px; background:#8FC4A2; color:#0F151C; font-weight:700; border:none;
    border-radius:9px; padding:11px 22px; font-size:15px; cursor:pointer; }}
  .muted {{ color:#97A5B2; font-size:13px; }}
  #msg {{ margin-top:14px; font-size:14px; }}
  #msg.ok {{ color:#8FC4A2; }} #msg.err {{ color:#E58C8C; }}
</style></head><body><main>
<h1>Enter the checked quote</h1>
<p class="muted">Request {e(record.id)} · what you enter here is what the homeowner sees in the app.</p>
<h2>Lead</h2>
<table>{rows_html}</table>
<h2>Synopsis</h2>
<div class="syn">{e(project["synopsis"] or "—")}</div>
{model_html}
<h2>Quote</h2>
<label>Provider name (as the homeowner should see it)</label>
<input id="pname" placeholder="e.g. Summit Painting Co.">
<div class="pair">
  <div><label>Fixed price (USD)</label><input id="pfixed" type="number" min="0" step="1" placeholder="1850"></div>
  <div><label>or range low</label><input id="plow" type="number" min="0" step="1"></div>
  <div><label>range high</label><input id="phigh" type="number" min="0" step="1"></div>
</div>
<label>Notes for the homeowner (optional)</label>
<textarea id="pnotes" rows="3" placeholder="What's included, timing, caveats…"></textarea>
<label>Valid until (optional, e.g. 2026-09-30)</label>
<input id="pvalid" placeholder="">
<button id="send">Send to the homeowner</button>
<div id="msg"></div>
<script>
const ENTRY = {{ exp: {exp}, sig: "{e(sig)}", post: "{settings.api_prefix}/ops/entry/{e(record.id)}" }};
document.getElementById('send').onclick = async () => {{
  const msg = document.getElementById('msg');
  const body = {{
    exp: ENTRY.exp, sig: ENTRY.sig,
    providerName: document.getElementById('pname').value.trim(),
    priceUsd: document.getElementById('pfixed').value ? Number(document.getElementById('pfixed').value) : null,
    priceLowUsd: document.getElementById('plow').value ? Number(document.getElementById('plow').value) : null,
    priceHighUsd: document.getElementById('phigh').value ? Number(document.getElementById('phigh').value) : null,
    notes: document.getElementById('pnotes').value.trim() || null,
    validUntil: document.getElementById('pvalid').value.trim() || null
  }};
  if (!body.providerName) {{ msg.className='err'; msg.textContent='Provider name is required.'; return; }}
  if (body.priceUsd === null && (body.priceLowUsd === null || body.priceHighUsd === null)) {{
    msg.className='err'; msg.textContent='Enter a fixed price, or both a low and high.'; return;
  }}
  msg.className=''; msg.textContent='Sending…';
  const resp = await fetch(ENTRY.post, {{ method:'POST',
    headers: {{'Content-Type':'application/json'}}, body: JSON.stringify(body) }});
  const data = await resp.json().catch(() => ({{}}));
  if (resp.ok) {{
    msg.className='ok';
    msg.textContent='Done — the quote is queued; the homeowner sees it in the app on their next chat turn.';
  }} else {{
    msg.className='err';
    msg.textContent = data.detail || ('Something went wrong (' + resp.status + ').');
  }}
}};
</script>
</main></body></html>"""
    return HTMLResponse(page)


class OpsEntrySubmission(BaseModel):
    exp: int
    sig: str
    providerName: str = Field(min_length=1, max_length=200)
    priceUsd: Optional[float] = None
    priceLowUsd: Optional[float] = None
    priceHighUsd: Optional[float] = None
    notes: Optional[str] = Field(default=None, max_length=2000)
    validUntil: Optional[str] = Field(default=None, max_length=40)


@router.post("/ops/entry/{request_id}")
async def ops_entry_submit(request_id: str, body: OpsEntrySubmission) -> JSONResponse:
    record = await _entry_record(request_id, body.exp, body.sig)
    if record.status == "closed":
        raise HTTPException(status_code=409, detail="This quote request is closed")
    try:
        quote = ReturnedQuote(
            providerName=body.providerName,
            priceUsd=body.priceUsd,
            priceLowUsd=body.priceLowUsd,
            priceHighUsd=body.priceHighUsd,
            notes=body.notes,
            validUntil=body.validUntil,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    _upsert_returned_quotes(record, [quote])
    await quote_store.save(record)
    logger.info("Ops entry page added quote to %s", request_id)
    return JSONResponse({"schemaVersion": SCHEMA_VERSION, "status": record.status, "quoteId": quote.id})


# Staged on-device uploads. Legacy /ingest remains available to old builds;
# these routes require a verified Supabase guest/account and its upload row.
class ScanContextUpload(HomeIngestRequest):
    revision: str = Field(pattern=r"^[a-f0-9-]{36}$")


class ScanModelUpload(BaseModel):
    key: str = Field(pattern=r"^(home|room-[0-9]+)$")
    bucket: str = "metashape-exports"
    objectPath: str = Field(min_length=1, max_length=500)
    bytes: int = Field(gt=0, le=1_048_576_000)


class ScanModelsUpload(BaseModel):
    revision: str = Field(pattern=r"^[a-f0-9-]{36}$")
    models: list[ScanModelUpload] = Field(min_length=1, max_length=250)


async def _scan_upload_owner(home_id: str, token: str | None) -> str:
    from .flow import home_registry, supabase_store

    if not re.fullmatch(r"[a-f0-9-]{36}", home_id):
        raise HTTPException(status_code=422, detail="Invalid scan id")
    sub = await homeowner_id_from_header(token)
    if not sub:
        raise HTTPException(status_code=401, detail="A verified homeowner session is required")
    homeowner = await supabase_store.resolve_homeowner(sub)
    if not homeowner or not homeowner.get("id"):
        raise HTTPException(status_code=403, detail="No homeowner profile for this session")
    owner = str(homeowner["id"]).lower()
    index = await home_registry.load_index_async(home_id)
    if index and index.upload.get("ownerId"):
        if index.upload["ownerId"] != owner:
            raise HTTPException(status_code=403, detail="This scan belongs to another homeowner")
    else:
        # Even an index from before staged uploads must not be claimable by
        # choosing its UUID as a new folder in one's own Storage namespace.
        try:
            owners = await supabase_store.scan_upload_owners(home_id)
        except Exception as exc:
            raise HTTPException(status_code=503, detail="Could not verify scan ownership") from exc
        if owners != {owner}:
            raise HTTPException(status_code=403, detail="This scan has no matching upload ownership record")
    return owner


def _check_scan_object(home_id: str, owner: str, bucket: str, path: str, extension: str) -> None:
    # Exact canonical paths only. No encoded separators, traversal, foreign
    # bucket, or alternate homeowner folder passed to the service credential.
    parts = path.split("/")
    if (bucket != "metashape-exports" or len(parts) != 3
            or parts[:2] != [owner, home_id]
            or not re.fullmatch(r"[A-Za-z0-9_-]+" + re.escape(extension), parts[-1])):
        raise HTTPException(status_code=403, detail="The object must be this homeowner's scan upload")


@router.post("/ai/homes/{home_id}/uploads/context", dependencies=[Depends(require_token)], status_code=202)
async def upload_scan_context(home_id: str, body: ScanContextUpload, background: BackgroundTasks,
                              x_homeowner_token: Optional[str] = Header(default=None)) -> JSONResponse:
    from .flow import home_registry

    owner = await _scan_upload_owner(home_id, x_homeowner_token)
    _check_scan_object(home_id, owner, body.bucket, body.objectPath, ".zip")
    current = home_registry.ingest_status(home_id)
    if current and current.get("status") in ("queued", "running"):
        if current.get("objectPath") != body.objectPath:
            raise HTTPException(status_code=409, detail="Another upload is being indexed; retry shortly")
        return JSONResponse(status_code=202, content={"status": current["status"]})
    index = await home_registry.load_index_async(home_id)
    if (index and index.upload.get("revision") == body.revision
            and index.upload.get("contextObject") == body.objectPath
            and index.upload.get("contextReady")):
        return JSONResponse(status_code=202, content={"status": "done"})
    home_registry._ingest_status[home_id] = {"status": "queued", "stage": "reading_scan",
                                            "objectPath": body.objectPath, "revision": body.revision}
    background.add_task(home_registry.ingest_from_storage, home_id, body.bucket, body.objectPath,
                        enrich=body.enrich, upload_revision=body.revision, owner_id=owner)
    return JSONResponse(status_code=202, content={"status": "queued"})


@router.get("/ai/homes/{home_id}/uploads/context", dependencies=[Depends(require_token)])
async def scan_context_status(home_id: str, objectPath: str,
                              x_homeowner_token: Optional[str] = Header(default=None)) -> JSONResponse:
    from .flow import home_registry

    owner = await _scan_upload_owner(home_id, x_homeowner_token)
    _check_scan_object(home_id, owner, "metashape-exports", objectPath, ".zip")
    index = await home_registry.load_index_async(home_id)
    status = home_registry.ingest_status(home_id) or {}
    if (status.get("status") == "done" and status.get("objectPath") == objectPath
            and not (index and index.upload.get("contextObject") == objectPath
                     and index.upload.get("contextReady"))):
        index = await home_registry.load_index_async(home_id, refresh=True)
    if index and index.upload.get("contextObject") == objectPath and index.upload.get("contextReady"):
        return JSONResponse({"status": "done", "stage": "ready", "roomCount": len(index.rooms),
                             "revision": index.upload.get("revision")})
    if status.get("objectPath") == objectPath:
        if status.get("status") == "done":
            # A finished task alone cannot confirm that model registration
            # will see this revision as ready. Keep polling the durable index.
            return JSONResponse({"status": "running", "stage": "saving_context"})
        return JSONResponse({k: status[k] for k in
            ("status", "stage", "error", "roomCount", "completedRooms", "totalRooms", "revision") if k in status})
    return JSONResponse({"status": "unknown"})  # e.g. host restarted: POST the same object again


@router.post("/ai/homes/{home_id}/uploads/models", dependencies=[Depends(require_token)])
async def upload_scan_models(home_id: str, body: ScanModelsUpload,
                             x_homeowner_token: Optional[str] = Header(default=None)) -> JSONResponse:
    from .flow import home_registry, supabase_store
    from .home_index import HomeIndex

    owner = await _scan_upload_owner(home_id, x_homeowner_token)
    index = await home_registry.load_index_async(home_id)
    if not index or not index.upload.get("contextReady") or index.upload.get("revision") != body.revision:
        index = await home_registry.load_index_async(home_id, refresh=True)
    if index and index.upload.get("ownerId") not in (None, owner):
        raise HTTPException(status_code=403, detail="This scan belongs to another homeowner")
    if index and index.upload.get("revision") not in (None, body.revision):
        raise HTTPException(status_code=409, detail={"code": "revision_mismatch", "retryable": False,
            "message": "This scan has a newer upload revision. Open the latest scan before retrying."})
    if not index or not index.upload.get("contextReady") or index.upload.get("revision") != body.revision:
        raise HTTPException(status_code=409, detail={"code": "context_not_ready", "retryable": True,
            "message": "The files are uploaded. Home Guide is still preparing this scan's photos and metadata."})
    expected = {r.key for r in index.rooms} | {"home"}
    keys = [m.key for m in body.models]
    if len(keys) != len(set(keys)) or set(keys) != expected:
        raise HTTPException(status_code=422, detail="Upload the final home model and every area's model")
    for model in body.models:
        _check_scan_object(home_id, owner, model.bucket, model.objectPath, ".usdz")
        try:
            actual_size = await supabase_store.stored_object_size(model.bucket, model.objectPath)
        except Exception as exc:
            raise HTTPException(status_code=422, detail=f"Model {model.key} is not available in Storage") from exc
        if actual_size != model.bytes:
            raise HTTPException(status_code=422, detail=f"Model {model.key} upload is incomplete")
    # Re-read after network awaits: a newer scan revision may have started.
    current = home_registry.load_index(home_id)
    ingest = home_registry.ingest_status(home_id) or {}
    if (not current or current.upload.get("revision") != body.revision
            or (ingest.get("status") in ("queued", "running")
                and ingest.get("revision") not in (None, body.revision))):
        raise HTTPException(status_code=409, detail={"code": "revision_mismatch", "retryable": False,
            "message": "This scan has a newer upload revision. Open the latest scan before retrying."})
    if ingest.get("status") in ("queued", "running"):
        raise HTTPException(status_code=409, detail={"code": "context_ingesting", "retryable": True,
            "message": "The files are uploaded. Home Guide is finishing this scan's metadata."})
    updated = HomeIndex.from_json(current.to_json())
    for model in body.models:
        record = {"bucket": model.bucket, "object": model.objectPath, "bytes": model.bytes,
                  "uploadedAt": home_registry.now_iso(), "file": f"{model.key}.usdz"}
        if model.key == "home":
            updated.home_model = record
        else:
            updated.by_key(model.key).model = record
    updated.upload["modelsReady"] = True
    try:
        await home_registry.save_index_confirmed(home_id, updated)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return JSONResponse({"status": "done", "modelCount": len(body.models)})
