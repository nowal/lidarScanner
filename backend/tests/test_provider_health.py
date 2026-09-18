"""Provider health signal — the gap that let an exhausted API key serve
canned fallback copy to a client while /health said "ok" (Sep 2 2026)."""

import pytest
from httpx import ASGITransport, AsyncClient

from app.config import settings
from app.main import app
from app.provider_health import DEGRADED_AFTER, ProviderHealth, classify


def test_classifies_the_failure_that_actually_happened():
    assert classify(
        "Error code: 400 - {'type': 'error', 'error': {'type': 'invalid_request_error', "
        "'message': 'Your credit balance is too low to access the Anthropic API.'}}"
    ) == "billing_exhausted"


@pytest.mark.parametrize(
    "text,expected",
    [
        ("insufficient_quota for this organization", "billing_exhausted"),
        ("401 authentication_error: invalid api key", "auth_failed"),
        ("429 rate limit exceeded", "rate_limited"),
        ("Request timed out", "timeout"),
        ("Connection error: dns failure", "network"),
        ("something else entirely", "other"),
    ],
)
def test_classification_routes_the_fix(text, expected):
    assert classify(text) == expected


def test_one_failure_is_noise_three_is_an_outage():
    health = ProviderHealth()
    health.record_failure("credit balance is too low")
    assert not health.degraded, "a single failed turn must not cry wolf"
    for _ in range(DEGRADED_AFTER - 1):
        health.record_failure("credit balance is too low")
    assert health.degraded
    assert health.snapshot()["advice"], "a degraded provider must say what to do about it"
    assert "top up" in health.snapshot()["advice"].lower()


def test_recovery_clears_the_signal():
    health = ProviderHealth()
    for _ in range(DEGRADED_AFTER):
        health.record_failure("timeout")
    assert health.degraded
    health.record_success()
    assert not health.degraded
    assert health.snapshot()["status"] == "ok"


def test_credentials_never_leak_into_the_health_payload():
    health = ProviderHealth()
    health.record_failure("bad key sk-ant-api03-SUPERSECRETVALUE123456 rejected")
    detail = health.snapshot()["lastErrorDetail"]
    assert "SUPERSECRET" not in detail and "[redacted]" in detail


@pytest.mark.asyncio
async def test_health_endpoint_reports_degraded_provider(monkeypatch):
    from app import provider_health as module

    fresh = ProviderHealth()
    monkeypatch.setattr(module, "provider_health", fresh)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        before = (await http.get("/health")).json()
        assert before["provider"]["status"] == "ok"
        for _ in range(DEGRADED_AFTER):
            fresh.record_failure("Your credit balance is too low")
        after = (await http.get("/health")).json()
    assert after["status"] == "degraded"
    assert after["provider"]["lastErrorClass"] == "billing_exhausted"
    assert after["provider"]["consecutiveFailures"] == DEGRADED_AFTER
