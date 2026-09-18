"""Request instrumentation (roadmap workstream F).

One JSONL line per request: request class, latency, status, payload size,
and process RSS where the platform exposes it (Linux/Render — the platform
the measurements are *for*). This is the data that answers "will Render get
overwhelmed" with numbers instead of intuition, and shows whether chat and
processing actually contend.

Kept dependency-free and fail-open: a metrics problem must never affect a
request.
"""

from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

from .config import settings
from .models import now_utc

logger = logging.getLogger("lidarai.metrics")

# Path → request class. Order matters; first match wins.
_CLASSES: list[tuple[re.Pattern, str]] = [
    (re.compile(r"/ai/home-chat/opening$"), "chat_opening"),
    (re.compile(r"/ai/home-chat$"), "chat_turn"),
    (re.compile(r"/ai/home-events$"), "chat_event"),
    (re.compile(r"/ai/quote-requests"), "quote"),
    (re.compile(r"/ops/"), "ops"),
    (re.compile(r"/jobs/[^/]+/upload$"), "job_upload"),
    (re.compile(r"/jobs/[^/]+/finalize$"), "job_finalize"),
    (re.compile(r"/jobs/[^/]+/result"), "job_result"),
    (re.compile(r"/jobs/[^/]+/events$"), "job_sse"),
    (re.compile(r"/jobs"), "job_control"),
    (re.compile(r"/health$"), "health"),
]


def _classify(path: str) -> str:
    for pattern, name in _CLASSES:
        if pattern.search(path):
            return name
    return "other"


def _rss_mb() -> float | None:
    try:  # Linux (Render) — the deployment we're measuring.
        import resource

        return round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1)
    except Exception:  # noqa: BLE001 — unavailable on Windows dev boxes
        return None


class RequestMetricsMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        started = time.monotonic()
        status = 500
        try:
            response = await call_next(request)
            status = response.status_code
            return response
        finally:
            try:
                record = {
                    "ts": now_utc().isoformat(),
                    "class": _classify(request.url.path),
                    "method": request.method,
                    "path": request.url.path,
                    "status": status,
                    "durationMs": int((time.monotonic() - started) * 1000),
                    "requestBytes": int(request.headers.get("content-length") or 0),
                    "rssMb": _rss_mb(),
                }
                base = Path(settings.storage_dir) / "metrics"
                base.mkdir(parents=True, exist_ok=True)
                with (base / "requests.jsonl").open("a", encoding="utf-8") as f:
                    f.write(json.dumps(record) + "\n")
            except Exception as exc:  # noqa: BLE001
                logger.warning("Metrics write failed: %s", exc)
