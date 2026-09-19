"""Flow runtime: wraps `generate_home_ai_response` with the SOW §2 flow
engine (API_CONTRACT_V1 §3, §4, §8).

Per turn:
1. Resolve flow state — signed client token first, then the local state file,
   then fresh. The client echoing the token makes flow position survive
   server restarts; the state file covers legacy clients that don't echo.
2. Reconcile scan-processing state (client `scanContext` + legacy
   `photorealStatus`, cross-checked against the in-process job store — the
   server wins).
3. Plan the turn: gates decide which asks the prompt may include.
4. Call the model with flow directives injected; check the reply against the
   gates; one corrective regeneration, then deterministic safe copy.
5. Update slots from the model's `flowCapture` (validated) plus deterministic
   capture, journal the turn with its wording ids, re-sign the token, and
   attach the additive `flow` / `priceGuidance` response fields.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import secrets
import time
import uuid
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, Field

from .config import settings
from .flow import (
    FlowEngine,
    FlowState,
    FlowStep,
    FlowTokenCodec,
    InvalidFlowToken,
    ScanProcessingState,
)
from .flow import enforcement
from .flow import input_guard
from .flow import regional_naming
from .flow import supabase_store
from .flow.journal import TurnJournalEntry, write_turn
from .flow.machine import (
    EXTENSION_GENERIC_ONCE,
    EXTENSION_NAMED_ROOMS,
    EXTENSION_NONE,
    EXTENSION_OPEN,
    GateDecision,
    TurnPlan,
)
from .flow.state import ScopeIntent
from .flow.pricing import (
    compute_price_guidance,
    parse_size_hint,
    priced_per_window,
    static_rates,
    user_asked_for_price,
    user_asked_to_compare,
)
from .flow.wire import (
    FlowScanContext,
    FlowWire,
    LocalContextWire,
    LocalProvidersWire,
    LocalProviderWire,
    PriceGuidance,
    PriceOption,
)
from .flow.wording import (
    ADDRESS_WORDING,
    FIRST_NAME_WORDING,
    GUARD_COPY,
    LOCAL_STYLES_NO_RESEARCH_GUIDANCE,
    SAFE_IMPERSONATION_COPY,
    SAFE_SCAN_WAIT_COPY,
    SAFE_SCOPE_COPY,
    assign_scope_wording,
    assign_zip_wording,
    wording_by_id,
)
from .armor import turn_guard
from .home_ai import (
    HomeAIChatMessage,
    HomeAIChatRequest,
    HomeAIChatResponse,
    HomeAIContextPacket,
    HomeAIConversationState,
    HomeAIQuoteDraft,
    HomeAIWorkflowState,
    generate_home_ai_response,
)
from .home_guide_tools import detect_service_type, normalize_service_type
from .store import store

logger = logging.getLogger("lidarai.flow.runtime")

_engine = FlowEngine()

_ZIP_IN_MESSAGE = re.compile(r"(?<!\d)(\d{5})(?:-\d{4})?(?!\d)")
# The homeowner saying they cannot see something the agent has pointed at.
# The agent has no view of their screen: once they say this, insisting the
# element is there, claiming to re-send it, or diagnosing their app are all
# things it cannot know (Sep 4 battery — it told a homeowner his app was
# broken and to contact support).
_CANNOT_SEE_UI = re.compile(
    r"(?i)(don'?t see|do not see|dont see|can'?t (?:see|find)|cannot (?:see|find)|"
    r"no card|nothing (?:show|there|here)|not show|nuthin|isn'?t (?:there|showing)|"
    r"where (?:do i|is the) (?:click|button|card)|nada)"
)

# The homeowner insisting they already tapped Confirm. Telling them a
# third time where the button is reads as calling them a liar (persona
# battery, Sep 3), so the runtime counts these and the directive backs off.
_CLAIMS_CONFIRMED = re.compile(
    r"(?i)("
    # "i confirmed", "i did hit confirm", "we already tapped it"
    r"\b(?:i|we)\s+(?:already\s+|alrdy\s+|alredy\s+|just\s+)?(?:did\s+)?"
    r"(?:hit|tap|tapped|press|pressed|click|clicked|confirm|confirmed|submitted)\b"
    r"|\b(?:already|alrdy|alredy)\s+(?:confirmed|tapped|did\s+(?:it|that))\b"
    # "k confirmed lets go" -- a bare past-tense confirmation opening a message
    r"|^\s*(?:k|ok|okay|aight|yes|yep|yeah)?[,\s]*confirmed\b"
    r"|\bit(?:'s| is| has been)?\s+confirmed\b"
    # "stop askin" is the same signal, louder
    r"|\bstop\s+ask(?:in|ing)\b"
    r")"
)
_ZIP_VALID = re.compile(r"^\d{5}$")

# A plain yes. Only read as accepting the request offer when it answers that
# offer on the very next turn (see _accepts_request) — otherwise "yeah" to
# "want to see the ceiling too?" would silently authorize a lead package.
_AFFIRMATIVE = re.compile(
    r"(?i)^\s*(?:yes|yea|yeah|yep|yup|ya|sure|ok|okay|k|please|pls|"
    r"do it|go ahead|go for it|sounds good|that works|lets do it|let's do it|"
    r"absolutely|definitely|for sure)\b[\s.!,]*"
)
# Asking for the request unprompted — no offer needed, they said it themselves.
# Deliberately narrow: "can you help me get quotes?" is what prompts the OFFER,
# not consent to ship a package, so wanting quotes is not on this list. Only
# language about sending or packaging the thing itself counts.
# "The request" in any of its namings: the request, my quote request, that
# request card. Naming the thing is what separates "build the request"
# (consent) from "can you help me get quotes" (an ask for the offer).
_THE_REQUEST = (
    r"(?:a|the|my|that|this|your)\s+(?:[\w-]+\s+){0,3}?(?:quote\s+)?request(?:\s+card)?"
)
_ASKS_FOR_REQUEST = re.compile(
    r"(?i)\b(?:send|submit)\s+(?:it|this|that|them|" + _THE_REQUEST + r")\b"
    # "put it together" and, from the Sep 14 simulator walk, "put the quote
    # request together" -- the request named before "together".
    r"|\bput\s+(?:it|this|that|everything|" + _THE_REQUEST + r")\s+together\b"
    r"|\b(?:send|submit)\s+(?:it\s+|this\s+)?(?:to|off\s+to|over\s+to)\s+takeshape\b"
    r"|\bwrite\s+(?:it|this|that)\s+up\b"
    # "can you generate a request for me to confirm?" (Sep 15, #77); "build
    # the request card", "show me the request to review" (Sep 14 walk).
    r"|\b(?:generate|draft|create|prepare|build|write\s+up|put\s+together|show\s+me)\s+(?:me\s+)?"
    + _THE_REQUEST + r"\b"
)


def _draft_from_state(state: FlowState, home_index=None) -> HomeAIQuoteDraft:
    """The request card built from what the conversation captured. Used on the
    turn the homeowner says yes when the model wrote no draft: waiting for the
    model left the card a whole turn late (Sep 13)."""
    slots = state.slots
    service = slots.project_type or "Home project"
    room = home_index.by_key(state.active_room_key) if home_index and state.active_room_key else None
    where = f" in the {room.display_name}" if room else ""
    scope = "; ".join(slots.scope_options)
    summary = f"{service}{where}: {scope}." if scope else f"{service}{where}."
    notes = list(slots.scope_options)
    if slots.materials:
        notes.append("Materials: " + ", ".join(slots.materials))
    return HomeAIQuoteDraft(
        serviceType=slots.project_type,
        title=f"{service} request",
        homeownerSummary=summary,
        providerRequest=summary + (f" Zip {slots.zip}." if slots.zip else ""),
        scopeNotes=notes,
    )


def _capture_has_address(response) -> bool:
    """Did the model just capture a street address from the message it is
    answering? The slot is written after the reply is checked, so this is the
    only view enforcement gets of an address given on this very turn. Same
    length test `_apply_capture` applies before storing one."""
    value = _clean_str((getattr(response, "_flow_capture", None) or {}).get("address"), max_len=240)
    return bool(value and len(value) >= 8)


def _accepts_request(state: FlowState, message: str) -> bool:
    """Did the homeowner just agree to the request being put together? A bare
    affirmative counts only as an answer to an offer made on the immediately
    preceding turn; an explicit ask stands on its own."""
    if state.request_accepted:
        return False
    ask = _ASKS_FOR_REQUEST.search(message)
    if ask and not _NEGATED_ASK.search(message[: ask.start()]):
        return True
    return bool(
        state.request_offer_at_turn == state.user_turns
        and _AFFIRMATIVE.match(message)
    )

_EMAIL_IN_MESSAGE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")
# A North American phone number as people actually type it. Anchored on ten
# digits so a zip, a year, or a square footage can never become a phone number.
_PHONE_IN_MESSAGE = re.compile(
    r"(?<!\d)(?:\+?1[\s.\-]?)?\(?\d{3}\)?[\s.\-]?\d{3}[\s.\-]?\d{4}(?!\d)"
)
# Whose details are these? The zip backstop already refuses a bare five digits
# without context, and contact needs the same: "my contractor's number is
# 555-0100, can you call him" must not become the homeowner's lead contact,
# which is write-once and opens the request card.
_OWN_CONTACT = re.compile(
    r"""(?ix)
    (?:
        (?:reach|call|text|email|contact|ping)\s+me
      | my\s+(?:cell|mobile|phone|number|email|address|info|details)
      | (?:i'?m|im)\s+at\b
      | here'?s\s+my
      | (?:you\s+can\s+)?(?:use|try)\s+my
      | (?:i\s+)?live\s+at\b
      | send\s+(?:it|them|that|quotes?)\s+to
    )
    """
)


# Captured slots are write-once on purpose: a settled fact should survive a
# passing mention of something that looks like it. But a homeowner correcting
# themselves is not a passing mention, and the flow used to drop every one of
# them — the agent would say "got it, changed" and nothing moved (Sep 16, the
# misspelled name; the same hole sits under zip and contact). A correction is
# recognised by the homeowner typing the NEW value this turn and framing it as
# replacing the old one. Both halves are required.
_CORRECTION_FRAME = r"""
        typo | mis(?:typed|spell(?:ed|ing)?|took) | autocorrect | spelled
      | i\s+mean(?:t)? | actually | instead | correction | corrected
      | scratch\s+that | disregard | ignore\s+(?:that|the\s+last)
      | (?:should|shoulda)\s+(?:be|have\s+been) | (?:make|change)\s+(?:it|that)
      | i\s+(?:typed|gave|sent|put)\s+(?:it|that|you)?\s*(?:the\s+)?wrong
      | wrong\s+(?:one|name|number|zip|email|address)
      | (?:that|it|this)\s+(?:was|is|isn'?t)\s+(?:wrong|not\s+right)
      | use\s+(?:this|my\s+other) | new\s+(?:number|email|zip)
"""
_VALUE_CORRECTION = re.compile(r"(?ix)(?:" + _CORRECTION_FRAME + r")")
# The name takes the frames above plus its own: "my name is Chance" corrects
# a name and nothing else.
_NAME_CORRECTION = re.compile(
    r"(?ix)(?:" + _CORRECTION_FRAME + r"""
      | my\s+name(?:'?s)?\s+is | name\s+is\s+actually
      | (?:it'?s|its|that'?s)\s+actually | call\s+me | go(?:es)?\s+by
    )"""
)


def _replaces_value(
    stored: str | None,
    captured: str | None,
    user_message: str,
    *,
    frames: re.Pattern[str] | None = None,
    digits_only: bool = False,
) -> bool:
    """May ``captured`` overwrite ``stored``? Only when the homeowner typed it
    in this message AND said it replaces what they gave before.

    ``digits_only`` compares a phone number by its digits, because the one
    they type back is rarely punctuated the way the first one was.
    """
    if not (stored and captured) or captured.strip().lower() == stored.strip().lower():
        return False
    message = user_message or ""
    if digits_only:
        wanted = re.sub(r"\D", "", captured)
        typed = bool(wanted) and wanted in re.sub(r"\D", "", message)
    else:
        typed = re.search(rf"\b{re.escape(captured)}\b", message, re.I) is not None
    return typed and bool((frames or _VALUE_CORRECTION).search(message))


# Someone else's details, however short the message. "call Dave at
# 509-216-0574" is four words and is not the homeowner's number.
_THIRD_PARTY_CONTACT = re.compile(
    r"""(?ix)
    \b(?:his|her|hers|their|theirs|him|them|he|she|they)\b
    # Case matters here and nowhere else in this pattern: a capitalised word
    # after "call" is somebody's name, "call back" is not.
    | (?:call|text|email|contact|reach|ping|try)\s+(?!me\b|us\b)(?-i:[A-Z][a-z]+)
    """
)


def _gives_own_details(message: str) -> bool:
    """A short message IS the answer to what was just asked — someone typing
    their number types the number. A longer one has to say whose it is, and
    neither is enough if the message points at somebody else."""
    if _THIRD_PARTY_CONTACT.search(message):
        return False
    return len(message.split()) <= 6 or bool(_OWN_CONTACT.search(message))


def _precapture_from_message(state: FlowState, message: str) -> dict[str, str]:
    """Take contact details straight out of the homeowner's message, BEFORE
    the turn is planned.

    The gates run on the pre-turn state, but the model's capture is applied
    after the reply is generated and checked — so the turn where someone
    finally types their number was judged as though contact were still
    missing. On Sep 16 that cost a card: the reply announcing it was
    suppressed as a claim about a card that wasn't there, and the card then
    shipped attached to the safe copy, which doesn't mention it. Reading the
    contact here settles it on the turn that earned it, the way
    `_accepts_request` already does for the homeowner's yes.

    ponytail: contact only. An address has the same off-by-one-turn problem,
    but every regex loose enough to catch "1450 Pheasant Hill Drive" also
    caught "2 rooms done this way" — and the slot is write-once, so a wrong
    capture poisons the lead permanently. The address is settled from the
    model's own capture instead, at the point enforcement runs
    (`_capture_has_address`).
    """
    delta: dict[str, str] = {}
    if not message or not _gives_own_details(message):
        return delta
    slots = state.slots
    email = _EMAIL_IN_MESSAGE.search(message)
    if email and not slots.contact_email:
        slots.contact_email = email.group(0)[:120]
        delta["contactEmail"] = slots.contact_email
    phone = _PHONE_IN_MESSAGE.search(message)
    if phone and not slots.contact_phone:
        slots.contact_phone = phone.group(0).strip()[:40]
        delta["contactPhone"] = slots.contact_phone
    return delta


# "don't send it yet", "no need to generate a request": the ask words, negated.
_NEGATED_ASK = re.compile(r"(?i)\b(?:don'?t|do\s+not|not|never|no\s+need\s+to)\b[\w\s']{0,12}$")
# A 5-digit number is only a zip when the message says so ("a 15000 budget" is not).
_ZIP_CONTEXT = re.compile(r"(?i)\b(zip|area|code|live|located)\b")

_SAFE_GENERIC_COPY = (
    "Happy to keep exploring ideas for your space — tell me a bit more about "
    "what you'd like it to feel like, and we can go from there."
)

_SAFE_NO_CARD_COPY = (
    "There's no request card in front of you yet, so nothing is waiting on a "
    "tap from you. Tell me a bit more about what you'd like done and I'll get "
    "it ready for you to look over."
)

_SAFE_TIMEOUT_COPY = (
    "Sorry — that one took longer than it should have on my end. Could you "
    "send that again? I'm still here with your room details."
)

_SAFE_NO_PRICE_COPY = (
    "A real number has to come from a provider who's seen your actual space, "
    "so I won't guess at one. If you'd like, I'll package what we've "
    "discussed as a quote request and bring their figures back to you here."
)


# --------------------------------------------------------------------------
# Opening request (turn zero) — API_CONTRACT_V1 §4
# --------------------------------------------------------------------------
class HomeAIOpeningRequest(BaseModel):
    threadId: Optional[str] = None
    userId: Optional[str] = None
    projectId: Optional[str] = None
    homeProfileId: Optional[str] = None
    sourcePage: Optional[str] = None
    homeContext: HomeAIContextPacket = Field(default_factory=HomeAIContextPacket)
    workflowState: HomeAIWorkflowState = Field(default_factory=HomeAIWorkflowState)
    scanContext: Optional[Any] = None
    flowToken: Optional[str] = None
    homeId: Optional[str] = Field(default=None, max_length=120)


# --------------------------------------------------------------------------
# State resolution and persistence
# --------------------------------------------------------------------------
_runtime_flow_secret: str | None = None


def _codec() -> FlowTokenCodec:
    """Token signing key: LIDARAI_FLOW_TOKEN_SECRET, else the service auth
    token, else a per-process random secret. Never a committed constant — a
    public fallback key would let anyone forge flow state (scan complete,
    prefilled slots, linked identity). The random fallback means tokens die
    with the process; state then recovers from the durable store."""
    global _runtime_flow_secret
    secret = settings.flow_token_secret or settings.auth_token
    if not secret:
        if _runtime_flow_secret is None:
            _runtime_flow_secret = secrets.token_hex(32)
            logger.warning(
                "No LIDARAI_FLOW_TOKEN_SECRET or auth token configured; using a "
                "per-process random flow-token secret (tokens will not survive restarts)"
            )
        secret = _runtime_flow_secret
    return FlowTokenCodec(secret)


def _state_path(thread_id: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", thread_id)
    return Path(settings.storage_dir) / "flow_state" / f"{safe}.json"


async def resolve_flow_state(thread_id: str, flow_token: str | None) -> FlowState:
    """Highest revision wins between the signed client token and the durable
    store (token wins ties); then the local state file; then fresh. The
    revision comparison is what lets server-side advances (a quote
    submission, an ops status change) surface even when the client echoes an
    older token."""
    token_state: FlowState | None = None
    if flow_token:
        try:
            token_state = _codec().decode(flow_token)
            # A token is valid for the thread it was minted for and no other.
            # Without this, a token from one conversation replayed against
            # another thread id takes that thread over: it wins the revision
            # race, adopts the other party's verified identity, and its own
            # contents are persisted over theirs.
            if token_state.thread_id and token_state.thread_id != thread_id:
                logger.warning(
                    "Flow token for thread %s presented on thread %s; ignoring",
                    token_state.thread_id, thread_id,
                )
                token_state = None
            else:
                token_state.thread_id = thread_id
        except InvalidFlowToken as exc:
            logger.warning("Invalid flow token for thread %s: %s", thread_id, exc)
    durable = await supabase_store.get_flow_state(thread_id)
    if durable is None:
        try:
            path = _state_path(thread_id)
            if path.exists():
                durable = FlowState.model_validate(json.loads(path.read_text(encoding="utf-8")))
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not load flow state for thread %s: %s", thread_id, exc)
    if durable is not None:
        durable.thread_id = thread_id
    if token_state is not None and durable is not None:
        if token_state.revision >= durable.revision:
            return _merge_redacted(token_state, durable)
        return durable
    if token_state is not None:
        return token_state
    if durable is not None:
        return durable
    return FlowState(thread_id=thread_id)


def _merge_redacted(chosen: FlowState, durable: FlowState) -> FlowState:
    """The client token carries captured/linked flags instead of sensitive
    values (tokens.py); when the token wins the revision race, refill the
    actual values from the durable copy."""
    s, d = chosen.slots, durable.slots
    if s.address is None and s.address_redacted and d.address:
        s.address = d.address
        s.address_redacted = False
    if s.contact_email is None and s.contact_email_redacted and d.contact_email:
        s.contact_email = d.contact_email
        s.contact_email_redacted = False
    if s.contact_phone is None and s.contact_phone_redacted and d.contact_phone:
        s.contact_phone = d.contact_phone
        s.contact_phone_redacted = False
    if chosen.homeowner_id is None and chosen.homeowner_linked and durable.homeowner_id:
        chosen.homeowner_id = durable.homeowner_id
    if (
        chosen.homeowner_auth_sub is None
        and chosen.homeowner_linked
        and durable.homeowner_auth_sub
    ):
        chosen.homeowner_auth_sub = durable.homeowner_auth_sub
    if chosen.opening_response is None and durable.opening_response is not None:
        chosen.opening_response = durable.opening_response
    return chosen


def encode_flow_token(state: FlowState) -> str:
    return _codec().encode(state)


async def persist_flow_state(state: FlowState) -> None:
    try:
        path = _state_path(state.thread_id or "unknown")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(state.model_dump_json(), encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not persist flow state: %s", exc)
    await supabase_store.upsert_flow_state(state)


async def _attach_identity(state: FlowState, auth_sub: str | None) -> None:
    """Attach a verified identity to the flow state.

    ``auth_sub`` is the JWT ``sub`` — the Supabase **auth** user id. The flow
    tables' ``homeowner_id`` columns reference ``homeowners(id)``, a different
    uuid, so the sub must be resolved before it can be written durably; the
    homeowners row also supplies real contact info for the lead package
    (signed-in homeowners are never asked for an email in chat)."""
    if not auth_sub:
        return
    state.homeowner_auth_sub = auth_sub
    row = await supabase_store.resolve_homeowner(auth_sub)
    if row:
        state.homeowner_id = row.get("id")
        slots = state.slots
        if not slots.contact_email and row.get("email"):
            slots.contact_email = row["email"]
        if not slots.contact_phone and row.get("phone"):
            slots.contact_phone = row["phone"]
        if not slots.first_name and row.get("full_name"):
            slots.first_name = str(row["full_name"]).strip().split()[0]
    elif not supabase_store.enabled():
        # No durable store to violate: keep the sub as the working identity
        # (local runs and unit tests).
        state.homeowner_id = auth_sub
    elif state.homeowner_id == auth_sub:
        # A pre-resolution state stored the raw sub; clear it so durable
        # writes don't trip the homeowners(id) foreign key.
        state.homeowner_id = None


# --------------------------------------------------------------------------
# Scan-state reconciliation (SOW §3 — server wins)
# --------------------------------------------------------------------------
_JOB_STATUS_TO_FLOW: dict[str, ScanProcessingState] = {
    "queued": ScanProcessingState.PROCESSING,
    "running": ScanProcessingState.PROCESSING,
    "complete": ScanProcessingState.COMPLETE,
    "failed": ScanProcessingState.FAILED,
    "cancelled": ScanProcessingState.FAILED,
}


def _server_job_state(job_id: str | None) -> ScanProcessingState | None:
    if not job_id:
        return None
    try:
        record = store.get(job_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Job store lookup failed for %s: %s", job_id, exc)
        return None
    if record is None:
        return None
    status = getattr(record.status, "value", str(record.status))
    return _JOB_STATUS_TO_FLOW.get(status)


SCAN_SIGNAL_PROCESSOR_JOB = "processor_job"
SCAN_SIGNAL_DEVICE_BAKE = "device_bake"


def scan_complete_signal() -> str:
    """Which flag opens the SOW §3 gate (LIDARAI_SCAN_COMPLETE_SIGNAL). An
    unknown value falls back to the processor job -- the stricter of the
    two, since it can be server-verified."""
    value = (settings.scan_complete_signal or "").strip().lower()
    return SCAN_SIGNAL_DEVICE_BAKE if value == SCAN_SIGNAL_DEVICE_BAKE else SCAN_SIGNAL_PROCESSOR_JOB


def _reconcile_scan(state: FlowState, request: HomeAIChatRequest) -> None:
    signal = scan_complete_signal()
    if request.scanContext is not None:
        client_state = request.scanContext.parsed_state()
        client_job_id = request.scanContext.jobId
        client_scan_id = request.scanContext.scanId
        client_progress = request.scanContext.processingProgress
        bake_ready = request.scanContext.localModelReady
    else:
        # Legacy clients: the collapsed photorealStatus word, jobId from the packet.
        mesh = request.homeContext.meshSummary or {}
        photoreal = mesh.get("photorealStatus")
        client_state = ScanProcessingState.from_photoreal_status(
            photoreal if isinstance(photoreal, str) else None
        )
        client_job_id = request.homeContext.jobId
        client_scan_id = request.homeContext.scanId
        client_progress = None
        bake_ready = mesh.get("localModelReady") if isinstance(mesh.get("localModelReady"), bool) else None
    server_job_state = _server_job_state(client_job_id or state.scan.job_id)
    # The processor pipeline's own state, reconciled the usual way. It is
    # what the model link and the journal read whatever the gate's flag is.
    processor = _engine.reconcile_scan(
        state,
        client_state=client_state,
        client_job_id=client_job_id,
        client_scan_id=client_scan_id,
        client_progress=client_progress,
        server_job_state=server_job_state,
    )
    if signal == SCAN_SIGNAL_DEVICE_BAKE:
        # The phone's own bake is the flag: only an explicit localModelReady
        # opens or holds the gate. Keep the processor state alongside.
        if bake_ready is True:
            bake_state = ScanProcessingState.COMPLETE
        elif bake_ready is False:
            bake_state = ScanProcessingState.PROCESSING
        else:
            bake_state = ScanProcessingState.UNKNOWN   # keeps the last bake-derived state
        state.scan = _engine.reconcile_scan(
            state,
            client_state=bake_state,
            client_job_id=client_job_id,
            client_scan_id=client_scan_id,
            client_progress=client_progress,
            server_job_state=None,
        )
        state.scan.processor_state = processor.state
    else:
        state.scan = processor
        state.scan.processor_state = processor.state
    state.scan.signal = signal
    if (
        signal == SCAN_SIGNAL_PROCESSOR_JOB
        and state.scan.state is ScanProcessingState.COMPLETE
        and not state.scan.server_verified
    ):
        # The SOW's processing flag is the client's; honor it — but make the
        # unverified opening visible to ops (job record pruned, or a claim
        # for a job this server never saw).
        logger.warning(
            "Scan gate opening on client-reported complete without server "
            "verification thread=%s job=%s",
            state.thread_id,
            state.scan.job_id,
        )


# --------------------------------------------------------------------------
# Directives: what the prompt is permitted to do this turn
# --------------------------------------------------------------------------
# "that's the pantry" / "we call it the mudroom" — the homeowner naming a
# space the scan could not identify. Only accepted for a room already in
# focus, and only for words that are actually rooms, so "that's the problem"
# never renames anything.
_NAMES_A_ROOM = re.compile(
    r"(?i)(?:that(?:'s| is)|this is|it(?:'s| is)|we call it|that would be"
    # "the small space you called unnamed area 1 is the mudroom" (seen live,
    # Sep 10): a room phrase, then "is", then the name. Still guarded by the
    # room-word list and the in-focus rule below.
    r"|(?:room|space|area|one)\b[^.!?]{0,60}?\bis)\s+"
    r"(?:the\s+|our\s+|my\s+|a\s+)?([a-z][a-z ]{2,24}?)\s*[.!,]?$"
)

_QUESTION_OPENER = re.compile(
    r"(?i)^\s*(?:which|what|where|who|why|how|is|are|was|does|do|did|can|could|should|would|will)\b"
)

_NAMEABLE = {
    "pantry", "closet", "mudroom", "laundry room", "laundry", "office",
    "home office", "study", "den", "playroom", "nursery", "gym", "studio",
    "workshop", "sunroom", "foyer", "entry", "entryway", "hallway", "hall",
    "landing", "garage", "basement", "attic", "storage room", "utility room",
    "guest room", "spare room", "dining room", "sitting room", "living room",
    "family room", "bedroom", "bathroom", "kitchen", "breakfast nook",
    "media room", "library", "cellar", "porch", "conservatory",
}


def _detect_room_naming(state: FlowState, index, message: str, target=None):
    """Did the homeowner just tell us what a room is?

    Their word is the most authoritative name available — better than
    RoomPlan's label and far better than our fixture guess — so it is worth
    catching, but only when it is unambiguous: a room must be in focus (the
    one this message names, else the active one), and the word must be a
    room.
    """
    if not (index and message):
        return None
    text = message.strip()
    # A question is never a naming: "which one is the office" asks, it does
    # not tell (review, Sep 10).
    if "?" in text or _QUESTION_OPENER.match(text):
        return None
    match = _NAMES_A_ROOM.search(text)
    if not match:
        return None
    name = re.sub(r"\s+", " ", match.group(1).strip().lower())
    if name not in _NAMEABLE:
        return None
    room = target
    if room is None:
        room = index.by_key(state.active_room_key) if state.active_room_key else None
    if room is None:
        return None
    # Never overwrite a name the fixtures actually prove (a room with a
    # toilet is a bathroom whatever it gets called in passing).
    if room.confident and not room.named_by_homeowner:
        return None
    return room.key, name


def _reconcile_home(state: FlowState, request: HomeAIChatRequest):
    """Attach the whole-home index and track which room is being discussed.

    Single-room captures have no index and behave exactly as before. With
    one, the conversation gains a subject: the homeowner says "the master
    bathroom" and every later turn is about that room until they move.
    """
    from .flow.home_registry import load_index

    if request.homeId:
        if request.homeId != state.home_id:
            # A different home in the same thread starts a fresh subject.
            state.active_room_key = None
        state.home_id = request.homeId
    index = load_index(state.home_id)
    if index is None:
        return None

    # "that little room" is how a homeowner points at the space the scan
    # could not identify, and it is usually the turn before they name it.
    room, unresolved = index.mentioned_room(request.message or "")
    if room is None and not unresolved:
        room = index.small_unnamed_room(request.message or "")

    # "that space you called unnamed area 1 is the mudroom": the room the
    # message names is the one being named, not whichever was in focus.
    naming = _detect_room_naming(state, index, request.message or "", target=room)
    if naming:
        key, name = naming
        renamed = index.rename_room(key, name)
        if renamed is not None:
            from .flow.home_registry import save_index

            save_index(state.home_id, index)
            state.active_room_key = key
            state.unresolved_room_phrase = None
            logger.info("Homeowner named %s '%s' (home=%s)", key, name, state.home_id)
            return index

    if room is not None:
        state.active_room_key = room.key
        state.unresolved_room_phrase = None
        state.unresolved_room_turns = 0
    elif unresolved:
        # Do NOT clear the active room: they may be asking about a space
        # that was never scanned while still working on the current one.
        state.unresolved_room_turns = (
            state.unresolved_room_turns + 1
            if unresolved == state.unresolved_room_phrase
            else 1
        )
        state.unresolved_room_phrase = unresolved
    elif state.unresolved_room_phrase:
        # Still stuck on it. The follow-ups in a dead end ("ok how?",
        # "which room is closest?") rarely repeat the room's name, and
        # resetting on those is what let the loop run for twelve turns.
        state.unresolved_room_turns += 1
    else:
        state.unresolved_room_turns = 0
    return index


_SURFACE_WORDS = {
    "floor": "floor",
    "walls": "walls",
    "ceiling": "ceiling",
    "splashback": "splashback",
}


def _appearance_directives(home_id: str | None, room) -> list[str]:
    """What the scan photos show of a room's materials and finishes.

    Quintin, Sep 11: he asked what flooring he had and the agent could not
    say. Two reasons, and this covers both. His home had never been through
    the appearance pass, so there was nothing to say; and no code path ever
    put that pass's output in front of the model even where it had run, so
    the agent saw a room as a name, a footprint and a list of fixture
    classes -- geometry with no surfaces on it.

    When the pass has run, its surfaces go in. When it has not, saying so is
    the directive: the same call agreed that admitting a gap builds more
    trust than a confident wrong answer, and an agent that cannot see the
    floor must not guess what it is made of.
    """
    from .flow import home_registry

    context = home_registry.room_context_for(home_id, room.key) if room else None
    if not context:
        return [
            "- You have this room's SHAPE ONLY -- no photographs of its "
            "surfaces. If they ask what the flooring, paint colour, "
            "countertops, finishes or condition are, say plainly that this "
            "scan does not show you that and ask them, rather than guessing. "
            "Never describe a material, colour or condition you were not given."
        ]

    lines: list[str] = []
    surfaces = context.get("surfaces") or {}
    seen = [
        f"{_SURFACE_WORDS[key]}: {surfaces[key]}"
        for key in ("floor", "walls", "ceiling", "splashback")
        if isinstance(surfaces.get(key), str) and surfaces[key].strip()
    ]
    described = [
        f"{obj.get('class')} ({obj['appearance']})"
        for obj in context.get("objects") or []
        if obj.get("certainty") != "unobserved"
        and isinstance(obj.get("appearance"), str)
        and obj["appearance"].strip()
    ]
    style = (context.get("style") or "").strip()
    notable = [n for n in (context.get("notable") or []) if isinstance(n, str) and n.strip()]

    if seen or described or style or notable:
        detail = "- WHAT THE SCAN PHOTOS SHOW of this room, and the only "
        detail += "materials, colours or finishes you may state as fact:"
        if seen:
            detail += "\n  Surfaces -- " + "; ".join(seen) + "."
        if described:
            detail += "\n  Items -- " + "; ".join(described[:6]) + "."
        if style:
            detail += f"\n  Overall -- {style}."
        if notable:
            detail += "\n  Worth noting -- " + "; ".join(notable[:3]) + "."
        lines.append(
            detail + "\n  Use these when they are relevant, in your own words. "
            "They come from photographs of THIS room, so you really can say "
            "them. Anything not listed here you did not see."
        )
    else:
        lines.append(
            "- The appearance pass on this room returned nothing usable, so "
            "you have its shape but not its surfaces. If they ask about "
            "flooring, colours or finishes, say the scan does not show you "
            "that and ask them. Never guess a material."
        )

    coverage = context.get("coverage")
    if coverage in {"partial", "geometry_only"}:
        unobserved = [
            str(obj.get("class"))
            for obj in context.get("objects") or []
            if obj.get("certainty") == "unobserved"
        ]
        if unobserved:
            lines.append(
                "- These items are in the room's layout data but were never "
                "photographed, so you know they exist and nothing about how "
                "they look: " + ", ".join(sorted(set(unobserved))[:8]) + ". "
                "Name them if it helps, but never describe their colour, "
                "material or condition."
            )
    return lines


_HOME_MATERIALS_ROOMS = 8


def _home_appearance_directives(home_id: str | None, index) -> list[str]:
    """Floors and walls across the home, for when no room is in focus.

    Quintin, Sep 14: he asked what the agent thought of "my walls and floors"
    without naming a room. Surfaces only ever reached the model for the active
    room, so a whole-home question got nothing and the agent said it could
    not see them.
    """
    from .flow import home_registry

    seen: list[str] = []
    for room in sorted(index.rooms, key=lambda r: (r.storey, -r.area_sqft)):
        surfaces = (home_registry.room_context_for(home_id, room.key) or {}).get("surfaces") or {}
        parts = [
            f"{_SURFACE_WORDS[key]} {surfaces[key]}"
            for key in ("floor", "walls")
            if isinstance(surfaces.get(key), str) and surfaces[key].strip()
        ]
        if parts:
            seen.append(f"  {room.display_name}: " + "; ".join(parts))

    if not seen:
        return [
            "- You have this home's room SHAPES ONLY -- no photographs of its "
            "surfaces. If they ask about the flooring, wall colours or finishes, "
            "say plainly that this scan does not show you that and ask them, "
            "rather than guessing. Never describe a material, colour or "
            "condition you were not given."
        ]
    lines = [
        "- WHAT THE SCAN PHOTOS SHOW across the home, and the only floor and "
        "wall materials you may state as fact:\n"
        + "\n".join(seen[:_HOME_MATERIALS_ROOMS])
        + "\n  If they ask about the home's walls or floors, answer from these "
        "in your own words, then offer to look closer at one room."
    ]
    # Rooms past the cap are unlisted too, even when the pass covered them.
    if min(len(seen), _HOME_MATERIALS_ROOMS) < len(index.rooms):
        lines.append(
            "- For any room not listed above you do not know the floor or wall "
            "finishes. If it comes up, say so and ask them. Never guess."
        )
    return lines


def _home_directives(state: FlowState, index) -> list[str]:
    lines = [
        "- WHOLE-HOME SCAN. This homeowner walked their whole home, so you "
        "know its rooms. These are the ONLY rooms you can see — never invent "
        "or imply another one:\n" + index.as_text(),
        "- Rooms marked 'name uncertain' are the server's best guess from "
        "fixtures. Use the name naturally, but if the conversation turns on "
        "which room it is, ask rather than asserting.",
    ]
    room = index.by_key(state.active_room_key) if state.active_room_key else None
    if room is not None:
        fixtures = ", ".join(f"{n} {c}" for c, n in room.objects.most_common(6))
        detail = (
            f"- ACTIVE ROOM: the {room.display_name} — about "
            f"{round(room.area_sqft)} sq ft, {room.window_count} window(s), "
            f"{room.door_count} door(s)"
        )
        if fixtures:
            detail += f", with {fixtures}"
        lines.append(
            detail + ". Talk about THIS room unless the homeowner moves to "
            "another. Do not describe rooms they have not raised."
        )
        layout = index.layout_text(room)
        if layout:
            lines.append(
                "- LAYOUT, from the walk's floor plan (you DO know this; answer "
                f"layout questions from it): {layout} A level change is stated "
                "only where the floor heights differ; where none is listed, say "
                "the walk did not measure a step there rather than that you "
                "cannot tell."
            )
        else:
            lines.append(
                "- LAYOUT: the walk did not capture a wall this room shares with "
                "another. If they ask what is next to it, say so plainly, ask "
                "them which room it is, and use their answer from then on."
            )
        lines.extend(_appearance_directives(state.home_id, room))
        if room.named_by_homeowner:
            lines.append(
                f"- They told you this room is the {room.display_name}, and "
                "that is now its name. Use it plainly. Never say it isn't "
                "labelled or that you don't have it — you do."
            )
        elif not room.confident:
            lines.append(
                f"- The name '{room.display_name}' is inferred, not certain. "
                "If they correct you, accept it immediately and move on. If "
                "they tell you what the room is, use their name for it from "
                "then on."
            )
    else:
        lines.append(
            "- No specific room is in focus yet. If their message is about "
            "the home generally, help at that level; when they name a room, "
            "work in that one."
        )
        lines.extend(_home_appearance_directives(state.home_id, index))
        overview = index.layout_overview()
        if overview:
            lines.append(
                "- LAYOUT, rooms that share a wall (from the walk's floor plan; "
                f"answer layout questions from it): {overview}."
            )
    if state.unresolved_room_phrase:
        if state.unresolved_room_turns >= 2:
            # Twelve turns of scan-extension instructions and no design help
            # is a dead end (Sep 4 battery). The room being missing does not
            # make the project unhelpable.
            lines.append(
                f"- They still want the {state.unresolved_room_phrase}, which is "
                "NOT in the scan, and you have already explained how to add it. "
                "STOP repeating those instructions — they are not working. "
                "Help with the project anyway: ask them to describe the space "
                "in a line or two (size, light, what's in it) and give real "
                "design guidance from that, exactly as you would for a room "
                "you can see. Never describe it as though you can see it."
            )
        else:
            lines.append(
                f"- They mentioned '{state.unresolved_room_phrase}', which is NOT "
                "in the scan. Say plainly that you don't have that space in what "
                "was walked, and never describe it as though you can see it. "
                "Mention adding it to the walk at most once, then move straight "
                "to being useful — you can still help with that room from what "
                "they tell you about it."
            )
    return lines


def _memory_directives(state: FlowState) -> list[str]:
    """Tell the model what it already knows.

    The persona battery (Sep 3) found the agent's worst behaviour was here,
    not in safety: it re-asked for a zip that had just been given, denied a
    colour the homeowner named one turn earlier, and once recited the
    scanned room's geometry as a summary of a conversation that never
    happened. The captured slots existed the whole time — they were simply
    never put in front of the model. An explicit ledger, plus a rule about
    what it may claim to remember, is the fix.
    """
    s = state.slots
    known: list[str] = []
    if s.first_name:
        known.append(f"their name is {s.first_name}")
    if s.zip:
        known.append(f"zip {s.zip}")
    if s.project_type:
        known.append(f"project: {s.project_type}")
    if s.scope_options:
        known.append("scope: " + "; ".join(s.scope_options))
    if s.materials:
        known.append("materials: " + ", ".join(s.materials))
    if s.address_captured:
        known.append("address captured")
    if s.contact_captured:
        known.append("contact details captured")

    lines: list[str] = []
    if known:
        lines.append(
            "- ALREADY ESTABLISHED — " + "; ".join(known) + ". Treat every one "
            "of these as said and settled: never ask for them again, never say "
            "you have no record of them, and do not re-confirm them unless the "
            "homeowner brings one up to change it. If they DO correct one, "
            "record the new value in flowCapture — acknowledging it in your "
            "reply changes nothing on its own."
        )
    if not s.address_captured:
        # The mirror image of the ledger above, and the more dangerous half:
        # on Sep 16 the agent told a homeowner his address only goes to the
        # provider he picks, then — challenged — said it "was available
        # earlier as part of your contact details". No address had ever been
        # given. An unset slot has to be stated as plainly as a set one.
        lines.append(
            "- No street address has been captured in this conversation so "
            "far. Never say you have one, never say it came with their "
            "contact details or was captured earlier, and make no promise "
            "about where their address goes. If they raise it, say plainly "
            "that you don't have one. (If they give you an address in THIS "
            "message, take it and record it in flowCapture.address — that one "
            "you do have.)"
        )
    lines.append(
        "- MEMORY DISCIPLINE. What you know is this conversation plus the "
        "home details you were given. When the homeowner refers to something "
        "they told you, CHECK the conversation above. If it is there, treat "
        "it as settled — never claim you have no record of it. If it is NOT "
        "there, do not affirm it as remembered: never open with 'you're "
        "right' about something you cannot find. Say lightly that you don't "
        "have it from your conversation, then take their word and move on — "
        "agreeing to the fact is fine, pretending to remember it is not. If "
        "you are asked to recap, summarise ONLY what was actually discussed. "
        "Never present the room's measurements or contents as something they "
        "told you, and never invent an earlier exchange."
    )
    lines.append(
        "- Do not invent specifics you cannot know: no brand names, product "
        "models, SKUs, store names, quantities of materials, or timelines "
        "presented as fact. Describe qualities and let a provider specify."
    )

    # Energy matching, measured and proportional. A 35-word cap still reads
    # as a lecture to someone typing "queen" (Sep 4 battery), so the cap
    # tracks how terse they actually are.
    recent = state.recent_user_words[-3:]
    if len(recent) >= 2:
        average = sum(recent) / len(recent)
        if average <= 3:
            lines.append(
                "- They are answering in ONE OR TWO WORDS. Mirror it: a single "
                "short sentence, 20 words at most. No preamble, no explaining "
                "your reasoning, no naming the trade-offs, and ask a question "
                "only when you genuinely cannot go on without it. If you can "
                "answer in five words, do."
            )
        elif average <= 7:
            lines.append(
                "- They are answering in a few words at a time. MATCH THAT: "
                "reply in one or two short sentences, 35 words at most, one "
                "question maximum, no lists of options and no explaining why "
                "you're asking. Long replies to short answers read as not "
                "listening."
            )
    return lines


def _build_directives(
    state: FlowState,
    plan: TurnPlan,
    *,
    opening: bool,
    price_guidance: PriceGuidance | None,
    quotes_to_present: list[dict[str, Any]] | None,
    price_asked: bool = False,
    new_quote_count: int = 0,
    local_context=None,      # LocalContextWire | None
    local_providers=None,    # LocalProvidersWire | None
    home_index=None,         # HomeIndex | None
    home_switched: bool = False,
    guard=None,              # input_guard.GuardVerdict | None
    selected_quote: dict[str, Any] | None = None,
    quotes_on_file: list[dict[str, Any]] | None = None,
    declined_quotes: list[dict[str, Any]] | None = None,
    lead_delivered: bool = False,
) -> str:
    g = plan.gates
    lines: list[str] = [
        "FLOW DIRECTIVES (server-enforced; follow exactly — violations are "
        "stripped before the homeowner sees them):",
    ]
    if home_index is not None:
        lines.extend(_home_directives(state, home_index))
        # Their word for a room beats the scan's label, and which words are
        # likely follows from the zip we already captured (Sep 11 call).
        if (vocabulary := regional_naming.directive_for(state.slots.zip)) is not None:
            lines.append(vocabulary)
        if home_switched:
            # Same thread, different home: the transcript above is about the
            # previous house, and the model otherwise insists there is only
            # one home (seen live, Sep 10).
            lines.append(
                "- The homeowner has just switched to a DIFFERENT home than the "
                "one discussed earlier in this conversation. Everything above "
                "this point was about the previous home; the rooms listed here "
                "are the new one's. Acknowledge the switch plainly and work "
                "from this home's rooms from now on — never say there is only "
                "one home."
            )

    if opening and home_index is not None:
        # A whole-home walk has no single room to open on: opening with one
        # would pick a room they never chose.
        lines.append(
            "- This is the OPENING turn. There is no homeowner message yet. "
            "Introduce yourself as TakeShape's AI assistant for their home in "
            "one natural clause — not a disclaimer, not an explanation of how "
            "you work. "
            "They walked their WHOLE HOME, so open at that level: say what "
            "you can see across the home (how many spaces, how many levels), "
            "name one or two rooms specifically so it's clear you really "
            "have them, then ask which space they'd like to start with and "
            "ask for their first name. "
            f"{FIRST_NAME_WORDING.guidance} Keep it under 80 words. Do not "
            "list every room."
        )
    elif opening:
        lines.append(
            "- This is the OPENING turn. There is no homeowner message yet. "
            "Introduce yourself as TakeShape's AI assistant for their home in "
            "one natural clause — not a disclaimer, not an explanation of how "
            "you work. Then open by naming the room you can see and ONE "
            "specific thing you "
            "genuinely observe in the provided views (a piece of furniture, a "
            "finish, the light). Name the room type when the evidence is "
            "clear — a bed means bedroom, a stove means kitchen — and just "
            "say 'this room' when it genuinely isn't; never guess a room "
            "type the views don't support. Then ask one warm engagement "
            "question about their hopes for the space, and ask for their "
            "first name only. "
            f"{FIRST_NAME_WORDING.guidance} Keep it under 80 words."
        )

    # SOW §3 hard constraint, then the stated scope narrows an open gate.
    continuity = (
        "always with this continuity guidance: start from the room already "
        "captured, walk a connected path to the new space, and keep "
        "everything in the same capture session."
    )
    if g.can_prompt_additional_scan:
        mode = g.extension_prompt_mode
        if mode == EXTENSION_OPEN:
            if not state.is_complete(FlowStep.SCAN_EXTENSION):
                lines.append(
                    "- The home model is fully ready. If more of the home would "
                    "help the conversation, you may invite them to add another "
                    "area — " + continuity
                    + (
                        " They said the WHOLE HOME is the project, so inviting "
                        "them to add areas not yet captured is appropriate."
                        if state.scope_intent is ScopeIntent.WHOLE_HOME else ""
                    )
                )
        elif mode == EXTENSION_NAMED_ROOMS:
            rooms = ", ".join(state.scope_rooms)
            lines.append(
                f"- The home model is ready. Their project covers ONLY these "
                f"rooms: {rooms}. If one of those is not captured yet you may "
                f"invite them to add THAT room — {continuity} Never suggest "
                "capturing the rest of the home, other rooms, or unrelated areas."
                + (
                    " You may ask ONCE, lightly, whether there is anything else "
                    "they'd want to include — only once in the whole conversation."
                    if g.extension_generic_offer_available else
                    " You have already asked whether there is anything else; do not ask again."
                )
            )
        elif mode == EXTENSION_GENERIC_ONCE:
            lines.append(
                "- The home model is ready. Their project is THIS room only. Do "
                "not suggest capturing other rooms or the rest of the home. You "
                "may ask ONCE, lightly, whether there is anything else they'd "
                "want to include; if they say no, never raise it again."
            )
        elif mode == EXTENSION_NONE:
            lines.append(
                "- The home model is ready, and their project is limited to the "
                "space(s) they chose. You have already asked whether there is "
                "anything else. Do NOT suggest capturing, adding, or updating "
                "any other room or area, and do not ask again."
            )
    else:
        lines.append(
            "- HARD RULE: the home model is still being prepared "
            f"(state: {state.scan.state}). Do NOT suggest capturing, adding, "
            "updating, or re-doing any room or area, and do not imply more "
            "coverage is needed. If asked, say the home model is still being "
            "prepared and continue with what it already shows — in your own "
            "words, varied each time."
        )
        if state.scan.state is ScanProcessingState.FAILED:
            lines.append(
                "- The preparation hit a problem. If the homeowner asks about "
                "their model, let them know it needs another try from the app "
                "and keep the design conversation going."
            )

    if not opening:
        if g.can_ask_first_name:
            lines.append(f"- You may ask for their first name once. {FIRST_NAME_WORDING.guidance}")
        else:
            lines.append("- Do not ask for their name.")
        if g.can_ask_zip and plan.zip_wording_id:
            wording = wording_by_id(plan.zip_wording_id)
            if wording:
                guidance = wording.guidance
                if wording.id == "step4.zip.local_styles_v1" and not settings.local_context_enabled:
                    guidance = LOCAL_STYLES_NO_RESEARCH_GUIDANCE
                lines.append(
                    f"- You may ask for their zip code this turn if it fits "
                    f"naturally — and if you do, that is your ONE question for "
                    f"this reply (no second question). {guidance}"
                )
        else:
            lines.append("- Do not ask for a zip or postal code.")
        if g.can_ask_address:
            lines.append(
                f"- You may ask for their street address. {ADDRESS_WORDING.guidance} "
                "The address is a nice-to-have, NOT a requirement: if they skip "
                "it, deflect it, or say no, let it go entirely and never hold the "
                "quote request for it."
            )
        else:
            lines.append("- Do not ask for a street address.")
        if g.can_ask_scope and plan.scope_wording_id:
            wording = wording_by_id(plan.scope_wording_id)
            if wording:
                lines.append(
                    "- You may ask about the SCOPE of their project this turn if "
                    "it fits naturally — and if you do, that is your ONE question "
                    f"for this reply. {wording.guidance} Record their answer in "
                    "flowCapture.scopeIntent."
                )
        elif state.scope_intent is ScopeIntent.UNDECIDED and state.scope_asks:
            lines.append("- Do not ask again whether the project is one room or the whole home.")
        if state.scope_intent is not ScopeIntent.UNDECIDED:
            if state.scope_intent is ScopeIntent.WHOLE_HOME:
                scope_line = "the WHOLE HOME"
            elif state.scope_intent is ScopeIntent.SELECTED_ROOMS and state.scope_rooms:
                scope_line = "these rooms only: " + ", ".join(state.scope_rooms)
            elif state.scope_intent is ScopeIntent.SELECTED_ROOMS:
                scope_line = "a few specific rooms (they have not named them all yet)"
            else:
                scope_line = "THIS ROOM only"
            lines.append(
                f"- Project scope, in the homeowner's own words: {scope_line}. Keep "
                "guidance at that level and do not widen it."
            )
        # (The contact and quote-offer directives below apply whatever the
        # scope is -- review of Sep 10 caught them nested under the scope
        # block by an indentation slip.)
        needs_contact = not (state.has_identity or state.slots.contact_captured)
        if needs_contact and g.can_offer_quote_request:
            lines.append(
                "- No account is linked to this conversation, so quotes need a "
                "way back to them: at the natural moment in quote gathering "
                "(usually with or right after the address), ask for an email "
                "or phone number — one of the two, once. If you have already "
                "asked and they declined, that ask is over: do not raise it "
                "again. Take the no in a few words and keep helping with the "
                "design work in the same reply — never end the turn on the "
                "acknowledgment alone."
            )
            # ponytail: the decline lives in the transcript, not in state, so
            # this bullet fires every turn and the model reads its own history
            # to know which half applies. If journals show a re-ask after a
            # decline, add a contactDeclined flag to flowCapture and drop the
            # ask half of this directive outright.
        if g.can_offer_quote_request and not g.can_ask_address:
            lines.append(
                "- A project is identified. You may gather scope options, "
                "material preferences, and offer decision help as natural "
                "conversation. Mention the option of getting real provider "
                "quotes AT MOST ONCE in the whole conversation, and only "
                "after the homeowner asks about cost, providers, or next "
                "steps — or clearly says they want the work done. If they "
                "don't take it up, drop it entirely until they raise it."
            )

    if state.slots.first_name:
        lines.append(
            f"- The homeowner's first name is {state.slots.first_name}; use it "
            "naturally, sparingly. If they correct it or say it was a typo, "
            "use the corrected spelling from that moment on AND record it in "
            "flowCapture.firstName — that is the only way the correction sticks."
        )

    lines.extend(_memory_directives(state))

    if state.ui_not_visible_claims:
        # You cannot see their screen. Insisting an element is there,
        # claiming to re-send it, or diagnosing their app are all claims
        # about something you have no access to.
        lines.append(
            "- They say they cannot see something on their screen. You have NO "
            "view of their app, so do not insist it is there, do not say you "
            "are attaching or re-sending it, and do not diagnose their app or "
            "tell them it is broken — you cannot know any of that. Take their "
            "word, tell them what you have captured is safely recorded, and "
            "carry on with what you CAN do in this conversation."
        )

    if price_guidance is not None:
        if "from recent web sources" in (price_guidance.basis or ""):
            # The range was just looked up. Without this the model disclaimed
            # the capability it had already used -- "I can't search the web
            # for real quotes, but I can give you a rough ballpark" attached
            # to a web-researched Palo Alto band (Sep 16). Saying you cannot
            # do the thing you just did is its own kind of wrong.
            lines.append(
                "- This range is not from a table: it is current pricing for "
                "their area that you looked up just now, and you may say so "
                "plainly. Do NOT tell them you cannot look prices up or "
                "search the web. What you cannot do is produce a real QUOTE "
                "that way: a quote is priced by a provider who has seen the "
                "space, and it comes back through the request."
            )
        # Not when real quotes are also going out this turn: those ARE quotes,
        # and telling the model to call them a ballpark undersells them.
        if price_asked and not quotes_to_present:
            lines.append(
                "- Call what you are about to give them a rough ballpark, never "
                "a quote. A quote is a real price for their home and it only "
                "comes back after you send their request out and a provider "
                "prices it. Say which one this is in a clause, not a lecture."
            )
        if not price_asked and not price_guidance.options:
            # The band exists because we know the trade, not because this
            # message looked like a price question to a regex. The model is
            # the one that can tell, so give it the permission and the bound
            # and let it judge -- including the phrasings no pattern catches
            # ("what's that gonna run me", "can you look for quotes online").
            lines.append(
                "- IF the homeowner is asking what something costs, in any "
                "words at all -- a ballpark, what it runs, what they are "
                f"looking at, whether you can look prices up -- answer with "
                f"${price_guidance.lowUsd:,.0f}–${price_guidance.highUsd:,.0f} and NO other "
                "figure, said in your reply because nothing is shown on their "
                "screen. Never answer a cost question with a flat refusal: you "
                "CAN give a rough range, and a wide honest one is the answer. "
                "Stress it is wide on purpose and that a real local provider "
                "has to see the space for a true price. Call it a rough "
                "ballpark and never a quote -- a quote is a real price for "
                "their home and only comes back after you send their request "
                "out and a provider prices it. If they are NOT asking about "
                "money, do not bring it up."
            )
        elif price_guidance.options:
            per_option = "; ".join(
                f"{o.label}: ${o.lowUsd:,.0f}-${o.highUsd:,.0f}" for o in price_guidance.options
            )
            lines.append(
                "- The homeowner asked to compare costs. These are the ONLY "
                f"figures you may state, and you state them IN YOUR REPLY ({per_option}). "
                "Nothing is rendered on their screen, so do not point at a card "
                "or tell them to look anywhere — say the numbers. Walk through "
                "the comparison in words: which lands cheaper and why the ranges "
                "overlap or don't, while stressing they are wide on purpose and "
                "real providers give true prices. No other numbers."
            )
        else:
            lines.append(
                "- The homeowner asked about cost. State this range IN YOUR "
                f"REPLY: ${price_guidance.lowUsd:,.0f}–${price_guidance.highUsd:,.0f}. Nothing "
                "is shown on their screen alongside this message, so do not "
                "mention a card or tell them to look at one — if you do not say "
                "the numbers, they never see them. Stress the range is wide on "
                "purpose and that a real local provider has to look at the space "
                "for a true price. State no other numbers."
            )
            if "nothing measured yet" in price_guidance.basis:
                lines.append(
                    "- That range is wide because nothing about their home has been "
                    "measured yet. Say so plainly, then offer ONE way to narrow it — "
                    "either they tell you the rough size (square footage or number of "
                    "rooms) or they scan the space. Offer, do not insist."
                )
    else:
        lines.append(
            "- Never state prices, cost ranges, or estimates yourself. If cost "
            "comes up, explain that real local providers give the actual "
            "numbers and offer to start that when they're ready."
        )

    if local_context is not None and (local_context.styleNotes or local_context.practicalNotes):
        notes = "; ".join(local_context.styleNotes + local_context.practicalNotes)
        lines.append(
            f"- You have real local insight for {local_context.regionLabel} (from "
            f"current web sources): {notes}. Weave ONE relevant point into your "
            "reply naturally, as local knowledge — don't dump the list or cite "
            "sources."
        )

    if local_providers is not None and local_providers.providers:
        listing = "; ".join(f"{p.name} ({p.note})" for p in local_providers.providers)
        lines.append(
            "- BETA local providers found online for their area: " + listing + ". "
            "You may share these as a helpful starting point IF the homeowner "
            "wants provider options now, but you MUST say plainly they were "
            "found online and are NOT TakeShape-vetted partners — they should "
            "check reviews and credentials themselves. Never imply TakeShape "
            "endorses them. Prefer steering to a TakeShape quote request when "
            "one is possible."
        )

    if quotes_to_present:
        # Some returned entries are market ESTIMATES attached to real
        # businesses (demo), not actual bids — flagged explicitly with
        # isEstimate. Real ops bids whose notes merely contain the word
        # "estimate" are still real bids.
        is_estimate = any(bool(q.get("isEstimate")) for q in quotes_to_present)
        # The privacy reassurance is only true when there is an address to
        # keep private. Said to someone who never gave one, it is the agent
        # inventing a detail about them (Sep 16).
        address_line = (
            " Remind them their address is shared only with the provider they "
            "pick."
            if state.slots.address_captured
            else " They have given no street address, so do not tell them "
            "where their address goes; the provider they pick gets what they "
            "did share."
        )
        if 0 < new_quote_count < len(quotes_to_present):
            lines.append(
                f"- {new_quote_count} new quote(s) just arrived — the LAST "
                f"{new_quote_count} in the quotes JSON below. Lead with the new "
                "arrivals, then compare across everything received so far."
            )
        if is_estimate:
            lines.append(
                "- Results are back, but these are ESTIMATED market ranges for "
                "the scope near the homeowner, shown next to real local "
                "businesses — NOT actual bids those businesses gave. Present "
                "them exactly that way: 'here's roughly what this runs in your "
                "area, and a couple of real local painters you could go "
                "through.' Do not say a business 'quoted' a price. Remind them "
                "a real quote comes once a provider sees the space."
                + address_line
                + " Data: "
                + json.dumps(quotes_to_present, ensure_ascii=True)
            )
        else:
            lines.append(
                "- QUOTES ARE BACK. Present these returned quotes warmly and "
                "compare them for the homeowner (price, what's included, timing)."
                + address_line
                + " Quotes JSON: " + json.dumps(quotes_to_present, ensure_ascii=True)
            )

    lines.append(
        "- In flowCapture, record ONLY values the homeowner explicitly stated "
        "in their latest message (name, zip, project type, scope options, "
        "materials, address, contact). Use null / empty when not stated. "
        "Never guess or carry values over. projectType is ONE service in "
        "plain words — whatever the homeowner is actually pursuing (window "
        "treatments, closet build-out...), NOT limited to the known service "
        "catalog. If they mention several, use the one the conversation is "
        "focused on. scopeOptions means variations of "
        "the identified project's work scope, in that trade's terms (walls "
        "only vs walls and trim for painting, reupholster vs replace for a "
        "sofa) — never rooms or areas the homeowner wants to add to their "
        "home model. When they are comparing product/material options (solar "
        "shades vs drapes, hardwood vs laminate), record EACH option as its "
        "own materials entry. scopeIntent is set ONLY when the homeowner "
        "states, in this message, whether the project is one room "
        "(single_room), a few named rooms (selected_rooms — list them in "
        "scopeRooms), or the whole home (whole_home). Never infer it from "
        "how many spaces you can see, and never from them merely mentioning "
        "or asking about a room ('what about the garage', 'let's start with "
        "the kitchen' are NOT scope statements); null otherwise."
    )
    lines.append(
        # Client feedback (Noah, Aug 25): the agent must feel like design
        # conversation, not a sales funnel.
        "- TONE: you are a relaxed, knowledgeable design friend, never a "
        "salesperson. State a fact like the room's size at most once in the "
        "whole conversation — repeating measurements or re-summarizing the "
        "scope every turn sounds robotic. Vary your acknowledgments; don't "
        "start consecutive replies the same way."
    )
    if state.quote_request is not None and not quotes_to_present:
        # SOW §2 step 9: the agent sets the expectations, and it knows its
        # own submission happened.
        lines.append(
            "- The homeowner's quote request HAS been submitted"
            + (
                " and has now LANDED -- delivery is confirmed, so if they ask "
                "whether it actually went anywhere, say yes, it is in front of "
                "providers, in your own voice and without naming anyone behind "
                "you"
                if lead_delivered
                else ""
            )
            + ". If they ask "
            "about it, confirm plainly that it's in: YOU are getting it in "
            "front of local providers and YOU will bring their real quotes "
            "back here as soon as they come in. Keep yourself as the subject "
            "of those sentences -- the homeowner is talking to you, so do "
            "NOT make the people working behind you the story (no 'a person "
            "on my team reviews every request'). You never claim to have "
            "done the pricing yourself. NEVER promise a specific turnaround "
            "time (no hour or day figures). Final prices land within about "
            "10% of the quote once a provider has seen the space. Don't "
            "re-collect project details unless they want to change "
            "something."
        )
        if selected_quote:
            # The choice is made in the app (SOW §12): the address went to
            # that provider and nobody else. Without this line the agent
            # answered "I can't route it to a specific company" to a choice
            # the server had already recorded (Sep 15).
            provider = selected_quote.get("providerName")
            lines.append(
                "- The homeowner has CHOSEN a quote: "
                f"{provider}"
                + (f" at {selected_quote['price']}" if selected_quote.get("price") else "")
                + ". YOU own what happens next; say it in your own voice, never "
                "as a hand-off to a team. The facts: their address has gone to "
                f"{provider} and to no one else; {provider} reaches out using the "
                "contact details on the request, and you bring their scheduling "
                "here as it comes; nothing is owed until they have agreed a start "
                f"date and a written quote with {provider} -- if a deposit is "
                "asked for, it is typically a portion of the quoted price and "
                "belongs on that written agreement; before the crew arrives they "
                "should clear the work area, move fragile or valuable things, "
                "sort out parking and access, and keep pets out of the way. Answer "
                "questions about any of this plainly. Never promise a date, a "
                "time window, or an amount you were not given; never say you "
                "cannot route or pass along the choice; never ask them to pick "
                "again, and do not re-present the quotes as if the choice were "
                "still open."
            )
        if declined_quotes:
            # Declining is a real answer (Quintin, Sep 17). Without this the
            # agent either ignored the decline or apologised for it; what it
            # should do is treat it as a decision it made happen and say
            # what follows from it.
            names = ", ".join(d["providerName"] for d in declined_quotes)
            lines.append(
                f"- The homeowner has DECLINED: {names}. That is settled, so do "
                "not talk them back into it or ask them to reconsider. Their "
                "address did NOT go to anyone they declined. "
                + (
                    "Say plainly that you have passed the reason on and are "
                    "looking for another quote, and ask anything you need in "
                    "order to get a better fit."
                    if not selected_quote
                    else "Leave it there and stay on the quote they chose."
                )
                + " Reasons they gave, verbatim: "
                + json.dumps(
                    [
                        {"provider": d["providerName"], "reason": d.get("note")}
                        for d in declined_quotes
                    ],
                    ensure_ascii=True,
                )
            )
        if quotes_on_file:
            lines.append(
                "- WHAT IS ON FILE about each provider who quoted (relationship, "
                "rating, reviews, website), for when they ask whether a company "
                "is any good: 'partner' is a company I work with, 'quoted' has "
                "done work through me before, 'prospect' was found and checked "
                "by my team. Say what is on file plainly and offer the website or "
                "Google profile; where nothing is on file, say they came through "
                "my team's vetting and offer to get references. Never say you "
                "have no access to their reviews. Providers JSON: "
                + json.dumps(quotes_on_file, ensure_ascii=True)
            )
    elif state.client_flow_aware:
        base = (
            "- NEVER claim a quote request has been submitted, sent, or is on "
            "its way to providers — sending only happens when the homeowner taps "
            "the Confirm control on the request card in the app, and you will be "
            "told when that happened."
        )
        if state.confirm_claims >= 1:
            # Once they say they did it, stop. The nag was still landing at a
            # threshold of two (Sep 4 battery): "If you only confirmed here in
            # chat, use the Confirm button" to a homeowner who just said he
            # had is the whole complaint.
            lines.append(
                base + " They have now said more than once that they confirmed, "
                "and you have already explained the control. Do NOT explain it "
                "again and do not contradict them: acknowledge it, say you'll "
                "flag it the moment it comes through on your side, and move the "
                "conversation forward."
            )
        else:
            lines.append(
                base + " If they type a confirmation in chat, thank them and "
                "point them to the Confirm button on the card — once, warmly."
            )
    else:
        # Legacy app builds submit through their own card control on a path
        # this conversation cannot observe: never promise confirmation
        # mechanics or claim knowledge of a submission either way.
        lines.append(
            "- NEVER claim a quote request has been submitted or sent — you "
            "cannot see that from here. If the homeowner wants provider "
            "quotes, point them to the request card shown in the app; my "
            "team takes it from there."
        )
    missing_for_submit = _engine.missing_submission_slots(state)
    if missing_for_submit:
        readable = {
            "projectType": "the project",
            "scopeOptions": "what the work itself covers (parts of the job, not which rooms)",
            "zip": "their zip", "contact": "an email or phone",
        }
        lines.append(
            "- The request card is NOT shown to the homeowner yet — still "
            "missing: " + ", ".join(readable.get(m, m) for m in missing_for_submit)
            + ". Do not present, describe, or promise a request card this "
            "turn; keep the conversation going and gather what's missing at "
            "natural moments."
        )
    elif state.quote_request is None and not state.request_accepted:
        # Everything is captured but they have not agreed to the request yet.
        # The card is theirs to ask for: it appearing unprompted reads as the
        # agent sending their details on its own (Sep 12 feedback).
        if g.can_offer_request_package:
            lines.append(
                "- Everything a quote request needs is captured, but the "
                "homeowner has NOT agreed to one yet. Ask them — plainly and "
                "once — whether you should put what you've discussed together "
                "as a request for my team to price, and then wait "
                "for their answer. Do NOT present, describe, or promise a "
                "request card this turn: nothing goes anywhere until they say "
                "yes."
            )
        else:
            lines.append(
                "- You have already asked whether to put a request together "
                "and they have not taken you up on it. Do not ask again and do "
                "not present a request card; keep helping with the design, and "
                "if they later ask for quotes, treat that as their yes."
            )
    elif state.quote_request is None:
        if state.client_flow_aware:
            lines.append(
                "- Everything a quote request needs is captured. When you include "
                "a quoteDraft in your reply, the app displays it as a card "
                "IMMEDIATELY in this same message with a Confirm button — so "
                "present it ('here is the request, look it over and confirm when "
                "ready'), never ask whether you should draft it."
            )
        else:
            lines.append(
                "- Everything a quote request needs is captured. When you "
                "include a quoteDraft in your reply, the app displays it as a "
                "card in this same message — present it plainly ('here is the "
                "request, look it over and send it when ready')."
            )
    lines.append(
        "- ANSWER FIRST: when the homeowner asks a question or requests "
        "something, address THAT fully before any next-step or "
        "information ask. Never deflect a request by steering back to the "
        "quote process — if you can't do what they asked, say so plainly "
        "and offer the nearest thing you can do."
    )
    if guard is not None and guard.steered:
        # A question we must not answer authoritatively. The model still
        # replies in its own voice — the directive only takes away the
        # certainty (issue #57).
        lines.append(input_guard.STEER_DIRECTIVES[guard.category])
    lines.append(
        # Constrained JSON decoding occasionally corrupts escape sequences
        # (a quoted word can lose its first letter). Plain prose avoids the
        # escapes entirely.
        "- Write assistantMessage as plain prose: no double quotation marks, "
        "no backslashes, no tabs or special characters. Use apostrophes and "
        "commas instead of quoting words."
    )
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Slot updates
# --------------------------------------------------------------------------
def _dedupe_options(values: list[str]) -> list[str]:
    """Drop near-duplicates ('heavy drapes' vs 'heavier drapes'): exact-lower
    matches and containment collapse to the first-seen entry."""
    kept: list[str] = []
    for value in values:
        lowered = value.lower().strip()
        if any(
            lowered == k.lower() or lowered in k.lower() or k.lower() in lowered
            for k in kept
        ):
            continue
        kept.append(value)
    return kept


def _apply_capture(
    state: FlowState,
    capture: dict[str, Any] | None,
    user_message: str,
    quote_draft: dict[str, Any] | None = None,
    home_index=None,
) -> dict[str, Any]:
    """Validate and apply the model's flowCapture; returns the delta journal."""
    delta: dict[str, Any] = {}
    capture = capture or {}
    slots = state.slots

    first_name = _clean_str(capture.get("firstName"), max_len=60)
    if first_name and not slots.first_name:
        slots.first_name = first_name
        delta["firstName"] = first_name
    elif _replaces_value(
        slots.first_name, first_name, user_message, frames=_NAME_CORRECTION
    ):
        # "Chance I mean - typo" left the ledger saying Chane, the agent kept
        # using it, and then denied the correction had happened (Sep 16,
        # thread phone-quintin--mu3ctf32).
        logger.info("Correcting first name '%s' -> '%s'", slots.first_name, first_name)
        slots.first_name = first_name
        delta["firstName"] = first_name

    zip_value = _clean_str(capture.get("zip"), max_len=10)
    if not (zip_value and _ZIP_VALID.match(zip_value)):
        zip_value = None
    if zip_value is None and slots.zip is None:
        # Deterministic backstop: a volunteered 5-digit zip in the message.
        match = _ZIP_IN_MESSAGE.search(user_message or "")
        if match and _ZIP_CONTEXT.search(user_message or ""):
            zip_value = match.group(1)
    if zip_value and not slots.zip:
        slots.zip = zip_value
        delta["zip"] = zip_value
        state.mark_complete(FlowStep.ZIP_COLLECTION)
    elif _replaces_value(slots.zip, zip_value, user_message):
        # A wrong zip is invisible and steers everything downstream of it:
        # local research, the regional naming, provider matching, the lead.
        logger.info("Correcting zip '%s' -> '%s'", slots.zip, zip_value)
        slots.zip = zip_value
        delta["zip"] = zip_value

    project_type = _clean_str(capture.get("projectType"), max_len=80)
    if project_type:
        # One service per quote request; a comma-joined capture keeps its
        # first (focus) service.
        project_type = project_type.split(",")[0].strip()
        # Normalize onto the service catalog: the model captures the
        # homeowner's words ("kitchen repaint", "bedroom refresh"), and
        # provider matching keys off the catalog, so an unnormalized value
        # matches no partner. When the captured phrase maps to nothing, try
        # what the homeowner actually typed — "paint, new bed" is a painting
        # job even when the model summarised it as "bedroom refresh".
        project_type = (
            normalize_service_type(project_type)
            or detect_service_type(user_message or "", strong_only=True)
            or project_type
        )
    if project_type and not slots.project_type:
        slots.project_type = project_type
        delta["projectType"] = project_type
    elif slots.project_type and normalize_service_type(slots.project_type) is None:
        # An early vague capture ("bathroom remodel") used to be permanent,
        # because the slot is only written when empty — so a later "repaint
        # the walls and trim" never fixed it, and the lead matched no
        # partner (observed live, Sep 4). A value that maps to a real trade
        # upgrades one that maps to nothing.
        upgrade = normalize_service_type(project_type) or detect_service_type(
            user_message or "", strong_only=True
        )
        if upgrade:
            logger.info(
                "Upgrading project type '%s' -> '%s'", slots.project_type, upgrade
            )
            slots.project_type = upgrade
            delta["projectType"] = upgrade

    for key, attr in (("scopeOptions", "scope_options"), ("materials", "materials")):
        values = capture.get(key)
        if isinstance(values, list):
            cleaned = [v.strip() for v in values if isinstance(v, str) and v.strip()][:8]
            existing = getattr(slots, attr)
            merged = _dedupe_options([*existing, *cleaned])
            added = [v for v in merged if v not in existing]
            if added or len(merged) != len(existing):
                setattr(slots, attr, merged)
                if added:
                    delta[key] = added

    scope_delta = _apply_scope_capture(state, capture, user_message, home_index)
    delta.update(scope_delta)

    address = _clean_str(capture.get("address"), max_len=240)
    if address and len(address) >= 8:
        if not slots.address:
            slots.address = address
            delta["address"] = address
            state.mark_complete(FlowStep.ADDRESS_COLLECTION)
        elif _replaces_value(slots.address, address, user_message):
            logger.info("Correcting street address")
            slots.address = address
            delta["address"] = address

    email = _clean_str(capture.get("contactEmail"), max_len=120)
    if email and "@" in email:
        # A mistyped email is how a lead goes unreachable, and it is the one
        # slot the homeowner can't see to check.
        if not slots.contact_email:
            slots.contact_email = email
            delta["contactEmail"] = email
        elif _replaces_value(slots.contact_email, email, user_message):
            logger.info("Correcting contact email")
            slots.contact_email = email
            delta["contactEmail"] = email
    phone = _clean_str(capture.get("contactPhone"), max_len=40)
    if phone:
        if not slots.contact_phone:
            slots.contact_phone = phone
            delta["contactPhone"] = phone
        elif _replaces_value(
            slots.contact_phone, phone, user_message, digits_only=True
        ):
            logger.info("Correcting contact phone")
            slots.contact_phone = phone
            delta["contactPhone"] = phone

    # Safety net: the model sometimes leaves projectType/scope empty for
    # services outside the known catalog while still producing a coherent
    # draft — adopt the draft's own fields so the flow can't dead-end.
    if quote_draft:
        if slots.project_type is None:
            draft_service = _clean_str(quote_draft.get("serviceType"), max_len=80)
            if draft_service:
                slots.project_type = draft_service.split(",")[0].strip()
                delta["projectType"] = slots.project_type
        if not slots.scope_options:
            notes = [
                n.strip()
                for n in quote_draft.get("scopeNotes", [])
                if isinstance(n, str) and n.strip()
            ][:4]
            if notes:
                slots.scope_options = _dedupe_options(notes)
                delta["scopeOptions"] = slots.scope_options

    if slots.project_type and slots.scope_options:
        state.mark_complete(FlowStep.QUOTE_GATHERING)
    return delta


# Deterministic backstop for the scope capture: the homeowner's own words,
# only the unambiguous phrasings. The model's flowCapture comes first; this
# catches a plain statement the model left null. Nothing here looks at the
# scan -- scope is never inferred from the number of areas captured.
_SCOPE_WHOLE_HOME = re.compile(
    r"(?i)\b(?:the\s+)?(?:whole|entire)\s+(?:house|home|place)\b|\bevery\s+room\b|\ball\s+(?:the|of\s+the)\s+rooms\b"
)
_SCOPE_SINGLE_ROOM = re.compile(
    r"(?i)\b(?:just|only)\s+(?:this|the|this\s+one|that)\s+(?:room|space)\b"
    r"|\b(?:this|the)\s+(?:room|space)\s+(?:only|is\s+(?:the\s+)?(?:whole|entire|only)\s+project)\b"
    r"|\bone\s+room\s+(?:only|for\s+now)\b|\bjust\s+(?:the\s+)?one\s+room\b"
)
_SCOPE_SELECTED = re.compile(
    r"(?i)\b(?:a\s+few|a\s+couple\s+of|two|three|several|these\s+two|those\s+two)\s+rooms\b"
    r"|\b(?:this\s+room\s+and\s+the|this\s+one\s+and\s+the)\s+\w+"
)
_SCOPE_VALUES = {s.value for s in ScopeIntent if s is not ScopeIntent.UNDECIDED}


_SCOPE_NEGATED_WHOLE = re.compile(
    r"(?i)\b(?:not|isn't|isnt|don't|dont|won't|wont|never|rather\s+than|instead\s+of)\b[^.!?,]{0,24}?"
    r"\b(?:whole|entire)\s+(?:house|home|place)\b"
    r"|\bnot\b[^.!?,]{0,24}?\bevery\s+room\b"
)


def _detect_scope_intent(message: str | None) -> ScopeIntent | None:
    """Narrowest explicit statement first, and a negated whole-home
    ("not the whole house") never reads as whole_home (review, Sep 10)."""
    text = message or ""
    if _SCOPE_SINGLE_ROOM.search(text):
        return ScopeIntent.SINGLE_ROOM
    if _SCOPE_SELECTED.search(text):
        return ScopeIntent.SELECTED_ROOMS
    if _SCOPE_WHOLE_HOME.search(text) and not _SCOPE_NEGATED_WHOLE.search(text) and "?" not in text:
        return ScopeIntent.WHOLE_HOME
    return None


_DEICTIC_ROOM = re.compile(r"(?i)^(?:the\s+|this\s+)?(?:this|that|current)?\s*(?:room|space|one|area|here)$|^this\s+(?:room|one|space|area)$")


def _apply_scope_capture(
    state: FlowState, capture: dict[str, Any], user_message: str, home_index=None
) -> dict[str, Any]:
    """Scope intent from the homeowner's words: the model's explicit capture
    first, the phrase backstop second, never the scan. A later explicit
    statement replaces an earlier one (people change their minds). "This
    room" resolves to the active room's name when a home index is loaded,
    so the package and the directives name a real room (review, Sep 10)."""
    delta: dict[str, Any] = {}
    stated = _clean_str(capture.get("scopeIntent"), max_len=20)
    stated = stated.lower() if stated else None
    chosen: ScopeIntent | None = ScopeIntent(stated) if stated in _SCOPE_VALUES else None
    if chosen is None:
        chosen = _detect_scope_intent(user_message)
    rooms_raw = capture.get("scopeRooms")
    rooms = (
        [_clean_str(r, max_len=60) for r in rooms_raw if isinstance(r, str) and r.strip()][:12]
        if isinstance(rooms_raw, list) else []
    )
    active = None
    if home_index is not None and state.active_room_key:
        room = home_index.by_key(state.active_room_key)
        active = room.display_name if room is not None else None
    resolved: list[str] = []
    for r in rooms:
        if not r:
            continue
        if _DEICTIC_ROOM.match(r.strip()):
            if active:
                resolved.append(active)
            continue   # a bare "this room" with nothing to resolve to says nothing
        resolved.append(r)
    rooms = resolved
    if chosen is not None and chosen is not state.scope_intent:
        state.scope_intent = chosen
        delta["scopeIntent"] = chosen.value
        state.mark_complete(FlowStep.DESIGN_CONVERSATION)
        # A re-statement replaces the room list too: "a couple of rooms, the
        # kitchen and the bathroom, nothing else" must not keep a room named
        # under an earlier, different scope (seen live, Sep 10).
        if state.scope_rooms:
            delta["scopeRoomsCleared"] = list(state.scope_rooms)
            state.scope_rooms = []
    if rooms and state.scope_intent in (ScopeIntent.SELECTED_ROOMS, ScopeIntent.SINGLE_ROOM):
        merged = _dedupe_options([*state.scope_rooms, *rooms])[:12]
        added = [r for r in merged if r not in state.scope_rooms]
        if added:
            state.scope_rooms = merged
            delta["scopeRooms"] = added
    if state.scope_intent is ScopeIntent.WHOLE_HOME and state.scope_rooms:
        state.scope_rooms = []
    return delta


def _clean_str(value: Any, *, max_len: int) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned[:max_len] if cleaned else None


# --------------------------------------------------------------------------
# Enforcement wrapper
# --------------------------------------------------------------------------
_CONTROL_CHARS = re.compile(r"[\f\v\r\x00-\x08\x0e-\x1f]")


def _normalize_model_text(text: str) -> str:
    """Strip decoding artifacts: control characters from corrupted JSON
    escapes, and lone mid-sentence newlines (paragraph breaks survive)."""
    cleaned = _CONTROL_CHARS.sub("", text)
    cleaned = re.sub(r"(?<!\n)\n(?!\n)", " ", cleaned)
    return re.sub(r" {2,}", " ", cleaned).strip()


def _enforcement_text(response: HomeAIChatResponse) -> str:
    """Check the model's RAW text alongside the polished one: the homeowner-
    voice sanitizer rewrites words like 'capture' → 'record' BEFORE we get
    here, which would otherwise launder a scan suggestion past the patterns
    tuned for the model's own vocabulary."""
    raw = getattr(response, "_raw_message", "") or ""
    final = response.message.content or ""
    return f"{raw}\n{final}" if raw and raw != final else final


def _deadline_response(request: HomeAIChatRequest) -> HomeAIChatResponse:
    """A valid, coherent turn for a homeowner whose request outran the
    deadline — the app keeps working, the conversation stays intact, and
    nothing is claimed that didn't happen."""
    response = HomeAIChatResponse(
        threadId=request.threadId or "",
        message=HomeAIChatMessage(role="assistant", content=_SAFE_TIMEOUT_COPY),
        state=HomeAIConversationState(intent="exploring"),
        model=settings.anthropic_model or settings.openai_model or "unknown",
        provider=settings.ai_provider,
        usedFallback=True,
    )
    response._raw_message = _SAFE_TIMEOUT_COPY
    return response


def _guard_response(
    request: HomeAIChatRequest, verdict: input_guard.GuardVerdict
) -> HomeAIChatResponse:
    """A blocked message's reply, built without calling the model at all.

    Same shape as `_deadline_response` on purpose: the caller binds it to the
    same names the generated response would have used, so the whole tail of
    the turn (capture, gates, wire, persistence, journal) runs unchanged.

    `usedFallback` stays False — that flag means the model failed to answer,
    and a refusal is a correct answer, not a degraded one."""
    response = HomeAIChatResponse(
        threadId=request.threadId or "",
        message=HomeAIChatMessage(
            role="assistant", content=GUARD_COPY[verdict.category]
        ),
        state=HomeAIConversationState(intent="exploring"),
        model="input-guard",
        provider="local",
    )
    response._raw_message = response.message.content
    return response


async def _generate_enforced(
    request: HomeAIChatRequest,
    directives: str,
    gates: GateDecision,
    *,
    max_images_override: int | None = None,
    card_already_shown: bool = True,
) -> tuple[HomeAIChatResponse, list[dict[str, Any]], bool]:
    """Generate, validate against gates, regenerate once, then safe copy.
    Returns ``(response, suppressed_drafts, substituted)`` — ``substituted``
    is True when the reply the homeowner sees is deterministic safe copy
    rather than a model turn.

    The whole thing runs under the turn deadline: a generate + regenerate
    pair can otherwise stack two provider timeouts plus durable writes,
    which is a phone stuck on a spinner."""
    if settings.turn_deadline_enabled:
        try:
            return await asyncio.wait_for(
                _generate_enforced_inner(
                    request, directives, gates,
                    max_images_override=max_images_override,
                    card_already_shown=card_already_shown,
                ),
                timeout=settings.turn_deadline_seconds,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "Turn deadline (%.0fs) exceeded; serving safe copy thread=%s",
                settings.turn_deadline_seconds,
                request.threadId,
            )
            return _deadline_response(request), [], True
    return await _generate_enforced_inner(
        request, directives, gates, max_images_override=max_images_override,
        card_already_shown=card_already_shown,
    )


async def _generate_enforced_inner(
    request: HomeAIChatRequest,
    directives: str,
    gates: GateDecision,
    *,
    max_images_override: int | None = None,
    card_already_shown: bool = True,
) -> tuple[HomeAIChatResponse, list[dict[str, Any]], bool]:
    suppressed: list[dict[str, Any]] = []
    response = await generate_home_ai_response(
        request, flow_directives=directives, max_images_override=max_images_override
    )
    response.message.content = _normalize_model_text(response.message.content)
    violations = enforcement.check(
        _enforcement_text(response), gates,
        # A draft only becomes a card if the gate lets it through; the drop
        # at the end of the turn is what the homeowner's screen obeys.
        card_on_screen=(
            card_already_shown
            or (response.quoteDraft is not None and gates.can_present_request_card)
        ),
        address_given_this_turn=_capture_has_address(response),
    )
    if not violations:
        return response, suppressed, False
    suppressed.append(
        {"text": response.message.content, "violations": [v.rule for v in violations]}
    )
    logger.info(
        "Flow enforcement tripped (%s); regenerating turn thread=%s",
        ",".join(v.rule for v in violations),
        response.threadId,
    )
    retry_directives = (
        directives + "\nCOMPLIANCE CORRECTION (your previous draft violated "
        "these rules — rewrite without the violation): "
        + enforcement.correction_instruction(violations)
    )
    response = await generate_home_ai_response(
        request, flow_directives=retry_directives, max_images_override=max_images_override
    )
    response.message.content = _normalize_model_text(response.message.content)
    violations = enforcement.check(
        _enforcement_text(response), gates,
        # A draft only becomes a card if the gate lets it through; the drop
        # at the end of the turn is what the homeowner's screen obeys.
        card_on_screen=(
            card_already_shown
            or (response.quoteDraft is not None and gates.can_present_request_card)
        ),
        address_given_this_turn=_capture_has_address(response),
    )
    if violations:
        suppressed.append(
            {"text": response.message.content, "violations": [v.rule for v in violations]}
        )
        logger.warning(
            "Flow enforcement tripped twice; substituting safe copy thread=%s", response.threadId
        )
        if any(v.rule == "scan_suggestion_while_processing" for v in violations):
            safe = SAFE_SCAN_WAIT_COPY
        elif any(v.rule == "scan_suggestion_outside_scope" for v in violations):
            safe = SAFE_SCOPE_COPY
        elif any(v.rule == "request_card_claimed_but_absent" for v in violations):
            safe = _SAFE_NO_CARD_COPY
        elif any(v.rule == "unauthorized_price_figure" for v in violations):
            safe = _SAFE_NO_PRICE_COPY
        elif any(v.rule == "human_impersonation" for v in violations):
            safe = SAFE_IMPERSONATION_COPY
        else:
            safe = _SAFE_GENERIC_COPY
        response.message.content = safe
        return response, suppressed, True
    return response, suppressed, False


# --------------------------------------------------------------------------
# Ask accounting — which scripted asks actually appeared in the final text
# --------------------------------------------------------------------------
_NAME_ASK = re.compile(r"(?i)\b(your (first )?name|what should i call you|may i ask who)\b")
_ZIP_MENTION = re.compile(r"(?i)\b(zip|postal)\b")
_ADDRESS_MENTION = re.compile(r"(?i)\baddress\b")
# The step-9 offer actually made: "want me to put this together for the
# TakeShape team?" in any of its phrasings. Recorded so the offer budget is
# spent by an offer the homeowner really saw.
_REQUEST_OFFER = re.compile(
    r"(?i)(put (?:this|it|that|everything) together"
    # Sep 15 (#77): "put together the living room request", "put the request
    # together", "draft the request now" -- none of these counted, so no yes
    # to them ever opened the card.
    r"|put together (?:a|the|your|that)\b[^.?!]{0,40}?\brequest"
    r"|put (?:a|the|your) (?:quote )?request together"
    r"|(?:draft|write up|create|generate|prepare) (?:a|the|your|that)\b[^.?!]{0,40}?\brequest"
    r"|package (?:this|it|that|everything)"
    r"|(?:send|pass|hand) (?:this|it|that|everything) (?:on |over |off )?to"
    r"|(?:a|the|your) (?:quote )?request (?:over )?to"
    r"|write (?:this|it|that) up"
    r"|(?:takeshape|my) team)"
)
# The scope ask, in any of its framings: one room / a few rooms / the whole home.
_SCOPE_MENTION = re.compile(
    r"(?i)\b(whole\s+(?:home|house|place)|other\s+rooms|a\s+few\s+(?:other\s+)?rooms|"
    r"just\s+this\s+room|this\s+room\s+only|the\s+whole\s+project|one\s+room\s+or|"
    r"rest\s+of\s+(?:the|your)\s+(?:home|house)|which\s+rooms|bigger\s+(?:refresh|project))\b"
)


def _record_asks_and_wordings(
    state: FlowState, plan: TurnPlan, final_text: str, *, opening: bool, raw_text: str = ""
) -> list[str]:
    wording_ids: list[str] = []
    if (opening or plan.gates.can_ask_first_name) and _NAME_ASK.search(final_text):
        _engine.record_asks(state, asked_first_name=True)
        wording_ids.append(FIRST_NAME_WORDING.id)
    if plan.gates.can_ask_zip and plan.zip_wording_id and _ZIP_MENTION.search(final_text):
        _engine.record_asks(state, asked_zip=True)
        wording_ids.append(plan.zip_wording_id)
    if plan.gates.can_ask_address and _ADDRESS_MENTION.search(final_text):
        _engine.record_asks(state, asked_address=True)
        wording_ids.append(ADDRESS_WORDING.id)
    if plan.gates.can_offer_request_package and _REQUEST_OFFER.search(final_text):
        _engine.record_asks(state, offered_request=True)
        wording_ids.append("step9.request_offer_v1")
    elif (
        not state.request_accepted
        and state.quote_request is None
        and _REQUEST_OFFER.search(final_text)
    ):
        # An offer made before the gate opened ("I can put that together as a
        # request now", scope still missing) spends no budget, but a yes to it
        # still counts; the card follows once the slots are in (#77).
        # ponytail: any offer-shaped sentence arms the next yes, so a yes to a
        # different question in that same reply is read as acceptance. Require
        # the offer to be the reply's last question if journals show that.
        state.request_offer_at_turn = state.user_turns
    if plan.gates.can_ask_scope and plan.scope_wording_id and _SCOPE_MENTION.search(final_text):
        _engine.record_asks(state, asked_scope=True)
        wording_ids.append(plan.scope_wording_id)
    # A step-6 invitation actually made -- a scan suggestion or the one
    # generic offer -- spends the scope's extension budget. Only an
    # invitation counts: the opening's "your whole home mapped out" is a
    # description, and it must not use up the single offer a homeowner gets
    # after they narrow the scope (seen in the console, Sep 10).
    if plan.gates.can_prompt_additional_scan and state.scope_intent in (
        ScopeIntent.SINGLE_ROOM, ScopeIntent.SELECTED_ROOMS
    ):
        # Under selected_rooms an invitation to add a NAMED room is permitted
        # and unlimited; only the generic "anything else?" is budgeted.
        # Under single_room any invitation is that one offer.
        named_rooms_mode = plan.gates.extension_prompt_mode == EXTENSION_NAMED_ROOMS
        for text in (final_text, raw_text):
            if not text:
                continue
            offered = bool(enforcement._GENERIC_OFFER.search(text))
            if not named_rooms_mode:
                offered = offered or bool(
                    enforcement._SCAN_SUGGESTION.search(text) or enforcement._BROAD_EXTENSION.search(text)
                )
            if offered:
                _engine.record_asks(state, offered_extension=True)
                wording_ids.append("step6.extension.offer_v1")
                break
    return wording_ids


# --------------------------------------------------------------------------
# Price guidance
# --------------------------------------------------------------------------
def _window_count(state: FlowState, home_index, context: HomeAIContextPacket | None) -> int | None:
    """Windows the walk counted: the room in focus, else the whole home, else
    the context packet's rooms. None when nothing was counted."""
    if home_index is not None:
        if state.active_room_key:
            room = home_index.by_key(state.active_room_key)
            if room is not None:
                return int(room.window_count or 0) or None
        total = sum(int(r.window_count or 0) for r in home_index.rooms)
        return total or None
    rooms = (context.rooms if context else None) or []
    total = sum(int(r.get("windowCount") or 0) for r in rooms if isinstance(r, dict))
    return total or None


def _active_room_area_sqft(state: FlowState, home_index) -> tuple[float | None, str | None]:
    """Floor area of the room in focus, and what to call it.

    Whole-home scans made the unqualified total wrong rather than merely
    imprecise: every home on the demo is a whole walk-through, so "what would
    it cost to repaint the laundry room" priced all 1,373 sq ft of the house
    and quoted an 85 sq ft room at several thousand dollars. When the
    homeowner is standing in one room, that room is the job.
    """
    if home_index is None or not state.active_room_key:
        return None, None
    room = home_index.by_key(state.active_room_key)
    if room is None:
        return None, None
    measured = (room.measurements or {}).get("floor_sqft")
    if isinstance(measured, (int, float)) and measured > 0:
        return float(measured), room.display_name or None
    if room.area_sqft and room.area_sqft > 0:
        return float(room.area_sqft), room.display_name or None
    return None, None


async def _maybe_price_guidance(
    state: FlowState, request: HomeAIChatRequest, home_index=None
) -> tuple[PriceGuidance | None, bool]:
    """Returns (band, asked_now).

    The band is built whenever we know the trade, whether or not this message
    looks like a price question; `asked_now` is the regex's opinion that it
    does. That split exists because one regex was doing two jobs. Deciding
    whether to SPEND on a web search is a job a pattern does well: a miss just
    means the static table. Deciding whether the agent is ALLOWED to say a
    number is a job it does badly, and a miss there is "I can't give you a
    number myself" -- 19 times out of 19 in the Sep 16 battery, with people
    leaving over it. `_PRICE_ASK` carries five separate "this phrasing was
    missed in production" patches and could not have caught "can you look for
    quotes online now?" at any width, because `quote` is the flow's own word
    for the thing that is NOT a ballpark. So the model decides whether the
    homeowner is asking; the band bounds what it may say if they are, and
    `enforcement.allowed_price_range` is unchanged.
    """
    if not settings.agent_price_guidance_enabled:
        return None, False
    service = state.slots.project_type or detect_service_type(request.message)
    if not service:
        return None, False
    asked = user_asked_for_price(request.message)
    # The room in focus wins, then the whole scan, then a size the homeowner
    # volunteered ("about 1800 sq ft", "3 bedrooms"), then None, which yields
    # the deliberately wide typical-job band. No size is a normal state, not
    # a reason to refuse a ballpark.
    area, area_label = _active_room_area_sqft(state, home_index)
    if area is None:
        area = _total_floor_area_sqft(request.homeContext) or parse_size_hint(request.message)
    windows = _window_count(state, home_index, request.homeContext)

    # Pin the first card of the conversation: the range the homeowner saw
    # must never silently change on a later ask (observed jumping when a
    # research call succeeded after an earlier static fallback). A NEW size
    # is the one thing that legitimately re-prices — they asked us to narrow
    # it — so the pin is keyed on size as well as service.
    snapshot = state.price_guidance_snapshot
    if (
        snapshot
        and snapshot.get("service") == service
        and snapshot.get("areaSqft") == area
        and snapshot.get("areaLabel") == area_label
        and snapshot.get("windows") == windows
    ):
        return PriceGuidance.model_validate(snapshot["guidance"]), asked

    # Slots are captured from the model's output, i.e. AFTER this runs, so on
    # the first turn a zip the homeowner just typed is not in them yet — and
    # "what's painting run in 37212?" is the most natural way to ask. Read it
    # off this message too, the way the service type already is.
    zip_code = state.slots.zip
    if not zip_code:
        m = _ZIP_IN_MESSAGE.search(request.message or "")
        zip_code = m.group(1) if m else None
    # A count-priced trade never goes to the web: "installed cost per square
    # foot for window cleaning" is the question that produced $5,300 (#95).
    # No zip is no longer a reason to skip the search: the question usually
    # arrives before the zip does, and a national range beats the static
    # table. The per-unit guard stays -- "installed cost per square foot for
    # window cleaning" is the question that produced $5,300 (#95).
    # `asked` is the spend gate: a false negative costs the static table, not
    # a refusal, which is the failure a regex is allowed to have.
    #
    # ponytail: this also skips the 30-day cache, so a phrasing the regex
    # misses answers from the static table even when a researched band for
    # that service+zip is already on disk. Reading the cache costs nothing;
    # give lookup_regional_rates / lookup_job_estimate a cache_only flag and
    # pass it here if the static answer ever reads wrong next to the
    # researched one.
    research_on = (
        settings.price_research_enabled and asked and not priced_per_window(service)
    )
    low_rate, high_rate = static_rates(service)

    async def _research(material: str | None):
        if not research_on:
            return None
        from .flow.price_research import lookup_regional_rates

        return await lookup_regional_rates(
            service, zip_code, static_low=low_rate,
            static_high=high_rate, material=material,
        )

    # Comparative ask (e.g. "separate ranges for shades vs drapes"): one
    # researched range per captured option, envelope on top. Options that
    # research can't actually distinguish are dropped rather than shown as a
    # sham comparison of identical numbers.
    materials = _dedupe_options([m for m in state.slots.materials if m])[:3]
    if user_asked_to_compare(request.message) and len(materials) >= 2:
        options: list[PriceOption] = []
        for label in materials:
            researched = await _research(label)
            if researched is None:
                continue  # only genuinely-researched options belong in a comparison
            per = compute_price_guidance(
                service, area, area_label=area_label, researched=researched,
                window_count=windows,
            )
            if per:
                options.append(
                    PriceOption(label=label, lowUsd=per.lowUsd, highUsd=per.highUsd)
                )
        distinct = len({(o.lowUsd, o.highUsd) for o in options}) > 1
        if len(options) >= 2 and distinct:
            envelope = compute_price_guidance(
                service, area, area_label=area_label, researched=await _research(None),
                window_count=windows,
            )
            if envelope:
                envelope.lowUsd = min(o.lowUsd for o in options)
                envelope.highUsd = max(o.highUsd for o in options)
                envelope.options = options
                envelope.basis += " — per-option ranges included"
                state.price_guidance_snapshot = {
                    "service": service,
                    "areaSqft": area,
                    "areaLabel": area_label,
                    "windows": windows,
                    "guidance": envelope.model_dump(mode="json"),
                }
                return envelope, asked

    # Single-estimate path: prefer a tight, job-specific total estimate
    # (web-searched for this size + scope + zip); fall back to per-sqft rates.
    job_estimate = None
    if research_on:
        from .flow.price_research import lookup_job_estimate

        scope_bits = [f"{service.lower()} in a {area_label}" if area_label else service.lower()]
        if state.slots.scope_options:
            scope_bits.append(", ".join(state.slots.scope_options[:3]))
        if materials:
            scope_bits.append("in " + ", ".join(materials[:2]))
        scope_desc = " — ".join(scope_bits)
        job_estimate = await lookup_job_estimate(
            service, zip_code, area, scope_desc,
            static_low_rate=low_rate, static_high_rate=high_rate,
        )
    researched = None if job_estimate else await _research(materials[0] if materials else None)
    guidance = compute_price_guidance(
        service, area, area_label=area_label,
        researched=researched, job_estimate=job_estimate, window_count=windows,
    )
    # Pin ONLY a band the homeowner was actually offered. Pinning an
    # unasked-for static band would make it the answer to the question they
    # have not asked yet, and research would never get the chance to run.
    if guidance is not None and asked:
        state.price_guidance_snapshot = {
            "service": service,
            "areaSqft": area,
            "areaLabel": area_label,
            "windows": windows,
            "guidance": guidance.model_dump(mode="json"),
        }
    return guidance, asked


_WANTS_LOCAL_CONTEXT = re.compile(
    r"(?i)\b(what'?s popular|in my area|around here|locally|local (style|trend|design)|"
    r"in my region|for my area|near me|common around|do people (here|around))\b"
)
_PROVIDER_NOUN = r"(?:painters?|contractors?|providers?|pros?|companies|company|businesses?|specialists?|cleaners?)"
_NEAR = r"(?:near|around|local|locally|my area|me\b|here\b|nearby)"
_WANTS_PROVIDERS = re.compile(
    r"(?i)(?:"
    rf"{_PROVIDER_NOUN}.{{0,30}}{_NEAR}"
    rf"|{_NEAR}.{{0,30}}{_PROVIDER_NOUN}"
    rf"|(?:recommend|suggest|know)\s+(?:a|any|some)?\s*{_PROVIDER_NOUN}"
    r")"
)


async def _maybe_local_research(state: FlowState, request: HomeAIChatRequest):
    """Web-grounded local context + (beta) providers. Demand-driven: fetched
    only when the homeowner asks about local styles or local providers (so the
    web-search latency lands on a turn where they expect it), once per
    conversation. Returns (LocalContextWire|None, LocalProvidersWire|None)."""
    service = state.slots.project_type
    message = request.message or ""
    # A zip typed in this very message reaches the slots only after the turn,
    # same as in _maybe_price_guidance (#79).
    zip_match = _ZIP_IN_MESSAGE.search(message) if _ZIP_CONTEXT.search(message) else None
    zip_code = state.slots.zip or (zip_match.group(1) if zip_match else None)
    if not service or not zip_code:
        return None, None

    want_context = (
        settings.local_context_enabled
        and not state.local_context_delivered
        and bool(_WANTS_LOCAL_CONTEXT.search(message))
    )
    want_providers = (
        settings.local_provider_research_enabled
        and not state.local_providers_delivered
        and bool(_WANTS_PROVIDERS.search(message))
    )
    if not (want_context or want_providers):
        return None, None

    from .flow.local_research import (
        PROVIDER_DISCLAIMER,
        lookup_local_context,
        lookup_local_providers,
    )

    # Run the two web searches concurrently when both are wanted this turn.
    ctx_task = asyncio.create_task(lookup_local_context(service, zip_code)) if want_context else None
    prov_task = asyncio.create_task(lookup_local_providers(service, zip_code)) if want_providers else None
    ctx = await ctx_task if ctx_task else None
    prov = await prov_task if prov_task else None

    context_wire = None
    if ctx is not None:
        state.local_context_delivered = True
        context_wire = LocalContextWire(
            regionLabel=ctx.region_label,
            styleNotes=ctx.style_notes,
            practicalNotes=ctx.practical_notes,
        )
    providers_wire = None
    if prov is not None:
        state.local_providers_delivered = True
        providers_wire = LocalProvidersWire(
            regionLabel=prov.region_label,
            providers=[LocalProviderWire(name=p["name"], note=p["note"]) for p in prov.providers],
            disclaimer=PROVIDER_DISCLAIMER,
        )
    return context_wire, providers_wire


def _total_floor_area_sqft(context: HomeAIContextPacket) -> float | None:
    totals = context.totals or {}
    value = totals.get("floorAreaSquareMeters")
    if isinstance(value, (int, float)) and value > 0:
        return float(value) * 10.7639
    return None


# --------------------------------------------------------------------------
# Results return (step 10) — quotes uploaded by ops get presented
# --------------------------------------------------------------------------
def _quote_price_label(quote: Any) -> str:
    if quote.priceUsd is not None:
        return f"${quote.priceUsd:,.0f}"
    if quote.priceLowUsd is not None and quote.priceHighUsd is not None:
        return f"${quote.priceLowUsd:,.0f}-${quote.priceHighUsd:,.0f}"
    return ""


def _quotes_on_file(record: Any) -> list[dict[str, Any]]:
    """Provider facts for every returned quote, for the directives after the
    quotes were presented (the presentation JSON carries them on the turn
    they arrive; later turns need them too, #98)."""
    if record is None or not getattr(record, "quotes", None):
        return []
    out = []
    for quote in record.quotes:
        entry: dict[str, Any] = {"providerName": quote.providerName}
        price = _quote_price_label(quote)
        if price:
            entry["price"] = price
        view = quote.homeowner_view()
        if view.get("provider"):
            entry["provider"] = view["provider"]
        out.append(entry)
    return out


def _declined_quotes(record: Any) -> list[dict[str, Any]]:
    """Provider and reason for every quote the homeowner turned down
    (Quintin, Sep 17), so the agent can act on a decline instead of
    talking past it."""
    if record is None or not getattr(record, "quotes", None):
        return []
    return [
        {"providerName": q.providerName, "note": q.decisionNote}
        for q in record.quotes
        if getattr(q, "decision", None) == "declined"
    ]


async def _selected_quote(state: FlowState, record: Any = None) -> dict[str, Any] | None:
    """The quote the homeowner chose in the app, if any: provider and price
    for the directives. Reuses the record when the caller already has it."""
    if not state.quote_request:
        return None
    if record is None:
        from .flow_quotes import quote_store

        record = await quote_store.get(state.quote_request.id)
    if record is None or not record.selectedQuoteId:
        return None
    quote = next((q for q in record.quotes if q.id == record.selectedQuoteId), None)
    if quote is None:
        return None
    return {"providerName": quote.providerName, "price": _quote_price_label(quote)}


async def _pending_quotes(
    state: FlowState,
) -> tuple[Any, list[dict[str, Any]], int] | None:
    """Quotes awaiting presentation: ``(record, all_quote_views, new_count)``.

    Presentation is NOT one-shot — ops uploads in batches (first price at
    24h, the rest by 48h), so this fires whenever the store holds more
    quotes than the agent has presented."""
    if not state.quote_request:
        return None
    from .flow_quotes import quote_store

    record = await quote_store.get(state.quote_request.id)
    if record is None:
        return None
    ref = state.quote_request
    ref.quotes_returned_count = len(record.quotes)
    new_count = len(record.quotes) - ref.presented_quote_count
    if new_count > 0:
        ref.status = "quotes_ready"
        return record, [q.homeowner_view() for q in record.quotes], new_count
    return None


# --------------------------------------------------------------------------
# Public entry points
# --------------------------------------------------------------------------
async def run_flow_turn(
    request: HomeAIChatRequest, *, homeowner_id: str | None = None
) -> HomeAIChatResponse:
    """Serialized per thread: flow state is read-modify-write, so two turns
    racing on one thread can lose a captured slot."""
    thread_id = request.threadId or str(uuid.uuid4())
    async with turn_guard(thread_id) as acquired:
        if not acquired:
            from fastapi import HTTPException

            raise HTTPException(
                status_code=429,
                detail="A previous message on this conversation is still being answered.",
                headers={"Retry-After": "5"},
            )
        return await _run_flow_turn_locked(request, thread_id, homeowner_id)


async def _run_flow_turn_locked(
    request: HomeAIChatRequest, thread_id: str, homeowner_id: str | None
) -> HomeAIChatResponse:
    started = time.monotonic()
    request.threadId = thread_id

    state = await resolve_flow_state(thread_id, request.flowToken)
    await _attach_identity(state, homeowner_id)
    if request.flowToken or request.scanContext is not None:
        state.client_flow_aware = True
    if state.zip_wording_id is None:
        state.zip_wording_id = assign_zip_wording(thread_id).id
    if state.scope_wording_id is None:
        state.scope_wording_id = assign_scope_wording(thread_id).id

    _reconcile_scan(state, request)
    # Pre-warm from durable storage: the host's disk is ephemeral, so a
    # redeploy leaves the local copy missing and the conversation would
    # otherwise silently lose its home.
    if request.homeId:
        from .flow.home_registry import load_index_async

        await load_index_async(request.homeId)
    previous_home = state.home_id
    home_index = _reconcile_home(state, request)
    home_switched = bool(previous_home and state.home_id and previous_home != state.home_id)
    # Screen the message before anything is spent on it, and before anything
    # is READ from it (issue #57). A blocked turn reaches no model, no research
    # call, no ask budget, and no slot — it is both the safe answer and the
    # cheap one. Ordering matters: the first draft ran the message-derived
    # state block first, so a blocked message could still set request_accepted
    # (review of #63).
    guard = (
        input_guard.check_message(request.message)
        if settings.guard_enabled
        else input_guard.GuardVerdict()
    )
    precapture: dict[str, str] = {}
    if request.message and not guard.blocked:
        state.recent_user_words.append(len(request.message.split()))
        del state.recent_user_words[:-4]
        if _CLAIMS_CONFIRMED.search(request.message):
            state.confirm_claims += 1
        if _CANNOT_SEE_UI.search(request.message):
            state.ui_not_visible_claims += 1
        if _accepts_request(state, request.message):
            # Checked before plan_turn so the card can appear on this very
            # turn: "yes" then a card next turn is a dead beat.
            state.request_accepted = True
        # Same reason, for the other half of the card's preconditions.
        precapture = _precapture_from_message(state, request.message)
    if not guard.blocked:
        # A blocked message does not advance the conversation: no ask budget
        # spent, no step progression, nothing for a troll to walk forward.
        _engine.record_user_turn(state, request.message)
    plan = _engine.plan_turn(state, request.message)

    if guard.blocked:
        logger.info(
            "Input guard blocked a message (%s) thread=%s", guard.category, thread_id
        )
        price_guidance = None
        local_context = local_providers = None
        pending = None
        quotes_to_present = None
        response = _guard_response(request, guard)
        # Journaled through the existing suppressed-drafts channel, which is
        # what `write_turn` already projects (text + violations).
        suppressed = [
            {
                "text": request.message or "",
                "violations": [f"input_{guard.category}"],
                "excerpt": guard.excerpt,
            }
        ]
        # Same meaning as a safe-copy substitution: the homeowner did not see a
        # model turn, so quote presentation must not be consumed by it.
        substituted = True
    else:
        price_guidance, price_asked = await _maybe_price_guidance(state, request, home_index)
        local_context, local_providers = await _maybe_local_research(state, request)
        pending = await _pending_quotes(state)
        quotes_to_present = pending[1] if pending else None
        quote_record = pending[0] if pending else None
        if quote_record is None and state.quote_request is not None:
            from .flow_quotes import quote_store

            quote_record = await quote_store.get(state.quote_request.id)
        selected_quote = await _selected_quote(state, quote_record)
        quotes_on_file = _quotes_on_file(quote_record) if not quotes_to_present else None
        declined_quotes = _declined_quotes(quote_record)
        lead_delivered = bool(getattr(quote_record, "opsEmailDeliveredAt", None))

        directives = _build_directives(
            state,
            plan,
            opening=False,
            price_guidance=price_guidance,
            price_asked=price_asked,
            quotes_to_present=quotes_to_present,
            selected_quote=selected_quote,
            quotes_on_file=quotes_on_file,
            declined_quotes=declined_quotes,
            lead_delivered=lead_delivered,
            new_quote_count=pending[2] if pending else 0,
            local_context=local_context,
            local_providers=local_providers,
            home_index=home_index,
            home_switched=home_switched,
            guard=guard,
        )
        plan.gates.can_state_prices = (
            quotes_to_present is not None
            or bool(state.quote_request and state.quote_request.presented_quote_count)
            # A deliberately re-enabled guidance flag shows a range in the UI and
            # directs the model to acknowledge it — don't fight the directive.
            or price_guidance is not None
        )
        # Real returned quotes are real numbers and stay unbounded. A guidance
        # card is not: the model may restate that band and nothing else.
        if price_guidance is not None and not (
            quotes_to_present is not None
            or (state.quote_request and state.quote_request.presented_quote_count)
        ):
            plan.gates.allowed_price_range = (price_guidance.lowUsd, price_guidance.highUsd)
        response, suppressed, substituted = await _generate_enforced(
            request, directives, plan.gates,
            # Legacy builds carry their own card on a path this conversation
            # cannot see, so its prose is not ours to check.
            # A permitted card is guaranteed on this turn (built below if the
            # model writes none), so "look it over below" is true.
            card_already_shown=(
                not state.client_flow_aware
                or state.request_card_delivered
                or plan.gates.can_present_request_card
            ),
        )

    delta = precapture | _apply_capture(
        state,
        response._flow_capture,
        # Nothing is captured from a message we refused: the backstops in
        # _apply_capture read the raw text and would happily take a zip out of
        # a blocked turn (review of #63).
        "" if guard.blocked else request.message,
        quote_draft=response.quoteDraft.model_dump() if response.quoteDraft else None,
        home_index=home_index,
    )
    # A suppressed draft was never delivered, so it spends no ask or offer
    # budget: only the text the homeowner actually saw counts.
    wording_ids = _record_asks_and_wordings(
        state, plan, response.message.content, opening=False,
        raw_text="" if substituted else response._raw_message,
    )
    if pending is not None and state.quote_request:
        record, quote_views, _new = pending
        if response.usedFallback or substituted:
            # The homeowner never saw the quotes this turn — leave them
            # pending so the next turn presents them.
            logger.warning(
                "Quotes pending but the reply was %s; presentation deferred thread=%s",
                "a fallback" if response.usedFallback else "safe copy",
                thread_id,
            )
        else:
            state.quote_request.presented_quote_count = len(quote_views)
            state.quote_request.status = "presented"
            state.mark_complete(FlowStep.RESULTS_RETURN)
            if record.status != "presented":
                # Ops can see delivery happened (their acceptance evidence).
                record.status = "presented"
                from .flow_quotes import quote_store

                await quote_store.save(record)

    # Re-plan gates on the updated state so the wire reflects this turn's outcome.
    final_gates = _engine.evaluate_gates(state, request.message)
    state.step = _engine.plan_turn(state, request.message).step
    state.revision += 1

    # The clients render the request card on the mere presence of quoteDraft
    # (iOS HomeAIChatView bottomBar, phone-demo renderDraft), so a draft the
    # model emitted too early IS the card appearing too early — the complaint
    # that started this (Sep 12: cards before the zip was even asked for).
    # Dropping the draft here is what enforces the gate, on every client.
    if response.quoteDraft is not None and not final_gates.can_present_request_card:
        suppressed.append(
            {
                "quoteDraft": response.quoteDraft.model_dump(),
                "violations": ["request_card_not_permitted"],
                "reason": final_gates.reasons.get("can_present_request_card", ""),
            }
        )
        logger.info(
            "Dropped a premature quoteDraft (%s) thread=%s",
            final_gates.reasons.get("can_present_request_card", ""),
            thread_id,
        )
        response.quoteDraft = None
    # A fallback reply's draft is built from the message text alone ("asking
    # about: yes please"), so on the yes turn the captured state is the card.
    if (
        (response.quoteDraft is None or response.usedFallback)
        and final_gates.can_present_request_card
        and state.quote_request is None
        and not state.request_card_delivered
    ):
        response.quoteDraft = _draft_from_state(state, home_index)
    if response.quoteDraft is not None:
        state.request_card_delivered = True

    response.flow = FlowWire.from_state(
        state, final_gates, _codec().encode(state),
        wording_ids[0] if wording_ids else None, home_index=home_index,
    )
    response.priceGuidance = price_guidance
    response.localContext = local_context
    response.localProviders = local_providers
    await persist_flow_state(state)

    presented_this_turn = bool(
        pending is not None
        and state.quote_request
        and state.quote_request.status == "presented"
        and not (response.usedFallback or substituted)
    )
    # An address-capture turn's raw text IS the address in whatever form the
    # homeowner typed it — exact-match masking can miss a normalized variant,
    # so the whole line is withheld (SOW §12).
    journal_user_text = (
        "[address provided by homeowner — text withheld]"
        if "address" in delta
        else request.message
    )
    journal_record = write_turn(
        settings.storage_dir,
        TurnJournalEntry(
            thread_id=thread_id,
            step=int(state.step),
            step_name=response.flow.stepName,
            wording_ids=wording_ids,
            user_text=journal_user_text,
            agent_text=response.message.content,
            suppressed_drafts=suppressed,
            gates=final_gates.client_view(),
            gate_reasons=final_gates.reasons,
            slots_delta=delta,
            homeowner_id=state.homeowner_id,
            model=response.model,
            prompt_version=response.promptVersion,
            prompt_variant=response.promptVariant,
            used_fallback=response.usedFallback,
            latency_ms=int((time.monotonic() - started) * 1000),
            kind=(
                "blocked" if guard.blocked
                else "results_presented" if presented_this_turn
                else "chat"
            ),
        ),
        state,
        mask_pii=settings.log_pii_masking_enabled,
    )
    if journal_record:
        await supabase_store.insert_journal(journal_record)
    return response


_OPENING_SYNTHETIC_MESSAGE = (
    "(The homeowner just opened the conversation after capturing their home. "
    "Deliver the opening turn described in the flow directives.)"
)


async def run_opening_turn(
    opening: HomeAIOpeningRequest, *, homeowner_id: str | None = None
) -> HomeAIChatResponse:
    """Same per-thread serialization as a normal turn: without it, a
    double-tapped chat open runs two openers concurrently (both miss the
    idempotency cache) and pays for two model calls."""
    thread_id = opening.threadId or str(uuid.uuid4())
    async with turn_guard(thread_id) as acquired:
        if not acquired:
            from fastapi import HTTPException

            raise HTTPException(
                status_code=429,
                detail="This conversation is already being opened.",
                headers={"Retry-After": "5"},
            )
        return await _run_opening_turn_locked(opening, thread_id, homeowner_id)


def _conversation_underway(state: FlowState) -> bool:
    """The thread has moved past its opening: the homeowner has spoken, or a
    request is out. Replaying turn zero at them ("what's your first name?")
    was the Sep 14 finding; regenerating it against mid-flow state, with the
    draft card blanked, was worse."""
    return bool(state.opening_delivered and (state.user_turns > 0 or state.quote_request is not None))


async def _resume_opening(
    thread_id: str,
    state: FlowState,
    request: HomeAIChatRequest,
    homeowner_id: str | None,
) -> HomeAIChatResponse:
    """The opening call on a conversation that is already underway: a short
    welcome-back that says where things stand, built without a model call,
    with the live flow wire and the request card if one was agreed to."""
    from .flow_quotes import quote_store

    await _attach_identity(state, homeowner_id)
    state.client_flow_aware = True
    home_index = _reconcile_home(state, request)
    name = f", {state.slots.first_name}" if state.slots.first_name else ""
    service = (state.slots.project_type or "").strip()
    record = await quote_store.get(state.quote_request.id) if state.quote_request else None

    quote_status = "exploring"
    if record is not None or state.quote_request is not None:
        quote_status = "sent"
        what = f"your {service.lower()} request" if service else "your request"
        chosen = next(
            (q for q in record.quotes if record.selectedQuoteId and q.id == record.selectedQuoteId),
            None,
        ) if record is not None else None
        if chosen is not None:
            content = (
                f"Welcome back{name}. You chose {chosen.providerName} for {what}; your "
                "address has gone to them, and I'll bring their scheduling here as "
                "soon as I have it. Anything else on your mind for the home?"
            )
        elif record is not None and record.status == "quotes_ready" and record.quotes:
            content = (
                f"Welcome back{name}. Quotes are in for {what}. "
                "Want me to walk you through them?"
            )
        elif record is not None and record.opsEmailDeliveredAt:
            # The homeowner asked to be told when their request actually
            # landed (Quintin, Sep 17). The delivery stamp is the fact, so
            # say it plainly rather than repeating "it's on its way".
            content = (
                f"Welcome back{name}. {what[0].upper() + what[1:]} has landed — I've "
                "got it in front of local providers now, and I'll bring their quotes "
                "back here as soon as they come in. Anything else you'd like to look "
                "at meanwhile?"
            )
        else:
            content = (
                f"Welcome back{name}. {what[0].upper() + what[1:]} is out with local "
                "providers now, and I'll bring their quotes back here as soon as they "
                "come in. Anything else you'd like to look at meanwhile?"
            )
    else:
        room = (
            home_index.by_key(state.active_room_key)
            if home_index is not None and state.active_room_key
            else None
        )
        subject = service.lower() if service else ""
        if room is not None:
            subject = f"{subject} in the {room.display_name}" if subject else f"the {room.display_name}"
        content = (
            f"Welcome back{name}. We were talking about {subject}. Where would you like to pick up?"
            if subject
            else f"Welcome back{name}. Where would you like to pick up?"
        )

    gates = _engine.evaluate_gates(state, None)
    draft = None
    if gates.can_present_request_card and state.quote_request is None:
        draft = _draft_from_state(state, home_index)
        quote_status = "awaiting_approval"
    response = HomeAIChatResponse(
        threadId=thread_id,
        message=HomeAIChatMessage(role="assistant", content=content),
        state=HomeAIConversationState(
            intent="provider_request" if state.quote_request is not None else "exploring",
            quoteStatus=quote_status,
            suggestedServiceType=service or None,
        ),
        quoteDraft=draft,
        model="resume",
        provider="local",
    )
    response.flow = FlowWire.from_state(
        state, gates, _codec().encode(state), None, home_index=home_index,
    )
    await persist_flow_state(state)
    return response


async def _run_opening_turn_locked(
    opening: HomeAIOpeningRequest, thread_id: str, homeowner_id: str | None
) -> HomeAIChatResponse:
    started = time.monotonic()

    request = HomeAIChatRequest(
        threadId=thread_id,
        userId=opening.userId,
        projectId=opening.projectId,
        homeProfileId=opening.homeProfileId,
        sourcePage=opening.sourcePage,
        message=_OPENING_SYNTHETIC_MESSAGE,
        homeContext=opening.homeContext,
        workflowState=opening.workflowState,
        flowToken=opening.flowToken,
        homeId=opening.homeId,
    )
    if opening.scanContext is not None:
        try:
            request.scanContext = FlowScanContext.model_validate(opening.scanContext)
        except Exception:  # noqa: BLE001 — opening must not fail on a bad hint
            request.scanContext = None

    state = await resolve_flow_state(thread_id, opening.flowToken)
    # A conversation that is underway is resumed, not reopened: the app calls
    # this on every launch, and the original opener asks for a name the
    # server already knows.
    if _conversation_underway(state):
        return await _resume_opening(thread_id, state, request, homeowner_id)
    # Idempotent per thread: repeat calls return the original opener — from
    # the local cache, or from the durable flow state after a redeploy.
    cached = _load_cached_opening(thread_id)
    if cached is not None:
        return cached
    if state.opening_response is not None:
        # The durable state remembers the opener even when the local cache
        # died with the instance.
        try:
            return HomeAIChatResponse.model_validate(state.opening_response)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Bad durable opening cache for %s: %s", thread_id, exc)
    await _attach_identity(state, homeowner_id)
    state.client_flow_aware = True
    if state.zip_wording_id is None:
        state.zip_wording_id = assign_zip_wording(thread_id).id
    _reconcile_scan(state, request)
    if request.homeId:
        from .flow.home_registry import load_index_async

        await load_index_async(request.homeId)
    # The opener for a walked home speaks about the home, not one room.
    home_index = _reconcile_home(state, request)
    plan = _engine.plan_turn(state, None)
    directives = _build_directives(
        state, plan, opening=True, price_guidance=None, quotes_to_present=None,
        home_index=home_index,
    )
    response, suppressed, _substituted = await _generate_enforced(
        request,
        directives,
        plan.gates,
        max_images_override=max(0, int(settings.opening_max_images or 0)),
        # Turn zero: nothing has been captured, so no card can exist.
        card_already_shown=not state.client_flow_aware,
    )

    if not response.usedFallback:
        # A fallback opener is generic error copy — never cache it as the
        # thread's one grounded opener, and leave steps 1-2 incomplete so
        # the next opening call retries the real model.
        state.opening_delivered = True
        state.mark_complete(FlowStep.RECOGNITION)
        state.mark_complete(FlowStep.ENGAGEMENT)
    wording_ids = _record_asks_and_wordings(state, plan, response.message.content, opening=True)
    # No card on turn zero, ever: nothing has been captured or agreed to.
    response.quoteDraft = None
    final_gates = _engine.evaluate_gates(state, None)
    state.step = _engine.plan_turn(state, None).step
    state.revision += 1

    if not response.usedFallback:
        state.opening_response = response.model_dump(mode="json")
    response.flow = FlowWire.from_state(
        state, final_gates, _codec().encode(state),
        wording_ids[0] if wording_ids else None, home_index=home_index,
    )
    if state.opening_response is not None:
        # Store the final wire shape (with flow attached) for idempotent replays.
        state.opening_response = response.model_dump(mode="json")
    await persist_flow_state(state)
    if not response.usedFallback:
        _cache_opening(thread_id, response)

    record = write_turn(
        settings.storage_dir,
        TurnJournalEntry(
            thread_id=thread_id,
            step=int(state.step),
            step_name=response.flow.stepName,
            wording_ids=wording_ids,
            user_text="",
            agent_text=response.message.content,
            suppressed_drafts=suppressed,
            gates=final_gates.client_view(),
            gate_reasons=final_gates.reasons,
            slots_delta={},
            homeowner_id=state.homeowner_id,
            model=response.model,
            prompt_version=response.promptVersion,
            prompt_variant=response.promptVariant,
            used_fallback=response.usedFallback,
            latency_ms=int((time.monotonic() - started) * 1000),
            kind="opening",
        ),
        state,
        mask_pii=settings.log_pii_masking_enabled,
    )
    if record:
        await supabase_store.insert_journal(record)
    return response


def _opening_cache_path(thread_id: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", thread_id)
    return Path(settings.storage_dir) / "flow_state" / f"{safe}.opening.json"


def _load_cached_opening(thread_id: str) -> HomeAIChatResponse | None:
    try:
        path = _opening_cache_path(thread_id)
        if path.exists():
            return HomeAIChatResponse.model_validate(
                json.loads(path.read_text(encoding="utf-8"))
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not load cached opening for %s: %s", thread_id, exc)
    return None


def _cache_opening(thread_id: str, response: HomeAIChatResponse) -> None:
    try:
        path = _opening_cache_path(thread_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(response.model_dump_json(), encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not cache opening for %s: %s", thread_id, exc)
