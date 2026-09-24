"""Four defects found running the ops loop end to end on the deployed demo
(Sep 4), all of which degrade quietly rather than failing loudly:

1. A vague service type captured early ("bathroom remodel") was permanent,
   so the lead matched no partner even after the homeowner said "repaint
   the walls and trim".
2. Home indexes lived only on the host's ephemeral disk, so every redeploy
   wiped them and the demo answered "that home has not been ingested".
3. The lead email said "not available: scan processing state is 'complete'"
   for whole-home scans, which contradicts itself.
4. Resending a lead email ran provider research synchronously and outran
   the platform's HTTP timeout, so a success looked like a failure.
"""

import asyncio
import json

import pytest

import app.flow_runtime as flow_runtime
from app.config import settings
from app.flow import home_registry, supabase_store
from app.flow.state import FlowState, ScanStatus, Slots
from app.flow_quotes import build_model_link
from app.home_index import HomeIndex, Room


# --------------------------------------------------- 1. service type upgrade
def _apply(state: FlowState, captured: str | None, message: str) -> dict:
    return flow_runtime._apply_capture(
        state, {"projectType": captured} if captured else {}, message
    )


def test_a_vague_service_type_is_upgraded_by_a_later_turn():
    state = FlowState(thread_id="t", slots=Slots(project_type="general refresh"))
    delta = _apply(state, "painting", "repaint the walls and trim")
    assert state.slots.project_type == "Painting"
    assert delta["projectType"] == "Painting"


def test_the_homeowners_own_words_can_do_the_upgrade():
    """Even when the model keeps summarising it vaguely."""
    state = FlowState(thread_id="t", slots=Slots(project_type="general refresh"))
    _apply(state, "bathroom refresh", "we just want the walls painted")
    assert state.slots.project_type == "Painting"


def test_bathroom_remodel_is_now_a_trade_rather_than_a_vague_phrase():
    """This phrase used to be the example of an unmappable capture, because
    the catalog had six services and none of them was remodeling. Quintin's
    Sep 11 list added it, so the early capture is now correct on its own and
    there is nothing to upgrade -- which is the better outcome: the old
    behaviour left the lead matching no partner at all."""
    from app.home_guide_tools import normalize_service_type

    state = FlowState(thread_id="t", slots=Slots(project_type="bathroom remodel"))
    _apply(state, "painting", "repaint the walls and trim")
    # The slot keeps the homeowner's own words, as it always has; what
    # changed is that they now resolve to a trade instead of to nothing, so
    # the lead matches a provider without any upgrade having to fire.
    assert state.slots.project_type == "bathroom remodel"
    assert normalize_service_type(state.slots.project_type) == "Interior Remodeling"


def test_a_good_service_type_is_never_downgraded():
    state = FlowState(thread_id="t", slots=Slots(project_type="Painting"))
    _apply(state, "bathroom remodel", "and maybe new tile")
    assert state.slots.project_type == "Painting"


def test_an_unmappable_type_survives_when_nothing_better_arrives():
    state = FlowState(thread_id="t", slots=Slots(project_type="bathroom remodel"))
    _apply(state, "general refresh", "just make it nicer")
    assert state.slots.project_type == "bathroom remodel"


# ------------------------------------------------- 2. home index durability
@pytest.fixture
def durable(monkeypatch, tmp_path):
    """A stand-in for Supabase Storage."""
    monkeypatch.setattr(settings, "storage_dir", str(tmp_path / "disk"))
    home_registry._cache.clear()
    store: dict[str, dict] = {}

    async def put(home_id, payload):
        store[home_id] = payload
        return True

    async def get(home_id):
        return store.get(home_id)

    async def delete(home_id):
        store.pop(home_id, None)
        return True

    monkeypatch.setattr(supabase_store, "enabled", lambda: True)
    monkeypatch.setattr(supabase_store, "put_home_index", put)
    monkeypatch.setattr(supabase_store, "get_home_index", get)
    monkeypatch.setattr(supabase_store, "delete_home_index", delete)
    yield store
    home_registry._cache.clear()


def _index() -> HomeIndex:
    room = Room(key="room-1", index=1, storey=1, plan_label="kitchen",
                area_sqft=200.0, floor_y=0.0, display_name="kitchen", confident=True)
    return HomeIndex([room], bundle_id="B", storey_count=1)


@pytest.mark.asyncio
async def test_a_home_survives_the_disk_being_wiped(durable, tmp_path):
    home_registry.save_index("h", _index())
    await asyncio.sleep(0)   # let the fire-and-forget durable write land
    assert "h" in durable, "the durable copy must be written on save"

    # A redeploy: empty disk, cold process.
    home_registry._cache.clear()
    for stale in (tmp_path / "disk" / "homes").glob("*.json"):
        stale.unlink()
    assert home_registry.load_index("h") is None      # local really is gone

    rehydrated = await home_registry.load_index_async("h")
    assert rehydrated is not None
    assert rehydrated.resolve("kitchen").key == "room-1"


@pytest.mark.asyncio
async def test_rehydration_rewarms_the_local_copy(durable, tmp_path):
    home_registry.save_index("h", _index())
    await asyncio.sleep(0)   # the durable write is fire-and-forget
    home_registry._cache.clear()
    for stale in (tmp_path / "disk" / "homes").glob("*.json"):
        stale.unlink()
    await home_registry.load_index_async("h")
    assert home_registry.load_index("h") is not None, "later turns should hit local"


@pytest.mark.asyncio
async def test_deletion_removes_the_durable_copy_too(durable):
    """Otherwise deletion lasts only until the next rehydrate."""
    home_registry.save_index("h", _index())
    home_registry.forget("h")
    assert "h" not in durable
    assert await home_registry.load_index_async("h") is None


@pytest.mark.asyncio
async def test_unknown_home_stays_unknown(durable):
    assert await home_registry.load_index_async("nope") is None


# --------------------------------------------- 3. honest model-link message
@pytest.mark.asyncio
async def test_whole_home_scans_get_an_honest_model_message(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "storage_dir", str(tmp_path))
    state = FlowState(thread_id="t", home_id="h", scan=ScanStatus(state="complete"))
    link = await build_model_link(state)
    assert link["status"] == "not_available"
    assert "walked-home" in link["reason"]
    assert "complete" not in link["reason"], "quoting a 'complete' state reads as a contradiction"


@pytest.mark.asyncio
async def test_a_single_room_scan_still_reports_its_processing_state(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "storage_dir", str(tmp_path))
    state = FlowState(thread_id="t", scan=ScanStatus(job_id="job-1", state="processing"))
    link = await build_model_link(state)
    assert "processing" in link["reason"]



# The tests above mock put/get_home_index wholesale, which is why a
# NameError inside the real function survived them: supabase_store never
# imported json, so every durable write raised and was swallowed by the
# broad except. These exercise the real code path with only the HTTP layer
# faked.
@pytest.mark.asyncio
async def test_the_real_durable_write_actually_runs(monkeypatch):
    import httpx

    from app.flow import supabase_store as ss

    monkeypatch.setattr(settings, "supabase_url", "https://example.supabase.co")
    monkeypatch.setattr(settings, "supabase_service_role_key", "service-key")
    seen = {}

    class FakeClient:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, url, headers=None, content=None, **kw):
            seen["url"] = url
            seen["content"] = content
            # raise_for_status needs the request set on the response
            return httpx.Response(200, json={}, request=httpx.Request("POST", url))
        async def get(self, url, headers=None, **kw):
            return httpx.Response(200, json={"bundleId": "B", "storeys": 1, "rooms": []},
                                  request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    assert await ss.put_home_index("h", {"bundleId": "B", "storeys": 1, "rooms": []}) is True
    assert "home-indexes/h.json" in seen["url"]
    assert b"bundleId" in seen["content"], "the payload must actually be serialised"
    assert (await ss.get_home_index("h"))["bundleId"] == "B"


@pytest.mark.asyncio
async def test_a_durable_write_failure_is_reported_not_swallowed_silently(monkeypatch, caplog):
    import httpx

    from app.flow import supabase_store as ss

    monkeypatch.setattr(settings, "supabase_url", "https://example.supabase.co")
    monkeypatch.setattr(settings, "supabase_service_role_key", "service-key")

    class Failing:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, *a, **k): raise RuntimeError("bucket missing")

    monkeypatch.setattr(httpx, "AsyncClient", Failing)
    with caplog.at_level("WARNING"):
        assert await ss.put_home_index("h", {}) is False
    assert any("home-index upload failed" in r.getMessage() for r in caplog.records),         "a failed durable write must be visible in the logs, not swallowed"


@pytest.mark.asyncio
async def test_cold_load_does_not_keep_a_cached_pre_model_index(monkeypatch, tmp_path):
    """Production served a CDN HIT after registration despite no-cache headers."""
    import httpx

    monkeypatch.setattr(settings, "storage_dir", str(tmp_path))
    monkeypatch.setattr(settings, "supabase_url", "https://example.supabase.co")
    monkeypatch.setattr(settings, "supabase_service_role_key", "service-key")
    origin = {"bundleId": "cdn-home", "rooms": [], "upload": {"modelsReady": False}}
    edge = {}

    def cdn(request):
        key = request.url.params.get("cacheNonce", "unchanged-url")
        if key not in edge:
            edge[key] = json.loads(json.dumps(origin))
        return httpx.Response(200, json=edge[key])

    real_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real_client(transport=httpx.MockTransport(cdn), **kw))
    home_id = "cdn-home"
    home_registry._cache.pop(home_id, None)
    first = await home_registry.load_index_async(home_id)
    assert first.upload["modelsReady"] is False
    origin["upload"]["modelsReady"] = True
    # A redeployed worker has neither its previous cache nor its local file.
    home_registry._cache.pop(home_id, None)
    home_registry._path(home_id).unlink()
    second = await home_registry.load_index_async(home_id)
    assert second.upload["modelsReady"] is True
    home_registry._cache.pop(home_id, None)
