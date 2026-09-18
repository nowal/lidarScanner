from __future__ import annotations

import re
from typing import Any


KNOWN_SERVICE_TYPES = [
    "Painting",
    "Flooring",
    "Interior Remodeling",
    "Window & Door Install",
    "Handyman",
    "Interior Cleaning",
    "Decking",
    "Roofing & Siding",
    "Window Cleaning",
    "Gutter Cleaning",
    "Power Washing",
    "Moving",
    "Junk Removal",
]

# What an interior LiDAR walk can actually measure for a trade. The lead
# package carries room measurements, so a trade priced off the exterior
# envelope (roof pitch, siding elevation, gutter runs) gets a conversation
# and a provider visit, not numbers we do not have. Recorded here so the
# lead package and the rubric work can both read it from one place.
SCAN_SUPPORT = {
    "Painting": "measured",
    "Flooring": "measured",
    "Interior Remodeling": "measured",
    "Window & Door Install": "measured",
    "Handyman": "measured",
    "Interior Cleaning": "measured",
    "Moving": "measured",
    "Junk Removal": "measured",
    "Window Cleaning": "partial",
    "Decking": "partial",
    "Roofing & Siding": "exterior",
    "Gutter Cleaning": "exterior",
    "Power Washing": "exterior",
}


def get_service_catalog(zip_code: str | None = None) -> list[dict[str, Any]]:
    """Return the currently known services.

    This is intentionally not provider availability. Availability must come from
    the marketplace data after the homeowner chooses to look for providers.
    """
    availability = None if not zip_code else "unknown_until_provider_lookup"
    return [
        {
            "serviceType": service,
            "availableInZipCode": availability,
            "scopeExamples": list(examples),
            "scanSupport": SCAN_SUPPORT.get(service, "partial"),
        }
        for service, examples in _SCOPE_EXAMPLES.items()
    ]


# Scope examples double as the starting point for TakeShape's per-service
# rubrics (Quintin, Sep 11): the quirks a homeowner would not assume are
# exactly what a rubric has to settle, so they belong next to the service.
_SCOPE_EXAMPLES: dict[str, tuple[str, ...]] = {
    "Painting": ("walls", "trim", "ceilings", "doors", "room refresh"),
    "Flooring": ("replacement", "repair", "refinishing", "material planning"),
    "Interior Remodeling": (
        "kitchen remodel", "bathroom remodel", "built-ins", "layout changes",
    ),
    "Window & Door Install": (
        "window replacement", "interior doors", "exterior doors", "patio doors",
    ),
    "Handyman": ("drywall patching", "fixture swaps", "small repairs", "mounting"),
    "Interior Cleaning": (
        "deep clean", "recurring maid service", "move-in clean", "post-project clean",
    ),
    "Decking": ("deck repair", "refresh", "replacement"),
    "Roofing & Siding": ("roof repair", "roof replacement", "siding", "gutter install"),
    "Window Cleaning": (
        "interior glass", "exterior glass", "screens", "sills and tracks",
    ),
    "Gutter Cleaning": ("clearing", "downspout flush", "guard check"),
    "Power Washing": ("siding", "patio", "driveway", "walkways"),
    "Moving": ("local move", "packing", "loading", "furniture only"),
    "Junk Removal": ("single item", "whole room", "garage clear-out", "haul away"),
}


# Distinctive phrases. The LONGEST match wins, not the first service in the
# list: with thirteen trades the same word belongs to several of them
# ("clean my windows" is window cleaning, "gutters need cleaning" is gutter
# cleaning, and neither is interior cleaning), and ordering a flat list so
# that every pair comes out right is not possible.
_STRONG_KEYWORDS: dict[str, tuple[str, ...]] = {
    "Painting": ("paint", "painting", "repaint", "primer", "accent wall"),
    "Flooring": (
        "floor", "flooring", "hardwood", "laminate", "carpet", "vinyl plank",
        "lvp", "subfloor", "refinish the floor", "tile floor",
    ),
    "Interior Remodeling": (
        "remodel", "renovat", "reno ", "gut ", "tear out", "knock down a wall",
        "new kitchen", "new bathroom", "built-ins", "addition",
    ),
    "Window & Door Install": (
        "new window", "replace the window", "replace windows", "window replacement",
        "window install", "door install", "install a door", "replace the door",
        "new door", "patio door", "storm door", "sliding door",
    ),
    "Handyman": (
        "handyman", "odd job", "small repair", "drywall patch", "patch the drywall",
        "hang a", "mount the", "punch list",
    ),
    "Interior Cleaning": (
        "deep clean", "maid", "housekeep", "house cleaning", "clean the house",
        "move-in clean", "move out clean", "post-construction clean",
    ),
    "Decking": ("deck", "decking", "railing", "porch board"),
    "Roofing & Siding": (
        "roof", "roofing", "shingle", "siding", "soffit", "fascia",
        "new gutters", "gutter install", "gutter repair",
    ),
    "Window Cleaning": (
        "window clean", "clean the window", "clean my window", "wash the window",
        "washing the window", "glass clean", "clean the glass", "window washing",
        "window", "windows",
    ),
    "Gutter Cleaning": (
        "gutter clean", "clean the gutter", "clear the gutter", "gutters cleaned",
        "downspout", "gutter",
    ),
    "Power Washing": (
        "pressure wash", "power wash", "soft wash", "driveway", "walkway",
    ),
    "Moving": (
        "moving company", "movers", "move out", "move in", "relocat", "packing",
        "pack up", "moving quote",
    ),
    "Junk Removal": (
        "junk", "haul away", "hauling", "dumpster", "declutter", "clear out",
        "get rid of",
    ),
}

# Generic words that only decide the trade when nothing distinctive matched.
# "the walls" is painting unless something better is on the table; "broken"
# is a handyman job unless a real trade was named.
_WEAK_KEYWORDS: dict[str, tuple[str, ...]] = {
    "Painting": ("color", "colour", "walls", "trim", "ceiling"),
    "Handyman": ("fix", "repair", "broken", "leaking", "sticking"),
    "Interior Cleaning": ("cleaning", "clean", "dust", "tidy"),
}


def detect_service_type(message: str, *, strong_only: bool = False) -> str | None:
    """``strong_only`` skips the weak words. A guess from a whole homeowner
    message needs it: "blue is the color direction" about a sofa is not a
    paint job (Sep 15, #78)."""
    text = (message or "").lower()
    if not text:
        return None
    best_service: str | None = None
    best_length = 0
    for service in KNOWN_SERVICE_TYPES:
        for keyword in _STRONG_KEYWORDS.get(service, ()):
            if keyword in text and len(keyword) > best_length:
                best_service, best_length = service, len(keyword)
    if best_service is not None or strong_only:
        return best_service
    for service in KNOWN_SERVICE_TYPES:
        if any(keyword in text for keyword in _WEAK_KEYWORDS.get(service, ())):
            return service
    return None


def normalize_service_type(value: str | None) -> str | None:
    """Map free text onto the known service catalog.

    The model captures the homeowner's own words ("kitchen repaint", "a
    refresh"), which read fine in a conversation and are useless for
    matching: provider lookup keys off the catalog, so an unnormalized
    value silently matches no partner at all. Returns None when nothing in
    the catalog fits, so the caller can keep the raw phrase rather than
    guess a trade.
    """
    if not value:
        return None
    text = value.strip()
    for known in KNOWN_SERVICE_TYPES:
        if known.lower() == text.lower():
            return known
    return detect_service_type(text)


def quote_intent_detected(message: str) -> bool:
    text = message.lower()
    return bool(
        re.search(
            r"\b(cost|price|pricing|budget|estimate|quote|quotes|provider|contractor|"
            r"book|schedule|hire|availability|what'?s next|next step|can someone|"
            r"get this done|send|request)\b",
            text,
        )
    )


def project_intent_detected(message: str) -> bool:
    text = message.lower()
    return bool(
        re.search(
            r"\b(paint|painted|floor|flooring|clean|washed|replace|install|repair|"
            r"refinish|renovate|redo|update|finish|fix)\b",
            text,
        )
    )


def cta_label_for_quote(service_type: str | None) -> str:
    if service_type:
        return f"Request a {service_type.lower()} quote for this space"
    return "Request a quote for this space"


def build_quote_cta(
    *,
    cta_allowed: bool,
    service_type: str | None,
    room_ids: list[str] | None = None,
    scope_notes: list[str] | None = None,
) -> dict[str, Any] | None:
    if not cta_allowed:
        return None
    return {
        "type": "quote_request",
        "label": cta_label_for_quote(service_type),
        "serviceType": service_type,
        "roomIds": room_ids or [],
        "scopeNotes": scope_notes or [],
    }


def create_quote_request_draft_action(
    *,
    project_id: str | None,
    service_type: str | None,
    room_ids: list[str] | None,
    scope_notes: list[str] | None,
) -> dict[str, Any]:
    """Create an internal action recommendation, not a submitted quote."""
    return {
        "action": "createQuoteRequestDraft",
        "requiresUserConfirmation": True,
        "projectId": project_id,
        "serviceType": service_type,
        "roomIds": room_ids or [],
        "scopeNotes": scope_notes or [],
    }


def submit_quote_request_action(quote_request_draft_id: str | None) -> dict[str, Any]:
    """Represent the guarded submit action.

    The model must never call this directly. The app can use this only after the
    homeowner approves the editable draft and chooses a provider.
    """
    return {
        "action": "submitQuoteRequest",
        "quoteRequestDraftId": quote_request_draft_id,
        "requiresUserConfirmation": True,
        "allowedForModelDirectExecution": False,
    }


def save_user_preference_action(project_id: str | None, preference: str) -> dict[str, Any]:
    return {
        "action": "saveUserPreference",
        "projectId": project_id,
        "preference": preference[:240],
        "requiresUserConfirmation": False,
    }


def handoff_to_human_action(project_id: str | None, reason: str) -> dict[str, Any]:
    return {
        "action": "handoffToHuman",
        "projectId": project_id,
        "reason": reason[:240],
        "requiresUserConfirmation": False,
    }
