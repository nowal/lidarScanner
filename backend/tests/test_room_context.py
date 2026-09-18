"""The room context document, with the model's output treated as untrusted.

Every guarantee the appearance pass makes is enforced here rather than requested
in the prompt, so these tests are mostly about what happens when the model
misbehaves: invents a geometry match, returns prose, returns nothing, or fails.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from app import room_context as rc
from tests.test_frame_select import IDENTITY, make_frame, make_room, translated


def make_walls(*sizes: tuple[float, float]) -> list[dict]:
    return [{"dimensions": [width, height, 0.0]} for width, height in sizes]


# A floor surface's polygon lives in its own plane; the transform lays that
# plane flat in a y-up world. Real exports look like this -- local +y maps to
# world -z -- so a fixture using the identity would be a vertical "floor".
FLOOR_PLANE = [1, 0, 0, 0, 0, 0, -1, 0, 0, 1, 0, 0, 0, 0, 0, 1]


def square_floor(side: float) -> list[dict]:
    half = side / 2
    return [
        {
            "transform": list(FLOOR_PLANE),
            "polygonCorners": [
                [-half, -half, 0.0],
                [half, -half, 0.0],
                [half, half, 0.0],
                [-half, half, 0.0],
            ],
            "dimensions": [side, side, 0.0],
        }
    ]


# --------------------------------------------------------------------------
# Measurements come from geometry, never from the model
# --------------------------------------------------------------------------


def test_paintable_area_is_walls_minus_doors_and_windows() -> None:
    """Wall area minus door and window area is the whole calculation."""
    room = {
        "walls": make_walls((4.0, 2.5), (3.0, 2.5)),      # 10.0 + 7.5 = 17.5
        "doors": [{"dimensions": [0.9, 2.0, 0.0]}],       # 1.8
        "windows": [{"dimensions": [1.2, 1.0, 0.0]}],     # 1.2
    }
    measurements = rc.measurements_from_geometry(room)
    assert measurements["wall_m2"] == pytest.approx(17.5)
    assert measurements["paintable_m2"] == pytest.approx(14.5)
    assert measurements["perimeter_m"] == pytest.approx(7.0)


def test_paintable_area_never_goes_negative() -> None:
    """A capture with more opening than wall is bad data, not a negative quote."""
    room = {
        "walls": make_walls((1.0, 1.0)),
        "doors": [{"dimensions": [5.0, 5.0, 0.0]}],
    }
    assert rc.measurements_from_geometry(room)["paintable_m2"] == 0.0


def test_floor_area_uses_the_polygon_not_the_bounding_box() -> None:
    """Shoelace over the real outline; a bounding rectangle overestimates
    L-shaped rooms, and quotes are built on this number."""
    room = {"floors": square_floor(4.0)}
    measurements = rc.measurements_from_geometry(room)
    assert measurements["floor_m2"] == pytest.approx(16.0)
    assert measurements["floor_sqft"] == pytest.approx(16.0 * rc.SQM_TO_SQFT, rel=1e-3)


def test_floor_area_falls_back_to_dimensions_without_a_polygon() -> None:
    room = {"floors": [{"dimensions": [3.0, 2.0, 0.0]}]}
    assert rc.measurements_from_geometry(room)["floor_m2"] == pytest.approx(6.0)


def test_measurements_omit_what_the_geometry_does_not_support() -> None:
    assert rc.measurements_from_geometry({}) == {}


# --------------------------------------------------------------------------
# The model's response is validated structurally
# --------------------------------------------------------------------------


def test_unknown_geometry_match_is_discarded() -> None:
    """The join is the only thing between "two classifiers agreed" and "the
    model asserted agreement", so a label that was never offered is dropped."""
    validated = rc.validate_appearance(
        {"objects": [{"class": "range cooker", "geometry_match": "helicopter"}]},
        ["sink", "storage"],
    )
    assert validated["objects"][0]["geometry_match"] is None


def test_geometry_match_ignores_underscores_and_case() -> None:
    validated = rc.validate_appearance(
        {"objects": [{"class": "settee", "geometry_match": "Sofa"}]},
        ["sofa"],
    )
    assert validated["objects"][0]["geometry_match"] == "sofa"


def test_validation_caps_runaway_output() -> None:
    raw = {
        "objects": [{"class": f"thing {i}"} for i in range(200)],
        "notable": [f"note {i}" for i in range(100)],
        "style": "x" * 5000,
    }
    validated = rc.validate_appearance(raw, [])
    assert len(validated["objects"]) == rc.MAX_OBJECTS
    assert len(validated["notable"]) == rc.MAX_NOTABLE
    assert len(validated["style"]) <= 80


def test_validation_drops_unknown_surfaces_and_nameless_objects() -> None:
    validated = rc.validate_appearance(
        {
            "objects": [{"appearance": "oak"}, {"class": "  "}],
            "surfaces": {"walls": "matt white", "roof": "slate"},
        },
        [],
    )
    assert validated["objects"] == []
    assert validated["surfaces"] == {"walls": "matt white"}


def test_validation_survives_junk() -> None:
    for junk in (None, [], "text", {"objects": "not a list"}):
        validated = rc.validate_appearance(junk, ["sink"])
        assert validated["objects"] == []


def test_parse_json_object_handles_fences_and_prose() -> None:
    assert rc.parse_json_object('```json\n{"room": "kitchen"}\n```')["room"] == "kitchen"
    assert rc.parse_json_object('Here you go: {"room": "bath"} hope that helps')["room"] == "bath"
    assert rc.parse_json_object("no json at all") == {}
    assert rc.parse_json_object('{"broken": ') == {}


# --------------------------------------------------------------------------
# Two-classifier calibration
# --------------------------------------------------------------------------


def test_agreement_is_high_and_a_lone_sighting_is_low() -> None:
    merged = rc.merge_certainty(
        ["sink", "sofa"],
        [
            {"class": "butler sink", "appearance": "white", "geometry_match": "sink"},
            {"class": "rug", "appearance": "wool", "geometry_match": None},
        ],
    )
    by_class = {entry["class"]: entry["certainty"] for entry in merged}
    assert by_class["butler sink"] == "high"
    assert by_class["rug"] == "low"


def test_geometry_the_frames_never_showed_is_kept_as_unobserved() -> None:
    """Dropping these silently loses real features and hides the coverage gap
    that gives a rescan offer an honest reason to fire."""
    merged = rc.merge_certainty(["sink", "fireplace"], [])
    assert {entry["class"] for entry in merged} == {"sink", "fireplace"}
    assert all(entry["certainty"] == "unobserved" for entry in merged)


def test_high_certainty_and_unobserved_partition_the_document() -> None:
    context = {
        "objects": [
            {"class": "a", "certainty": "high"},
            {"class": "b", "certainty": "low"},
            {"class": "c", "certainty": "unobserved"},
        ]
    }
    assert [o["class"] for o in rc.high_certainty(context)] == ["a"]
    assert rc.unobserved(context) == ["c"]


# --------------------------------------------------------------------------
# Building the document
# --------------------------------------------------------------------------


def kitchen_bundle(tmp_path):
    """A minimal on-disk bundle: one room, one object, one frame."""
    room = make_room(("sink", (0.0, 0.0, -2.0), (0.8, 0.8, 0.4)))
    room["walls"] = make_walls((4.0, 2.5))
    room["floors"] = square_floor(4.0)
    room_dir = tmp_path / "rooms" / "room-1"
    (room_dir / "rebuild").mkdir(parents=True)
    (room_dir / "room.json").write_text(json.dumps(room), encoding="utf-8")
    (room_dir / "rebuild" / "manifest.json").write_text(
        json.dumps({"frames": [make_frame("f1", translated(0.0, 0.0, 0.0))]}),
        encoding="utf-8",
    )
    return tmp_path


def test_build_merges_the_two_passes_and_keeps_geometry_measurements(tmp_path, monkeypatch) -> None:
    bundle = kitchen_bundle(tmp_path)
    monkeypatch.setattr(rc, "encode_frame", lambda path, turns: "ZmFrZQ==")

    async def caller(content):
        assert any(block["type"] == "image" for block in content)
        return json.dumps(
            {
                "room": "kitchen",
                "objects": [
                    {"class": "butler sink", "appearance": "white ceramic",
                     "geometry_match": "sink"},
                    {"class": "kettle", "appearance": "chrome", "geometry_match": None},
                ],
                "surfaces": {"walls": "matt cream"},
                "style": "shaker",
                "notable": ["dated splashback"],
            }
        )

    context = asyncio.run(rc.build(bundle, "room-1", caller=caller))
    by_class = {o["class"]: o["certainty"] for o in context["objects"]}
    assert by_class["butler sink"] == "high"
    assert by_class["kettle"] == "low"
    assert context["room"] == "kitchen"
    assert context["measurements"]["paintable_m2"] == pytest.approx(10.0)
    assert context["frames"] == ["f1"]
    assert context["coverage"] == "complete"


def test_a_provider_failure_degrades_to_geometry_rather_than_killing_ingest(tmp_path, monkeypatch) -> None:
    bundle = kitchen_bundle(tmp_path)
    monkeypatch.setattr(rc, "encode_frame", lambda path, turns: "ZmFrZQ==")

    async def caller(content):
        raise RuntimeError("provider down")

    context = asyncio.run(rc.build(bundle, "room-1", caller=caller))
    assert context["coverage"] == "geometry_only"
    assert "provider down" in context["coverage_reason"]
    # The measurements did not need the model and must survive its failure.
    assert context["measurements"]["paintable_m2"] == pytest.approx(10.0)


def test_a_room_with_no_objects_never_calls_the_model(tmp_path) -> None:
    """The real export's room-10 is a closet: no objects, three photos."""
    room = {"objects": [], "walls": make_walls((1.5, 2.4)), "floors": square_floor(1.2)}
    room_dir = tmp_path / "rooms" / "room-10"
    (room_dir / "rebuild").mkdir(parents=True)
    (room_dir / "room.json").write_text(json.dumps(room), encoding="utf-8")
    (room_dir / "rebuild" / "manifest.json").write_text(
        json.dumps({"frames": [make_frame("f1", IDENTITY)]}), encoding="utf-8"
    )

    async def caller(content):  # pragma: no cover -- must not be reached
        raise AssertionError("the model was called for a room with nothing to see")

    context = asyncio.run(rc.build(tmp_path, "room-10", caller=caller))
    assert context["coverage"] == "geometry_only"
    assert context["measurements"]["paintable_m2"] == pytest.approx(3.6)


def test_a_missing_room_json_is_not_a_crash(tmp_path) -> None:
    (tmp_path / "rooms" / "room-9").mkdir(parents=True)
    context = asyncio.run(rc.build(tmp_path, "room-9", caller=None))
    assert context["coverage"] == "geometry_only"
    assert context["objects"] == []


def test_unreadable_images_do_not_reach_the_model(tmp_path) -> None:
    """Frames selected but absent from disk must not produce an image-free
    request that the model answers from the label list alone."""
    bundle = kitchen_bundle(tmp_path)

    async def caller(content):  # pragma: no cover -- must not be reached
        raise AssertionError("called with no images")

    context = asyncio.run(rc.build(bundle, "room-1", caller=caller))
    assert context["coverage"] == "geometry_only"
    assert "unreadable" in context["coverage_reason"]


# --------------------------------------------------------------------------
# Cache
# --------------------------------------------------------------------------


def test_get_caches_and_does_not_call_twice(tmp_path, monkeypatch) -> None:
    """Runs once per room; every conversation afterwards reads the document and
    never touches an image again."""
    bundle = kitchen_bundle(tmp_path)
    monkeypatch.setattr(rc.settings, "storage_dir", str(tmp_path / "storage"))
    monkeypatch.setattr(rc, "encode_frame", lambda path, turns: "ZmFrZQ==")
    calls = []

    async def caller(content):
        calls.append(1)
        return json.dumps({"room": "kitchen", "objects": []})

    first = asyncio.run(rc.get(bundle, "home-1", "room-1", caller=caller))
    second = asyncio.run(rc.get(bundle, "home-1", "room-1", caller=caller))
    assert len(calls) == 1
    assert first["room"] == second["room"] == "kitchen"


def test_a_geometry_only_document_is_a_placeholder_not_a_cache_hit(tmp_path, monkeypatch) -> None:
    """Ingest stores one for every room. Treating it as a hit would mean the
    appearance pass could never run on a room that had been ingested -- and a
    provider outage would serve the degraded document forever."""
    bundle = kitchen_bundle(tmp_path)
    monkeypatch.setattr(rc.settings, "storage_dir", str(tmp_path / "storage"))
    monkeypatch.setattr(rc, "encode_frame", lambda path, turns: "ZmFrZQ==")
    rc.save("home-1", rc.geometry_context(bundle, "room-1"))

    async def caller(content):
        return json.dumps({"room": "kitchen", "objects": []})

    document = asyncio.run(rc.get(bundle, "home-1", "room-1", caller=caller))
    assert document["room"] == "kitchen"
    assert document["coverage"] != "geometry_only"


def test_refresh_forces_a_rebuild(tmp_path, monkeypatch) -> None:
    bundle = kitchen_bundle(tmp_path)
    monkeypatch.setattr(rc.settings, "storage_dir", str(tmp_path / "storage"))
    monkeypatch.setattr(rc, "encode_frame", lambda path, turns: "ZmFrZQ==")
    calls = []

    async def caller(content):
        calls.append(1)
        return json.dumps({"room": "kitchen", "objects": []})

    asyncio.run(rc.get(bundle, "home-1", "room-1", caller=caller))
    asyncio.run(rc.get(bundle, "home-1", "room-1", caller=caller, refresh=True))
    assert len(calls) == 2
