"""Homeowner identity from the ``X-Homeowner-Token`` header
(API_CONTRACT_V1 §2).

Deployed projects validate access tokens with Supabase Auth, which handles
both asymmetric signing keys (ES256/RS256) and legacy HS256 secrets. Only
the verified Auth response supplies the identity. Legacy local verification
is retained for installations without a configured Supabase connection.
The returned identity is the Supabase
**auth user id** (``auth.users.id``). That is NOT the ``homeowners.id`` the
flow tables reference; ``flow_runtime._attach_identity`` resolves the sub to
the homeowners row via ``supabase_store.resolve_homeowner``.

Absent header or any verification failure → ``None`` —
chat degrades to today's anonymous behavior; endpoints that *require*
identity (quote submission) raise on ``None`` themselves.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import time
from uuid import UUID

import httpx

logger = logging.getLogger("lidarai.flow.identity")


async def resolve_homeowner_token(
    token: str | None, *, supabase_url: str, api_key: str, jwt_secret: str = ""
) -> str | None:
    """Validate against this project's Auth server without assuming a JWT algorithm."""
    if not token:
        return None
    if not (supabase_url and api_key):
        return verify_homeowner_token(token, jwt_secret)
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(
                f"{supabase_url.rstrip('/')}/auth/v1/user",
                headers={"apikey": api_key, "Authorization": f"Bearer {token}"},
            )
            response.raise_for_status()
            user = response.json()
        if not isinstance(user, dict) or not isinstance(user.get("id"), str):
            return None
        return str(UUID(user["id"]))
    except (httpx.HTTPError, ValueError, TypeError) as exc:
        # Never fall back to unverified claims or log the credential itself.
        logger.warning("Supabase homeowner verification failed (%s)", type(exc).__name__)
        return None


def _b64url_decode(segment: str) -> bytes:
    padding = "=" * (-len(segment) % 4)
    return base64.urlsafe_b64decode(segment + padding)


def verify_homeowner_token(token: str | None, jwt_secret: str) -> str | None:
    """Return the homeowner id (JWT ``sub``) or None."""
    if not token or not jwt_secret:
        return None
    try:
        header_b64, payload_b64, sig_b64 = token.strip().split(".")
        header = json.loads(_b64url_decode(header_b64))
        if header.get("alg") != "HS256":
            logger.warning("Rejected homeowner token with alg=%s", header.get("alg"))
            return None
        expected = hmac.new(
            jwt_secret.encode("utf-8"),
            f"{header_b64}.{payload_b64}".encode("ascii"),
            hashlib.sha256,
        ).digest()
        if not hmac.compare_digest(_b64url_decode(sig_b64), expected):
            logger.warning("Rejected homeowner token: signature mismatch")
            return None
        payload = json.loads(_b64url_decode(payload_b64))
        exp = payload.get("exp")
        if isinstance(exp, (int, float)) and exp < time.time():
            logger.info("Rejected homeowner token: expired")
            return None
        sub = payload.get("sub")
        return str(sub) if sub else None
    except Exception as exc:  # noqa: BLE001 — any malformed token is just anonymous
        logger.warning("Could not verify homeowner token: %s", exc)
        return None
