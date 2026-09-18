"""Web-grounded local research for zips TakeShape doesn't cover yet.

Two features, both extending the price_research pattern (cached web search,
sanity guards, silent fallback, feature flag):

1. local CONTEXT (LIDARAI_LOCAL_CONTEXT_ENABLED) — regional style trends and
   practical notes (seasonal timing, permits). Pure engagement value; no
   business risk. Cached 30 days per zip+service.

2. local PROVIDER research (LIDARAI_LOCAL_PROVIDER_RESEARCH_ENABLED) — BETA.
   For zips with no TakeShape partner, surface a few real local providers
   FOUND ONLINE as a starting point. Guardrails, because this is sensitive:
   - web-search-grounded only; if the search returns nothing usable we
     return nothing (the model may never invent a provider);
   - every result is labeled unvetted / not-a-TakeShape-partner;
   - capped, cached 7 days (providers churn);
   - the agent presents them as "found online, not vetted by TakeShape".
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

logger = logging.getLogger("lidarai.flow.local_research")

CONTEXT_TTL_SECONDS = 30 * 24 * 3600
PROVIDER_TTL_SECONDS = 7 * 24 * 3600

PROVIDER_DISCLAIMER = (
    "These are options I found online as a starting point — they are not "
    "TakeShape-vetted partners, so check reviews and credentials yourself."
)

_CONTEXT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["regionLabel", "styleNotes", "practicalNotes"],
    "properties": {
        "regionLabel": {"type": "string"},
        "styleNotes": {"type": "array", "items": {"type": "string"}},
        "practicalNotes": {"type": "array", "items": {"type": "string"}},
    },
}

_PROVIDER_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["regionLabel", "providers"],
    "properties": {
        "regionLabel": {"type": "string"},
        "providers": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["name", "note", "foundOnline"],
                "properties": {
                    "name": {"type": "string"},
                    "note": {"type": "string"},
                    # The model must set this true only for a provider it
                    # actually saw in the web results — our honesty tripwire.
                    "foundOnline": {"type": "boolean"},
                },
            },
        },
    },
}


@dataclass
class LocalContext:
    region_label: str
    style_notes: list[str]
    practical_notes: list[str]
    fetched_at: float

    def to_json(self) -> dict:
        return {
            "regionLabel": self.region_label,
            "styleNotes": self.style_notes,
            "practicalNotes": self.practical_notes,
            "fetchedAt": self.fetched_at,
        }

    @classmethod
    def from_json(cls, data: dict) -> "LocalContext":
        return cls(
            region_label=str(data.get("regionLabel", "")),
            style_notes=[str(s) for s in data.get("styleNotes", [])][:4],
            practical_notes=[str(s) for s in data.get("practicalNotes", [])][:4],
            fetched_at=float(data.get("fetchedAt", 0)),
        )


@dataclass
class LocalProviders:
    region_label: str
    providers: list[dict[str, str]]
    fetched_at: float

    def to_json(self) -> dict:
        return {
            "regionLabel": self.region_label,
            "providers": self.providers,
            "fetchedAt": self.fetched_at,
        }

    @classmethod
    def from_json(cls, data: dict) -> "LocalProviders":
        return cls(
            region_label=str(data.get("regionLabel", "")),
            providers=[
                {"name": str(p.get("name", "")), "note": str(p.get("note", ""))}
                for p in data.get("providers", [])
            ][:4],
            fetched_at=float(data.get("fetchedAt", 0)),
        )


def _slug(service: str, zip_code: str, kind: str) -> Path:
    s = re.sub(r"[^a-z0-9]+", "-", service.lower()).strip("-") or "service"
    z = re.sub(r"[^0-9]", "", zip_code)[:5] or "00000"
    return Path(settings.storage_dir) / "local_research" / f"{kind}_{s}_{z}.json"


async def _search(
    prompt: str, schema: dict, *, max_uses: int, timeout: float = 50.0, max_tokens: int = 2500
) -> dict | None:
    """Homeowner-facing lookups keep the tight default timeout (they sit on
    a chat turn); ops-facing lookups pass a longer one — a multi-search
    lead research run regularly needs 60-120s and nobody is waiting on it."""
    if not settings.anthropic_api_key:
        return None
    try:
        client = anthropic.AsyncAnthropic(
            api_key=settings.anthropic_api_key, timeout=timeout, max_retries=0
        )
        # Streamed, not create(): a multi-search run holds the connection
        # open for minutes with no bytes on a non-streaming call, and PaaS
        # egress (observed on Railway) kills the idle connection mid-search.
        # SSE events flow continuously, so nothing sees an idle socket.
        async with client.messages.stream(
            model=settings.anthropic_model.strip() or "claude-sonnet-5",
            max_tokens=max_tokens,
            tools=[{"type": "web_search_20260209", "name": "web_search", "max_uses": max_uses}],
            output_config={"effort": "low", "format": {"type": "json_schema", "schema": schema}},
            messages=[{"role": "user", "content": prompt}],
        ) as stream:
            response = await stream.get_final_message()
        if response.stop_reason == "refusal":
            return None
        text = next((b.text for b in response.content if b.type == "text"), None)
        return json.loads(text) if text else None
    except Exception as exc:  # noqa: BLE001
        logger.warning("local_research web search failed: %s", exc)
        return None


# --------------------------------------------------------------- local context
async def lookup_local_context(service: str, zip_code: str) -> LocalContext | None:
    if not settings.local_context_enabled:
        return None
    path = _slug(service, zip_code, "context")
    cached = read_json_cache(path, CONTEXT_TTL_SECONDS)
    if cached:
        return LocalContext.from_json(cached)
    parsed = await _search(
        f"A homeowner near zip {zip_code} is planning {service.lower()}. "
        "Search the web to identify the city/metro for this zip, then give "
        "genuinely useful LOCAL guidance grounded in that region's real "
        "conditions:\n"
        "- practicalNotes: concrete, findable facts — best season/timing for "
        f"this kind of work in that climate, typical permit or HOA norms, and "
        "any regional cost or logistics factors.\n"
        "- styleNotes: only regional design leanings you can actually support "
        "(architecture, climate-driven material choices); leave empty rather "
        "than guess generic trends.\n"
        "Set regionLabel to the real city and state (never 'placeholder'). "
        "Every note must be specific to this region — omit anything generic.",
        _CONTEXT_SCHEMA,
        max_uses=3,
    )
    if parsed and str(parsed.get("regionLabel", "")).lower() in {"placeholder", "", "unknown"}:
        return None
    if not parsed:
        return None
    result = LocalContext(
        region_label=str(parsed.get("regionLabel", "")).strip()[:60],
        # Capped per note like the provider fields: these notes are written
        # into the turn's directives, and their source is web pages nobody
        # here controls. A note long enough to be an instruction is not a note.
        style_notes=[str(s).strip()[:200] for s in parsed.get("styleNotes", []) if str(s).strip()][:4],
        practical_notes=[str(s).strip()[:200] for s in parsed.get("practicalNotes", []) if str(s).strip()][:4],
        fetched_at=time.time(),
    )
    if not (result.style_notes or result.practical_notes):
        return None
    write_json_cache(path, result.to_json())
    return result


# ------------------------------------------------------------- local providers
_GENERIC_NAMES = re.compile(
    r"(?i)^(a |your |local |the )?(local |nearby |area )?"
    r"(provider|contractor|company|business|professional|service|painter|cleaner|specialist)s?$"
)


async def lookup_local_providers(service: str, zip_code: str) -> LocalProviders | None:
    """BETA. Web-searched local providers, clearly unvetted. Returns None if
    the search surfaces nothing real."""
    if not settings.local_provider_research_enabled:
        return None
    path = _slug(service, zip_code, "providers")
    cached = read_json_cache(path, PROVIDER_TTL_SECONDS)
    if cached:
        return LocalProviders.from_json(cached)
    parsed = await _search(
        f"Find real, currently-operating businesses that provide "
        f"{service.lower()} services near zip code {zip_code}. Search the web "
        "for actual local companies. For each, give the business name exactly "
        "as listed and a short note on what they do or their specialty. Only "
        "include businesses you actually found in the search results; set "
        "foundOnline true only for those. Return at most 4. Give a region label.",
        _PROVIDER_SCHEMA,
        max_uses=4,
    )
    if not parsed:
        return None
    providers = []
    for entry in parsed.get("providers", []):
        name = str(entry.get("name", "")).strip()
        note = str(entry.get("note", "")).strip()
        # Honesty guards: must be flagged found-online, have a real-looking
        # name (not "a local provider"), and not be a placeholder.
        if not entry.get("foundOnline") or not name or _GENERIC_NAMES.match(name):
            continue
        providers.append({"name": name[:80], "note": note[:160]})
    if not providers:
        return None
    result = LocalProviders(
        region_label=str(parsed.get("regionLabel", "")).strip()[:60],
        providers=providers[:4],
        fetched_at=time.time(),
    )
    write_json_cache(path, result.to_json())
    return result


# ---------------------------------------------------------- ops provider leads
_LEADS_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["regionLabel", "providers"],
    "properties": {
        "regionLabel": {"type": "string"},
        "providers": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["name", "note", "phone", "website", "ratingLabel", "foundOnline"],
                "properties": {
                    "name": {"type": "string"},
                    "note": {"type": "string"},
                    "phone": {"type": "string"},
                    "website": {"type": "string"},
                    # e.g. "4.6 stars (128 Google reviews)"; empty when unknown.
                    "ratingLabel": {"type": "string"},
                    "foundOnline": {"type": "boolean"},
                },
            },
        },
    },
}


async def lookup_provider_leads(service: str, zip_code: str) -> list[dict] | None:
    """Ops-facing only (the lead email to operations, provider-finder scope
    agreed Sep 1) — never shown to the homeowner. Richer than the homeowner
    surface: phone, website, review rating. Gated by its own flag, cached
    like the provider search."""
    if not settings.provider_finder_enabled:
        return None
    path = _slug(service, zip_code, "leads")
    cached = read_json_cache(path, PROVIDER_TTL_SECONDS)
    if cached:
        return cached.get("leads") or None
    parsed = await _search(
        f"Find real, currently-operating businesses that provide "
        f"{service.lower()} services near zip code {zip_code}. Search the web "
        "for actual local companies. For each give: the business name exactly "
        "as listed, phone number, website, a ratingLabel with their Google "
        "rating and review count like '4.6 stars (128 Google reviews)' when "
        "findable (empty string otherwise), and a one-line note on their "
        "specialty. Only include businesses you actually found in the search "
        "results; set foundOnline true only for those. Return at most 5. Give "
        "a region label.",
        _LEADS_SCHEMA,
        max_uses=5,
        timeout=150.0,
        max_tokens=4000,
    )
    if not parsed:
        return None
    leads = []
    unreachable = 0
    for entry in parsed.get("providers", []):
        name = str(entry.get("name", "")).strip()
        if not entry.get("foundOnline") or not name or _GENERIC_NAMES.match(name):
            continue
        # A lead operations cannot call or click is not a lead. The schema
        # marks phone and website required but an empty string satisfies it,
        # and two of five came back that way in a real email (Sep 13). They
        # are dropped here so the cache never holds them either; a Places key
        # is what fills these in properly (attach_discovery copies phone and
        # website onto a name-matched row).
        if not str(entry.get("phone", "")).strip() and not str(entry.get("website", "")).strip():
            unreachable += 1
            continue
        leads.append(
            {
                "name": name[:100],
                "phone": str(entry.get("phone", "")).strip()[:40],
                "website": str(entry.get("website", "")).strip()[:200],
                "ratingLabel": str(entry.get("ratingLabel", "")).strip()[:80],
                "note": str(entry.get("note", "")).strip()[:160],
            }
        )
    if unreachable:
        logger.info(
            "Provider research for %s/%s dropped %d lead(s) with no phone and no website",
            service, zip_code, unreachable,
        )
    if not leads:
        return None
    write_json_cache(path, {"leads": leads[:5], "fetchedAt": time.time()})
    return leads[:5]


# ----------------------------------------------------------- provider discovery
# Extends the provider finder with platform presences for the ranking
# (partners.rank_for_lead). Terms of service are a hard constraint here:
#
# * Google Places (API, keyed, TakeShape-owned account) is the ONLY source of
#   ratings and review counts. Its terms allow display with attribution and
#   cap caching of anything but the place id at 30 days -- hence the TTL
#   and the retrieval timestamp on every number stored.
# * Yelp, Facebook, Instagram, Nextdoor are never fetched or scraped. A
#   profile LINK is recorded when the existing web-search path sees one in
#   search results; the count fields for those platforms stay null.
#
# Design record: docs/adr/provider-ranking.md.
PLACES_SEARCH_URL = "https://places.googleapis.com/v1/places:searchText"
# The field mask decides the Places SKU billed; rating/userRatingCount and
# website/phone are the "Pro/Enterprise" tier. Keep the list minimal.
PLACES_FIELD_MASK = (
    "places.id,places.displayName,places.rating,places.userRatingCount,"
    "places.googleMapsUri,places.websiteUri,places.nationalPhoneNumber,"
    "places.formattedAddress"
)
PLACES_SOURCE = "google_places_api"
PROFILE_LINK_SOURCE = "web_search_profile_link"

# Platforms whose numbers we may not collect; only a profile link on the
# platform's own host is accepted, and only when it points at a page, not
# the site root.
LINK_ONLY_PLATFORMS: dict[str, tuple[str, ...]] = {
    "yelp": ("yelp.com",),
    "facebook": ("facebook.com", "fb.com"),
    "instagram": ("instagram.com",),
    "nextdoor": ("nextdoor.com",),
}


def discovery_ttl_seconds() -> int:
    return max(1, int(settings.provider_discovery_ttl_days)) * 24 * 3600


def discovery_query(service: str) -> str:
    """Trade category -> Places text query, from configuration. A category
    the map does not list falls back to "<category> contractor"; adding a
    trade means editing LIDARAI_PROVIDER_DISCOVERY_QUERIES, not code."""
    try:
        table = json.loads(settings.provider_discovery_queries or "{}")
    except Exception:  # noqa: BLE001
        logger.warning("LIDARAI_PROVIDER_DISCOVERY_QUERIES is not valid JSON; using defaults")
        table = {}
    for key, query in (table or {}).items():
        if str(key).strip().lower() == service.strip().lower() and str(query).strip():
            return str(query).strip()
    return f"{service.strip().lower()} contractor"


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _optional_number(value, cast):
    """None stays None (the API did not report it); only a real number is
    stored. Never coerces absence to zero."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return cast(value)
    except (TypeError, ValueError):
        return None


async def search_places(service: str, zip_code: str) -> list[dict] | None:
    """One Places Text Search for this trade near this zip. Returns a list
    of candidates ``{name, phone, website, address, presences: [google]}``,
    or None when the key is unset or the call failed (the caller treats
    that as "no data", not "no providers")."""
    key = settings.google_places_api_key
    if not key:
        return None
    body = {
        "textQuery": f"{discovery_query(service)} near {zip_code}",
        "maxResultCount": max(1, min(20, int(settings.provider_discovery_max_results))),
        "languageCode": "en",
        "regionCode": "US",
    }
    try:
        import httpx

        async with httpx.AsyncClient(timeout=httpx.Timeout(20.0)) as client:
            resp = await client.post(
                PLACES_SEARCH_URL,
                headers={
                    "X-Goog-Api-Key": key,
                    "X-Goog-FieldMask": PLACES_FIELD_MASK,
                    "Content-Type": "application/json",
                },
                json=body,
            )
            resp.raise_for_status()
            data = resp.json() or {}
    except Exception as exc:  # noqa: BLE001
        logger.warning("Google Places search failed for %s/%s: %s", service, zip_code, exc)
        return None
    verified_at = _now_iso()
    found: list[dict] = []
    for place in data.get("places") or []:
        name = str(((place.get("displayName") or {}).get("text")) or "").strip()
        if not name or _GENERIC_NAMES.match(name):
            continue
        presence = {
            "platform": "google",
            "profileUrl": str(place.get("googleMapsUri") or "").strip() or None,
            "rating": _optional_number(place.get("rating"), float),
            "reviewCount": _optional_number(place.get("userRatingCount"), int),
            "followerCount": None,
            "source": PLACES_SOURCE,
            "lastVerifiedAt": verified_at,
            "placeId": str(place.get("id") or "").strip() or None,
        }
        found.append(
            {
                "name": name[:200],
                "phone": str(place.get("nationalPhoneNumber") or "").strip()[:40],
                "website": str(place.get("websiteUri") or "").strip()[:200],
                "address": str(place.get("formattedAddress") or "").strip()[:200],
                "presences": [presence],
            }
        )
    return found


_PROFILE_LINKS_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["businesses"],
    "properties": {
        "businesses": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["name", "yelpUrl", "facebookUrl", "instagramUrl", "nextdoorUrl"],
                "properties": {
                    "name": {"type": "string"},
                    # Empty string when no such profile appeared in the
                    # search results. Never a guess.
                    "yelpUrl": {"type": "string"},
                    "facebookUrl": {"type": "string"},
                    "instagramUrl": {"type": "string"},
                    "nextdoorUrl": {"type": "string"},
                },
            },
        }
    },
}


def accept_profile_url(platform: str, url: str) -> str | None:
    """A profile URL is kept only when it sits on the platform's own host
    and names a page (the site root is not a profile)."""
    from urllib.parse import urlsplit

    url = str(url or "").strip()
    if not url:
        return None
    try:
        parts = urlsplit(url if "://" in url else f"https://{url}")
    except ValueError:
        return None
    host = parts.netloc.lower().split("@")[-1].split(":")[0]
    if parts.scheme not in ("http", "https"):
        return None
    hosts = LINK_ONLY_PLATFORMS.get(platform, ())
    if not any(host == h or host.endswith("." + h) for h in hosts):
        return None
    if parts.path.strip("/") == "":
        return None
    return parts.geturl()[:300]


async def lookup_profile_links(names: list[str], zip_code: str) -> dict[str, dict[str, str]]:
    """Profile links on the link-only platforms for the named businesses,
    via the existing web-search path (search results, not the platforms'
    pages). ``{name: {platform: url}}``; a business with nothing found is
    absent. Counts are never returned: those fields stay null."""
    names = [n for n in names if n][:10]
    if not names or not settings.provider_profile_links_enabled:
        return {}
    listing = "\n".join(f"- {n}" for n in names)
    parsed = await _search(
        f"For each of these businesses near zip code {zip_code}, search the web "
        "and give the URL of their Yelp, Facebook, Instagram, and Nextdoor "
        "business profile pages ONLY if such a page appears in the search "
        "results. Use an empty string for any you did not actually see. Do "
        "not guess URLs. Do not report ratings or follower counts.\n" + listing,
        _PROFILE_LINKS_SCHEMA,
        max_uses=min(5, len(names)),
        timeout=120.0,
        max_tokens=3000,
    )
    if not parsed:
        return {}
    wanted = {" ".join(n.lower().split()): n for n in names}
    out: dict[str, dict[str, str]] = {}
    for entry in parsed.get("businesses") or []:
        name = wanted.get(" ".join(str(entry.get("name") or "").lower().split()))
        if not name:
            continue
        links: dict[str, str] = {}
        for platform, field in (
            ("yelp", "yelpUrl"),
            ("facebook", "facebookUrl"),
            ("instagram", "instagramUrl"),
            ("nextdoor", "nextdoorUrl"),
        ):
            url = accept_profile_url(platform, entry.get(field, ""))
            if url:
                links[platform] = url
        if links:
            out[name] = links
    return out


async def discover_providers(service: str, zip_code: str) -> list[dict] | None:
    """The discovery pass behind LIDARAI_PROVIDER_DISCOVERY_ENABLED: Places
    numbers plus link-only profiles, cached for the Places-terms window,
    and merged into the provider table (``partners.attach_discovery``) so
    the ranking and the durable table see the same rows. Returns the
    candidates found, or None when discovery is off or produced nothing."""
    if not settings.provider_discovery_enabled or not service or not zip_code:
        return None
    from . import partners

    path = _slug(service, zip_code, "discovery")
    cached = read_json_cache(path, discovery_ttl_seconds())
    if cached and isinstance(cached.get("found"), list):
        found = cached["found"]
    else:
        found = await search_places(service, zip_code)
        if found is None:
            return None
        links = await lookup_profile_links([f["name"] for f in found], zip_code) if found else {}
        verified_at = _now_iso()
        for candidate in found:
            for platform, url in (links.get(candidate["name"]) or {}).items():
                candidate["presences"].append(
                    {
                        "platform": platform,
                        "profileUrl": url,
                        "rating": None,
                        "reviewCount": None,
                        "followerCount": None,
                        "source": PROFILE_LINK_SOURCE,
                        "lastVerifiedAt": verified_at,
                    }
                )
        write_json_cache(path, {"found": found, "fetchedAt": time.time()})
    if not found:
        return None
    # Idempotent: a cached pass re-merges without rewriting unchanged rows,
    # which is what makes the table right again after a wiped disk.
    partners.attach_discovery(found, service, zip_code, source="google_places")
    return found
