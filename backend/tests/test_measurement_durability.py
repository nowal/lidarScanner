"""Room measurements survive a redeploy, and the lead package uses them.

PR #30 computed paintable area, floor area and perimeter at ingest and cached
them on local disk. The host's disk is wiped on every redeploy and the bundle
is gone by then, so the numbers now ride on the home index, which is the
durable copy. These tests pin that, plus the operator entry point for the
appearance pass and the ingest default for ``home_id``.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from app import room_context
from app.config import settings
from app.flow import home_registry, supabase_store
from app.flow.home_registry import enrich_rooms, ingest_bundle, load_index, room_context_for
from app.flow.state import FlowState
from app.flow_quotes import _room_measurements
from app.home_index import HomeIndex

IDENTITY = [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1]
# Local XY floor polygon onto the world XZ plane (y up), as real RoomPlan floors.
FLOOR_PLANE = [1, 0, 0, 0, 0, 0, -1, 0, 0, 1, 0, 0, 0, 0, 0, 1]


def _floor(width: float, depth: float) -> dict:
    hw, hd = width / 2, depth / 2
    return {
        "transform": list(FLOOR_PLANE),
        "polygonCorners": [[-hw, -hd, 0.0], [hw, -hd, 0.0], [hw, hd, 0.0], [-hw, hd, 0.0]],
        "dimensions": [width, depth, 0.0],
    }


def _write_room(base, index, *, label, width, depth, objects=(), walls=(), doors=()):
    room_dir = base / "rooms" / f"room-{index}"
    (room_dir / "rebuild").mkdir(parents=True, exist_ok=True)
    (room_dir / "floor.json").write_text(json.dumps({"floor": 1, "floorY": 0.0}), encoding="utf-8")
    (room_dir / "room.json").write_text(json.dumps({
        "sections": [{"label": label}] if label else [],
        "floors": [_floor(width, depth)],
        "objects": [{"category": {name: {}}, "transform": list(IDENTITY), "dimensions": [0.6, 0.9, 0.6]}
                    for name in objects],
        "walls": [{"dimensions": [w, h, 0.0]} for w, h in walls],
        "doors": [{"dimensions": [w, h, 0.0]} for w, h in doors],
        "windows": [], "openings": [],
    }), encoding="utf-8")
    (room_dir / "rebuild" / "manifest.json").write_text(json.dumps({"frames": []}), encoding="utf-8")


@pytest.fixture
def bundle(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "storage_dir", str(tmp_path / "storage"))
    home_registry._cache.clear()
    base = tmp_path / "bundle"
    (base / "rooms").mkdir(parents=True)
    (base / "meta.json").write_text(json.dumps({"id": "SCAN-UUID"}), encoding="utf-8")
    # Kitchen: 5x4 floor, 2.4 m walls, one door.
    _write_room(base, 1, label="kitchen", width=5.0, depth=4.0, objects=["sink", "stove"],
                walls=((5.0, 2.4), (4.0, 2.4), (5.0, 2.4), (4.0, 2.4)), doors=((0.9, 2.0),))
    # Stairway: walls report storey height, the open-volume case.
    _write_room(base, 2, label="", width=2.0, depth=3.0, objects=["stairs"],
                walls=((2.0, 5.1), (3.0, 5.1)))
    yield base
    home_registry._cache.clear()


# ------------------------------------------------------------- on the index
def test_ingest_attaches_measurements_to_the_index(bundle):
    index = ingest_bundle(bundle, "home-1")
    kitchen = index.resolve("kitchen")
    assert kitchen.measurements["floor_m2"] == pytest.approx(20.0)
    # 2 * (5 + 4) * 2.4 wall, minus the 0.9 x 2.0 door.
    assert kitchen.measurements["paintable_m2"] == pytest.approx(43.2 - 1.8)
    assert kitchen.measurements["paintable_sqft"] == pytest.approx(41.4 * 10.7639, rel=1e-3)


def test_measurements_survive_a_json_roundtrip(bundle):
    index = ingest_bundle(bundle, "home-1")
    again = HomeIndex.from_json(json.loads(json.dumps(index.to_json())))
    assert again.resolve("kitchen").measurements == index.resolve("kitchen").measurements


def test_the_stored_index_file_carries_the_measurements(bundle):
    ingest_bundle(bundle, "home-1")
    home_registry._cache.clear()
    assert load_index("home-1").resolve("kitchen").measurements["paintable_sqft"] > 0


# -------------------------------------------------- after the disk is wiped
def test_room_context_is_rebuilt_from_the_index_when_the_cache_is_gone(bundle, tmp_path):
    ingest_bundle(bundle, "home-1")
    cached = room_context_for("home-1", "room-1")
    assert cached["coverage"] == "geometry_only"

    for path in (tmp_path / "storage" / "room_context").rglob("*.json"):
        path.unlink()
    assert room_context.load("home-1", "room-1") is None

    rebuilt = room_context_for("home-1", "room-1")
    assert rebuilt is not None
    assert rebuilt["coverage"] == "geometry_only"
    assert rebuilt["coverage_reason"] == "rebuilt from the home index"
    assert rebuilt["measurements"] == cached["measurements"]
    assert {o["class"] for o in rebuilt["objects"]} == {"sink", "stove"}


def test_a_lookup_for_an_unknown_room_leaves_no_directory_behind(bundle, tmp_path):
    ingest_bundle(bundle, "home-1")
    assert room_context_for("never-ingested", "room-1") is None
    assert not (tmp_path / "storage" / "room_context" / "never-ingested").exists()


@pytest.fixture
def durable(monkeypatch, tmp_path):
    """A stand-in for Supabase Storage, as in test_durability_fixes."""
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


@pytest.mark.asyncio
async def test_measurements_survive_a_redeploy(bundle, durable, tmp_path):
    """The failure this exists for: ingest on Monday, redeploy on Tuesday,
    quote request on Wednesday. The paintable area must still be there."""
    ingest_bundle(bundle, "home-1")
    await asyncio.sleep(0)   # let the fire-and-forget durable write land
    assert durable["home-1"]["rooms"][0]["measurements"]["paintable_sqft"] > 0

    # The redeploy: cold process, empty disk.
    home_registry._cache.clear()
    for path in (tmp_path / "storage").rglob("*.json"):
        path.unlink()
    assert load_index("home-1") is None
    assert room_context.load("home-1", "room-1") is None

    rehydrated = await home_registry.load_index_async("home-1")
    assert rehydrated.resolve("kitchen").measurements["paintable_sqft"] > 0
    assert room_context_for("home-1", "room-1")["measurements"]["paintable_sqft"] > 0


# ------------------------------------------------------------ lead package
def test_the_lead_package_carries_paintable_area(bundle):
    ingest_bundle(bundle, "home-1")
    state = FlowState(thread_id="t", home_id="home-1", active_room_key="room-1")
    measurements, key, name = _room_measurements(state)
    assert key == "room-1" and name == "kitchen"
    assert measurements["paintableWallSquareFeet"] == pytest.approx(41.4 * 10.7639, rel=1e-3)
    assert measurements["perimeterFeet"] == pytest.approx(18.0 * 3.28084, rel=1e-3)
    assert "measurementCaveat" not in measurements


def test_an_open_volume_room_gets_a_caveat_not_a_correction(bundle):
    """A stairwell reports storey-height walls. The number is surfaced with a
    warning for a human; it is never silently adjusted."""
    ingest_bundle(bundle, "home-1")
    state = FlowState(thread_id="t", home_id="home-1", active_room_key="room-2")
    measurements, _, _ = _room_measurements(state)
    assert measurements["paintableWallSquareFeet"] == pytest.approx(25.5 * 10.7639, rel=1e-3)
    assert "5.1 m" in measurements["measurementCaveat"]
    assert "upper bound" in measurements["measurementCaveat"]


# ------------------------------------------------------- operator entry points
def test_ingest_defaults_the_home_id_to_the_scan_id(bundle):
    """The app and the server agree on a home's name without anyone typing
    it: the scan UUID in meta.json is the home id."""
    index = ingest_bundle(bundle)
    assert index.bundle_id == "SCAN-UUID"
    assert load_index("SCAN-UUID") is not None


def test_ingest_without_any_id_is_an_error(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "storage_dir", str(tmp_path / "storage"))
    base = tmp_path / "noid"
    (base / "rooms").mkdir(parents=True)
    # No meta.json: bundle_id falls back to the directory name, which is fine;
    # an empty id is the only thing refused.
    index = ingest_bundle(base)
    assert index.bundle_id == "noid"
    with pytest.raises(ValueError):
        ingest_bundle(base, "   ")


def test_enrich_rooms_runs_the_appearance_pass_for_every_room(bundle, monkeypatch):
    monkeypatch.setattr(room_context, "encode_frame", lambda path, turns: "ZmFrZQ==")
    monkeypatch.setattr(room_context, "select_context_frames",
                        lambda room, manifest, count: [{"id": "f1", "turns": 0}])
    calls: list[int] = []

    async def caller(content):
        calls.append(1)
        return json.dumps({"room": "kitchen", "objects": [{"class": "belfast sink", "geometry_match": "sink"}]})

    monkeypatch.setattr(room_context, "_call_model", caller)
    ingest_bundle(bundle, "home-1")

    documents = asyncio.run(enrich_rooms(bundle, "home-1"))
    assert set(documents) == {"room-1", "room-2"}
    assert len(calls) == 2
    assert room_context_for("home-1", "room-1")["room"] == "kitchen"
    # Measurements computed at ingest are kept through the pass.
    assert documents["room-1"]["measurements"]["paintable_sqft"] > 0


def test_enrich_rooms_can_target_one_room(bundle, monkeypatch):
    monkeypatch.setattr(room_context, "encode_frame", lambda path, turns: "ZmFrZQ==")
    monkeypatch.setattr(room_context, "select_context_frames",
                        lambda room, manifest, count: [{"id": "f1", "turns": 0}])
    calls: list[int] = []

    async def caller(content):
        calls.append(1)
        return json.dumps({"room": "stairs", "objects": []})

    monkeypatch.setattr(room_context, "_call_model", caller)
    ingest_bundle(bundle, "home-1")
    documents = asyncio.run(enrich_rooms(bundle, "home-1", ["room-2"]))
    assert list(documents) == ["room-2"] and len(calls) == 1
    assert room_context_for("home-1", "room-1")["coverage"] == "geometry_only"
