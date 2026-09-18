"""Model-provider health signal.

The service is designed to degrade rather than fail: when the provider call
errors, the homeowner still gets a reply from the local fallback writer and
HTTP stays 200. That is the right behavior for one bad turn — and exactly
the wrong thing to be silent about when EVERY turn is failing.

Observed Sep 2 2026: a valid, well-formed API key with an exhausted credit
balance. Nothing was unset, nothing 500'd, `/health` said "ok", and the
demo quietly served canned copy to a client who had been invited to try it.
`config_problems()` only catches *missing* configuration; this catches
configuration that is present and not working.

Classification is coarse on purpose — enough to route the fix (top up
billing, rotate a key, back off) without parroting provider text.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field

# Consecutive provider failures before /health reports degraded. One failed
# turn is noise (a timeout, a transient 529); three in a row is an outage.
DEGRADED_AFTER = 3

_KEY_LIKE = re.compile(r"(sk-[A-Za-z0-9_\-]{6,}|Bearer\s+\S+)")


def classify(error: BaseException | str) -> str:
    text = str(error).lower()
    if "credit balance" in text or "billing" in text or "quota" in text or "insufficient_quota" in text:
        return "billing_exhausted"
    if "rate limit" in text or "429" in text or "overloaded" in text or "529" in text:
        return "rate_limited"
    if "authentication" in text or "invalid api key" in text or "401" in text or "permission" in text:
        return "auth_failed"
    if "timeout" in text or "timed out" in text:
        return "timeout"
    if "connection" in text or "network" in text or "dns" in text:
        return "network"
    return "other"


def _scrub(text: str, limit: int = 180) -> str:
    return _KEY_LIKE.sub("[redacted]", text)[:limit]


@dataclass
class ProviderHealth:
    calls: int = 0
    failures: int = 0
    consecutive_failures: int = 0
    last_ok_ts: float | None = None
    last_failure_ts: float | None = None
    last_error_class: str | None = None
    last_error_detail: str | None = None
    recent_classes: list[str] = field(default_factory=list)

    def record_success(self) -> None:
        self.calls += 1
        self.consecutive_failures = 0
        self.last_ok_ts = time.time()

    def record_failure(self, error: BaseException | str) -> str:
        self.calls += 1
        self.failures += 1
        self.consecutive_failures += 1
        self.last_failure_ts = time.time()
        kind = classify(error)
        self.last_error_class = kind
        self.last_error_detail = _scrub(str(error))
        self.recent_classes.append(kind)
        del self.recent_classes[:-10]
        return kind

    @property
    def degraded(self) -> bool:
        return self.consecutive_failures >= DEGRADED_AFTER

    def snapshot(self) -> dict:
        """Health payload. `advice` is the operator's next action — the whole
        point of noticing."""
        advice = {
            "billing_exhausted": "The provider account is out of credit — top up billing. "
                                 "Every conversation is currently answered by the local fallback writer.",
            "auth_failed": "The provider rejected the API key — rotate or re-set it.",
            "rate_limited": "The provider is rate limiting or overloaded — back off or raise limits.",
            "timeout": "Provider calls are timing out — check provider status and the request timeout.",
            "network": "The host cannot reach the provider — check egress and DNS.",
            "other": "Provider calls are failing — see the service logs.",
        }.get(self.last_error_class or "", None)
        return {
            "status": "degraded" if self.degraded else "ok",
            "calls": self.calls,
            "failures": self.failures,
            "consecutiveFailures": self.consecutive_failures,
            "lastErrorClass": self.last_error_class,
            "lastErrorDetail": self.last_error_detail,
            "lastOkAgeSeconds": int(time.time() - self.last_ok_ts) if self.last_ok_ts else None,
            "advice": advice if self.degraded else None,
        }


provider_health = ProviderHealth()
