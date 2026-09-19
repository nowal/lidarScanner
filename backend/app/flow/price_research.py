"""Web-grounded regional price research for the rough-guidance card.

Noah's Aug 25 request: rough, deliberately wide guidance as an engagement
tool ("hey I've got a 500 sqft room, what might it roughly cost") — with the
caveat that real providers give real prices. This module uses the model's
server-side web search to find typical installed cost ranges for a service
in the homeowner's area, instead of our static fallback table.

Guardrails, in order:
- Results are cached per (service, zip) for 30 days — one search per area
  per service, not per conversation.
- Researched rates are sanity-clamped against the static table (0.25×–4×)
  so a bad search can't produce an absurd card.
- Any failure (timeout, tool error, unparseable result) falls back to the
  static table silently. The card is never blocked on research.
- The homeowner-facing disclaimer is unconditional either way.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path

import anthropic

from ..config import settings
from ._cache import read_json_cache, write_json_cache
from .pricing import typical_job_band

logger = logging.getLogger("lidarai.flow.price_research")

CACHE_TTL_SECONDS = 30 * 24 * 3600

_RESEARCH_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["lowUsdPerSqft", "highUsdPerSqft", "regionLabel", "confidence"],
    "properties": {
        "lowUsdPerSqft": {"type": "number"},
        "highUsdPerSqft": {"type": "number"},
        "regionLabel": {"type": "string"},
        "confidence": {"type": "string"},
    },
}


@dataclass
class ResearchedRates:
    low_per_sqft: float
    high_per_sqft: float
    region_label: str
    fetched_at: float

    def to_json(self) -> dict:
        return {
            "lowPerSqft": self.low_per_sqft,
            "highPerSqft": self.high_per_sqft,
            "regionLabel": self.region_label,
            "fetchedAt": self.fetched_at,
        }

    @classmethod
    def from_json(cls, data: dict) -> "ResearchedRates":
        return cls(
            low_per_sqft=float(data["lowPerSqft"]),
            high_per_sqft=float(data["highPerSqft"]),
            region_label=str(data.get("regionLabel", "")),
            fetched_at=float(data.get("fetchedAt", 0)),
        )


def _cache_path(service: str, zip_code: str | None, material: str | None = None) -> Path:
    slug = re.sub(r"[^a-z0-9]+", "-", service.lower()).strip("-") or "service"
    if material:
        slug += "-" + re.sub(r"[^a-z0-9]+", "-", material.lower()).strip("-")[:24]
    # No zip is its own cache entry, not zip 00000: the national range is a
    # different answer from any local one, and the two must not overwrite
    # each other.
    safe_zip = re.sub(r"[^0-9]", "", zip_code or "")[:5] or "national"
    return Path(settings.storage_dir) / "price_research" / f"{slug}_{safe_zip}.json"


def _read_cache(
    service: str, zip_code: str | None, material: str | None = None
) -> ResearchedRates | None:
    data = read_json_cache(_cache_path(service, zip_code, material), CACHE_TTL_SECONDS)
    return ResearchedRates.from_json(data) if data else None


def _write_cache(
    service: str, zip_code: str | None, rates: ResearchedRates, material: str | None = None
) -> None:
    write_json_cache(_cache_path(service, zip_code, material), rates.to_json())



NATIONAL_LABEL = "the US (national average)"


def _region_label(parsed: dict, zip_code: str | None) -> str:
    """With no zip the label is knowable and the model's is not worth the
    risk: it answered "United States (national average, unmeasured job -
    small sing", already chopped at 60 characters, and that string is read
    back into the next turn's prompt."""
    if not zip_code:
        return NATIONAL_LABEL
    return str(parsed.get("regionLabel", "")).strip()[:60]


def _clamp(
    rates: ResearchedRates,
    static_low: float,
    static_high: float,
    *,
    spread: float = 4.0,
) -> ResearchedRates | None:
    """Reject or bound nonsense; require a real spread (wide is the point).
    ``spread`` widens the sanity band — material-specific lookups use a
    looser band, since the static table only knows the generic service and a
    tight anchor crushes genuinely different options into identical ranges."""
    low, high = rates.low_per_sqft, rates.high_per_sqft
    if low <= 0 or high <= 0 or high < low:
        return None
    low = max(static_low / spread, min(low, static_low * spread))
    high = max(static_high / spread, min(high, static_high * spread))
    if high < low * 1.4:
        high = low * 1.4
    return ResearchedRates(low, high, rates.region_label, rates.fetched_at)


_JOB_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["lowUsd", "highUsd", "regionLabel"],
    "properties": {
        "lowUsd": {"type": "number"},
        "highUsd": {"type": "number"},
        "regionLabel": {"type": "string"},
    },
}


@dataclass
class JobEstimate:
    low_usd: float
    high_usd: float
    region_label: str
    fetched_at: float

    def to_json(self) -> dict:
        return {
            "lowUsd": self.low_usd,
            "highUsd": self.high_usd,
            "regionLabel": self.region_label,
            "fetchedAt": self.fetched_at,
        }

    @classmethod
    def from_json(cls, d: dict) -> "JobEstimate":
        return cls(float(d["lowUsd"]), float(d["highUsd"]), str(d.get("regionLabel", "")), float(d.get("fetchedAt", 0)))


def _job_cache_path(service: str, zip_code: str | None, area_bucket: int, scope_key: str) -> Path:
    slug = re.sub(r"[^a-z0-9]+", "-", service.lower()).strip("-") or "service"
    sk = re.sub(r"[^a-z0-9]+", "-", scope_key.lower()).strip("-")[:24]
    z = re.sub(r"[^0-9]", "", zip_code or "")[:5] or "national"
    return Path(settings.storage_dir) / "price_research" / f"job_{slug}_{z}_{area_bucket}_{sk}.json"


def _job_prompt(service: str, zip_code: str | None, area_sqft: float | None, scope_desc: str) -> str:
    if area_sqft and area_sqft > 0:
        size = f" — for a room of roughly {int(round(area_sqft))} square feet of floor area"
        tail = "Keep the range as tight as the real data supports."
    else:
        size = " — for a typical job of this kind, since nothing has been measured yet"
        tail = (
            "Nothing about this home has been measured, so give a WIDE range that "
            "honestly covers small and large versions of this job."
        )
    where = f"near zip {zip_code}" if zip_code else "in the United States"
    scope_line = (
        "in that specific area (not national averages)" if zip_code
        else "across the country, and say so in the region label"
    )
    return (
        f"Search current pricing and estimate the TOTAL cost range "
        f"to have a professional do this job {where}: "
        f"{service.lower()} — {scope_desc}{size}. Give a realistic "
        f"low-to-high total dollar range for the whole job {scope_line}, "
        f"and a short region label. {tail}"
    )


async def lookup_job_estimate(
    service: str,
    zip_code: str | None,
    area_sqft: float | None,
    scope_desc: str,
    *,
    static_low_rate: float,
    static_high_rate: float,
) -> JobEstimate | None:
    """Web-searched TOTAL cost range for THIS specific job (size + scope +
    area), which is naturally tighter than a per-sqft band because it answers
    the real question. Sanity-clamped against the static per-sqft table so a
    bad search can't produce an absurd total; cached per service+zip+area.

    ``area_sqft`` may be None, which asks for a typical job of that trade in
    that area instead — the answer most homeowners want before anyone has
    measured anything. Bucket 0 keeps those cached apart from sized lookups."""
    bucket = max(1, int(round(area_sqft / 100.0))) if area_sqft and area_sqft > 0 else 0
    scope_key = scope_desc or service
    path = _job_cache_path(service, zip_code, bucket, scope_key)
    cached = read_json_cache(path, CACHE_TTL_SECONDS)
    if cached:
        est = JobEstimate.from_json(cached)
        return _clamp_job(est, area_sqft, static_low_rate, static_high_rate, service=service)
    if not settings.anthropic_api_key:
        return None
    try:
        client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key, timeout=45.0, max_retries=0)
        response = await client.messages.create(
            model=settings.anthropic_model.strip() or "claude-sonnet-5",
            max_tokens=1500,
            tools=[{"type": "web_search_20260209", "name": "web_search", "max_uses": 3}],
            output_config={"effort": "low", "format": {"type": "json_schema", "schema": _JOB_SCHEMA}},
            messages=[{"role": "user", "content": _job_prompt(service, zip_code, area_sqft, scope_desc)}],
        )
        if response.stop_reason == "refusal":
            return None
        text = next((b.text for b in response.content if b.type == "text"), None)
        if not text:
            return None
        parsed = json.loads(text)
        est = JobEstimate(
            low_usd=float(parsed["lowUsd"]),
            high_usd=float(parsed["highUsd"]),
            region_label=_region_label(parsed, zip_code),
            fetched_at=time.time(),
        )
        clamped = _clamp_job(est, area_sqft, static_low_rate, static_high_rate, service=service)
        if clamped is None:
            return None
        write_json_cache(path, clamped.to_json())
        logger.info(
            "Job estimate %s/%s ~%dsqft: $%.0f-$%.0f (%s)",
            service, zip_code, int(area_sqft or 0), clamped.low_usd, clamped.high_usd, clamped.region_label,
        )
        return clamped
    except Exception as exc:  # noqa: BLE001
        logger.warning("Job estimate failed for %s/%s: %s", service, zip_code, exc)
        return None


def _clamp_job(
    est: JobEstimate,
    area_sqft: float | None,
    static_low_rate: float,
    static_high_rate: float,
    *,
    service: str | None = None,
) -> JobEstimate | None:
    """Bound the total against static per-sqft × area (0.3x-3x), and keep the
    spread reasonable (>=1.2x, <=4x).

    With no area — the pre-scan case — there is no per-sqft anchor, so bound
    against the service's typical-job band instead. A bad search must not be
    able to put an absurd number in front of a homeowner either way."""
    lo, hi = est.low_usd, est.high_usd
    if lo <= 0 or hi <= 0 or hi < lo:
        return None
    if area_sqft and area_sqft > 0:
        floor = static_low_rate * area_sqft * 0.3
        ceil = static_high_rate * area_sqft * 3.0
    else:
        typical_low, typical_high = typical_job_band(service or "")
        floor = typical_low * 0.3
        ceil = typical_high * 3.0
    lo = max(floor, min(lo, ceil))
    hi = max(floor, min(hi, ceil))
    if hi < lo * 1.2:
        hi = lo * 1.2
    if hi > lo * 4:
        hi = lo * 4
    return JobEstimate(round(lo, -1), round(hi, -1), est.region_label, est.fetched_at)


async def lookup_regional_rates(
    service: str,
    zip_code: str | None,
    *,
    static_low: float,
    static_high: float,
    material: str | None = None,
) -> ResearchedRates | None:
    """Cached web-searched $/sqft range for a service, near a zip when we have
    one and nationally when we don't. A material hint (e.g. "hardwood" for
    flooring) narrows the search.

    The zip usually arrives later than the first "what does this cost?", and
    waiting for it meant answering the question with the static table or not
    at all. A national range is a worse answer than a local one and a much
    better answer than none."""
    # Uniform sanity band: a wider band for material lookups was tried and
    # amplified per-unit-priced products (window treatments) into absurd
    # per-sqft ranges. Real per-option pricing is Sprint 2 work.
    spread = 4.0
    cached = _read_cache(service, zip_code, material)
    if cached is not None:
        return _clamp(cached, static_low, static_high, spread=spread)
    if not settings.anthropic_api_key:
        return None
    try:
        client = anthropic.AsyncAnthropic(
            api_key=settings.anthropic_api_key, timeout=25.0, max_retries=0
        )
        response = await client.messages.create(
            model=settings.anthropic_model.strip() or "claude-sonnet-5",
            max_tokens=2000,
            tools=[{"type": "web_search_20260209", "name": "web_search", "max_uses": 3}],
            output_config={
                "effort": "low",
                "format": {"type": "json_schema", "schema": _RESEARCH_SCHEMA},
            },
            messages=[
                {
                    "role": "user",
                    "content": (
                        f"What is the typical INSTALLED cost range in US dollars per "
                        f"square foot for {service.lower()}"
                        + (f" ({material})" if material else "")
                        + (
                            f" for a homeowner in or near zip code {zip_code}? "
                            "Search for current regional pricing. "
                            if zip_code
                            else " for a homeowner in the United States? Search "
                            "for current national pricing. "
                        )
                        + "Return a deliberately wide, honest range covering budget "
                        "through premium options, and a short region label like a "
                        "city or metro name"
                        + ("." if zip_code else ", or 'nationally' if the range is not local.")
                    ),
                }
            ],
        )
        if response.stop_reason == "refusal":
            return None
        text = next((b.text for b in response.content if b.type == "text"), None)
        if not text:
            return None
        parsed = json.loads(text)
        rates = ResearchedRates(
            low_per_sqft=float(parsed["lowUsdPerSqft"]),
            high_per_sqft=float(parsed["highUsdPerSqft"]),
            region_label=_region_label(parsed, zip_code),
            fetched_at=time.time(),
        )
        clamped = _clamp(rates, static_low, static_high, spread=spread)
        if clamped is None:
            logger.info("Price research rejected by sanity clamp: %s", parsed)
            return None
        _write_cache(service, zip_code, clamped, material)
        logger.info(
            "Price research %s/%s: $%.2f-$%.2f per sqft (%s)",
            service, zip_code, clamped.low_per_sqft, clamped.high_per_sqft, clamped.region_label,
        )
        return clamped
    except Exception as exc:  # noqa: BLE001
        logger.warning("Price research failed for %s/%s: %s", service, zip_code, exc)
        return None
