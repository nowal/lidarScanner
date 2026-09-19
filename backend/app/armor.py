"""Production armor: the guards that keep one bad client (or one bad
minute) from degrading the service for everyone.

Three independent pieces, each individually switchable:

1. **Turn lock** — flow state is read-modify-write per thread. Two turns
   racing on one thread can interleave into a lost update (a captured slot
   silently vanishing). Turns on the same thread are serialized; a caller
   that waits too long gets 429 rather than corrupting state. The app's own
   double-tap is the common case.

2. **Turn deadline** — the model call has its own timeout, but a turn can
   also regenerate (enforcement) and do durable writes, so worst-case wall
   time is a multiple of it. The deadline caps the whole turn and returns
   deterministic safe copy instead of hanging a phone on a spinner.

3. **Rate limit** — sliding window per identity and per address. Protects
   the model budget and the instance from a runaway client or a scraper.

Scope honesty: state is per-process. One instance today (the deploy is a
single service), so the guards are exact; a multi-instance deploy needs a
shared store (Supabase or Redis) and these become per-instance
approximations. Documented in CONFIG.md rather than pretended away.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from collections import defaultdict, deque

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from .config import settings

logger = logging.getLogger("lidarai.armor")


# --------------------------------------------------------------------------
# 1. Per-thread turn lock
# --------------------------------------------------------------------------
class TurnLocks:
    """One lock per conversation thread, created on demand and dropped when
    idle so a long-lived process doesn't accumulate a lock per thread ever
    seen."""

    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}
        self._waiters: dict[str, int] = defaultdict(int)

    def _lock_for(self, thread_id: str) -> asyncio.Lock:
        lock = self._locks.get(thread_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[thread_id] = lock
        return lock

    async def acquire(self, thread_id: str, timeout: float) -> bool:
        """True when the lock is held by this caller; False when the wait
        timed out (caller should refuse the request)."""
        lock = self._lock_for(thread_id)
        self._waiters[thread_id] += 1
        try:
            await asyncio.wait_for(lock.acquire(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            logger.warning("Turn lock timeout on thread=%s", thread_id)
            return False
        finally:
            self._waiters[thread_id] -= 1
            if self._waiters[thread_id] <= 0:
                self._waiters.pop(thread_id, None)

    def release(self, thread_id: str) -> None:
        lock = self._locks.get(thread_id)
        if lock is not None and lock.locked():
            lock.release()
        # Nobody waiting and nobody holding → forget it.
        if lock is not None and not lock.locked() and thread_id not in self._waiters:
            self._locks.pop(thread_id, None)

    def held(self, thread_id: str) -> bool:
        lock = self._locks.get(thread_id)
        return bool(lock and lock.locked())


turn_locks = TurnLocks()


class turn_guard:
    """``async with turn_guard(thread_id) as ok:`` — ``ok`` is False when the
    lock could not be acquired in time."""

    def __init__(self, thread_id: str, timeout: float | None = None) -> None:
        self.thread_id = thread_id
        self.timeout = settings.turn_lock_timeout_seconds if timeout is None else timeout
        self.acquired = False

    async def __aenter__(self) -> bool:
        if not settings.turn_lock_enabled:
            return True
        self.acquired = await turn_locks.acquire(self.thread_id, self.timeout)
        return self.acquired

    async def __aexit__(self, *exc) -> bool:
        if self.acquired:
            turn_locks.release(self.thread_id)
        return False


# --------------------------------------------------------------------------
# 3. Sliding-window rate limit
# --------------------------------------------------------------------------
class SlidingWindow:
    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._last_sweep = time.monotonic()

    def check(self, key: str, limit: int, window: float = 60.0) -> tuple[bool, int]:
        """(allowed, retry_after_seconds)."""
        now = time.monotonic()
        hits = self._hits[key]
        cutoff = now - window
        while hits and hits[0] < cutoff:
            hits.popleft()
        if len(hits) >= limit:
            return False, max(1, int(hits[0] + window - now) + 1)
        hits.append(now)
        self._maybe_sweep(now, window)
        return True, 0

    def _maybe_sweep(self, now: float, window: float) -> None:
        """Drop empty keys every few minutes: without this a scanner hitting
        unique identities grows the dict forever."""
        if now - self._last_sweep < 300:
            return
        self._last_sweep = now
        cutoff = now - window
        for key in [k for k, v in self._hits.items() if not v or v[-1] < cutoff]:
            self._hits.pop(key, None)


_identity_window = SlidingWindow()
_address_window = SlidingWindow()

# Endpoints worth protecting: they cost a model call or a durable write.
_LIMITED_SUFFIXES = ("/ai/home-chat", "/ai/home-chat/opening", "/ai/quote-requests")


def _identity_key(request: Request) -> str | None:
    token = request.headers.get("x-homeowner-token")
    if token:
        return "hw:" + hashlib.sha256(token.encode()).hexdigest()[:16]
    return None


def _address_key(request: Request) -> str:
    # Trust the platform's forwarded-for first hop (Railway/Render terminate
    # TLS upstream, so request.client is the proxy).
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return "ip:" + forwarded.split(",")[0].strip()
    return "ip:" + (request.client.host if request.client else "unknown")


def _limited(path: str) -> bool:
    return any(path.endswith(suffix) for suffix in _LIMITED_SUFFIXES)


class MaxBodyMiddleware(BaseHTTPMiddleware):
    """Refuse a body larger than the configured ceiling, before FastAPI
    parses it into memory.

    ponytail: Content-Length only. A chunked upload declares no length and
    slips past this; if that ever matters, count bytes off the receive
    stream instead.
    """

    async def dispatch(self, request: Request, call_next):
        declared = request.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > settings.max_request_bytes:
            logger.warning("Body of %s bytes refused on %s", declared, request.url.path)
            return JSONResponse(status_code=413, content={"detail": "Request body too large"})
        return await call_next(request)


class RateLimitMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if not settings.rate_limit_enabled or not _limited(request.url.path):
            return await call_next(request)

        identity = _identity_key(request)
        if identity:
            allowed, retry = _identity_window.check(
                identity, settings.rate_limit_per_identity_per_minute
            )
            if not allowed:
                return self._refuse(retry, "identity", request.url.path)

        allowed, retry = _address_window.check(
            _address_key(request), settings.rate_limit_per_address_per_minute
        )
        if not allowed:
            return self._refuse(retry, "address", request.url.path)

        return await call_next(request)

    @staticmethod
    def _refuse(retry_after: int, scope: str, path: str) -> JSONResponse:
        logger.warning("Rate limit hit (%s) on %s", scope, path)
        return JSONResponse(
            status_code=429,
            headers={"Retry-After": str(retry_after)},
            content={
                "detail": "Too many requests — please slow down.",
                "retryAfterSeconds": retry_after,
            },
        )
