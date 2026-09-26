"""Quote requests, the operations lead package, and the results-return loop
(SOW §2 steps 9–10; API_CONTRACT_V1 §5–§7).

The homeowner-side endpoint assembles the lead package server-side so it is
complete (synopsis, scope options, materials, measurements, model link,
contact) — replacing the app's bare 5-column Supabase insert. Operations is
human-in-the-loop (SOW §6): the package is delivered via webhook and stays
retrievable through the ops API; ops uploads returned quotes, and the agent
presents them on the homeowner's next turn (`flow_runtime._pending_quotes`).

Privacy (SOW §12): the street address is stored on the record but is exposed
to ops only after the homeowner selects a quote (`addressReleasePolicy`);
homeowner-facing views never include internal ops fields.

Storage is one JSON file per request under ``{storage}/quote_requests/`` —
same durability model as the job store today; the Week-2 Supabase layer
replaces the sink, not the shapes.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from pathlib import Path
from typing import Any, Literal, Optional

import httpx
from pydantic import BaseModel, Field, model_validator

from .config import settings
from .flow import supabase_store
from .flow.journal import read_thread_journal
from .flow.state import FlowState, FlowStep, QuoteRequestRef
from .models import SCHEMA_VERSION, now_utc

logger = logging.getLogger("lidarai.flow.quotes")

# Mean wall height above which a room is probably open to a stairwell or a
# double-height space, so paintable area is an upper bound (PR #30 review).
OPEN_VOLUME_WALL_HEIGHT_M = 3.2

# Softened per client direction (Sep 2): no exact turnaround window is
# promised anywhere homeowner-facing — a person reviews every request, so
# timing is theirs to keep. The ±10% price-accuracy caveat (SOW) stays.
EXPECTATIONS = {
    # The agent is the one who brings the quotes back (Quintin, Sep 15: it
    # should read as the assistant getting them, not a hand-off to a team
    # the homeowner never meets), in the first person (Sep 16, #99).
    # Sep 17 (#101): the people behind the scenes drop out of the sentence
    # entirely -- naming them made THEM the subject and the agent a
    # messenger. The agent owns the action and the follow-up. It stays
    # honest by not claiming the request has already reached a provider
    # (it reaches operations first) and by promising no turnaround.
    "copy": (
        "Sounds good — I'm getting your request in front of local providers "
        "now. As soon as their quotes come back, I'll bring them to you here."
    ),
    "accuracyCaveatPct": 10,
}

VALID_STATUSES = {"submitted", "ops_received", "in_progress", "quotes_ready", "presented", "closed"}


class QuoteLineItem(BaseModel):
    item: str
    priceUsd: Optional[float] = None


def _provider_profile(name: str | None) -> dict[str, Any] | None:
    from .flow.partners import profile_for_name

    try:
        return profile_for_name(name)
    except Exception:  # noqa: BLE001 -- a profile is a nicety, never a failure
        return None


class QuoteDocument(BaseModel):
    """The provider's own paperwork — a written estimate or invoice —
    attached to a quote (Quintin, Sep 17). PDF only: it is what providers
    send, it renders everywhere, and it keeps the accepted types to one."""

    fileName: str = Field(max_length=200)
    byteCount: int = Field(ge=0)
    contentType: str = "application/pdf"
    url: Optional[str] = None
    uploadedAt: str = Field(default_factory=lambda: now_utc().isoformat())


class ReturnedQuote(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    providerName: str = Field(max_length=200)
    providerId: Optional[str] = None
    priceUsd: Optional[float] = None
    priceLowUsd: Optional[float] = None
    priceHighUsd: Optional[float] = None
    lineItems: list[QuoteLineItem] = Field(default_factory=list)
    notes: Optional[str] = Field(default=None, max_length=2000)
    validUntil: Optional[str] = Field(default=None, max_length=40)
    # True only for illustrative market estimates (the demo layer); a real
    # ops-uploaded bid stays False even if its notes mention an "estimate".
    isEstimate: bool = False
    uploadedAt: str = Field(default_factory=lambda: now_utc().isoformat())
    # The homeowner's verdict on this quote (Quintin, Sep 17). "approved" is
    # the choice that releases the address; "declined" is a real answer we
    # want, not silence, because the note that comes with it is what lets
    # operations go back to the provider or find another one.
    # The provider's written estimate or invoice, when operations sent one.
    document: Optional[QuoteDocument] = None
    decision: Optional[Literal["approved", "declined"]] = None
    decisionNote: Optional[str] = Field(default=None, max_length=2000)
    decidedAt: Optional[str] = None

    @model_validator(mode="after")
    def _sane_prices(self) -> "ReturnedQuote":
        has_fixed = self.priceUsd is not None
        has_range = self.priceLowUsd is not None and self.priceHighUsd is not None
        if not (has_fixed or has_range):
            raise ValueError("a quote needs priceUsd, or both priceLowUsd and priceHighUsd")
        for value in (self.priceUsd, self.priceLowUsd, self.priceHighUsd):
            if value is not None and value < 0:
                raise ValueError("quote prices cannot be negative")
        if has_range and self.priceLowUsd > self.priceHighUsd:
            raise ValueError("priceLowUsd cannot exceed priceHighUsd")
        return self

    def homeowner_view(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "providerName": self.providerName,
            "priceUsd": self.priceUsd,
            "priceLowUsd": self.priceLowUsd,
            "priceHighUsd": self.priceHighUsd,
            "lineItems": [item.model_dump() for item in self.lineItems],
            "notes": self.notes,
            "validUntil": self.validUntil,
            "isEstimate": self.isEstimate,
            "document": self.document.model_dump() if self.document else None,
            "decision": self.decision,
            "decisionNote": self.decisionNote,
            "decidedAt": self.decidedAt,
            # What the partner table knows about the company (#98), so the
            # app and the agent can vouch for it with facts on file.
            "provider": _provider_profile(self.providerName),
        }


class QuoteRequestRecord(BaseModel):
    id: str
    createdAt: str
    threadId: str
    homeownerId: Optional[str] = None
    status: str = "submitted"
    serviceType: Optional[str] = None
    scopeOptions: list[str] = Field(default_factory=list)
    materials: list[str] = Field(default_factory=list)
    zip: Optional[str] = None
    address: Optional[str] = None          # withheld from ops until selection
    contactEmail: Optional[str] = None
    contactPhone: Optional[str] = None
    firstName: Optional[str] = None
    synopsis: str = ""
    # Whole-home scans: which room this request is actually for. Without it
    # a lead from a 19-room house reads "Painting, 37203" with the whole
    # house's square footage, and the provider quotes the wrong thing.
    homeId: Optional[str] = None
    roomKey: Optional[str] = None
    roomName: Optional[str] = None
    # What the homeowner said the project covers (docs/SCAN_SCOPE.md): one
    # room, the rooms in scopeRooms, or the whole home. The trade is the
    # same either way; what gets sent to a provider is not.
    scopeIntent: str = "undecided"
    scopeRooms: list[str] = Field(default_factory=list)
    measurements: dict[str, Any] = Field(default_factory=dict)
    modelLink: dict[str, Any] = Field(default_factory=dict)
    quoteDraft: dict[str, Any] = Field(default_factory=dict)
    quotes: list[ReturnedQuote] = Field(default_factory=list)
    selectedQuoteId: Optional[str] = None
    # Lead-email delivery, durable with the record: a package queued but not
    # yet sent when the process restarts is re-queued at the next startup
    # instead of vanishing with the in-memory queue (Sep 15 audit).
    opsEmailQueuedAt: Optional[str] = None
    opsEmailDeliveredAt: Optional[str] = None
    opsEmailCapturedAt: Optional[str] = None

    # ---------------------------------------------------------------- views
    def homeowner_view(self) -> dict[str, Any]:
        return {
            "schemaVersion": SCHEMA_VERSION,
            "quoteRequestId": self.id,
            "createdAt": self.createdAt,
            "status": self.status,
            "expectations": EXPECTATIONS,
            "serviceType": self.serviceType,
            "quotes": [q.homeowner_view() for q in self.quotes],
            # The homeowner's own choice: without it the app could not show
            # which quote was picked after a relaunch, nor the agent know the
            # address was already released (Sep 15 audit).
            "selectedQuoteId": self.selectedQuoteId,
        }

    def quote_by_id(self, quote_id: str) -> Optional["ReturnedQuote"]:
        return next((q for q in self.quotes if q.id == quote_id), None)

    @property
    def declined_quotes(self) -> list["ReturnedQuote"]:
        return [q for q in self.quotes if q.decision == "declined"]

    def ops_view(self, *, include_address: bool | None = None) -> dict[str, Any]:
        """The lead package as operations sees it (ops API, webhook, entry
        page, and the fields the lead email is composed from).

        Identifiers (FINDINGS_VERIFIED Appendix D2): operations addresses
        everything by ``quoteRequestId``. ``threadId`` is a resume
        credential, ``homeownerId`` is an internal key, and the processor
        ``jobId`` is a server-side handle -- none of them leaves the system
        through this view. Do not add an identifier here without checking
        Appendix D.
        """
        address_released = (
            include_address if include_address is not None else self.selectedQuoteId is not None
        )
        model = {k: v for k, v in (self.modelLink or {}).items() if k != "jobId"} or None
        return {
            "schemaVersion": SCHEMA_VERSION,
            "quoteRequestId": self.id,
            "createdAt": self.createdAt,
            "status": self.status,
            "homeowner": {
                "firstName": self.firstName,
                "contact": {"email": self.contactEmail, "phone": self.contactPhone},
                "zip": self.zip,
            },
            "address": self.address if address_released else None,
            "addressReleasePolicy": "withheld_until_quote_selected",
            "addressReleased": address_released,
            "project": {
                "serviceType": self.serviceType,
                "room": self.roomName,
                "scope": {
                    "intent": self.scopeIntent,
                    "rooms": list(self.scopeRooms),
                    "label": scope_label(self),
                },
                "synopsis": self.synopsis,
                "scopeOptions": self.scopeOptions,
                "materials": self.materials,
                "measurements": self.measurements,
                "quoteDraft": self.quoteDraft,
            },
            "model": model,
            "quotesUploaded": len(self.quotes),
        }


def scope_label(record: Any) -> str:
    """One line operations can act on: is this one room or a whole-home job?
    Reads the record's scopeIntent / scopeRooms / roomName only."""
    intent = str(getattr(record, "scopeIntent", None) or "undecided")
    rooms = [r for r in (getattr(record, "scopeRooms", None) or []) if r]
    room = getattr(record, "roomName", None)
    if intent == "whole_home":
        return "whole home"
    if intent == "selected_rooms":
        named = rooms or ([room] if room else [])
        return "selected rooms: " + ", ".join(named) if named else "selected rooms (not all named)"
    if intent == "single_room":
        which = rooms[0] if rooms else room
        return f"one room ({which})" if which else "one room"
    return "not stated by the homeowner" + (f" (conversation was about the {room})" if room else "")


class QuoteRequestStore:
    """Supabase-primary, file-fallback store. The local file is always
    written (fast cache + the only copy when Supabase is unconfigured); reads
    prefer Supabase so multiple instances and redeploys agree."""

    def __init__(self, storage_dir: str | None = None):
        self._storage_dir = storage_dir

    def _dir(self) -> Path:
        base = Path(self._storage_dir or settings.storage_dir) / "quote_requests"
        base.mkdir(parents=True, exist_ok=True)
        return base

    def _path(self, request_id: str) -> Path:
        safe = re.sub(r"[^A-Za-z0-9_-]", "_", request_id)
        return self._dir() / f"{safe}.json"

    async def save(self, record: QuoteRequestRecord) -> None:
        try:
            self._path(record.id).write_text(record.model_dump_json(), encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not write quote request file %s: %s", record.id, exc)
        await supabase_store.upsert_quote_request(
            record.id,
            record.threadId,
            record.homeownerId,
            record.status,
            record.model_dump(mode="json"),
        )

    def _get_local(self, request_id: str) -> QuoteRequestRecord | None:
        path = self._path(request_id)
        if not path.exists():
            return None
        try:
            return QuoteRequestRecord.model_validate(json.loads(path.read_text(encoding="utf-8")))
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not read quote request %s: %s", request_id, exc)
            return None

    async def get(self, request_id: str) -> QuoteRequestRecord | None:
        durable = await supabase_store.get_quote_request(request_id)
        if durable is not None:
            try:
                return QuoteRequestRecord.model_validate(durable)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Bad durable quote request %s: %s", request_id, exc)
        return self._get_local(request_id)

    async def list(self, status: str | None = None) -> list[QuoteRequestRecord]:
        durable = await supabase_store.list_quote_requests(status)
        if durable is not None:
            records = []
            for raw in durable:
                try:
                    records.append(QuoteRequestRecord.model_validate(raw))
                except Exception:  # noqa: BLE001
                    continue
            return records
        records = []
        for path in sorted(self._dir().glob("*.json")):
            try:
                record = QuoteRequestRecord.model_validate(
                    json.loads(path.read_text(encoding="utf-8"))
                )
            except Exception:  # noqa: BLE001
                continue
            if status is None or record.status == status:
                records.append(record)
        return records


quote_store = QuoteRequestStore()


# --------------------------------------------------------------------------
# Lead package assembly (step 9)
# --------------------------------------------------------------------------
async def build_synopsis(thread_id: str, state: FlowState) -> str:
    """Deterministic synopsis from the flow journal — what the homeowner asked
    about and what was captured, in a few ops-readable sentences. Journal
    lines come from the local file, falling back to the durable Supabase copy
    (the local JSONL dies with the instance disk)."""
    parts: list[str] = []
    name = state.slots.first_name or "The homeowner"
    _, _, room_name = _room_measurements(state)
    if state.slots.project_type and room_name:
        parts.append(f"{name} is interested in {state.slots.project_type.lower()} in the {room_name}.")
    elif state.slots.project_type:
        parts.append(f"{name} is interested in {state.slots.project_type.lower()}.")
    if state.slots.scope_options:
        parts.append("Scope options discussed: " + "; ".join(state.slots.scope_options) + ".")
    if state.slots.materials:
        parts.append("Material preferences: " + ", ".join(state.slots.materials) + ".")
    entries = read_thread_journal(settings.storage_dir, thread_id)
    user_lines = [e.get("userText", "") for e in entries if e.get("userText")]
    if not user_lines:
        durable_lines = await supabase_store.list_journal_user_texts(thread_id)
        user_lines = durable_lines or []
    if user_lines:
        recent = " / ".join(line[:160] for line in user_lines[-4:])
        parts.append(f"Recent conversation (homeowner, masked): {recent}")
    return " ".join(parts) or "Quote request submitted from the home conversation."


async def build_model_link(state: FlowState) -> dict[str, Any]:
    """The 3D model link in the lead package (SOW §2 step 9).

    Preferred: the completed USDZ uploaded (lazily, at submission time) to
    Supabase Storage with a 30-day signed URL — a link ops can open with no
    credentials, that survives redeploys and job pruning. Fallback: the
    processor's token-gated artifact route, which requires the service token
    and dies with the instance disk."""
    job_id = state.scan.job_id
    if not job_id:
        # A walked-home index has no processor job behind it, so quoting the
        # scan state reads as a contradiction ("not available: state is
        # 'complete'"). Say what is actually true instead.
        if state.home_id:
            return await _home_model_link(state)
        return {"status": "not_available", "reason": "no scan job is linked to this conversation"}
    # The processor's own state, not the gate's flag: under the device-bake
    # signal the gate can be open while the processor job is still running.
    if str(state.scan.processor) != "complete":
        return {"status": "not_available", "reason": f"scan processing state is '{state.scan.processor}'"}
    try:
        from .store import store as job_store

        artifact = job_store.result_dir(job_id) / "textured_mesh.usdz"
        signed = await supabase_store.ensure_model_asset(job_id, artifact)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Model link upload failed for job %s: %s", job_id, exc)
        signed = None
    if signed:
        return {
            "kind": "supabase_signed_url",
            "jobId": job_id,
            "url": signed,
            "note": "Signed link, valid ~30 days; no credentials needed.",
        }
    return {
        "kind": "processor_artifact",
        "jobId": job_id,
        "path": f"{settings.api_prefix}/jobs/{job_id}/result/textured_mesh.usdz",
        "note": "Requires the service bearer token; ops can download via the processor API.",
    }


async def _home_model_link(state: FlowState) -> dict[str, Any]:
    """The model link for a walked-home conversation: the phone's textured
    bake of the room under discussion, copied to Storage at ingest. Falls
    back to the whole-home bake, and otherwise says exactly why there is no
    link -- 'not textured yet' and 'too large for the storage plan' are
    different problems with different owners."""
    from .flow import home_registry

    index = await home_registry.load_index_async(state.home_id)
    if index is None:
        return {"status": "not_available",
                "reason": "the walked-home index for this conversation is not loaded on this host"}
    room = index.by_key(state.active_room_key) if state.active_room_key else None
    label = room.display_name if room else "this home"
    candidates: list[tuple[str, dict]] = []
    if room is not None and room.model:
        candidates.append((room.display_name, room.model))
    if index.home_model:
        candidates.append(("whole home", index.home_model))
    for name, record in candidates:
        if record.get("object"):
            signed = (await supabase_store._signed_storage_url(record["bucket"], record["object"])
                      if record.get("bucket") else await supabase_store.sign_home_model(record["object"]))
            if signed:
                return {
                    "kind": "supabase_signed_url",
                    "url": signed,
                    "room": name,
                    "source": "on-device bake",
                    "bytes": int(record.get("bytes") or 0),
                    "note": f"Textured model of the {name}, baked on the phone; "
                            "signed link, valid ~30 days, no credentials needed.",
                }
    for name, record in candidates:
        if record.get("skipped"):
            return {
                "status": "not_available",
                "reason": f"the export carries a {int(record.get('bytes') or 0) / 1048576:.0f} MB "
                          f"textured model of the {name}, but it was not stored: {record['skipped']}",
            }
    if room is not None and not room.model:
        return {"status": "not_available",
                "reason": f"the export carried no textured model of the {label} "
                          "(texturing had not finished on the phone when it was exported)"}
    return {"status": "not_available",
            "reason": "this walked-home export carried no textured model"}


def _room_measurements(state: FlowState) -> tuple[dict[str, Any] | None, str | None, str | None]:
    """Measurements for the room actually under discussion.

    A whole-home capture's totals describe the house; a provider quoting
    "the primary bathroom" needs that room's numbers, not 3,356 sq ft.
    Returns (measurements, room_key, room_name) or (None, None, None).
    """
    if not (state.home_id and state.active_room_key):
        return None, None, None
    from .flow.home_registry import load_index

    index = load_index(state.home_id)
    room = index.by_key(state.active_room_key) if index else None
    if room is None:
        return None, None, None
    measurements: dict[str, Any] = {
        "note": f"Measurements for the {room.display_name}, from the home capture.",
        "room": room.display_name,
        "floorAreaSquareFeet": round(room.area_sqft, 1),
        "windowCount": room.window_count,
        "doorCount": room.door_count,
        "storey": room.storey,
        "photoCount": len(room.frame_ids),
    }
    if room.window_openings:
        # Window replacement is priced per opening and by size. The scan has
        # both; a count alone sent a provider back to ask (Quintin, Sep 24).
        measurements["windowOpenings"] = [
            {"widthFeet": round(w * 3.28084, 1), "heightFeet": round(h * 3.28084, 1)}
            for w, h in room.window_openings
        ]
    if room.objects:
        measurements["fixtures"] = [f"{n}x {c}" for c, n in room.objects.most_common(8)]
    # Geometry-only numbers computed at ingest (room_context). A painter
    # quotes from wall area, not floor area, so this is the figure that
    # matters for the most common trade.
    geometry = room.measurements or {}
    if geometry.get("paintable_sqft") is not None:
        measurements["paintableWallSquareFeet"] = round(float(geometry["paintable_sqft"]), 1)
    if geometry.get("perimeter_m") is not None:
        measurements["perimeterFeet"] = round(float(geometry["perimeter_m"]) * 3.28084, 1)
    mean_height = geometry.get("mean_wall_height_m")
    if mean_height is not None and float(mean_height) > OPEN_VOLUME_WALL_HEIGHT_M:
        measurements["measurementCaveat"] = (
            f"Walls in this capture average {float(mean_height):.1f} m tall, which usually "
            "means the space is open to a stairwell or a double-height room. Treat the "
            "paintable area as an upper bound and confirm on site."
        )
    if not room.confident:
        measurements["nameCaveat"] = (
            f"'{room.display_name}' is inferred from fixtures ({room.name_basis}); "
            "confirm with the homeowner."
        )
    return measurements, room.key, room.display_name


async def create_quote_request(
    state: FlowState,
    *,
    thread_id: str,
    measurements: dict[str, Any],
    quote_draft: dict[str, Any] | None,
) -> QuoteRequestRecord:
    room_measurements, room_key, room_name = _room_measurements(state)
    if room_measurements is not None:
        measurements = room_measurements
    record = QuoteRequestRecord(
        id=f"qr_{uuid.uuid4().hex[:12]}",
        createdAt=now_utc().isoformat(),
        threadId=thread_id,
        homeownerId=state.homeowner_id,
        serviceType=state.slots.project_type,
        scopeOptions=list(state.slots.scope_options),
        materials=list(state.slots.materials),
        zip=state.slots.zip,
        address=state.slots.address,
        contactEmail=state.slots.contact_email,
        contactPhone=state.slots.contact_phone,
        firstName=state.slots.first_name,
        synopsis=await build_synopsis(thread_id, state),
        homeId=state.home_id,
        roomKey=room_key,
        roomName=room_name,
        scopeIntent=str(state.scope_intent),
        scopeRooms=list(state.scope_rooms),
        measurements=measurements,
        modelLink=await build_model_link(state),
        quoteDraft=quote_draft or {},
    )
    await quote_store.save(record)
    state.quote_request = QuoteRequestRef(id=record.id, status="submitted")
    state.mark_complete(FlowStep.QUOTE_REQUEST)
    return record


async def deliver_to_ops(record: QuoteRequestRecord) -> bool:
    """Webhook delivery; failure never blocks the homeowner (the package
    stays retrievable via the ops API)."""
    if not settings.ops_webhook_url:
        logger.info("No ops webhook configured; lead package %s retrievable via ops API", record.id)
        return False
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(8)) as client:
            resp = await client.post(
                settings.ops_webhook_url,
                json={"event": "quote_request_submitted", "leadPackage": record.ops_view()},
            )
            resp.raise_for_status()
            return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("Ops webhook delivery failed for %s: %s", record.id, exc)
        return False
