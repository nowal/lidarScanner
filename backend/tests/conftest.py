"""Shared test guards.

backend/.env carries live credentials for the internal dev environment
(Anthropic, Supabase). Tests must be hermetic: blank every outbound
credential for the whole suite so no test — including the upstream
test_api.py — can reach a live service by accident. Individual tests
opt back in by monkeypatching what they stub.
"""

import pytest

from app.config import settings


@pytest.fixture(autouse=True)
def no_live_services(monkeypatch):
    # Inbound auth is env-dependent too: a developer .env with
    # LIDARAI_AUTH_TOKEN set made 18 tests 401 while CI (no .env) was green.
    # Tests that exercise auth set their own token.
    monkeypatch.setattr(settings, "auth_token", "")
    monkeypatch.setattr(settings, "ops_token", "")
    monkeypatch.setattr(settings, "supabase_url", "")
    monkeypatch.setattr(settings, "supabase_service_role_key", "")
    monkeypatch.setattr(settings, "supabase_jwt_secret", "")
    monkeypatch.setattr(settings, "openai_api_key", "")
    monkeypatch.setattr(settings, "anthropic_api_key", "")
    monkeypatch.setattr(settings, "ops_webhook_url", "")
    monkeypatch.setattr(settings, "ops_email", "")
    monkeypatch.setattr(settings, "resend_api_key", "")
    monkeypatch.setattr(settings, "smtp_host", "")
    monkeypatch.setattr(settings, "smtp_username", "")
    monkeypatch.setattr(settings, "smtp_password", "")
    yield
