"""Wire models for the flow additions to the chat API (API_CONTRACT_V1 §3).

Everything here is additive: legacy clients that don't send `scanContext` /
`flowToken` and ignore `flow` / `priceGuidance` keep working unchanged.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from .state import FlowState, ScanProcessingState
from .machine import GateDecision


class FlowScanContext(BaseModel):
    """Client-reported scan/processing context (request field ``scanContext``)."""

    scanId: str | None = None
    jobId: str | None = None
    processingState: str | None = None   # iOS ProcessingState name; coerced leniently
    processingProgress: float | None = Field(default=None, ge=0.0, le=1.0)
    scanMode: str | None = None          # new_scan | update_existing
    # The phone's own textured bake for this scan is finished. The gate's
    # flag when LIDARAI_SCAN_COMPLETE_SIGNAL=device_bake; ignored otherwise.
    localModelReady: bool | None = None

    def parsed_state(self) -> ScanProcessingState:
        raw = (self.processingState or "").strip().lower()
        try:
            return ScanProcessingState(raw)
        except ValueError:
            return ScanProcessingState.UNKNOWN


class FlowQuoteRequestWire(BaseModel):
    id: str
    status: str
    quotesReturnedCount: int = 0


class FlowActiveRoomWire(BaseModel):
    """Which room of a whole-home scan the conversation is about, so the app
    can follow along (highlight it on the plan, show its photos)."""

    key: str
    name: str
    confidentName: bool = True


class FlowHomeWire(BaseModel):
    homeId: str
    roomCount: int
    activeRoom: FlowActiveRoomWire | None = None
    # A room they named that this home does not contain, echoed so the app
    # can offer to scan it rather than silently doing nothing.
    unresolvedRoom: str | None = None


class FlowWire(BaseModel):
    """The ``flow`` response object."""

    step: int
    stepName: str
    completedSteps: list[int] = Field(default_factory=list)
    slots: dict = Field(default_factory=dict)
    gates: dict = Field(default_factory=dict)
    wordingId: str | None = None
    token: str
    quoteRequest: FlowQuoteRequestWire | None = None
    home: FlowHomeWire | None = None

    @classmethod
    def from_state(
        cls,
        state: FlowState,
        gates: GateDecision,
        token: str,
        wording_id: str | None,
        home_index=None,
    ) -> "FlowWire":
        view = state.client_view()
        home = None
        if home_index is not None and state.home_id:
            room = (
                home_index.by_key(state.active_room_key) if state.active_room_key else None
            )
            home = FlowHomeWire(
                homeId=state.home_id,
                roomCount=len(home_index.rooms),
                activeRoom=(
                    FlowActiveRoomWire(
                        key=room.key, name=room.display_name, confidentName=room.confident
                    )
                    if room
                    else None
                ),
                unresolvedRoom=state.unresolved_room_phrase,
            )
        return cls(
            step=view["step"],
            stepName=view["stepName"],
            completedSteps=view["completedSteps"],
            slots=view["slots"],
            gates=gates.client_view(),
            wordingId=wording_id,
            token=token,
            quoteRequest=(
                FlowQuoteRequestWire.model_validate(view["quoteRequest"])
                if view["quoteRequest"]
                else None
            ),
            home=home,
        )


class PriceOption(BaseModel):
    """One option in a comparative guidance card (e.g. solar shades vs drapes)."""

    label: str
    lowUsd: float
    highUsd: float


class LocalContextWire(BaseModel):
    """Web-grounded local style + practical notes for the homeowner's area."""

    regionLabel: str
    styleNotes: list[str] = Field(default_factory=list)
    practicalNotes: list[str] = Field(default_factory=list)


class LocalProviderWire(BaseModel):
    name: str
    note: str = ""


class LocalProvidersWire(BaseModel):
    """BETA. Unvetted local providers found online; always carries the
    not-a-TakeShape-partner disclaimer."""

    regionLabel: str
    providers: list[LocalProviderWire] = Field(default_factory=list)
    disclaimer: str
    beta: bool = True


class PriceGuidance(BaseModel):
    """Deliberately wide, always-disclaimed rough guidance (Noah's Aug 25 ask).
    Server-computed and server-clamped; never a commitment. ``options`` is
    present when the homeowner asked to compare alternatives — the top-level
    range is then the envelope across options."""

    lowUsd: float
    highUsd: float
    basis: str
    disclaimer: str
    options: list[PriceOption] | None = None
