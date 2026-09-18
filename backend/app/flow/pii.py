"""PII masking for the wording journal and any log destined for tuning,
evaluation, or training (SOW §12: addresses, full names, and contact details
are masked; logs stay in TakeShape-controlled storage).

Masking is two-layered:
1. Exact-match replacement of captured slot values — the engine *knows* the
   homeowner's name and address, so those are removed with certainty.
2. Pattern-based masking for emails, phone numbers, and street-address
   shapes that appear before or outside slot capture.

Zip codes are intentionally NOT masked: §12 lists names, addresses, and
contact details, and zip-level locality is the substrate for the SOW §2
zip-placement analysis.
"""

from __future__ import annotations

import re

from .state import Slots

_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_PHONE = re.compile(
    r"(?<!\d)(?:\+?1[\s.-]?)?(?:\(\d{3}\)|\d{3})[\s.-]?\d{3}[\s.-]?\d{4}(?!\d)"
)
# "123 Maple St", "45 W Oak Avenue Apt 2", "4482 Grimmelman Trail",
# "221B Baker Street" … a street suffix is required, and the suffix list is
# broad — loose suffixes (Run, Loop, Walk…) are disambiguated by the
# measure-word filter below so "a 5 minute walk" or "a 2 mile loop" never
# masks.
_STREET_ADDRESS = re.compile(
    r"""(?ix)
    \b\d{1,6}[a-z]?\s+(?:[NSEW]\.?\s+)?
    ((?:[A-Za-z][A-Za-z'.-]*\s+){1,4})
    (?:st(?:reet)?|ave(?:nue)?|blvd|boulevard|dr(?:ive)?|r(?:oa)?d|ln|lane|
       ct|court|cir(?:cle)?|pl(?:ace)?|ter(?:race)?|way|pkwy|parkway|hwy|highway|
       run|trail|trl|loop|cove|cv|crossing|xing|path|pass|point|pt|ridge|bend|
       hollow|holw|walk|row|green|commons|landing|manor|grove|meadow|glen|
       knoll|shore|bay|harbor|bluff|trace|crest|vista|heights|hts|springs?|creek)
    \.?\b
    (?:\s*(?:apt|unit|suite|ste|\#)\s*\w+)?
    """
)
# Words that make a number-word-suffix shape a measurement, not an address.
_MEASURE_WORDS = frozenset(
    "minute minutes min mins mile miles hour hours second seconds foot feet "
    "meter meters metre metres km block blocks story stories step steps day "
    "days week weeks month months year years".split()
)


def _mask_street_addresses(text: str) -> str:
    def _replace(match: re.Match[str]) -> str:
        name_words = match.group(1).lower().split()
        if any(word.strip("'.-") in _MEASURE_WORDS for word in name_words):
            return match.group(0)
        return "[address]"

    return _STREET_ADDRESS.sub(_replace, text)


def _slot_value_pattern(value: str) -> re.Pattern[str]:
    """Exact-match masking that survives light normalization: the model's
    captured slot may differ from what the homeowner typed by commas,
    whitespace, or punctuation, so tokens match across any of those."""
    tokens = [re.escape(t) for t in re.split(r"[\s,.]+", value.strip()) if t]
    return re.compile(r"[\s,.]*".join(tokens), re.IGNORECASE)


def mask_text(text: str, slots: Slots | None = None) -> str:
    """Return ``text`` with PII replaced by typed placeholders."""
    if not text:
        return text
    masked = text
    if slots is not None:
        for value, placeholder in (
            (slots.address, "[address]"),
            (slots.contact_email, "[email]"),
            (slots.contact_phone, "[phone]"),
            (slots.first_name, "[first name]"),
        ):
            if value and len(value.strip()) >= 2:
                masked = _slot_value_pattern(value).sub(placeholder, masked)
    masked = _EMAIL.sub("[email]", masked)
    masked = _PHONE.sub("[phone]", masked)
    masked = _mask_street_addresses(masked)
    return masked
