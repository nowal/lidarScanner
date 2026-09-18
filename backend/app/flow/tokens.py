"""Signed, opaque flow-state tokens.

Render's ``/tmp`` storage is ephemeral and Supabase writes can lag or fail;
the token makes the conversation's flow position survive anything the server
does, because the client echoes it back each turn. HMAC-SHA256 keeps it
tamper-evident — a client can drop the token (fresh flow) but cannot forge
"processing complete" or skip the address step.

The payload is signed but NOT encrypted (base64 is trivially decodable), so
sensitive values never ride in it: the street address, contact email/phone,
and identity ids are stripped at encode time and replaced with captured/
linked flags (SOW §12; API_CONTRACT_V1 §3.2). The real values live only in
the durable store and are merged back server-side in ``resolve_flow_state``.

Format: ``base64url(json_payload) + "." + base64url(hmac)``.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json

from .state import FlowState


class InvalidFlowToken(ValueError):
    """Signature mismatch or undecodable payload."""


_SENSITIVE_SLOTS = ("address", "contact_email", "contact_phone")


def _client_safe_payload(state: FlowState) -> dict:
    """The full state minus everything a client-held token must not carry."""
    payload = state.model_dump(mode="json")
    slots = payload.get("slots") or {}
    for key in _SENSITIVE_SLOTS:
        if slots.get(key):
            slots[key] = None
            slots[f"{key}_redacted"] = True
    if payload.get("homeowner_id") or payload.get("homeowner_auth_sub"):
        payload["homeowner_id"] = None
        payload["homeowner_auth_sub"] = None
        payload["homeowner_linked"] = True
    # The cached opening reply is server bookkeeping, not client state.
    payload["opening_response"] = None
    return payload


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64d(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


class FlowTokenCodec:
    def __init__(self, secret: str):
        if not secret:
            raise ValueError("flow token secret must be non-empty")
        self._key = hashlib.sha256(secret.encode("utf-8")).digest()

    def encode(self, state: FlowState) -> str:
        payload = json.dumps(
            _client_safe_payload(state), separators=(",", ":"), sort_keys=True
        ).encode("utf-8")
        sig = hmac.new(self._key, payload, hashlib.sha256).digest()
        return f"{_b64e(payload)}.{_b64e(sig)}"

    def decode(self, token: str) -> FlowState:
        try:
            payload_b64, sig_b64 = token.split(".", 1)
            payload = _b64d(payload_b64)
            sig = _b64d(sig_b64)
        except Exception as exc:  # malformed structure/base64
            raise InvalidFlowToken("malformed flow token") from exc
        expected = hmac.new(self._key, payload, hashlib.sha256).digest()
        if not hmac.compare_digest(sig, expected):
            raise InvalidFlowToken("flow token signature mismatch")
        try:
            return FlowState.model_validate(json.loads(payload))
        except Exception as exc:
            raise InvalidFlowToken("flow token payload invalid") from exc
