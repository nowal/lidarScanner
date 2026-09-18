"""TakeShape provider table for the provider finder (scope agreed Sep 1).

Partners, previous quoters, imported prospects, and businesses found by
discovery all live in one table. Every lead ranks the rows matching its
trade and zip (``rank_for_lead``); web research only fills the gap
(local_research.lookup_provider_leads / discover_providers).

Durable home: the ``flow_partners`` Supabase table (migration
``20260909_flow_partners.sql``), read Supabase-first with the local file as
the fallback -- the same order as flow state and quote requests. The local
file ``{storage_dir}/partners.json`` is always written (fast cache, and the
only copy when Supabase is unconfigured); the labeled sample seed at
``demo_assets/partners_seed.json`` is the last resort so no email ever
presents a fake company as a real partner.

Row shape (``record`` in the table)::

    {
      "name": "...", "serviceTypes": ["Painting"], "zips": ["37203"],
      "zipPrefixes": ["372"], "contactName": "...", "phone": "...",
      "email": "...", "website": "...", "ratingLabel": "4.8 stars (52 Google reviews)",
      "notes": "...", "sample": true,
      "relationship": "partner | prospect | quoted",   # see relationship()
      "source": "returned_quote | sheet:<name> | google_places | ...",
      "quotedCount": 2, "lastQuotedAt": "...",           # written by note_quoted
      "presences": [                                     # written by discovery
        {"platform": "google", "profileUrl": "...", "rating": 4.7,
         "reviewCount": 128, "followerCount": null,
         "source": "google_places_api", "lastVerifiedAt": "2026-09-09T12:00:00+00:00"}
      ]
    }

Every presence number is nullable and null means "not known", never zero.
A discovery pass never touches what note_quoted wrote: a provider that has
actually quoted is a stronger signal than any social metric.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

from ..config import settings

logger = logging.getLogger("lidarai.flow.partners")

_SEED_RELATIVE = Path(__file__).resolve().parent.parent.parent / "demo_assets" / "partners_seed.json"

# Fields note_quoted owns. Discovery and imports may fill them when empty
# but never overwrite them.
QUOTE_OWNED_FIELDS = ("relationship", "quotedCount", "lastQuotedAt", "providerId")


def _rows_path() -> Path:
    return Path(settings.storage_dir) / "partners.json"


def _read_file(path: Path) -> list[dict[str, Any]] | None:
    try:
        if path.exists():
            rows = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(rows, list):
                return rows
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not read partner table %s: %s", path, exc)
    return None


def _load_rows() -> list[dict[str, Any]]:
    """Local file, then the labeled seed. The durable copy is pulled into
    the local file by ``rehydrate`` (startup and every lead email), so the
    synchronous readers below see it without an await."""
    for path in (_rows_path(), _SEED_RELATIVE):
        rows = _read_file(path)
        if rows is not None:
            return rows
    return []


RELATIONSHIPS = ("partner", "prospect", "quoted")


def relationship(row: dict[str, Any]) -> str:
    """What a row is allowed to be called in front of a homeowner or Quintin.

    ``partner``  a company TakeShape has an actual relationship with. Only rows
                 that say so, or hand-entered rows with no ``source`` field
                 (the original table format), count.
    ``quoted``   a company that has returned a real quote through this system
                 (written by ``record_quoted_provider``). Known to answer, not
                 a partner.
    ``prospect`` anything imported from a list -- the scraped sheet, an LLM
                 search, a CSV, a discovery pass. A row with a ``source`` and
                 no explicit relationship lands here, never as a partner,
                 because the ops email says "TakeShape partners" and that
                 word must never point at a stranger.
    """
    value = str(row.get("relationship") or "").strip().lower()
    if value in RELATIONSHIPS:
        return value
    return "prospect" if row.get("source") else "partner"


def _matches(service: str | None, zip_code: str | None) -> list[dict[str, Any]]:
    """Rows serving this service + zip, best match first (exact zip before
    prefix). Missing service/zip matches nothing -- a lead package should
    never suggest a company we can't place."""
    if not service or not zip_code:
        return []
    # Defence in depth: records written before service-type normalization
    # (and any future free-text leak) still match a partner rather than
    # silently returning nothing.
    from ..home_guide_tools import normalize_service_type

    service_l = (normalize_service_type(service) or service).strip().lower()
    zip5 = zip_code.strip()[:5]
    exact: list[dict[str, Any]] = []
    prefix: list[dict[str, Any]] = []
    for row in _load_rows():
        # The seed's sample rows exist so the local file is never empty. They
        # are placeholders, not companies, and they were reaching Quintin's
        # lead email as a "TakeShape partner" with a caption (Sep 13). A
        # caption is not a substitute for omission: nothing downstream of
        # here may ever see one.
        if row.get("sample"):
            continue
        services = [str(s).lower() for s in row.get("serviceTypes", [])]
        if service_l not in services:
            continue
        if zip5 in [str(z)[:5] for z in row.get("zips", [])]:
            exact.append(row)
        elif any(zip5.startswith(str(p)[:3]) for p in row.get("zipPrefixes", [])):
            prefix.append(row)
    return exact + prefix


def find_partners(service: str | None, zip_code: str | None) -> list[dict[str, Any]]:
    """Partners only. Prospects and previous quoters come from ``find_prospects``
    so the two can never be confused in the email."""
    return [row for row in _matches(service, zip_code) if relationship(row) == "partner"][:5]


def find_prospects(service: str | None, zip_code: str | None) -> list[dict[str, Any]]:
    """Companies that are NOT partners but are worth a call: previous quoters
    first (most quotes first), then imported prospects."""
    rows = [row for row in _matches(service, zip_code) if relationship(row) != "partner"]
    rows.sort(key=lambda r: (relationship(r) != "quoted", -int(r.get("quotedCount") or 0)))
    return rows[:5]


def candidates(service: str | None, zip_code: str | None) -> list[dict[str, Any]]:
    """Every matching row regardless of relationship -- the local candidate
    set the ranking scores within."""
    return _matches(service, zip_code)


def coverage_gap(
    partners: list[dict], prospects: list[dict] | None, researched: list[dict] | None
) -> bool:
    """True when nobody at all can be suggested for this lead. Ops should hear
    that up front rather than discover it after three unanswered calls."""
    return not partners and not (prospects or []) and not (researched or [])


# ------------------------------------------------------------------ ranking
def rank_for_lead(
    service: str | None,
    zip_code: str | None,
    researched: list[dict[str, Any]] | None = None,
    *,
    limit: int = 8,
) -> list[dict[str, Any]]:
    """Ranked suggestions for one lead: the table rows serving this trade and
    zip plus any web-researched leads not already in the table, scored by
    ``provider_ranking`` with the configured weights. Never raises -- an
    unrankable set is an empty list, and the email falls back to its
    grouped section."""
    try:
        from .provider_ranking import RankingWeights, rank_candidates

        pool: list[dict[str, Any]] = [
            expire_stale_presences(dict(r, relationship=relationship(r))) for r in candidates(service, zip_code)
        ]
        known = {_key(r.get("name")) for r in pool}
        for lead in researched or []:
            name = str(lead.get("name") or "").strip()
            if not name or _key(name) in known:
                continue
            # Research drops these at the source; this catches a lead that
            # arrived by any other route. Operations cannot act on a name
            # with nothing to call or click, so it is not a suggestion.
            if not str(lead.get("phone") or "").strip() and not str(lead.get("website") or "").strip():
                continue
            pool.append(dict(lead, relationship="researched", presences=lead.get("presences") or []))
            known.add(_key(name))
        legacy = bool(settings.preferred_partner_ordering_enabled)
        if legacy:
            _warn_legacy_ordering()
        ranked = rank_candidates(pool, RankingWeights.from_settings(), legacy_preferred_ordering=legacy)
        # The cap trims strangers, never a partner or a company that has
        # quoted: those rows are why the list exists (review, Sep 10).
        head = ranked[:limit]
        head += [e for e in ranked[limit:] if e.get("relationship") in ("partner", "quoted")]
        return head
    except Exception as exc:  # noqa: BLE001
        logger.warning("Provider ranking failed for %s/%s: %s", service, zip_code, exc)
        return []


_legacy_warned = False


def _warn_legacy_ordering() -> None:
    global _legacy_warned
    if not _legacy_warned:
        logger.warning(
            "LIDARAI_PREFERRED_PARTNER_ORDERING_ENABLED is deprecated: providers are "
            "ordered by relationship instead of the ranked score. The note_quoted "
            "promotion it carried is the ranked list's quotedBoost term."
        )
        _legacy_warned = True


# ------------------------------------------------------------------ writes
def _write_local(rows: list[dict[str, Any]]) -> Path:
    """The local file. Sample seed rows are dropped once any real row exists:
    the seed is a display fallback for an empty table, never data."""
    path = _rows_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    real = [r for r in rows if not r.get("sample")]
    path.write_text(json.dumps(real if real else rows, indent=2), encoding="utf-8")
    return path


def _write_rows(rows: list[dict[str, Any]], changed: list[dict[str, Any]] | None = None) -> Path:
    """Local file always; the durable copy for ``changed`` rows (every row
    when None) fire-and-forget, like the home index. Changed rows are
    stamped so a later merge can tell which copy is newer."""
    stamp = _now_iso()
    for row in (rows if changed is None else changed):
        row["updatedAt"] = stamp
    path = _write_local(rows)
    _durable_write(rows if changed is None else changed)
    return path


def _entries(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Durable upsert entries. Sample seed rows never go up (review, Sep 10)."""
    entries = []
    for row in rows:
        name = str(row.get("name") or "").strip()
        if not name or row.get("sample"):
            continue
        entries.append({"key": _key(name), "name": name, "relationship": relationship(row), "record": row})
    return entries


_pending: set[asyncio.Task] = set()


def _durable_write(rows: list[dict[str, Any]]) -> None:
    from . import supabase_store

    if not supabase_store.enabled():
        return
    entries = _entries(rows)
    if not entries:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        asyncio.run(supabase_store.upsert_partner_rows(entries))
        return
    task = loop.create_task(supabase_store.upsert_partner_rows(entries))
    _pending.add(task)
    task.add_done_callback(_pending.discard)


async def flush_durable_writes() -> None:
    """Wait for in-flight durable writes (tests, and a graceful shutdown)."""
    if _pending:
        await asyncio.gather(*list(_pending), return_exceptions=True)


async def rehydrate() -> str:
    """Bring the local file and the durable table into agreement so the
    synchronous readers see the truth. Read order Supabase -> local -> seed,
    matching flow state and quote requests.

    Merge by key, not replace (review, Sep 10): durable writes are
    fire-and-forget and can fail, so for a key both copies hold the newer
    ``updatedAt`` wins, and a real row that exists only locally is pushed up
    rather than wiped. Sample seed rows never go up. Returns:

    ``"unavailable"``  Supabase unset or unreachable; the local copy stands.
    ``"rehydrated"``   the merged rows are on disk (and any local-newer or
                       local-only rows were pushed up).
    ``"bootstrapped"`` the table was empty and the local file had real rows.
    ``"empty"``        nothing durable and nothing local beyond the seed.
    """
    from . import supabase_store

    if not supabase_store.enabled():
        return "unavailable"
    await flush_durable_writes()
    durable = await supabase_store.list_partner_rows()
    if durable is None:
        return "unavailable"
    local = [r for r in (_read_file(_rows_path()) or []) if not r.get("sample")]
    merged: dict[str, dict[str, Any]] = {_key(r.get("name")): r for r in durable if str(r.get("name") or "").strip()}
    push: list[dict[str, Any]] = []
    for row in local:
        key = _key(row.get("name"))
        if not key:
            continue
        theirs = merged.get(key)
        if theirs is None or str(row.get("updatedAt") or "") > str(theirs.get("updatedAt") or ""):
            merged[key] = row
            push.append(row)
    rows = list(merged.values())
    if not rows:
        return "empty"
    _write_local(rows)
    if push:
        ok = await supabase_store.upsert_partner_rows(_entries(push))
        logger.info("Pushed %d local provider row(s) newer than or absent from the durable table: %s", len(push), ok)
        if not durable:
            return "bootstrapped" if ok else "unavailable"
    logger.info("Rehydrated %d provider row(s) (durable %d, local %d)", len(rows), len(durable), len(local))
    return "rehydrated"


def _key(name: str) -> str:
    return " ".join(str(name or "").lower().split())


def profile_for_name(name: str | None) -> dict[str, Any] | None:
    """What the table knows about a company, for the homeowner: relationship,
    the rating label, the Google presence and the website. None when the
    name is not on file. Quintin asked "how are they on Google?" about a
    returned quote and got "I don't have access" (Sep 16, #98) -- this is
    what the agent has access to."""
    key = _key(name)
    if not key:
        return None
    for row in _load_rows():
        if _key(row.get("name")) != key:
            continue
        google = next(
            (p for p in row.get("presences") or [] if str(p.get("platform", "")).lower() == "google"),
            None,
        )
        profile = {
            "relationship": relationship(row),
            "ratingLabel": row.get("ratingLabel") or None,
            "website": row.get("website") or None,
            "quotedCount": int(row.get("quotedCount") or 0) or None,
            "googleRating": (google or {}).get("rating"),
            "googleReviewCount": (google or {}).get("reviewCount"),
            "googleProfileUrl": (google or {}).get("profileUrl"),
        }
        return {k: v for k, v in profile.items() if v is not None}
    return None


def import_prospects(rows: list[dict[str, Any]], source: str) -> int:
    """Load a list of companies (the Google Sheet, a scrape) as PROSPECTS.

    The relationship is forced: whatever the rows say, nothing imported here
    becomes a partner. Duplicates by name are skipped. Returns how many were
    added. Partner status is granted only by editing the row by hand or by
    the future TakeShape partner table.
    """
    if not source:
        raise ValueError("import_prospects needs a source label")
    existing = _load_rows()
    known = {_key(r.get("name")) for r in existing}
    added_rows: list[dict[str, Any]] = []
    for row in rows:
        name = str(row.get("name") or "").strip()
        if not name or _key(name) in known:
            continue
        clean = dict(row)
        clean["name"] = name[:200]
        clean["relationship"] = "prospect"
        clean["source"] = source
        clean.pop("sample", None)
        existing.append(clean)
        added_rows.append(clean)
        known.add(_key(name))
    if added_rows:
        _write_rows(existing, added_rows)
        logger.info("Imported %d prospect(s) from %s", len(added_rows), source)
    return len(added_rows)


def record_quoted_provider(
    name: str,
    service: str | None,
    zip_code: str | None,
    *,
    provider_id: str | None = None,
    contact: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """A company returned a real quote: remember it, with the trade and zip.

    This is how the provider list grows out of ordinary work instead of
    staying a spreadsheet. A new company is stored as ``quoted`` -- known to
    answer, still not a partner. An existing partner keeps its status and
    gains the count. Never raises: the quote upload must not fail because a
    JSON file could not be written.
    """
    name = str(name or "").strip()
    if not name:
        return None
    try:
        from ..home_guide_tools import normalize_service_type

        service_c = (normalize_service_type(service) or (service or "")).strip()
        zip5 = (zip_code or "").strip()[:5]
        rows = _load_rows()
        row = next((r for r in rows if _key(r.get("name")) == _key(name)), None)
        if row is None:
            row = {
                "name": name[:200],
                "serviceTypes": [],
                "zips": [],
                "zipPrefixes": [],
                "relationship": "quoted",
                "source": "returned_quote",
            }
            rows.append(row)
        if service_c and service_c not in row.setdefault("serviceTypes", []):
            row["serviceTypes"].append(service_c)
        if zip5 and zip5 not in [str(z) for z in row.setdefault("zips", [])]:
            row["zips"].append(zip5)
        if zip5 and zip5[:3] not in [str(p) for p in row.setdefault("zipPrefixes", [])]:
            row["zipPrefixes"].append(zip5[:3])
        if provider_id and not row.get("providerId"):
            row["providerId"] = provider_id
        for field in ("contactName", "phone", "email", "website"):
            if contact and contact.get(field) and not row.get(field):
                row[field] = str(contact[field])[:200]
        row["quotedCount"] = int(row.get("quotedCount") or 0) + 1
        row["lastQuotedAt"] = _now_iso()
        _write_rows(rows, [row])
        logger.info("Recorded quote from %s (%s, %s): %s", name, service_c or "?", zip5 or "?",
                    relationship(row))
        return row
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not record quoted provider %s: %s", name, exc)
        return None


def note_quoted(record: Any, quotes: list[Any]) -> int:
    """Call after quotes land on a request (any path: API, entry page, email
    reply). Illustrative estimates are ignored. Returns how many were noted."""
    noted = 0
    for quote in quotes:
        if getattr(quote, "isEstimate", False):
            continue
        if record_quoted_provider(
            getattr(quote, "providerName", ""),
            getattr(record, "serviceType", None),
            getattr(record, "zip", None),
            provider_id=getattr(quote, "providerId", None),
        ):
            noted += 1
    return noted


# --------------------------------------------------------------- presences
PRESENCE_FIELDS = ("platform", "profileUrl", "rating", "reviewCount", "followerCount", "source", "lastVerifiedAt")
# Sources whose numbers may only be held for the configured window
# (Google Places terms: 30 days for anything but the place id).
EXPIRING_SOURCES = ("google_places_api",)


def presence_expired(presence: dict[str, Any], *, ttl_days: int | None = None, now: str | None = None) -> bool:
    """True when a presence from an expiring source is older than the TTL
    (or carries no timestamp at all)."""
    if str(presence.get("source") or "") not in EXPIRING_SOURCES:
        return False
    ttl = int(settings.provider_discovery_ttl_days if ttl_days is None else ttl_days)
    from datetime import datetime, timedelta, timezone

    raw = str(presence.get("lastVerifiedAt") or "")
    try:
        verified = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if verified.tzinfo is None:
            verified = verified.replace(tzinfo=timezone.utc)
    except ValueError:
        return True
    current = datetime.fromisoformat(now) if now else datetime.now(timezone.utc)
    return current - verified > timedelta(days=max(ttl, 1))


def expire_stale_presences(row: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
    """A copy of ``row`` whose expired Places numbers are null (the link and
    place id stay). Applied at read time so a number is never ranked or
    shown past the window, whatever the discovery cache did (review, Sep 10)."""
    presences = []
    for presence in row.get("presences") or []:
        if not isinstance(presence, dict):
            continue
        copy = dict(presence)
        if presence_expired(copy, **kwargs):
            copy.update({"rating": None, "reviewCount": None, "followerCount": None, "expired": True})
        presences.append(copy)
    return dict(row, presences=presences)


def normalize_presence(presence: dict[str, Any]) -> dict[str, Any] | None:
    """One platform presence in the stored shape. Numbers are kept nullable:
    an absent count is None, never 0, and a 0 is kept only when the source
    actually said so."""
    platform = str(presence.get("platform") or "").strip().lower()
    if not platform:
        return None

    def num(value: Any, cast: type) -> Any:
        if value is None or isinstance(value, bool) or value == "":
            return None
        try:
            return cast(value)
        except (TypeError, ValueError):
            return None

    clean: dict[str, Any] = {
        "platform": platform,
        "profileUrl": (str(presence.get("profileUrl") or "").strip()[:300] or None),
        "rating": num(presence.get("rating"), float),
        "reviewCount": num(presence.get("reviewCount"), int),
        "followerCount": num(presence.get("followerCount"), int),
        "source": str(presence.get("source") or "unknown")[:80],
        "lastVerifiedAt": str(presence.get("lastVerifiedAt") or _now_iso()),
    }
    for extra in ("placeId",):
        if presence.get(extra):
            clean[extra] = str(presence[extra])[:200]
    return clean


def merge_presence(row: dict[str, Any], presence: dict[str, Any]) -> bool:
    """Attach or refresh one platform's presence on a row. The newer
    verification wins per field; a field the new record does not know keeps
    the old value (a link found last month is not erased by a count fetched
    today). Returns True when the row changed."""
    clean = normalize_presence(presence)
    if clean is None:
        return False
    presences = row.setdefault("presences", [])
    existing = next((p for p in presences if str(p.get("platform", "")).lower() == clean["platform"]), None)
    if existing is None:
        presences.append(clean)
        return True
    before = json.dumps(existing, sort_keys=True)
    for field in PRESENCE_FIELDS:
        value = clean.get(field)
        if value is not None:
            existing[field] = value
    for extra in ("placeId",):
        if clean.get(extra):
            existing[extra] = clean[extra]
    return json.dumps(existing, sort_keys=True) != before


def attach_discovery(
    found: list[dict[str, Any]],
    service: str | None,
    zip_code: str | None,
    *,
    source: str,
) -> int:
    """Merge a discovery pass into the table. A business already in the
    table (by name) gains presences and any empty contact field; a new one
    joins as a ``prospect`` with this ``source`` -- never a partner, and
    never touching a quoted row's note_quoted fields. Returns how many rows
    changed. Never raises: discovery must not break the lead email."""
    if not source:
        raise ValueError("attach_discovery needs a source label")
    try:
        from ..home_guide_tools import normalize_service_type

        service_c = (normalize_service_type(service) or (service or "")).strip()
        zip5 = (zip_code or "").strip()[:5]
        rows = _load_rows()
        by_key = {_key(r.get("name")): r for r in rows}
        changed: list[dict[str, Any]] = []
        for item in found:
            name = str(item.get("name") or "").strip()
            if not name:
                continue
            row = by_key.get(_key(name))
            touched = False
            if row is None:
                row = {
                    "name": name[:200],
                    "serviceTypes": [],
                    "zips": [],
                    "zipPrefixes": [],
                    "relationship": "prospect",
                    "source": source,
                }
                rows.append(row)
                by_key[_key(name)] = row
                touched = True
            if service_c and service_c not in row.setdefault("serviceTypes", []):
                row["serviceTypes"].append(service_c)
                touched = True
            if zip5 and zip5 not in [str(z) for z in row.setdefault("zips", [])]:
                row["zips"].append(zip5)
                touched = True
            if zip5 and zip5[:3] not in [str(p) for p in row.setdefault("zipPrefixes", [])]:
                row["zipPrefixes"].append(zip5[:3])
                touched = True
            for field in ("phone", "website", "address"):
                if item.get(field) and not row.get(field):
                    row[field] = str(item[field])[:200]
                    touched = True
            for presence in item.get("presences") or []:
                if merge_presence(row, presence):
                    touched = True
            if touched:
                changed.append(row)
        if changed:
            _write_rows(rows, changed)
            logger.info("Discovery (%s) updated %d provider row(s) for %s/%s",
                        source, len(changed), service_c or "?", zip5 or "?")
        return len(changed)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not attach discovery results (%s): %s", source, exc)
        return 0


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="seconds")
