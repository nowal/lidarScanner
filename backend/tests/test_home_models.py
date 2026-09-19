"""Baked room models from a whole-home export: found at load, copied to
Storage at ingest (under the plan's size cap), linked from the lead package,
and deleted with the home.

The first whole-home export that had finished texturing (Sep 8) carried a
238 MB whole-home bake and one model per area, 11-71 MB each. The lead
package for it said "no per-room model export yet" -- false, the kitchen
model was sitting in the zip. These pin the honest behaviour: link it when
it is stored, and say exactly why when it is not.
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import httpx
import pytest

from app.config import settings
from app.flow import home_registry, supabase_store
from app.flow.state import FlowState, ScanStatus
from app.flow_quotes import build_model_link
from app.home_index import HomeIndex, Room, load_bundle

IDENTITY = [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1]
FLOOR_PLANE = [1, 0, 0, 0, 0, 0, -1, 0, 0, 1, 0, 0, 0, 0, 0, 1]


def _usdz(path: Path, size: int = 64) -> None:
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("model.usda", b"#usda 1.0\n" + b"x" * size)


def _bundle(tmp_path: Path, *, kitchen_model: bool, bath_model: bool, home_model: bool) -> Path:
    base = tmp_path / "export"
    (base / "rooms").mkdir(parents=True)
    (base / "meta.json").write_text(json.dumps({"id": "HOME-1"}), encoding="utf-8")
    for idx, (label, obj, model) in enumerate(
        (("kitchen", "sink", kitchen_model), ("bathroom", "bathtub", bath_model)), start=1
    ):
        room_dir = base / "rooms" / f"room-{idx}"
        (room_dir / "rebuild").mkdir(parents=True)
        (room_dir / "floor.json").write_text(json.dumps({"floor": 1, "floorY": 0.0}), encoding="utf-8")
        t = list(IDENTITY)
        t[12] = idx * 20.0
        (room_dir / "room.json").write_text(json.dumps({
            "sections": [{"label": label}],
            "floors": [{"transform": FLOOR_PLANE[:12] + [idx * 20.0, 0, 0, 1],
                        "polygonCorners": [[-2, -2, 0], [2, -2, 0], [2, 2, 0], [-2, 2, 0]],
                        "dimensions": [4, 4, 0]}],
            "objects": [{"category": {obj: {}}, "transform": t, "dimensions": [1, 1, 1]}],
            "walls": [{"dimensions": [4, 2.4, 0]}] * 4, "doors": [], "windows": [], "openings": [],
        }), encoding="utf-8")
        (room_dir / "rebuild" / "manifest.json").write_text(json.dumps({"frames": []}), encoding="utf-8")
        if model:
            _usdz(room_dir / "model.usdz")
    if home_model:
        _usdz(base / "model.usdz", size=4096)
    return base


# ------------------------------------------------------------------ load + json
def test_load_bundle_records_each_baked_model_and_the_whole_home_one(tmp_path):
    index = load_bundle(_bundle(tmp_path, kitchen_model=True, bath_model=False, home_model=True))
    kitchen = index.by_key("room-1")
    bath = index.by_key("room-2")
    assert kitchen.model["file"] == "rooms/room-1/model.usdz" and kitchen.model["bytes"] > 0
    assert bath.model == {}, "an area the phone had not textured has no model record"
    assert index.home_model["file"] == "model.usdz" and index.home_model["bytes"] > 4096
    assert index.overview()["models"] == {"rooms": 1, "stored": 0, "home": True}
    assert kitchen.summary()["hasModel"] is True and bath.summary()["hasModel"] is False


def test_model_records_survive_the_json_round_trip(tmp_path):
    index = load_bundle(_bundle(tmp_path, kitchen_model=True, bath_model=True, home_model=True))
    index.by_key("room-1").model["object"] = "home-models/HOME-1/room-1.usdz"
    again = HomeIndex.from_json(json.loads(json.dumps(index.to_json())))
    assert again.by_key("room-1").model["object"] == "home-models/HOME-1/room-1.usdz"
    assert again.by_key("room-2").model["bytes"] == index.by_key("room-2").model["bytes"]
    assert again.home_model == index.home_model


def test_an_index_written_before_models_existed_still_loads():
    room = Room.from_json({"key": "room-1", "name": "kitchen"})
    assert room.model == {}
    assert HomeIndex.from_json({"rooms": [room.to_json()]}).home_model == {}


# ------------------------------------------------------------------ ingest upload
class _Recorder:
    """Fakes only the HTTP layer, so the real upload function runs."""

    def __init__(self, status: int = 200) -> None:
        self.status = status
        self.posts: list[tuple[str, int]] = []

    def client(self):
        rec = self

        class Client:
            def __init__(self, *a, **k): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
            async def post(self, url, headers=None, content=None, json=None, **kw):
                rec.posts.append((url, len(content or b"")))
                return httpx.Response(rec.status, json={}, text="" if rec.status < 400 else "EntityTooLarge",
                                      request=httpx.Request("POST", url))
            async def request(self, method, url, headers=None, json=None, **kw):
                rec.posts.append((f"{method} {url}", 0))
                return httpx.Response(200, json={}, request=httpx.Request(method, url))
            async def get(self, url, **kw):
                return httpx.Response(404, request=httpx.Request("GET", url))
        return Client


@pytest.fixture
def storage(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "storage_dir", str(tmp_path / "storage"))
    monkeypatch.setattr(settings, "supabase_url", "https://example.supabase.co")
    monkeypatch.setattr(settings, "supabase_service_role_key", "service-key")
    home_registry._cache.clear()
    yield
    home_registry._cache.clear()


def test_ingest_uploads_models_under_the_cap_and_records_the_object_path(storage, monkeypatch, tmp_path):
    rec = _Recorder()
    monkeypatch.setattr(httpx, "AsyncClient", rec.client())
    index = home_registry.ingest_bundle(
        _bundle(tmp_path, kitchen_model=True, bath_model=False, home_model=True), "HOME-1"
    )
    kitchen = index.by_key("room-1")
    assert kitchen.model["object"] == "home-models/HOME-1/room-1.usdz"
    assert kitchen.model.get("uploadedAt")
    assert index.home_model["object"] == "home-models/HOME-1/home.usdz"
    uploads = [u for u, _ in rec.posts if "home-models/" in u]
    assert len(uploads) == 2, uploads
    assert all(size > 0 for u, size in rec.posts if "home-models/" in u), "the bytes must be sent"
    # The saved index carries the outcome, so a cold host can link the model.
    saved = json.loads((tmp_path / "storage" / "homes" / "HOME-1.json").read_text(encoding="utf-8"))
    assert saved["rooms"][0]["model"]["object"] == "home-models/HOME-1/room-1.usdz"


def test_a_model_over_the_cap_is_skipped_with_the_reason_kept(storage, monkeypatch, tmp_path):
    rec = _Recorder()
    monkeypatch.setattr(httpx, "AsyncClient", rec.client())
    monkeypatch.setattr(settings, "model_upload_max_mb", 1)
    base = _bundle(tmp_path, kitchen_model=True, bath_model=False, home_model=False)
    (base / "rooms" / "room-1" / "model.usdz").write_bytes(b"P" * (2 * 1048576))
    index = home_registry.ingest_bundle(base, "HOME-1")
    model = index.by_key("room-1").model
    assert "object" not in model
    assert "2 MB exceeds LIDARAI_MODEL_UPLOAD_MAX_MB=1" in model["skipped"]
    assert not [u for u, _ in rec.posts if "home-models/" in u], "the bucket would refuse it; do not try"


def test_a_refused_upload_is_recorded_not_hidden(storage, monkeypatch, tmp_path, caplog):
    rec = _Recorder(status=400)
    monkeypatch.setattr(httpx, "AsyncClient", rec.client())
    with caplog.at_level("WARNING"):
        index = home_registry.ingest_bundle(
            _bundle(tmp_path, kitchen_model=True, bath_model=False, home_model=False), "HOME-1"
        )
    assert index.by_key("room-1").model["skipped"] == "upload failed (see server log)"
    assert any("model upload failed" in r.getMessage() for r in caplog.records)


def test_without_storage_the_index_says_so(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "storage_dir", str(tmp_path / "storage"))
    monkeypatch.setattr(settings, "supabase_url", "")
    home_registry._cache.clear()
    index = home_registry.ingest_bundle(
        _bundle(tmp_path, kitchen_model=True, bath_model=False, home_model=False), "HOME-1"
    )
    assert index.by_key("room-1").model["skipped"] == "no durable storage configured on this host"
    home_registry._cache.clear()


@pytest.mark.asyncio
async def test_ingest_from_inside_a_running_loop_still_uploads(storage, monkeypatch, tmp_path):
    """The bundle check ingests inside asyncio.run(); the upload must not be
    deferred past the point where the temp bundle is deleted."""
    rec = _Recorder()
    monkeypatch.setattr(httpx, "AsyncClient", rec.client())
    index = home_registry.ingest_bundle(
        _bundle(tmp_path, kitchen_model=True, bath_model=False, home_model=False), "HOME-1"
    )
    assert index.by_key("room-1").model["object"] == "home-models/HOME-1/room-1.usdz"


# ------------------------------------------------------------------ lead package link
def _state(room_key: str | None = "room-1") -> FlowState:
    return FlowState(thread_id="t", home_id="HOME-1", active_room_key=room_key,
                     scan=ScanStatus(state="complete"))


def _store_index(tmp_path: Path, index: HomeIndex) -> None:
    home_registry._cache.clear()
    home_registry.save_index("HOME-1", index)


@pytest.mark.asyncio
async def test_a_stored_room_model_becomes_a_signed_link_naming_the_room(storage, monkeypatch, tmp_path):
    index = load_bundle(_bundle(tmp_path, kitchen_model=True, bath_model=False, home_model=True))
    index.by_key("room-1").model["object"] = "home-models/HOME-1/room-1.usdz"
    _store_index(tmp_path, index)

    async def sign(object_path):
        return f"https://example.supabase.co/storage/v1/object/sign/{object_path}?token=x"

    monkeypatch.setattr(supabase_store, "sign_home_model", sign)
    link = await build_model_link(_state())
    assert link["kind"] == "supabase_signed_url"
    assert "room-1.usdz" in link["url"]
    assert link["room"] == "kitchen" and link["source"] == "on-device bake"
    assert "kitchen" in link["note"]


@pytest.mark.asyncio
async def test_the_whole_home_bake_is_the_fallback_when_the_room_has_none(storage, monkeypatch, tmp_path):
    index = load_bundle(_bundle(tmp_path, kitchen_model=False, bath_model=False, home_model=True))
    index.home_model["object"] = "home-models/HOME-1/home.usdz"
    _store_index(tmp_path, index)
    monkeypatch.setattr(supabase_store, "sign_home_model",
                        lambda p: _coro(f"https://signed/{p}"))
    link = await build_model_link(_state())
    assert link["room"] == "whole home" and "home.usdz" in link["url"]


async def _coro(value):
    return value


@pytest.mark.asyncio
async def test_a_model_over_the_cap_is_explained_not_denied(storage, tmp_path):
    index = load_bundle(_bundle(tmp_path, kitchen_model=True, bath_model=False, home_model=False))
    index.by_key("room-1").model["bytes"] = 71 * 1048576
    index.by_key("room-1").model["skipped"] = "71 MB exceeds LIDARAI_MODEL_UPLOAD_MAX_MB=50"
    _store_index(tmp_path, index)
    link = await build_model_link(_state())
    assert link["status"] == "not_available"
    assert "71 MB textured model of the kitchen" in link["reason"]
    assert "LIDARAI_MODEL_UPLOAD_MAX_MB=50" in link["reason"]


@pytest.mark.asyncio
async def test_an_untextured_room_says_texturing_had_not_finished(storage, tmp_path):
    index = load_bundle(_bundle(tmp_path, kitchen_model=False, bath_model=False, home_model=False))
    _store_index(tmp_path, index)
    link = await build_model_link(_state())
    assert link["status"] == "not_available"
    assert "no textured model of the kitchen" in link["reason"]
    assert "texturing" in link["reason"]


@pytest.mark.asyncio
async def test_the_old_contradiction_is_gone(storage, tmp_path):
    """'no per-room model export yet' was said about an export with six of them."""
    index = load_bundle(_bundle(tmp_path, kitchen_model=True, bath_model=True, home_model=True))
    _store_index(tmp_path, index)
    link = await build_model_link(_state())
    assert "no per-room model export" not in json.dumps(link)


# ------------------------------------------------------------------ deletion
@pytest.mark.asyncio
async def test_forgetting_a_home_deletes_its_stored_models_too(storage, monkeypatch, tmp_path):
    calls: list[str] = []

    async def delete_index(home_id):
        calls.append(f"index:{home_id}")
        return True

    async def delete_models(home_id):
        calls.append(f"models:{home_id}")
        return True

    monkeypatch.setattr(supabase_store, "delete_home_index", delete_index)
    monkeypatch.setattr(supabase_store, "delete_home_models", delete_models)
    home_registry.save_index("HOME-1", HomeIndex([], bundle_id="HOME-1"))
    home_registry.forget("HOME-1")
    for _ in range(3):
        await __import__("asyncio").sleep(0)
    assert "models:HOME-1" in calls and "index:HOME-1" in calls


@pytest.mark.asyncio
async def test_delete_home_models_lists_then_removes_every_object(monkeypatch):
    monkeypatch.setattr(settings, "supabase_url", "https://example.supabase.co")
    monkeypatch.setattr(settings, "supabase_service_role_key", "service-key")
    seen: dict = {}

    class Client:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, url, headers=None, json=None, **kw):
            seen["list"] = json
            return httpx.Response(200, json=[{"name": "room-1.usdz"}, {"name": "home.usdz"}],
                                  request=httpx.Request("POST", url))
        async def request(self, method, url, headers=None, json=None, **kw):
            seen["delete"] = (method, json)
            return httpx.Response(200, json={}, request=httpx.Request(method, url))

    monkeypatch.setattr(httpx, "AsyncClient", Client)
    assert await supabase_store.delete_home_models("HOME-1") is True
    assert seen["list"]["prefix"] == "home-models/HOME-1"
    assert seen["delete"] == ("DELETE", {"prefixes": ["home-models/HOME-1/room-1.usdz",
                                                        "home-models/HOME-1/home.usdz"]})
