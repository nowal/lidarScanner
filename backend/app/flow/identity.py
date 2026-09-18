"""Homeowner identity from the ``X-Homeowner-Token`` header
(API_CONTRACT_V1 §2).

Supabase access tokens are HS256 JWTs signed with the project's JWT secret.
Verification here is deliberately dependency-free (hmac + json): we check the
signature, algorithm, and expiry, and return the ``sub`` claim — the Supabase
**auth user id** (``auth.users.id``). That is NOT the ``homeowners.id`` the
flow tables reference; ``flow_runtime._attach_identity`` resolves the sub to
the homeowners row via ``supabase_store.resolve_homeowner``.

Absent header, absent secret, or any verification failure → ``None`` —
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

logger = logging.getLogger("lidarai.flow.identity")


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
