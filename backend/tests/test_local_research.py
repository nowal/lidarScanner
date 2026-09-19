"""Local research: demand-detection, provider honesty guards, and the
end-to-end wiring (web search mocked — no live calls)."""

import time

import pytest

import app.flow.local_research as lr
import app.flow_runtime as flow_runtime
from app.config import settings
from app.flow.local_research import LocalProviders, _GENERIC_NAMES, lookup_local_providers
from app.flow_runtime import _WANTS_LOCAL_CONTEXT, _WANTS_PROVIDERS, _maybe_local_research
from app.flow.state import FlowState, Slots
from app.home_ai import HomeAIChatRequest


@pytest.fixture(autouse=True)
def storage(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "storage_dir", str(tmp_path))
    monkeypatch.setattr(settings, "local_context_enabled", True)
    monkeypatch.setattr(settings, "local_provider_research_enabled", True)
    monkeypatch.setattr(settings, "anthropic_api_key", "test")
    yield


class TestDemandDetection:
    @pytest.mark.parametrize("msg", [
        "what's popular in my area?", "any local design trends?",
        "what do people around here do?", "styles common around here",
    ])
    def test_context_wanted(self, msg):
        assert _WANTS_LOCAL_CONTEXT.search(msg)

    @pytest.mark.parametrize("msg", [
        "are there any painters near me?", "recommend a local contractor",
        "know any pros nearby?", "any providers around here",
        "suggest some painters",
    ])
    def test_providers_wanted(self, msg):
        assert _WANTS_PROVIDERS.search(msg)

    def test_provider_trigger_is_precise(self):
        # No explicit provider noun → don't fire an expensive web search.
        assert not _WANTS_PROVIDERS.search("who can do this in my area?")
        assert not _WANTS_PROVIDERS.search("what should I do near the window?")

    def test_normal_turn_triggers_neither(self):
        assert not _WANTS_LOCAL_CONTEXT.search("I'd like warm neutral walls")
        assert not _WANTS_PROVIDERS.search("I'd like warm neutral walls")


class TestProviderHonestyGuards:
    def test_generic_names_rejected(self):
        for name in ["a local provider", "local contractors", "the company",
                     "your local painter", "nearby professionals"]:
            assert _GENERIC_NAMES.match(name)

    def test_real_names_pass(self):
        for name in ["CertaPro Painters", "MC Painting", "Kyees Construction, LLC"]:
            assert not _GENERIC_NAMES.match(name)

    @pytest.mark.asyncio
    async def test_fabricated_providers_dropped(self, monkeypatch):
        async def fake_search(prompt, schema, *, max_uses):
            return {"regionLabel": "Testville", "providers": [
                {"name": "Real Paint Co", "note": "residential", "foundOnline": True},
                {"name": "a local painter", "note": "generic", "foundOnline": True},
                {"name": "Invented LLC", "note": "hallucinated", "foundOnline": False},
            ]}
        monkeypatch.setattr(lr, "_search", fake_search)
        result = await lookup_local_providers("Painting", "43081")
        names = [p["name"] for p in result.providers]
        assert names == ["Real Paint Co"]  # generic + not-found dropped

    @pytest.mark.asyncio
    async def test_empty_search_returns_none(self, monkeypatch):
        async def fake_search(prompt, schema, *, max_uses):
            return {"regionLabel": "X", "providers": []}
        monkeypatch.setattr(lr, "_search", fake_search)
        assert await lookup_local_providers("Painting", "43081") is None


@pytest.mark.asyncio
async def test_runtime_gates_on_demand_and_slots(monkeypatch):
    calls = {"context": 0, "providers": 0}

    async def fake_context(service, zip_code):
        calls["context"] += 1
        return lr.LocalContext("Columbus", ["greige is popular"], ["permit rarely needed"], time.time())

    async def fake_providers(service, zip_code):
        calls["providers"] += 1
        return LocalProviders("Columbus", [{"name": "CertaPro", "note": "franchise"}], time.time())

    monkeypatch.setattr(lr, "lookup_local_context", fake_context)
    monkeypatch.setattr(lr, "lookup_local_providers", fake_providers)

    state = FlowState(thread_id="t", slots=Slots(project_type="Painting", zip="43081"))

    # Normal message: no research.
    ctx, prov = await _maybe_local_research(state, HomeAIChatRequest(message="warm neutrals please"))
    assert ctx is None and prov is None and calls == {"context": 0, "providers": 0}

    # Provider request: only providers fire.
    ctx, prov = await _maybe_local_research(state, HomeAIChatRequest(message="any painters near me?"))
    assert prov is not None and prov.providers[0].name == "CertaPro"
    assert prov.disclaimer and prov.beta is True
    assert calls["providers"] == 1

    # Without a zip, nothing fires even on demand.
    state2 = FlowState(thread_id="t2", slots=Slots(project_type="Painting"))
    ctx, prov = await _maybe_local_research(state2, HomeAIChatRequest(message="painters near me?"))
    assert ctx is None and prov is None
