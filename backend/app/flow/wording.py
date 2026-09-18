"""Wording catalog: every scripted ask the agent makes carries a stable
``wordingId`` so the journal can record which wording produced which user
response (SOW §2: trial zip wordings/placements and record answer rates;
SOW §4: per-step wording logging).

The texts here are guidance the prompt builder hands to the model ("ask for
the zip roughly like this"), not verbatim strings — except the deterministic
fallbacks, which are used as-is when the model output violates a gate twice.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass


@dataclass(frozen=True)
class Wording:
    id: str
    step: int
    guidance: str
    # Placement: the earliest substantive homeowner turn at which this ask
    # may be made (the scope trial varies placement as well as wording).
    min_user_turns: int = 0


# SOW §2: "the agent will try a few different wordings AND placements for
# the zip request and record which ones users actually answer". Placement
# rides on min_user_turns: two variants ask at the engagement threshold,
# the third waits one more substantive turn.
ZIP_WORDINGS: tuple[Wording, ...] = (
    Wording(
        id="step4.zip.local_styles_v1",
        step=4,
        min_user_turns=2,
        guidance=(
            "Tie the zip request to local style context: you can tailor ideas "
            "to what's popular and what things tend to run in their area if "
            "they share their zip code."
        ),
    ),
    Wording(
        id="step4.zip.pricing_context_v1",
        step=4,
        min_user_turns=2,
        guidance=(
            "Tie the zip request to pricing relevance: costs vary a lot by "
            "area, and a zip code lets you keep any guidance realistic for "
            "where they live."
        ),
    ),
    Wording(
        id="step4.zip.provider_availability_v1",
        step=4,
        min_user_turns=3,   # the later placement
        guidance=(
            "Tie the zip request to provider availability: knowing the zip "
            "lets TakeShape check which local pros cover their area when "
            "they're ready."
        ),
    ),
)

# local_styles_v1 promises local style context, which only exists when local
# research is on. With it off the same variant keeps its id (the answer-rate
# trial stays whole) but asks without the promise (Sep 15, #79).
LOCAL_STYLES_NO_RESEARCH_GUIDANCE = (
    "Tie the zip request to local relevance: knowing where they live keeps "
    "ideas and next steps practical for their area. Do NOT say you can tell "
    "them what is popular or trending locally -- you have no local trend data."
)

FIRST_NAME_WORDING = Wording(
    id="step2.first_name.opener_v1",
    step=2,
    guidance=(
        "After the engagement question, ask only for their first name so the "
        "conversation feels personal. Nothing else is requested at this point."
    ),
)

# Scope intent (docs/SCAN_SCOPE.md): asked once, lightly, inside the design
# conversation -- never as a form field. Three variants differ in framing
# AND placement so answer rates per wording/placement can be compared, the
# same way the zip trial works.
SCOPE_WORDINGS: tuple[Wording, ...] = (
    Wording(
        id="step3.scope.this_room_or_more_v1",
        step=3,
        min_user_turns=1,
        guidance=(
            "While talking design, ask lightly whether this room is the whole "
            "project, or whether other rooms -- or the whole home -- are on "
            "their mind too. One casual question, no form-filling."
        ),
    ),
    Wording(
        id="step3.scope.after_first_idea_v1",
        step=3,
        min_user_turns=2,
        guidance=(
            "Give one concrete idea for the space first, then ask whether "
            "they want to keep the focus here or whether a few other rooms, "
            "or the whole home, are part of the plan."
        ),
    ),
    Wording(
        id="step3.scope.planning_frame_v1",
        step=3,
        min_user_turns=1,
        guidance=(
            "Frame it as planning: ideas land differently for one room than "
            "for a whole-home refresh, so ask which they have in mind -- this "
            "room, a few rooms, or the whole place."
        ),
    ),
)

ADDRESS_WORDING = Wording(
    id="step8.address.quote_request_v1",
    step=8,
    guidance=(
        "Explain the address supports drive-time pricing, stays private, and "
        "is only shared after they choose a quote. Ask for it plainly, once."
    ),
)

# Safe copy when the model keeps inviting more capture than the stated
# scope allows (the model IS ready here, so the wait copy would be wrong).
SAFE_SCOPE_COPY = (
    "Let's keep the focus on the space you chose — I have what I need for "
    "it. Where would you like to take the design next?"
)

# Deterministic safe copy, used verbatim if regeneration also violates a gate.
SAFE_SCAN_WAIT_COPY = (
    "Your home model is still being prepared — I'll let you know as soon as "
    "it's ready. In the meantime, I'm happy to keep exploring ideas for the "
    "space we've been looking at."
)


# Used verbatim when the model claims to be human twice in a row. It answers
# the question rather than dodging it, because dodging is the thing that makes
# a homeowner ask again (#54).
SAFE_IMPERSONATION_COPY = (
    "I should be straight with you: I'm TakeShape's AI assistant for your "
    "home, not a person. The providers who price the work are real people, "
    "and I'll get your request to them whenever you're ready. What would you "
    "like to do with the room?"
)


# --------------------------------------------------------------------------
# Input guard copy (app/flow/input_guard.py)
#
# Used verbatim when a message is blocked before the model ever sees it. Short
# and warm on purpose: a lecture reads worse than a redirect, and the guide is
# supposed to sound like a friend who knows houses (#54).
# --------------------------------------------------------------------------

# Something is happening right now. Lead with the action — the design
# conversation can wait, and offering to keep chatting about paint here would
# be grotesque.
GUARD_EMERGENCY_COPY = (
    "That sounds like an emergency, so please deal with that first and come "
    "back to me after. If you smell gas or see fire, get everyone out and call "
    "911 from outside — for a gas smell, call your gas company's emergency "
    "line too, and don't flip any switches on your way out. For water, shut "
    "off the main if you can reach it safely. I'll be right here when things "
    "are handled."
)

# Someone trying to talk the guide into being something else. No explanation of
# what tripped, no scolding — just the same warm redirect every time.
GUARD_INJECTION_COPY = (
    "I'm just the home guide here — I help with your space and your projects, "
    "and that doesn't change. Happy to pick back up wherever we were: what "
    "would you like to do with the room?"
)

# Plainly not about this home.
GUARD_OFF_TOPIC_COPY = (
    "That one's outside what I do — I'm only useful on your home and the "
    "projects in it. What would you like to work on in the space?"
)

GUARD_COPY = {
    "emergency": GUARD_EMERGENCY_COPY,
    "injection": GUARD_INJECTION_COPY,
    "off_topic": GUARD_OFF_TOPIC_COPY,
}


def assign_zip_wording(thread_id: str) -> Wording:
    """Deterministic, sticky per-thread assignment (same approach as the
    existing prompt-variant hash so behavior is reproducible from the id)."""
    digest = hashlib.sha256(f"zip-wording:{thread_id}".encode("utf-8")).hexdigest()
    return ZIP_WORDINGS[int(digest[:4], 16) % len(ZIP_WORDINGS)]


def assign_scope_wording(thread_id: str) -> Wording:
    """Deterministic, sticky per-thread assignment (same scheme as zip)."""
    digest = hashlib.sha256(f"scope-wording:{thread_id}".encode("utf-8")).hexdigest()
    return SCOPE_WORDINGS[int(digest[:4], 16) % len(SCOPE_WORDINGS)]


def wording_by_id(wording_id: str) -> Wording | None:
    for w in (*ZIP_WORDINGS, *SCOPE_WORDINGS, FIRST_NAME_WORDING, ADDRESS_WORDING):
        if w.id == wording_id:
            return w
    return None
