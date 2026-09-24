"""Rough price guidance (Noah's Aug 25 request; API_CONTRACT_V1 §3.2).

Deliberately deterministic and wide. The model never authors numbers — the
server derives a band from the same per-service rates the deterministic
fallback already used, widens it, rounds it coarsely, and always attaches the
disclaimer. Feature-flagged via ``LIDARAI_AGENT_PRICE_GUIDANCE_ENABLED``
(default off) because SOW §6 lists agent pricing as out of scope; turning it
on is a config change, per the roadmap.
"""

from __future__ import annotations

import math
import re

from .wire import PriceGuidance

DISCLAIMER = (
    "Rough guidance only — costs vary a lot by home and by area, and real "
    "providers need to look at the space to give a true price."
)

# (low $/sqft, high $/sqft, floor low $, floor high $) — from the existing
# fallback's rate table, then widened by the factors below. Only the trades
# that are genuinely priced off measured floor area live here; the rest are
# sized by the job, not the square footage, and use _TYPICAL_JOB below.
_RATES: dict[str, tuple[float, float, float, float]] = {
    "Painting": (2.5, 6.5, 450, 950),
    "Flooring": (7.0, 18.0, 800, 1600),
    "Interior Remodeling": (60.0, 220.0, 5000, 25000),
    "Interior Cleaning": (0.18, 0.45, 160, 360),
    "Decking": (15.0, 40.0, 1500, 6000),
    "Roofing & Siding": (4.5, 14.0, 6000, 20000),
}
_DEFAULT_RATE = (1.5, 5.0, 300, 850)

# Trades that interior floor area says NOTHING about. Window cleaning priced
# off the house's square feet -- and web-researched as "installed cost per
# square foot" -- put $1,300-$5,300 in front of Quintin for 15 windows
# (Sep 16, #95). These never see a per-sqft rate: a count-priced trade uses
# the per-unit table, the rest use the typical-job band (or a job estimate
# bounded by it).
_NOT_FLOOR_AREA: frozenset[str] = frozenset({
    "Window Cleaning", "Power Washing", "Gutter Cleaning", "Moving",
    "Junk Removal", "Window & Door Install", "Handyman",
})
# (low $/unit, high $/unit, minimum low $, minimum high $). Window cleaning
# is interior + exterior per window, with a call-out minimum.
_PER_WINDOW: dict[str, tuple[float, float, float, float]] = {
    "Window Cleaning": (8.0, 20.0, 100.0, 180.0),
}


def priced_by_floor_area(service_type: str | None) -> bool:
    return bool(service_type) and service_type not in _NOT_FLOOR_AREA


def priced_per_window(service_type: str | None) -> bool:
    return bool(service_type) and service_type in _PER_WINDOW

# Wide low/high TOTAL band for a typical job, used when no measurements exist
# yet — the pre-scan case, which is most first asks. Covers every trade in
# home_guide_tools.KNOWN_SERVICE_TYPES, including the ones interior floor area
# says nothing about (a mover charges by truck-hours, a junk hauler by volume).
# These are national and deliberately loose; the web-researched estimate in
# flow.price_research narrows them when it can.
_TYPICAL_JOB: dict[str, tuple[float, float]] = {
    "Painting": (900, 6500),
    "Flooring": (1500, 12000),
    "Interior Remodeling": (8000, 60000),
    "Window & Door Install": (600, 12000),
    "Handyman": (150, 1200),
    "Interior Cleaning": (120, 500),
    "Decking": (2000, 20000),
    "Roofing & Siding": (6000, 30000),
    "Window Cleaning": (150, 600),
    "Gutter Cleaning": (100, 400),
    "Power Washing": (200, 800),
    "Moving": (500, 5000),
    "Junk Removal": (150, 900),
}
_DEFAULT_TYPICAL_JOB = (200, 5000)

# Static-table fallback stays deliberately wide (least information).
_LOW_FACTOR = 0.7
_HIGH_FACTOR = 1.5
# Web-researched per-sqft rates already reflect the region, so tighten the
# band around them (still an estimate, but less padding).
_RESEARCHED_LOW_FACTOR = 0.9
_RESEARCHED_HIGH_FACTOR = 1.15

_PRICE_ASK = re.compile(
    r"(?i)\b(how much|cost|price[ds]?|pricing|estimate|ballpark|budget|roughly|"
    r"ranges?|splits?|breakdown|broken out|hourly|per hour|"
    # "rate" only in a pricing sense - bare rate also matches
    # "can you rate this design?", which is not a price ask.
    r"(?:hourly|labor|labour|going|day|daily)\s+rates?|"
    r"(?:the|their|your|typical|average)\s+rates?|rates?\s+(?:per|for)|"
    # Follow-ups after a card was already shown ("what's the rate?", "break
    # that down per sq ft"). Without these the agent hands over a range and
    # then flatly refuses the next question about it, which reads broken.
    r"per square foot|per sq\.? ?ft|break (?:it|that|this) down|"
    # "a general guess for the quote" (Sep 13) got no card, so the directive
    # told the model to refuse. Bare "I guess" is not a price ask.
    r"guesstimate|(?:a|any|your|best|rough|general|educated|ballpark)\s+guess|"
    r"rough (?:idea|number|figure)|ball ?park|"
    r"what (?:it|that|this|they) (?:would|will|might) (?:run|cost|charge)|"
    # "what would a repaint run", but also "what DO maid services run" —
    # the present tense is at least as common and was being missed.
    r"what (?:would|do|does|will|might|should) .{0,40}?(run|cost|be|charge)|"
    # "what's that gonna run me" — five of the nineteen cost questions in the
    # Sep 16 battery, and the most common way people actually ask.
    r"(?:gonna|going to) (?:run|cost|set me back)|"
    r"what (?:it|this|that|they) costs?|"
    # Looking a price UP is a cost question; putting a quote request TOGETHER
    # is not. The verb separates them, which is why bare "quote" is still not
    # in this pattern: it is the flow's own word for the thing that is NOT a
    # ballpark, and no width of pattern could tell those two apart.
    r"(?:look|search|check|find|shop|hunt|browse|google) (?:\w+ ){0,3}?"
    r"(?:quotes?|prices?|pricing|costs?|estimates?|rates?)|"
    r"what (?:it|this|that|they) goes? for)\b"
)

_COMPARE_ASK = re.compile(
    r"(?i)\b(separate(ly)?|each|both|split|compare|versus|vs\.?|breakdown|"
    r"broken out|side by side)\b"
)


def user_asked_for_price(message: str | None) -> bool:
    return bool(message and _PRICE_ASK.search(message))


def user_asked_to_compare(message: str | None) -> bool:
    return bool(message and _COMPARE_ASK.search(message))


def static_rates(service_type: str) -> tuple[float, float]:
    low_rate, high_rate, _, _ = _RATES.get(service_type, _DEFAULT_RATE)
    return low_rate, high_rate


def typical_job_band(service_type: str) -> tuple[float, float]:
    return _TYPICAL_JOB.get(service_type, _DEFAULT_TYPICAL_JOB)


# Rough floor area per room/bedroom. Only used to turn a volunteered count
# into something the per-sqft table can chew on, never presented as a
# measurement — the band it produces is still widened and coarsely rounded.
_SQFT_PER_ROOM = 180.0

_SIZE_SQFT = re.compile(
    r"(?i)\b(\d[\d,]*(?:\.\d+)?)\s*(?:sq\.?\s*(?:ft|feet)|square\s+(?:ft|feet)|sf)\b"
)
_SIZE_ROOMS = re.compile(
    r"(?i)\b(\d{1,2})\s*(?:-|\s)?\s*(?:bed|bedroom|br|bdrm|room)s?\b"
)
_SIZE_WORDS: tuple[tuple[re.Pattern[str], float], ...] = (
    (re.compile(r"(?i)\b(whole|entire|full)\s+(house|home)\b"), 1800.0),
    (re.compile(r"(?i)\b(single|one|1)\s+room\b"), _SQFT_PER_ROOM),
)


def parse_size_hint(message: str | None) -> float | None:
    """Floor area in sqft from a size the homeowner volunteered, else None.

    Deliberately returns None rather than guessing: no size is a perfectly
    good state, it just means the typical-job band instead of a per-sqft one.
    """
    if not message:
        return None
    m = _SIZE_SQFT.search(message)
    if m:
        try:
            value = float(m.group(1).replace(",", ""))
        except ValueError:
            return None
        # Bound it: a typo'd "18000000 sq ft" must not drive a price.
        return value if 50 <= value <= 20000 else None
    m = _SIZE_ROOMS.search(message)
    if m:
        rooms = int(m.group(1))
        if 1 <= rooms <= 20:
            return rooms * _SQFT_PER_ROOM
        return None
    for pattern, sqft in _SIZE_WORDS:
        if pattern.search(message):
            return sqft
    return None


def compute_price_guidance(
    service_type: str | None,
    floor_area_sqft: float | None,
    *,
    area_label: str | None = None,   # "living room" when one room is in focus
    researched=None,      # flow.price_research.ResearchedRates | None
    job_estimate=None,    # flow.price_research.JobEstimate | None (tightest)
    window_count: int | None = None,  # from the walk, for count-priced trades
) -> PriceGuidance | None:
    """Return guidance, or None when there is nothing sane to say.

    Precedence, tightest first:
    1. a web-searched TOTAL job estimate (already tight, job-specific);
    2. web-searched per-sqft regional rates × area (moderately tight);
    3. the static national table (widest).

    ``floor_area_sqft`` may be None — the usual case before any scan, and the
    only case at all for trades interior floor area says nothing about. Then
    the band comes from the typical-job table and the basis says so plainly,
    so nobody reads a measured number into it.
    """
    if not service_type:
        return None
    if service_type in _PER_WINDOW:
        per_low, per_high, min_low, min_high = _PER_WINDOW[service_type]
        if window_count and window_count > 0:
            low = _round_coarse(max(min_low, window_count * per_low))
            high = _round_coarse(max(min_high, window_count * per_high * 1.25))
            if high <= low:
                high = low * 2
            basis = (
                f"{window_count} window openings counted in the walk, {service_type.lower()}"
                " -- typical per-window rates, inside and out"
            )
            return PriceGuidance(lowUsd=low, highUsd=high, basis=basis, disclaimer=DISCLAIMER)
        # No count yet: the typical band, never a per-sqft number.
        floor_area_sqft, researched, job_estimate = None, None, None
    elif service_type in _NOT_FLOOR_AREA:
        # Sized by the job, not the floor: a researched TOTAL (bounded by the
        # typical band in price_research) is fine; per-sqft is not.
        floor_area_sqft, researched = None, None
    has_area = bool(floor_area_sqft and floor_area_sqft > 0)

    # Naming the room the number was measured from matters on a whole-home
    # scan: "≈320 sq ft" next to a 1,400 sq ft house otherwise reads as a
    # mistake rather than as the one room they asked about.
    scope = (
        f"≈{int(round(floor_area_sqft, -1))} sq ft"
        + (f" ({area_label})" if area_label else "")
        if has_area
        else "typical job — nothing measured yet"
    )

    if job_estimate is not None:
        low, high = _round_coarse(job_estimate.low_usd), _round_coarse(job_estimate.high_usd)
        if high <= low:
            high = low * 2
        region = job_estimate.region_label or "your area"
        basis = f"{scope}, {service_type.lower()} — estimated for {region} from recent web sources"
        return PriceGuidance(lowUsd=low, highUsd=high, basis=basis, disclaimer=DISCLAIMER)

    if not has_area:
        # No measurements and no web estimate: the widest honest answer. The
        # typical-job table is already the wide band, so it is NOT widened
        # again here — doing that gave painting $630-$9,800, which is true
        # but useless to a homeowner.
        typical_low, typical_high = typical_job_band(service_type)
        low = _round_coarse(typical_low)
        high = _round_coarse(typical_high)
        if high <= low:
            high = low * 2
        basis = f"{service_type.lower()}, {scope}"
        return PriceGuidance(lowUsd=low, highUsd=high, basis=basis, disclaimer=DISCLAIMER)

    low_rate, high_rate, floor_low, floor_high = _RATES.get(service_type, _DEFAULT_RATE)
    region_note = None
    if researched is not None:
        low_rate, high_rate = researched.low_per_sqft, researched.high_per_sqft
        region_note = researched.region_label or None
        low_factor, high_factor = _RESEARCHED_LOW_FACTOR, _RESEARCHED_HIGH_FACTOR
    else:
        low_factor, high_factor = _LOW_FACTOR, _HIGH_FACTOR
    low = max(floor_low * low_factor, floor_area_sqft * low_rate * low_factor)
    high = max(floor_high, floor_area_sqft * high_rate) * high_factor
    low, high = _round_coarse(low), _round_coarse(high)
    if high <= low:
        high = low * 2
    basis = f"{scope}, {service_type.lower()}"
    if region_note:
        basis += f" — typical {region_note} rates from recent web sources"
    else:
        basis += " (approximate bounding measurements)"
    return PriceGuidance(lowUsd=low, highUsd=high, basis=basis, disclaimer=DISCLAIMER)


def _round_coarse(value: float) -> float:
    """Round to 2 significant figures so the number can't read as precise."""
    if value <= 0:
        return 0.0
    magnitude = 10 ** (int(math.floor(math.log10(value))) - 1)
    return float(int(round(value / magnitude)) * magnitude)
