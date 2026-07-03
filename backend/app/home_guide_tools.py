from __future__ import annotations

import re
from typing import Any


KNOWN_SERVICE_TYPES = [
    "Painting",
    "Flooring",
    "Interior Cleaning",
    "Decking",
    "Window Cleaning",
    "Power Washing",
]


def get_service_catalog(zip_code: str | None = None) -> list[dict[str, Any]]:
    """Return the currently known services.

    This is intentionally not provider availability. Availability must come from
    the marketplace data after the homeowner chooses to look for providers.
    """
    return [
        {
            "serviceType": "Painting",
            "availableInZipCode": None if not zip_code else "unknown_until_provider_lookup",
            "scopeExamples": ["walls", "trim", "ceilings", "room refresh"],
        },
        {
            "serviceType": "Flooring",
            "availableInZipCode": None if not zip_code else "unknown_until_provider_lookup",
            "scopeExamples": ["replacement", "repair", "refinishing", "material planning"],
        },
        {
            "serviceType": "Interior Cleaning",
            "availableInZipCode": None if not zip_code else "unknown_until_provider_lookup",
            "scopeExamples": ["deep clean", "move-in clean", "post-project clean"],
        },
        {
            "serviceType": "Decking",
            "availableInZipCode": None if not zip_code else "unknown_until_provider_lookup",
            "scopeExamples": ["deck repair", "refresh", "replacement"],
        },
        {
            "serviceType": "Window Cleaning",
            "availableInZipCode": None if not zip_code else "unknown_until_provider_lookup",
            "scopeExamples": ["interior", "exterior", "glass cleaning"],
        },
        {
            "serviceType": "Power Washing",
            "availableInZipCode": None if not zip_code else "unknown_until_provider_lookup",
            "scopeExamples": ["siding", "patio", "driveway", "outdoor surfaces"],
        },
    ]


def detect_service_type(message: str) -> str | None:
    text = message.lower()
    service_keywords = [
        ("Painting", ["paint", "painting", "color", "walls", "trim", "ceiling"]),
        ("Flooring", ["floor", "flooring", "hardwood", "tile", "carpet", "vinyl"]),
        ("Interior Cleaning", ["deep clean", "cleaning", "clean", "dust", "move-in"]),
        ("Decking", ["deck", "decking", "porch", "railing"]),
        ("Window Cleaning", ["window", "windows", "glass"]),
        ("Power Washing", ["pressure wash", "power wash", "siding", "driveway", "patio"]),
    ]
    for service, keywords in service_keywords:
        if any(keyword in text for keyword in keywords):
            return service
    return None


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
