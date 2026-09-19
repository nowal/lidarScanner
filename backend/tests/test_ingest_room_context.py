"""Ingest stores room context, and deletion takes it with it.

The bundle is on disk exactly once. Measurements are pure arithmetic, so every
room gets them at ingest; the appearance pass is a model call, so it stays
opt-in per room.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from app import room_context
from app.config import settings
from app.flow import home_registry
from app.flow.home_registry import (
    _cache,
    enrich_room,
    forget,
    ingest_bundle,
    room_context_for,
)

IDENTITY = [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1]
FLOOR_PLANE = [1, 0, 0, 0, 0, 0, -1, 0, 0, 1, 0, 0, 0, 0, 0, 1]


def _floor(width: float, depth: float) -> dict:
    half_w, half_d = width / 2, depth / 2
    return {
        "transform": list(FLOOR_PLANE),
        "polygonCorners": [
            [-half_w, -half_d, 0.0],
            [half_w, -half_d, 0.0],
            [half_w, half_d, 0.0],
            [-half_w, half_d, 0.0],
        ],
        "dimensions": [width, depth, 0.0],
    }


def _write_room(base, index, *, label, width, depth, objects=(), walls=()):
    room_dir = base / "rooms" / f"room-{index}"
    (room_dir / "rebuild").mkdir(parents=True, exist_ok=True)
    (room_dir / "floor.json").write_text(
        json.dumps({"floor": 1, "floorY": 0.0}), encoding="utf-8"
    )
    (room_dir / "room.json").write_text(
        json.dumps(
            {
                "sections": [{"label": label}] if label else [],
                "floors": [_floor(width, depth)],
                "objects": [
                    {
                        "category": {name: {}},
                        "transform": list(IDENTITY),
                        "dimensions": [0.6, 0.9, 0.6],
                    }
                    for name in objects
                ],
                "walls": [{"dimensions": [w, h, 0.0]} for w, h in walls],
                "doors": [],
                "windows": [],
                "openings": [],
            }
        ),
        encoding="utf-8",
    )
    (room_dir / "rebuild" / "manifest.json").write_text(
        json.dumps({"frames": []}), encoding="utf-8"
    )


@pytest.fixture
def bundle(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "storage_dir", str(tmp_path / "storage"))
    _cache.clear()
    base = tmp_path / "bundle"
    (base / "rooms").mkdir(parents=True)
    (base / "meta.json").write_text(json.dumps({"id": "HOUSE"}), encoding="utf-8")
    _write_room(base, 1, label="kitchen", width=5.0, depth=4.0,
                objects=["sink", "stove"], walls=((5.0, 2.4), (4.0, 2.4)))
    _write_room(base, 2, label="bathroom", width=2.0, depth=2.0,
                objects=["toilet"], walls=((2.0, 2.4),))
    yield base
    _cache.clear()


# --------------------------------------------------------------------------


def test_ingest_stores_measurements_for_every_room(bundle) -> None:
    """The bundle is on disk exactly once; these numbers cannot be recovered
    afterwards, so ingest is the only chance to take them."""
    index = ingest_bundle(bundle, "home-1")
    assert len(index.rooms) == 2
    for room in index.rooms:
        document = room_context_for("home-1", room.key)
        assert document is not None
        assert document["measurements"]["floor_m2"] > 0


def test_ingest_measures_the_kitchen_correctly(bundle) -> None:
    ingest_bundle(bundle, "home-1")
    measurements = room_context_for("home-1", "room-1")["measurements"]
    assert measurements["floor_m2"] == pytest.approx(20.0)
    # (5.0 + 4.0) * 2.4 of wall, nothing subtracted.
    assert measurements["paintable_m2"] == pytest.approx(21.6)
    assert measurements["mean_wall_height_m"] == pytest.approx(2.4)


def test_ingest_costs_no_model_call(bundle, monkeypatch) -> None:
    """A nineteen-room walk-through must not fire nineteen appearance passes."""

    async def explode(content):  # pragma: no cover -- must not be reached
        raise AssertionError("ingest called the model")

    monkeypatch.setattr(room_context, "_call_model", explode)
    ingest_bundle(bundle, "home-1")
    assert room_context_for("home-1", "room-1")["coverage"] == "geometry_only"


def test_ingest_reads_no_images(bundle, monkeypatch) -> None:
    def explode(path, turns):  # pragma: no cover -- must not be reached
        raise AssertionError("ingest read a keyframe")

    monkeypatch.setattr(room_context, "encode_frame", explode)
    ingest_bundle(bundle, "home-1")


def test_one_bad_room_does_not_take_the_ingest_down(bundle, monkeypatch) -> None:
    """The index is the thing the conversation cannot run without, and it is
    already saved by the time contexts are stored."""
    real = room_context.geometry_context

    def sometimes_broken(bundle_dir, room_key):
        if room_key == "room-2":
            raise ValueError("corrupt geometry")
        return real(bundle_dir, room_key)

    monkeypatch.setattr(room_context, "geometry_context", sometimes_broken)
    index = ingest_bundle(bundle, "home-1")
    assert len(index.rooms) == 2
    assert room_context_for("home-1", "room-1") is not None
    assert room_context_for("home-1", "room-2") is None


def test_store_room_geometry_reports_what_it_stored(bundle) -> None:
    index = ingest_bundle(bundle, "home-1")
    assert home_registry.store_room_geometry(bundle, "home-2", index) == 2


# --------------------------------------------------------------------------


def test_enrich_room_runs_the_appearance_pass_for_one_room(bundle, monkeypatch) -> None:
    monkeypatch.setattr(room_context, "encode_frame", lambda path, turns: "ZmFrZQ==")
    monkeypatch.setattr(
        room_context,
        "select_context_frames",
        lambda room, manifest, count: [{"id": "f1", "turns": 0}],
    )
    calls = []

    async def caller(content):
        calls.append(1)
        return json.dumps(
            {
                "room": "kitchen",
                "objects": [{"class": "belfast sink", "geometry_match": "sink"}],
            }
        )

    monkeypatch.setattr(room_context, "_call_model", caller)
    ingest_bundle(bundle, "home-1")
    document = asyncio.run(enrich_room(bundle, "home-1", "room-1"))
    assert len(calls) == 1
    assert document["room"] == "kitchen"
    assert [o["certainty"] for o in document["objects"] if o["class"] == "belfast sink"] == ["high"]
    # The enriched document replaces the geometry-only one in the cache.
    assert room_context_for("home-1", "room-1")["room"] == "kitchen"


def test_enrich_room_keeps_the_measurements_ingest_computed(bundle, monkeypatch) -> None:
    monkeypatch.setattr(room_context, "encode_frame", lambda path, turns: "ZmFrZQ==")
    monkeypatch.setattr(
        room_context,
        "select_context_frames",
        lambda room, manifest, count: [{"id": "f1", "turns": 0}],
    )

    async def caller(content):
        return json.dumps({"room": "kitchen", "objects": []})

    monkeypatch.setattr(room_context, "_call_model", caller)
    ingest_bundle(bundle, "home-1")
    document = asyncio.run(enrich_room(bundle, "home-1", "room-1"))
    assert document["measurements"]["paintable_m2"] == pytest.approx(21.6)


def test_room_context_for_is_none_without_ids(bundle) -> None:
    ingest_bundle(bundle, "home-1")
    assert room_context_for(None, "room-1") is None
    assert room_context_for("home-1", None) is None
    assert room_context_for("home-1", "room-99") is None


# --------------------------------------------------------------------------


def test_forgetting_a_home_removes_its_room_contexts(bundle) -> None:
    """These documents describe someone's home in words. A deletion that left
    them behind would not be a deletion (SOW section 12)."""
    ingest_bundle(bundle, "home-1")
    assert room_context_for("home-1", "room-1") is not None
    forget("home-1")
    assert room_context_for("home-1", "room-1") is None
    assert room_context_for("home-1", "room-2") is None


def test_forgetting_one_home_leaves_another_alone(bundle) -> None:
    index = ingest_bundle(bundle, "home-1")
    home_registry.store_room_geometry(bundle, "home-2", index)
    forget("home-1")
    assert room_context_for("home-2", "room-1") is not None


def test_forgetting_an_unknown_home_is_not_an_error(bundle) -> None:
    assert room_context.forget("never-ingested") == 0
