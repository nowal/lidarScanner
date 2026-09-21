"""Supabase persistence for flow state, the wording journal, and quote
requests (migration ``20260826_flow_agent_persistence.sql``).

Render's disk is ephemeral; this is what makes the SOW §4 logging
deliverable and quote lifecycle survive deploys. Writes go through
PostgREST with the service-role key (the tables accept no client role).

Failure philosophy, mirroring the flow design: persistence failures are
loud in logs but never break a homeowner's turn — the signed flow token
and local JSONL remain as fallbacks. When the settings are unset
(``LIDARAI_SUPABASE_URL`` / ``LIDARAI_SUPABASE_SERVICE_ROLE_KEY``), every
call is a cheap no-op and the backend behaves exactly as before.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

import httpx

from ..config import settings
from .state import FlowState

logger = logging.getLogger("lidarai.flow.supabase")

_client: httpx.AsyncClient | None = None
_client_key: tuple[str, str] | None = None


def enabled() -> bool:
    return bool(settings.supabase_url and settings.supabase_service_role_key)


def _rest() -> httpx.AsyncClient:
    """Shared async client, rebuilt if settings change (tests do this)."""
    global _client, _client_key
    key = (settings.supabase_url, settings.supabase_service_role_key)
    if _client is None or _client_key != key or _client.is_closed:
        _client = httpx.AsyncClient(
            base_url=f"{settings.supabase_url.rstrip('/')}/rest/v1",
            headers={
                "apikey": settings.supabase_service_role_key,
                "Authorization": f"Bearer {settings.supabase_service_role_key}",
                "Content-Type": "application/json",
            },
            timeout=httpx.Timeout(8.0),
        )
        _client_key = key
    return _client


# ---------------------------------------------------------------- identity
_homeowner_cache: dict[str, tuple[float, dict[str, Any] | None]] = {}
_HOMEOWNER_CACHE_TTL_SECONDS = 300.0


def invalidate_homeowner(auth_user_id: str) -> None:
    """Fetch newly confirmed contact details on the first turn after signup."""
    _homeowner_cache.pop(auth_user_id, None)


async def resolve_homeowner(auth_user_id: str) -> dict[str, Any] | None:
    """Map a verified Supabase auth user id (the JWT ``sub``) to its
    ``homeowners`` row.

    The flow tables' ``homeowner_id`` columns reference ``homeowners(id)`` —
    a uuid generated independently of the auth id — so writing the raw sub
    there violates the foreign key and the row is silently dropped. Returns
    ``{"id", "full_name", "email", "phone"}`` or None (no row, or Supabase
    unconfigured/unreachable). Successful lookups (including a confirmed
    missing row) are cached; failures are not, so transient outages retry.
    """
    if not enabled() or not auth_user_id:
        return None
    cached = _homeowner_cache.get(auth_user_id)
    if cached and time.monotonic() - cached[0] < _HOMEOWNER_CACHE_TTL_SECONDS:
        return cached[1]
    try:
        resp = await _rest().get(
            "/homeowners",
            params={
                "auth_user_id": f"eq.{auth_user_id}",
                "select": "id,full_name,email,phone",
            },
        )
        resp.raise_for_status()
        rows = resp.json()
        row = rows[0] if rows else None
    except Exception as exc:  # noqa: BLE001
        logger.warning("Supabase homeowner lookup failed for %s: %s", auth_user_id, exc)
        return None
    _homeowner_cache[auth_user_id] = (time.monotonic(), row)
    return row


# ------------------------------------------------------------------ flow state
async def get_flow_state(thread_id: str) -> FlowState | None:
    if not enabled():
        return None
    try:
        resp = await _rest().get(
            "/flow_states", params={"thread_id": f"eq.{thread_id}", "select": "state"}
        )
        resp.raise_for_status()
        rows = resp.json()
        if rows:
            return FlowState.model_validate(rows[0]["state"])
    except Exception as exc:  # noqa: BLE001
        logger.warning("Supabase flow_state read failed for %s: %s", thread_id, exc)
    return None


async def upsert_flow_state(state: FlowState) -> None:
    if not enabled() or not state.thread_id:
        return
    try:
        resp = await _rest().post(
            "/flow_states",
            headers={"Prefer": "resolution=merge-duplicates"},
            json={
                "thread_id": state.thread_id,
                "homeowner_id": state.homeowner_id,
                "state": state.model_dump(mode="json"),
                "updated_at": "now()",
            },
        )
        resp.raise_for_status()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Supabase flow_state write failed for %s: %s", state.thread_id, exc)


# --------------------------------------------------------------------- journal
async def insert_journal(record: dict[str, Any]) -> None:
    """``record`` is the masked journal line from ``flow.journal.write_turn``."""
    if not enabled():
        return
    try:
        resp = await _rest().post(
            "/flow_journal",
            json={
                "thread_id": record.get("threadId") or "unknown",
                "homeowner_id": record.get("homeownerId"),
                "kind": record.get("kind", "chat"),
                "step": record.get("step"),
                "record": record,
            },
        )
        resp.raise_for_status()
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Supabase journal write failed for %s: %s", record.get("threadId"), exc
        )


async def list_journal_user_texts(thread_id: str, limit: int = 50) -> list[str] | None:
    """The masked homeowner-side lines of a thread's journal, oldest first —
    the durable source for the ops synopsis when the local JSONL didn't
    survive a redeploy. None means the store is unavailable."""
    if not enabled():
        return None
    try:
        resp = await _rest().get(
            "/flow_journal",
            params={
                "thread_id": f"eq.{thread_id}",
                "select": "record",
                "order": "created_at.asc",
                "limit": str(limit),
            },
        )
        resp.raise_for_status()
        texts = []
        for row in resp.json():
            text = (row.get("record") or {}).get("userText")
            if text:
                texts.append(text)
        return texts
    except Exception as exc:  # noqa: BLE001
        logger.warning("Supabase journal read failed for %s: %s", thread_id, exc)
        return None


# ------------------------------------------------------------------- storage
async def ensure_model_asset(job_id: str, file_path: Path) -> str | None:
    """Upload a completed model artifact to Supabase Storage (idempotent) and
    return a 30-day signed URL, or None when storage is unavailable.

    The processor's own artifact route requires the app service token and its
    files die with the instance disk — a lead package needs a link ops can
    open days later with no credentials (SOW §2 step 9)."""
    if not enabled():
        return None
    bucket = "home-assets"
    object_path = f"flow-models/{job_id}.usdz"
    signed = await _signed_storage_url(bucket, object_path)
    if signed:
        return signed
    try:
        data = file_path.read_bytes()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not read model artifact %s: %s", file_path, exc)
        return None
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(120.0)) as client:
            resp = await client.post(
                f"{settings.supabase_url.rstrip('/')}/storage/v1/object/{bucket}/{object_path}",
                headers={
                    "apikey": settings.supabase_service_role_key,
                    "Authorization": f"Bearer {settings.supabase_service_role_key}",
                    "Content-Type": "model/vnd.usdz+zip",
                    "x-upsert": "true",
                },
                content=data,
            )
            resp.raise_for_status()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Supabase model upload failed for job %s: %s", job_id, exc)
        return None
    return await _signed_storage_url(bucket, object_path)


async def upload_bytes(
    bucket: str, object_path: str, data: bytes, content_type: str
) -> str | None:
    """Put ``data`` in storage and return a signed URL for it, or None when
    storage is off or the upload fails. Never raises: a document that will
    not upload must not cost operations the quote it came with."""
    if not enabled():
        return None
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(120.0)) as client:
            resp = await client.post(
                f"{settings.supabase_url.rstrip('/')}/storage/v1/object/{bucket}/{object_path}",
                headers={
                    "apikey": settings.supabase_service_role_key,
                    "Authorization": f"Bearer {settings.supabase_service_role_key}",
                    "Content-Type": content_type,
                    "x-upsert": "true",
                },
                content=data,
            )
            resp.raise_for_status()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Storage upload failed for %s/%s: %s", bucket, object_path, exc)
        return None
    return await _signed_storage_url(bucket, object_path)


async def download_object(bucket: str, object_path: str, dest: Path) -> bool:
    """Stream a storage object to ``dest`` with the service role. False when
    storage is off or the object is missing; never raises."""
    if not enabled():
        return False
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        async with httpx.AsyncClient(timeout=httpx.Timeout(600.0)) as client:
            async with client.stream(
                "GET",
                f"{settings.supabase_url.rstrip('/')}/storage/v1/object/{bucket}/{object_path}",
                headers={
                    "apikey": settings.supabase_service_role_key,
                    "Authorization": f"Bearer {settings.supabase_service_role_key}",
                },
            ) as resp:
                if resp.status_code in (400, 404):
                    logger.warning("Storage object %s/%s not found", bucket, object_path)
                    return False
                resp.raise_for_status()
                with dest.open("wb") as out:
                    async for chunk in resp.aiter_bytes():
                        out.write(chunk)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("Storage download failed for %s/%s: %s", bucket, object_path, exc)
        return False


async def _signed_storage_url(bucket: str, object_path: str, expires_in: int = 30 * 86400) -> str | None:
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(15.0)) as client:
            resp = await client.post(
                f"{settings.supabase_url.rstrip('/')}/storage/v1/object/sign/{bucket}/{object_path}",
                headers={
                    "apikey": settings.supabase_service_role_key,
                    "Authorization": f"Bearer {settings.supabase_service_role_key}",
                    "Content-Type": "application/json",
                },
                json={"expiresIn": expires_in},
            )
            if resp.status_code == 400 or resp.status_code == 404:
                return None  # object doesn't exist yet
            resp.raise_for_status()
            signed_path = (resp.json() or {}).get("signedURL")
            if signed_path:
                return f"{settings.supabase_url.rstrip('/')}/storage/v1{signed_path}"
    except Exception as exc:  # noqa: BLE001
        logger.warning("Supabase sign failed for %s/%s: %s", bucket, object_path, exc)
    return None


# ------------------------------------------------------------- home indexes
# A resolved whole-home index is small (tens of KB) but the host's disk is
# ephemeral: every redeploy wiped it, and the demo answered "that home has
# not been ingested" until it was uploaded again. Storage, not a table, so
# this needs no migration — the object is a plain JSON document.
_HOME_BUCKET = "home-assets"


def _home_object(home_id: str) -> str:
    return f"home-indexes/{home_id}.json"


async def put_home_index(home_id: str, payload: dict) -> bool:
    if not enabled():
        return False
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as client:
            resp = await client.post(
                f"{settings.supabase_url.rstrip('/')}/storage/v1/object/"
                f"{_HOME_BUCKET}/{_home_object(home_id)}",
                headers={
                    "apikey": settings.supabase_service_role_key,
                    "Authorization": f"Bearer {settings.supabase_service_role_key}",
                    "Content-Type": "application/json",
                    "x-upsert": "true",
                },
                content=json.dumps(payload).encode("utf-8"),
            )
            resp.raise_for_status()
            return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("Supabase home-index upload failed for %s: %s", home_id, exc)
        return False


async def get_home_index(home_id: str) -> dict | None:
    if not enabled():
        return None
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as client:
            resp = await client.get(
                f"{settings.supabase_url.rstrip('/')}/storage/v1/object/"
                f"{_HOME_BUCKET}/{_home_object(home_id)}",
                headers={
                    "apikey": settings.supabase_service_role_key,
                    "Authorization": f"Bearer {settings.supabase_service_role_key}",
                },
            )
            if resp.status_code in (400, 404):
                return None
            resp.raise_for_status()
            return resp.json()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Supabase home-index read failed for %s: %s", home_id, exc)
        return None


async def list_home_indexes() -> list[str] | None:
    """Home ids with a durable index (``home-indexes/<id>.json``), or None
    when storage is unavailable."""
    if not enabled():
        return None
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as client:
            resp = await client.post(
                f"{settings.supabase_url.rstrip('/')}/storage/v1/object/list/{_HOME_BUCKET}",
                headers={
                    "apikey": settings.supabase_service_role_key,
                    "Authorization": f"Bearer {settings.supabase_service_role_key}",
                    "Content-Type": "application/json",
                },
                json={"prefix": "home-indexes", "limit": 1000},
            )
            if resp.status_code >= 400:
                return None
            return sorted(
                o["name"][: -len(".json")]
                for o in (resp.json() or [])
                if isinstance(o.get("name"), str) and o["name"].endswith(".json")
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Supabase home-index list failed: %s", exc)
        return None


async def delete_home_index(home_id: str) -> bool:
    """Homeowner deletion path (SOW §12) — the durable copy must go too."""
    if not enabled():
        return False
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as client:
            resp = await client.delete(
                f"{settings.supabase_url.rstrip('/')}/storage/v1/object/"
                f"{_HOME_BUCKET}/{_home_object(home_id)}",
                headers={
                    "apikey": settings.supabase_service_role_key,
                    "Authorization": f"Bearer {settings.supabase_service_role_key}",
                },
            )
            return resp.status_code in (200, 204, 404)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Supabase home-index delete failed for %s: %s", home_id, exc)
        return False


# ---------------------------------------------------------------- home models
# The phone's textured bake for each area (``rooms/room-N/model.usdz``) is the
# 3D model a provider should see in the lead package (SOW section 2 step 9).
# The export is gone after ingest, so the copy that outlives it is this one.
def home_model_object(home_id: str, key: str) -> str:
    return f"home-models/{home_id}/{key}.usdz"


async def put_home_model(home_id: str, key: str, file_path: Path) -> bool:
    """Upload one baked model (idempotent). ``key`` is the room key, or
    ``home`` for the whole-home bake. False, with a warning, on any failure."""
    if not enabled():
        return False
    try:
        data = file_path.read_bytes()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not read model %s for home %s: %s", file_path, home_id, exc)
        return False
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(300.0)) as client:
            resp = await client.post(
                f"{settings.supabase_url.rstrip('/')}/storage/v1/object/"
                f"{_HOME_BUCKET}/{home_model_object(home_id, key)}",
                headers={
                    "apikey": settings.supabase_service_role_key,
                    "Authorization": f"Bearer {settings.supabase_service_role_key}",
                    "Content-Type": "model/vnd.usdz+zip",
                    "x-upsert": "true",
                },
                content=data,
            )
            if resp.status_code >= 400:
                logger.warning("Supabase model upload failed for %s/%s: HTTP %s %s",
                               home_id, key, resp.status_code, resp.text[:200])
                return False
            return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("Supabase model upload failed for %s/%s: %s", home_id, key, exc)
        return False


async def sign_home_model(object_path: str) -> str | None:
    """A 30-day signed URL for a stored model, or None when it is not there."""
    if not enabled():
        return None
    return await _signed_storage_url(_HOME_BUCKET, object_path)


async def delete_home_models(home_id: str) -> bool:
    """Remove every stored model for a home (deletion path, SOW section 12)."""
    if not enabled():
        return False
    prefix = f"home-models/{home_id}"
    headers = {
        "apikey": settings.supabase_service_role_key,
        "Authorization": f"Bearer {settings.supabase_service_role_key}",
        "Content-Type": "application/json",
    }
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(60.0)) as client:
            listing = await client.post(
                f"{settings.supabase_url.rstrip('/')}/storage/v1/object/list/{_HOME_BUCKET}",
                headers=headers, json={"prefix": prefix, "limit": 1000},
            )
            if listing.status_code >= 400:
                return False
            names = [f"{prefix}/{o['name']}" for o in (listing.json() or []) if o.get("name")]
            if not names:
                return True
            resp = await client.request(
                "DELETE",
                f"{settings.supabase_url.rstrip('/')}/storage/v1/object/{_HOME_BUCKET}",
                headers=headers, json={"prefixes": names},
            )
            return resp.status_code in (200, 204)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Supabase model delete failed for %s: %s", home_id, exc)
        return False


# --------------------------------------------------------------- quote requests
async def upsert_quote_request(
    request_id: str,
    thread_id: str,
    homeowner_id: str | None,
    status: str,
    record: dict[str, Any],
) -> bool:
    if not enabled():
        return False
    try:
        resp = await _rest().post(
            "/flow_quote_requests",
            headers={"Prefer": "resolution=merge-duplicates"},
            json={
                "id": request_id,
                "thread_id": thread_id,
                "homeowner_id": homeowner_id,
                "status": status,
                "record": record,
                "updated_at": "now()",
            },
        )
        resp.raise_for_status()
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("Supabase quote_request write failed for %s: %s", request_id, exc)
        return False


async def get_quote_request(request_id: str) -> dict[str, Any] | None:
    if not enabled():
        return None
    try:
        resp = await _rest().get(
            "/flow_quote_requests",
            params={"id": f"eq.{request_id}", "select": "record"},
        )
        resp.raise_for_status()
        rows = resp.json()
        return rows[0]["record"] if rows else None
    except Exception as exc:  # noqa: BLE001
        logger.warning("Supabase quote_request read failed for %s: %s", request_id, exc)
        return None


async def list_quote_requests(status: str | None = None) -> list[dict[str, Any]] | None:
    """None means 'store unavailable' (caller falls back); [] means empty."""
    if not enabled():
        return None
    try:
        params: dict[str, str] = {"select": "record", "order": "created_at.asc"}
        if status:
            params["status"] = f"eq.{status}"
        resp = await _rest().get("/flow_quote_requests", params=params)
        resp.raise_for_status()
        return [row["record"] for row in resp.json()]
    except Exception as exc:  # noqa: BLE001
        logger.warning("Supabase quote_request list failed: %s", exc)
        return None


# ------------------------------------------------------------------ partners
# The provider table (partners, previous quoters, prospects, discovered
# businesses) used to live only in ``{storage}/partners.json`` and reset to
# the sample seed on every deploy — every note_quoted promotion was lost.
# One row per provider, keyed by the normalised name; the full row is the
# ``record`` document, the same shape partners.py reads and writes.
async def list_partner_rows() -> list[dict[str, Any]] | None:
    """Every provider row, or None when the store is unavailable (the
    caller keeps its local copy). An empty table is [] — distinct from None
    so a first deploy can bootstrap the table from the local file."""
    if not enabled():
        return None
    try:
        # A fresh client per call, like the storage helpers: partner writes
        # also run from synchronous callers via asyncio.run, and the shared
        # pooled client would be bound to a closed loop (review, Sep 10).
        async with _fresh_rest() as client:
            resp = await client.get(
                "/flow_partners",
                params={"select": "record", "order": "updated_at.asc", "limit": "5000"},
            )
        resp.raise_for_status()
        return [row["record"] for row in resp.json() if isinstance(row.get("record"), dict)]
    except Exception as exc:  # noqa: BLE001
        logger.warning("Supabase partner list failed: %s", exc)
        return None


async def upsert_partner_rows(entries: list[dict[str, Any]]) -> bool:
    """``entries`` are ``{"key", "name", "relationship", "record"}`` built by
    partners.py. Upsert on ``key`` so a re-recorded quote updates the row."""
    if not enabled() or not entries:
        return False
    try:
        async with _fresh_rest() as client:
            resp = await client.post(
                "/flow_partners",
                headers={"Prefer": "resolution=merge-duplicates"},
                json=[
                {
                    "key": e["key"],
                    "name": e["name"],
                    "relationship": e["relationship"],
                    "record": e["record"],
                    "updated_at": "now()",
                }
                for e in entries
            ],
            )
        resp.raise_for_status()
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("Supabase partner write failed (%d rows): %s", len(entries), exc)
        return False


def _fresh_rest() -> httpx.AsyncClient:
    """A per-call PostgREST client with the service-role headers. Tests that
    patch ``_client``/``_client_key`` keep working: the patched client is
    reused when present and open."""
    if _client is not None and not _client.is_closed and _client_key == (
        settings.supabase_url, settings.supabase_service_role_key
    ):
        return _NoCloseClient(_client)
    return httpx.AsyncClient(
        base_url=f"{settings.supabase_url.rstrip('/')}/rest/v1",
        headers={
            "apikey": settings.supabase_service_role_key,
            "Authorization": f"Bearer {settings.supabase_service_role_key}",
            "Content-Type": "application/json",
        },
        timeout=httpx.Timeout(8.0),
    )


class _NoCloseClient:
    """Async context wrapper that hands out an existing client without
    closing it on exit."""

    def __init__(self, client: httpx.AsyncClient):
        self._client = client

    async def __aenter__(self) -> httpx.AsyncClient:
        return self._client

    async def __aexit__(self, *exc: Any) -> None:
        return None
