"""Production armor: per-thread turn lock, turn deadline, rate limiting."""

import asyncio

import pytest
from httpx import ASGITransport, AsyncClient

import app.flow_runtime as flow_runtime
from app.armor import SlidingWindow, TurnLocks, turn_guard
from app.config import settings
from app.home_ai import (
    HomeAIChatMessage,
    HomeAIChatRequest,
    HomeAIChatResponse,
    HomeAIConversationState,
)
from app.main import app


@pytest.fixture(autouse=True)
def isolated_storage(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "storage_dir", str(tmp_path))
    yield tmp_path


def client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def auth_headers():
    return {"Authorization": f"Bearer {settings.auth_token}"} if settings.auth_token else {}


# ------------------------------------------------------------------ turn lock
@pytest.mark.asyncio
async def test_turn_lock_serializes_same_thread():
    locks = TurnLocks()
    order: list[str] = []

    async def worker(name: str, hold: float):
        assert await locks.acquire("t-1", timeout=5)
        order.append(f"{name}-in")
        await asyncio.sleep(hold)
        order.append(f"{name}-out")
        locks.release("t-1")

    await asyncio.gather(worker("a", 0.05), worker("b", 0.01))
    # Never interleaved: one turn finishes before the other starts.
    assert order in (["a-in", "a-out", "b-in", "b-out"], ["b-in", "b-out", "a-in", "a-out"])


@pytest.mark.asyncio
async def test_turn_lock_does_not_block_other_threads():
    locks = TurnLocks()
    assert await locks.acquire("thread-a", timeout=1)
    # A different conversation is unaffected.
    assert await locks.acquire("thread-b", timeout=0.2)
    locks.release("thread-a")
    locks.release("thread-b")


@pytest.mark.asyncio
async def test_turn_lock_times_out_rather_than_queueing_forever():
    locks = TurnLocks()
    assert await locks.acquire("t-slow", timeout=1)
    assert await locks.acquire("t-slow", timeout=0.1) is False
    locks.release("t-slow")


@pytest.mark.asyncio
async def test_turn_lock_releases_and_forgets_idle_threads():
    locks = TurnLocks()
    await locks.acquire("t-tidy", timeout=1)
    locks.release("t-tidy")
    assert "t-tidy" not in locks._locks


@pytest.mark.asyncio
async def test_turn_guard_is_a_noop_when_disabled(monkeypatch):
    monkeypatch.setattr(settings, "turn_lock_enabled", False)
    async with turn_guard("whatever") as ok:
        assert ok is True


@pytest.mark.asyncio
async def test_concurrent_turns_on_one_thread_get_429(monkeypatch):
    """The app's own double-tap: the second turn is refused, not silently
    interleaved into a lost update."""
    monkeypatch.setattr(settings, "turn_lock_enabled", True)
    monkeypatch.setattr(settings, "turn_lock_timeout_seconds", 0.2)
    started = asyncio.Event()

    async def slow_turn(request, thread_id, homeowner_id):
        started.set()
        await asyncio.sleep(1.5)
        return HomeAIChatResponse(
            threadId=thread_id,
            message=HomeAIChatMessage(role="assistant", content="done"),
            state=HomeAIConversationState(intent="exploring"),
            model="stub", provider="stub",
        )

    monkeypatch.setattr(flow_runtime, "_run_flow_turn_locked", slow_turn)

    async def first():
        return await flow_runtime.run_flow_turn(HomeAIChatRequest(threadId="t-race", message="hi"))

    task = asyncio.create_task(first())
    await started.wait()
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        await flow_runtime.run_flow_turn(HomeAIChatRequest(threadId="t-race", message="again"))
    assert exc.value.status_code == 429
    assert (await task).message.content == "done"


# ------------------------------------------------------------------- deadline
@pytest.mark.asyncio
async def test_turn_deadline_serves_safe_copy_instead_of_hanging(monkeypatch):
    monkeypatch.setattr(settings, "turn_deadline_enabled", True)
    monkeypatch.setattr(settings, "turn_deadline_seconds", 0.15)

    async def never_returns(*args, **kwargs):
        await asyncio.sleep(30)

    monkeypatch.setattr(flow_runtime, "_generate_enforced_inner", never_returns)
    request = HomeAIChatRequest(threadId="t-slow", message="hello")
    response, suppressed, substituted = await flow_runtime._generate_enforced(
        request, "", flow_runtime.GateDecision()
    )
    assert substituted is True
    assert response.usedFallback is True
    assert "took longer" in response.message.content
    assert response.threadId == "t-slow"


@pytest.mark.asyncio
async def test_deadline_disabled_lets_slow_turns_finish(monkeypatch):
    monkeypatch.setattr(settings, "turn_deadline_enabled", False)
    sentinel = ("resp", [], False)

    async def slow_but_finishes(*args, **kwargs):
        await asyncio.sleep(0.05)
        return sentinel

    monkeypatch.setattr(flow_runtime, "_generate_enforced_inner", slow_but_finishes)
    result = await flow_runtime._generate_enforced(
        HomeAIChatRequest(threadId="t", message="x"), "", flow_runtime.GateDecision()
    )
    assert result is sentinel


# ---------------------------------------------------------------- rate limit
def test_sliding_window_allows_then_refuses():
    window = SlidingWindow()
    for _ in range(5):
        allowed, _ = window.check("k", limit=5)
        assert allowed
    allowed, retry = window.check("k", limit=5)
    assert not allowed and retry >= 1


def test_sliding_window_keys_are_independent():
    window = SlidingWindow()
    assert window.check("a", limit=1)[0]
    assert window.check("b", limit=1)[0]
    assert not window.check("a", limit=1)[0]


def test_sliding_window_forgets_old_hits():
    window = SlidingWindow()
    assert window.check("k", limit=1, window=0.05)[0]
    assert not window.check("k", limit=1, window=0.05)[0]
    import time as _t

    _t.sleep(0.06)
    assert window.check("k", limit=1, window=0.05)[0]


@pytest.mark.asyncio
async def test_rate_limit_returns_429_with_retry_after(monkeypatch):
    monkeypatch.setattr(settings, "rate_limit_enabled", True)
    monkeypatch.setattr(settings, "rate_limit_per_address_per_minute", 3)
    import app.armor as armor

    monkeypatch.setattr(armor, "_address_window", armor.SlidingWindow())
    statuses = []
    async with client() as http:
        for _ in range(5):
            r = await http.post(
                "/api/v1/ai/home-chat", headers=auth_headers(), json={"message": "hi"}
            )
            statuses.append(r.status_code)
    assert 429 in statuses
    limited = statuses.index(429)
    assert limited >= 3, "the first requests under the limit must not be refused"


@pytest.mark.asyncio
async def test_rate_limit_ignores_unlimited_paths(monkeypatch):
    monkeypatch.setattr(settings, "rate_limit_enabled", True)
    monkeypatch.setattr(settings, "rate_limit_per_address_per_minute", 1)
    import app.armor as armor

    monkeypatch.setattr(armor, "_address_window", armor.SlidingWindow())
    async with client() as http:
        for _ in range(4):
            r = await http.get("/health")
            assert r.status_code == 200


@pytest.mark.asyncio
async def test_rate_limit_off_switch(monkeypatch):
    monkeypatch.setattr(settings, "rate_limit_enabled", False)
    import app.armor as armor

    monkeypatch.setattr(armor, "_address_window", armor.SlidingWindow())
    async with client() as http:
        for _ in range(6):
            r = await http.post(
                "/api/v1/ai/home-chat", headers=auth_headers(), json={"message": "hi"}
            )
            assert r.status_code != 429
