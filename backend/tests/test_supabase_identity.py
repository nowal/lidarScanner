import base64
import json

import httpx
import pytest

from app.config import settings
from app.flow import identity
from app.flow_api import homeowner_id_from_header
from app.main import config_problems


def mock_auth(monkeypatch, handler):
    client_type = httpx.AsyncClient
    monkeypatch.setattr(
        identity.httpx,
        "AsyncClient",
        lambda **kwargs: client_type(transport=httpx.MockTransport(handler), **kwargs),
    )


@pytest.mark.asyncio
async def test_es256_identity_comes_from_supabase_not_unverified_claims(monkeypatch):
    def encode(value):
        return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")

    token = f'{encode({"alg": "ES256"})}.{encode({"sub": "untrusted-claim"})}.signature'
    verified_id = "d552f674-9a33-4a7c-96e9-a797489f834f"

    def auth(request):
        assert str(request.url) == "https://project.supabase.co/auth/v1/user"
        assert request.headers["apikey"] == "server-api-key"
        assert request.headers["Authorization"] == f"Bearer {token}"
        return httpx.Response(200, json={"id": verified_id})

    mock_auth(monkeypatch, auth)
    monkeypatch.setattr(settings, "supabase_url", "https://project.supabase.co/")
    monkeypatch.setattr(settings, "supabase_service_role_key", "server-api-key")
    assert await homeowner_id_from_header(token) == verified_id


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403, 500])
async def test_rejected_or_unavailable_auth_never_trusts_claims(monkeypatch, status):
    mock_auth(monkeypatch, lambda _: httpx.Response(status, json={"id": "claimed-user"}))

    def no_local_fallback(*args):
        pytest.fail("Configured projects must not bypass an Auth rejection")

    monkeypatch.setattr(identity, "verify_homeowner_token", no_local_fallback)
    assert await identity.resolve_homeowner_token(
        "bad-token", supabase_url="https://project.supabase.co",
        api_key="server-api-key", jwt_secret="legacy-secret",
    ) is None


@pytest.mark.asyncio
async def test_auth_timeout_returns_no_identity(monkeypatch):
    def timeout(request):
        raise httpx.ReadTimeout("Auth unavailable", request=request)

    mock_auth(monkeypatch, timeout)
    assert await identity.resolve_homeowner_token(
        "token", supabase_url="https://project.supabase.co", api_key="server-api-key",
    ) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [{}, [], {"id": 123}, {"id": "not-a-uuid"}])
async def test_invalid_auth_response_returns_no_identity(monkeypatch, payload):
    mock_auth(monkeypatch, lambda _: httpx.Response(200, json=payload))
    assert await identity.resolve_homeowner_token(
        "token", supabase_url="https://project.supabase.co", api_key="server-api-key",
    ) is None


@pytest.mark.asyncio
async def test_absent_user_token_does_not_call_auth(monkeypatch):
    def unexpected(_):
        pytest.fail("Anonymous requests do not need an Auth round trip")

    mock_auth(monkeypatch, unexpected)
    assert await identity.resolve_homeowner_token(
        None, supabase_url="https://project.supabase.co", api_key="server-api-key",
    ) is None


def test_supabase_connection_makes_legacy_jwt_secret_optional(monkeypatch):
    monkeypatch.setattr(settings, "supabase_url", "https://project.supabase.co")
    monkeypatch.setattr(settings, "supabase_service_role_key", "server-api-key")
    assert not any("JWT_SECRET" in problem for problem in config_problems())


@pytest.mark.asyncio
async def test_guest_status_is_verified_by_auth(monkeypatch):
    user_id = "d552f674-9a33-4a7c-96e9-a797489f834f"
    mock_auth(monkeypatch, lambda _: httpx.Response(200, json={"id": user_id, "is_anonymous": True}))
    result = await identity.resolve_homeowner_token(
        "guest-token", supabase_url="https://project.supabase.co", api_key="server-api-key",
    )
    assert result == user_id
    assert result.is_anonymous is True


@pytest.mark.asyncio
async def test_guest_owns_flow_without_skipping_contact_and_upgrade_keeps_owner(monkeypatch):
    from app import flow_runtime
    from app.flow import FlowEngine, FlowState, FlowTokenCodec

    auth_id = "d552f674-9a33-4a7c-96e9-a797489f834f"
    homeowner_id = "593b96ce-187c-4d6c-8065-567151084ccd"
    async def profile(_):
        return {"id": homeowner_id, "full_name": "", "email": None, "phone": None}
    monkeypatch.setattr(flow_runtime.supabase_store, "resolve_homeowner", profile)
    state = FlowState(thread_id="guest-history")
    await flow_runtime._attach_identity(state, identity.VerifiedHomeownerID(auth_id, is_anonymous=True))
    assert state.homeowner_id == homeowner_id
    assert state.homeowner_auth_sub == auth_id
    assert not state.has_identity
    assert "contact" in FlowEngine().missing_submission_slots(state, require_values=True)
    codec = FlowTokenCodec("test-secret")
    redacted = codec.decode(codec.encode(state))
    assert redacted.homeowner_linked and redacted.homeowner_is_guest
    assert not redacted.has_identity
    assert "contact" in FlowEngine().missing_submission_slots(redacted)

    state.slots.contact_email = "guest@example.invalid"
    assert "contact" not in FlowEngine().missing_submission_slots(state, require_values=True)
    flow_runtime.supabase_store._homeowner_cache[auth_id] = (0, {"email": None})
    await flow_runtime._attach_identity(state, identity.VerifiedHomeownerID(auth_id))
    assert auth_id not in flow_runtime.supabase_store._homeowner_cache
    assert state.homeowner_id == homeowner_id
    assert state.homeowner_auth_sub == auth_id
    assert state.has_identity
    assert state.slots.contact_email == "guest@example.invalid"


@pytest.mark.asyncio
async def test_signing_into_another_account_cannot_reassign_guest_history():
    from fastapi import HTTPException
    from app import flow_runtime
    from app.flow import FlowState

    state = FlowState(thread_id="guest-history", homeowner_auth_sub="original-guest",
                      homeowner_id="original-homeowner", homeowner_is_guest=True)
    with pytest.raises(HTTPException) as failure:
        await flow_runtime._attach_identity(state, identity.VerifiedHomeownerID("another-account"))
    assert failure.value.status_code == 403
    assert state.homeowner_auth_sub == "original-guest"
    assert state.homeowner_id == "original-homeowner"
    assert state.homeowner_is_guest
