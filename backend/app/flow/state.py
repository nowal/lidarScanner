"""Flow state: the server-owned record of where a conversation is in the
SOW §2 ten-step journey.

The state is small on purpose. It rides the wire as a signed token
(`tokens.py`) and is persisted to Supabase, so every field must be cheap to
serialize and safe to expose to the signing layer. The one exception is the
street address: it is stored here (the engine needs it for the lead package)
but is excluded from client-facing serialization — see
``FlowState.client_view`` and API_CONTRACT_V1.md §3.2.
"""

from __future__ import annotations

from enum import IntEnum, StrEnum

from pydantic import BaseModel, Field


class FlowStep(IntEnum):
    """SOW §2 steps. Step 0 (the initial scan) is app-side; the agent starts at 1."""

    RECOGNITION = 1          # grounded opener: name the room + one specific thing
    ENGAGEMENT = 2           # engagement question + first-name ask
    DESIGN_CONVERSATION = 3  # open design conversation, no conversion pressure
    ZIP_COLLECTION = 4       # asked after engagement, when local context matters
    SCAN_HANDOFF = 5         # wait-for-processing constraint (SOW §3, hard)
    SCAN_EXTENSION = 6       # continuity guidance for scanning more of the home
    QUOTE_GATHERING = 7      # project type, scope options, materials
    ADDRESS_COLLECTION = 8   # address, write-only, withheld from providers
    QUOTE_REQUEST = 9        # expectations + lead package to operations
    RESULTS_RETURN = 10      # ops-uploaded quotes presented and compared


class ScanProcessingState(StrEnum):
    """Client-reported processing state (API_CONTRACT_V1 §3.1), mirroring the
    iOS ``ProcessingState`` enum, plus ``unknown`` for legacy clients that
    only send the collapsed ``photorealStatus``."""

    UNKNOWN = "unknown"
    IDLE = "idle"
    PREPARING_UPLOAD = "preparing_upload"
    UPLOADING = "uploading"
    PROCESSING = "processing"
    DOWNLOADING = "downloading"
    COMPLETE = "complete"
    FAILED = "failed"

    @classmethod
    def from_photoreal_status(cls, status: str | None) -> "ScanProcessingState":
        """Map the legacy one-word signal onto the richer enum."""
        mapping = {
            "not_started": cls.IDLE,
            "processing": cls.PROCESSING,
            "ready": cls.COMPLETE,
            "failed": cls.FAILED,
        }
        return mapping.get((status or "").strip().lower(), cls.UNKNOWN)


class ScopeIntent(StrEnum):
    """What the homeowner came to do (docs/SCAN_SCOPE.md). Captured from
    what they SAY, never inferred from how many areas the scan holds: a
    one-area walk can be the start of a whole-home refresh, and a
    nineteen-room walk can be about one bathroom."""

    UNDECIDED = "undecided"
    SINGLE_ROOM = "single_room"
    SELECTED_ROOMS = "selected_rooms"
    WHOLE_HOME = "whole_home"


class Slots(BaseModel):
    """Typed capture slots. ``address`` is server-side only."""

    first_name: str | None = None
    zip: str | None = None
    project_type: str | None = None
    scope_options: list[str] = Field(default_factory=list)
    materials: list[str] = Field(default_factory=list)
    address: str | None = None  # never sent to the client (client_view + token both exclude it)
    contact_email: str | None = None
    contact_phone: str | None = None
    # Set by the token codec when it strips a sensitive value from the
    # client-held token: the flow still knows the value WAS captured (so it
    # never re-asks), while the value itself lives only in the durable store
    # and is merged back in `resolve_flow_state`.
    address_redacted: bool = False
    contact_email_redacted: bool = False
    contact_phone_redacted: bool = False

    @property
    def address_captured(self) -> bool:
        return self.address is not None or self.address_redacted

    @property
    def contact_captured(self) -> bool:
        return bool(
            self.contact_email
            or self.contact_phone
            or self.contact_email_redacted
            or self.contact_phone_redacted
        )


class ScanStatus(BaseModel):
    """The engine's view of the first scan's processing, after reconciling the
    client signal with the server's own job store (server wins)."""

    scan_id: str | None = None
    job_id: str | None = None
    state: ScanProcessingState = ScanProcessingState.UNKNOWN
    progress: float | None = None
    server_verified: bool = False  # True when the job store confirmed the state
    # Sticky: once the server's own job store has confirmed COMPLETE, that
    # fact survives job-record pruning and redeploys (the flow state is
    # durable; the job store is not).
    verified_complete: bool = False
    # Which flag ``state`` was derived from this turn (LIDARAI_SCAN_COMPLETE_SIGNAL):
    # "processor_job" or "device_bake". Recorded so a journal line says
    # which signal opened or held the gate.
    signal: str = "processor_job"
    # The processor pipeline's own state, kept separately so the model link
    # and the journal read it even when the gate runs on the device bake.
    processor_state: ScanProcessingState | None = None

    @property
    def processor(self) -> ScanProcessingState:
        return self.processor_state if self.processor_state is not None else self.state


class QuoteRequestRef(BaseModel):
    id: str
    status: str = "submitted"  # submitted | ops_received | quotes_ready | presented
    quotes_returned_count: int = 0
    # How many quotes the agent has actually presented. Ops uploads arrive in
    # batches (first price at 24h, the rest by 48h) — presentation fires
    # whenever the store holds more quotes than this count, not just once.
    presented_quote_count: int = 0


class FlowState(BaseModel):
    """Everything the flow engine needs to decide what the agent may do this
    turn. One instance per thread."""

    version: int = 1
    # Monotonic per-turn revision: the client token and the durable store can
    # diverge (e.g. a quote submission advances state server-side while the
    # client still holds the pre-submission token); resolution picks the
    # higher revision, token winning ties.
    revision: int = 0
    thread_id: str | None = None
    # The homeowners-table row id (homeowners.id) — the id the flow tables'
    # foreign keys reference. NOT the Supabase auth user id: the JWT `sub` is
    # auth.users.id, resolved to this via flow_runtime._attach_identity.
    homeowner_id: str | None = None
    # The raw verified JWT `sub` (auth.users.id), kept even when no
    # homeowners row resolves — it still proves ops has a way to the person.
    homeowner_auth_sub: str | None = None
    # True in a client token whose identity ids were stripped by the codec.
    homeowner_linked: bool = False

    step: FlowStep = FlowStep.RECOGNITION
    completed_steps: list[int] = Field(default_factory=list)
    slots: Slots = Field(default_factory=Slots)
    scan: ScanStatus = Field(default_factory=ScanStatus)
    quote_request: QuoteRequestRef | None = None

    # --- Whole-home scans -------------------------------------------------
    # When a walked-home index has been ingested, the conversation is about
    # a named room within it rather than "the room". The id is a key into
    # the home registry; the active room is the export's own room key.
    home_id: str | None = None
    active_room_key: str | None = None
    # A room the homeowner named that the index could not resolve: the agent
    # must say it cannot see that room rather than describe a different one.
    unresolved_room_phrase: str | None = None

    # --- Scope intent (docs/SCAN_SCOPE.md) ---------------------------------
    # One room, a few named rooms, or the whole home -- from the homeowner's
    # own words in the design conversation (step 3). Default undecided keeps
    # every existing behaviour. Rides in the token and the durable state.
    scope_intent: ScopeIntent = ScopeIntent.UNDECIDED
    scope_rooms: list[str] = Field(default_factory=list)
    scope_asks: int = 0
    scope_last_asked_at_turn: int = -2
    scope_wording_id: str | None = None   # sticky wording/placement variant, like zip
    # Generic "anything else you'd want to include?" offers made once the
    # model is ready (step 6). Under single_room / selected_rooms the budget
    # is one for the whole conversation.
    extension_offers: int = 0

    # Counters that gates depend on.
    user_turns: int = 0                # substantive user messages seen
    # Word counts of the last few homeowner messages. The prompt tells the
    # agent to match the homeowner's energy; measured behaviour said it
    # doesn't, so the runtime states the case explicitly when they are
    # clearly giving one-line answers.
    recent_user_words: list[int] = Field(default_factory=list)
    first_name_asks: int = 0
    zip_asks: int = 0
    zip_last_asked_at_turn: int = -2   # user_turns value when zip was last asked
    address_asks: int = 0
    # How many times the homeowner has said they already tapped Confirm.
    # Repeating the same "tap Confirm" instruction at someone who says they
    # did is the loop the persona battery caught (Sep 3).
    confirm_claims: int = 0
    # "Want me to put this together for the TakeShape team?" — offers made,
    # and whether the homeowner said yes. The request card is withheld until
    # they do (Sep 12): a card that appears unannounced reads as the agent
    # sending their details on its own.
    request_offers: int = 0
    # user_turns value when the offer was last made, so a bare "yes" is only
    # read as acceptance when it answers the offer rather than some other
    # question the agent asked two turns ago.
    request_offer_at_turn: int = -2
    request_accepted: bool = False
    # Whether a request card has actually reached the homeowner's screen. The
    # gates say a card MAY exist; only this says one DOES, and the agent's
    # prose about the card is checked against it (Sep 13: "it's on the request
    # card now" with no card anywhere in the thread).
    request_card_delivered: bool = False
    # Turns spent on a room that is not in the scan. Explaining how to add
    # it is useful once; a terse homeowner got twelve turns of it and no
    # design help at all (Sep 4).
    unresolved_room_turns: int = 0
    # The homeowner saying they cannot see something in the app. The agent
    # cannot see their screen, so it must stop asserting what is on it.
    ui_not_visible_claims: int = 0
    zip_wording_id: str | None = None  # sticky wording variant for the zip trial (SOW §2)
    opening_delivered: bool = False
    # First guidance card issued this conversation, pinned so the number the
    # homeowner saw never silently changes between turns.
    price_guidance_snapshot: dict | None = None
    # Local research is fetched at most once per conversation (each does a web
    # search); these flags gate the one-shot.
    local_context_delivered: bool = False
    local_providers_delivered: bool = False
    # True once the client has demonstrated flow awareness (sent a flowToken
    # or scanContext, or called the opening endpoint). Legacy TestFlight
    # builds never do — their quote card submits through the old path, so the
    # agent must not promise Confirm-button mechanics to them.
    client_flow_aware: bool = False
    # The delivered opening response (kept in durable state, stripped from
    # the client token) so the opening endpoint stays idempotent across
    # restarts and redeploys.
    opening_response: dict | None = None

    @property
    def has_identity(self) -> bool:
        """Any verified way back to the person: a resolved homeowners row, a
        verified auth sub, or a token attesting one was linked."""
        return bool(self.homeowner_id or self.homeowner_auth_sub or self.homeowner_linked)

    def mark_complete(self, step: FlowStep) -> None:
        if int(step) not in self.completed_steps:
            self.completed_steps.append(int(step))
            self.completed_steps.sort()

    def is_complete(self, step: FlowStep) -> bool:
        return int(step) in self.completed_steps

    # ------------------------------------------------------------------ wire
    def client_view(self) -> dict:
        """The ``flow`` response object (API_CONTRACT_V1 §3.2), minus the
        token, which the codec appends. The address value never appears."""
        return {
            "step": int(self.step),
            "stepName": self.step.name.lower(),
            "completedSteps": list(self.completed_steps),
            "slots": {
                "firstName": self.slots.first_name,
                "zip": self.slots.zip,
                "projectType": self.slots.project_type,
                "scopeOptions": list(self.slots.scope_options),
                "materials": list(self.slots.materials),
                "addressCaptured": self.slots.address_captured,
                "contactCaptured": bool(self.has_identity or self.slots.contact_captured),
                "scopeIntent": str(self.scope_intent),
                "scopeRooms": list(self.scope_rooms),
            },
            "quoteRequest": (
                {
                    "id": self.quote_request.id,
                    "status": self.quote_request.status,
                    "quotesReturnedCount": self.quote_request.quotes_returned_count,
                }
                if self.quote_request
                else None
            ),
        }
