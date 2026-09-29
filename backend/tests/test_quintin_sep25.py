"""Quintin's Sep 25 notes: a detached garage scanned from the outside.

1. The chat opened as if it were a room and could not tell it was a garage.
2. Told it was a detached garage, it had no idea how big the garage was.
3. On exterior scans generally, the agent does not recognise the exterior
   (the Sep 24 fix ran only on ingested homes; the TestFlight build sends a
   context packet and never ingests).

So: one vision pass over the packet's own keyframes on the opening turn
(setting + which building), the LiDAR mesh extent as the size of a capture
that RoomPlan cannot measure, and the app's flat export package adapted into
the rooms/ layout so an auto-uploaded scan ingests as a real capture.
"""

from __future__ import annotations

import base64
import io
import json

import pytest

import app.flow_runtime as flow_runtime
from app import room_context
from app.config import settings
from app.flow import home_registry, ops_email
from app.flow.state import FlowState
from app.home_ai import HomeAIChatRequest, HomeAIContextPacket, HomeAIKeyframe
from app.home_index import HomeIndex, Room, load_bundle


def _jpeg_b64(size: int = 32) -> str:
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (size, size), (200, 180, 160)).save(buffer, format="JPEG")
    return base64.standard_b64encode(buffer.getvalue()).decode()


GARAGE = {
    "room": "exterior",
    "setting": "exterior",
    "structure": "detached garage",
    "objects": [],
    "surfaces": {"walls": "white vinyl siding"},
    "windows": [{"count": 2, "type": "slider", "gridded": False, "where": "side wall"}],
    "style": "plain detached garage",
    "notable": ["roll-up door"],
}
BOUNDS = {"widthMeters": 7.3, "lengthMeters": 6.7, "heightMeters": 3.7}


def _packet(*, images: bool = True, rooms: int = 0) -> HomeAIContextPacket:
    return HomeAIContextPacket(
        roomCount=rooms,
        rooms=[{"name": f"Room {i + 1}", "windowCount": 1} for i in range(rooms)],
        meshSummary={"rawAnchorCount": 3, "boundsMeters": dict(BOUNDS)},
        selectedKeyframes=[HomeAIKeyframe(id="f1", jpegBase64=_jpeg_b64() if images else None)],
    )


def _request(packet: HomeAIContextPacket) -> HomeAIChatRequest:
    return HomeAIChatRequest(threadId="t", message="hi", homeContext=packet)


# ---------------------------------------------------------------- the pass
def test_validate_appearance_keeps_a_known_structure_and_drops_the_rest():
    doc = room_context.validate_appearance({"setting": "exterior", "structure": "Detached Garage"}, [])
    assert doc["structure"] == "detached garage"
    assert room_context.validate_appearance({"structure": "spaceship"}, [])["structure"] == ""


@pytest.mark.asyncio
async def test_describe_frames_sends_the_images_and_returns_the_setting():
    seen = {}

    async def fake(content):
        seen["images"] = sum(1 for block in content if block["type"] == "image")
        seen["text"] = content[-1]["text"]
        return json.dumps(GARAGE)

    doc = await room_context.describe_frames([_jpeg_b64(), _jpeg_b64(), "not base64!!"], caller=fake)
    assert seen["images"] == 2 and "one LiDAR capture" in seen["text"]
    assert doc["setting"] == "exterior" and doc["structure"] == "detached garage"
    assert doc["coverage"] == "photos_only" and doc["frames"] == 2
    assert doc["windows"][0]["count"] == 2


@pytest.mark.asyncio
async def test_describe_frames_shrinks_big_images_and_survives_the_provider():
    big = _jpeg_b64(1600)
    sizes = {}

    async def fake(content):
        raw = base64.standard_b64decode(content[0]["source"]["data"])
        from PIL import Image

        sizes["edge"] = max(Image.open(io.BytesIO(raw)).size)
        raise RuntimeError("provider down")

    assert await room_context.describe_frames([big], caller=fake) == {}
    assert sizes["edge"] == room_context.MAX_IMAGE_EDGE
    assert await room_context.describe_frames(["%%%"], caller=fake) == {}


# ---------------------------------------------------------------- the opening turn
@pytest.mark.asyncio
async def test_the_opening_turn_learns_it_is_a_detached_garage_and_how_big(monkeypatch):
    monkeypatch.setattr(settings, "anthropic_api_key", "k")
    monkeypatch.setattr(settings, "capture_vision_enabled", True)

    async def fake_describe(frames, **_):
        assert len(frames) == 1
        return dict(GARAGE)

    monkeypatch.setattr(room_context, "describe_frames", fake_describe)
    state = FlowState(thread_id="t", client_flow_aware=True)
    request = _request(_packet())
    flow_runtime._remember_mesh_bounds(state, request)
    await flow_runtime._maybe_capture_appearance(state, request, None)
    assert state.scan_appearance["structure"] == "detached garage"
    assert state.scan_mesh_bounds == {**BOUNDS, "roomCount": 0}

    plan = flow_runtime._engine.plan_turn(state, None)
    text = flow_runtime._build_directives(
        state, plan, opening=True, price_guidance=None, quotes_to_present=None
    )
    assert "THIS CAPTURE IS THE EXTERIOR OF A DETACHED GARAGE, not a room" in text
    assert "SEPARATE building from the house" in text
    assert "garage doors" in text
    assert "OVERALL SIZE FROM THE LIDAR MESH: about 24 x 22 ft footprint and 12 ft tall" in text
    assert "RoomPlan found no rooms in this capture" in text
    assert "the outside of their detached garage" in text
    assert "naming the room you can see" not in text
    assert "WINDOWS SEEN IN THE PHOTOS: about 2 individual windows" in text
    assert "white vinyl siding" in text

    # A later turn carries no images and no directive re-runs the pass.
    calls = []

    async def counting(frames, **_):
        calls.append(1)
        return dict(GARAGE)

    monkeypatch.setattr(room_context, "describe_frames", counting)
    await flow_runtime._maybe_capture_appearance(state, request, None)
    assert calls == []
    later = flow_runtime._build_directives(
        state, flow_runtime._engine.plan_turn(state, "how big is it?"),
        opening=False, price_guidance=None, quotes_to_present=None,
    )
    assert "EXTERIOR OF A DETACHED GARAGE" in later and "24 x 22 ft" in later


@pytest.mark.asyncio
async def test_the_pass_is_skipped_without_images_a_key_or_when_an_index_exists(monkeypatch):
    calls = []

    async def counting(frames, **_):
        calls.append(1)
        return dict(GARAGE)

    monkeypatch.setattr(room_context, "describe_frames", counting)
    monkeypatch.setattr(settings, "anthropic_api_key", "k")
    state = FlowState(thread_id="t")
    await flow_runtime._maybe_capture_appearance(state, _request(_packet(images=False)), None)
    await flow_runtime._maybe_capture_appearance(state, _request(_packet()), object())
    monkeypatch.setattr(settings, "anthropic_api_key", "")
    await flow_runtime._maybe_capture_appearance(state, _request(_packet()), None)
    assert calls == [] and state.scan_appearance is None


@pytest.mark.asyncio
async def test_a_failing_or_slow_pass_leaves_the_turn_alone(monkeypatch):
    monkeypatch.setattr(settings, "anthropic_api_key", "k")
    monkeypatch.setattr(settings, "capture_vision_timeout_seconds", 0.05)

    async def slow(frames, **_):
        import asyncio

        await asyncio.sleep(0.5)
        return dict(GARAGE)

    monkeypatch.setattr(room_context, "describe_frames", slow)
    state = FlowState(thread_id="t")
    await flow_runtime._maybe_capture_appearance(state, _request(_packet()), None)
    assert state.scan_appearance is None

    async def boom(frames, **_):
        raise RuntimeError("no")

    monkeypatch.setattr(room_context, "describe_frames", boom)
    await flow_runtime._maybe_capture_appearance(state, _request(_packet()), None)
    assert state.scan_appearance is None
    # And an interior capture with RoomPlan rooms does not get the mesh line.
    state.scan_appearance = {"setting": "interior", "surfaces": {"floor": "oak"}, "objects": []}
    flow_runtime._remember_mesh_bounds(state, _request(_packet(rooms=2)))
    text = "\n".join(flow_runtime._capture_directives(state))
    assert "oak" in text and "OVERALL SIZE" not in text


def test_bad_mesh_bounds_are_ignored():
    state = FlowState(thread_id="t")
    packet = HomeAIContextPacket(meshSummary={"boundsMeters": {"widthMeters": -1, "lengthMeters": 2, "heightMeters": 3}})
    flow_runtime._remember_mesh_bounds(state, _request(packet))
    assert state.scan_mesh_bounds is None


# ---------------------------------------------------------------- the lead package
def test_the_lead_package_says_which_building_and_how_big():
    from app.flow_api import _add_capture_measurements

    state = FlowState(thread_id="t", scan_appearance=dict(GARAGE), scan_mesh_bounds={**BOUNDS, "roomCount": 0})
    measurements = {"note": "Approximate (bounding-box) measurements from the home capture."}
    _add_capture_measurements(state, measurements)
    assert measurements["capture"] == "exterior of a detached garage (from the scan photos)"
    assert measurements["meshExtentFeet"] == {"width": 24.0, "length": 22.0, "height": 12.1}
    lines = "\n".join(ops_email._fmt_measurements(measurements))
    assert "Capture: exterior of a detached garage" in lines
    assert "Overall size (LiDAR mesh, approx.): 24.0 x 22.0 ft footprint, 12.1 ft tall" in lines


def test_mesh_bounds_reach_the_model_context():
    from app.home_context_builder import _compact_mesh_summary

    compact = _compact_mesh_summary({"rawAnchorCount": 3, "boundsMeters": dict(BOUNDS)})
    assert compact["extentFeet"] == {"widthFt": pytest.approx(23.95, abs=0.01), "lengthFt": pytest.approx(21.98, abs=0.01), "heightFt": pytest.approx(12.14, abs=0.01)}
    assert "extentFeet" not in _compact_mesh_summary({"rawAnchorCount": 3})


# ---------------------------------------------------------------- the ingested home
def _exterior_index() -> HomeIndex:
    room = Room(key="room-1", index=1, storey=0, plan_label="", area_sqft=0.0, floor_y=0.0,
                display_name="exterior", role="exterior", window_count=2)
    return HomeIndex([room], bundle_id="h", mesh_bounds=dict(BOUNDS))


def test_the_index_describes_an_exterior_capture_by_its_footprint():
    index = _exterior_index()
    text = index.as_text()
    assert "exterior: the outside of the building, about 24 x 22 ft footprint, 12 ft tall (LiDAR mesh), 2 window opening(s)" in text
    assert "0 sq ft" not in text
    again = HomeIndex.from_json(index.to_json())
    assert again.mesh_bounds == BOUNDS and again.mesh_extent_feet() == index.mesh_extent_feet()


def test_the_active_capture_directive_uses_the_mesh_not_a_zero_area(monkeypatch):
    index = _exterior_index()
    monkeypatch.setattr(home_registry, "room_context_for", lambda h, k: {
        "setting": "exterior", "structure": "detached garage", "surfaces": {}, "objects": [],
        "style": "", "notable": [], "coverage": "complete",
    })
    state = FlowState(thread_id="t", home_id="h", active_room_key="room-1")
    text = "\n".join(flow_runtime._home_directives(state, index))
    assert "ACTIVE CAPTURE: the exterior — the outside of the building, not a room; about 24 x 22 ft footprint and 12 ft tall" in text
    assert "about 0 sq ft" not in text
    assert "EXTERIOR OF A DETACHED GARAGE" in text
    assert "Talk about THIS capture" in text


def test_the_exterior_room_measurements_carry_the_building_not_a_floor_area(monkeypatch):
    from app.flow_quotes import _room_measurements

    index = _exterior_index()
    monkeypatch.setattr(home_registry, "load_index", lambda home_id: index)
    monkeypatch.setattr(home_registry, "room_context_for", lambda h, k: {"structure": "detached garage"})
    state = FlowState(thread_id="t", home_id="h", active_room_key="room-1")
    measurements, key, name = _room_measurements(state)
    assert key == "room-1" and name == "exterior"
    assert measurements["capture"] == "exterior of a detached garage (from the scan photos)"
    assert "floorAreaSquareFeet" not in measurements
    assert measurements["meshExtentFeet"]["width"] == 24.0


# ---------------------------------------------------------------- the flat export package
def _write_package(root, *, roomplan: bool, frames: int = 3):
    pkg = root / "scan_abc123"
    (pkg / "images").mkdir(parents=True)
    (pkg / "metadata").mkdir()
    (pkg / "geometry").mkdir()
    (pkg / "manifest.json").write_text(json.dumps({"scan_id": "abc123", "created_at": "2026-09-25T00:00:00Z", "image_count": frames}))
    records = []
    for i in range(frames):
        name = f"images/frame_{i:06d}.jpg"
        (pkg / name).write_bytes(base64.standard_b64decode(_jpeg_b64()))
        x = 1.0 + i * 0.5
        records.append({
            "frame_index": i, "filename": name, "timestamp": float(i),
            "image_width": 640, "image_height": 480,
            "camera_transform_camera_to_world_4x4": [[1, 0, 0, x], [0, 1, 0, 1.5], [0, 0, 1, 2.0], [0, 0, 0, 1]],
            "camera_position_world": [x, 1.5, 2.0],
            "intrinsics_3x3": [[500, 0, 320], [0, 500, 240], [0, 0, 1]],
            "fx": 500.0, "fy": 500.0, "cx": 320.0, "cy": 240.0,
        })
    (pkg / "metadata" / "frames.json").write_text(json.dumps(records))
    (pkg / "geometry" / "mesh_stats.json").write_text(json.dumps({
        "vertex_count": 10, "face_count": 4, "bounds_min": [-3.65, 0.0, -3.35], "bounds_max": [3.65, 3.7, 3.35],
    }))
    if roomplan:
        (pkg / "roomplan_optional").mkdir()
        room = {
            "floors": [{
                "transform": [1, 0, 0, 0, 0, 0, -1, 0, 0, 1, 0, 0, 0, 0, 0, 1],
                "polygonCorners": [[-3, -3, 0], [3, -3, 0], [3, 3, 0], [-3, 3, 0]],
                "dimensions": [6, 6, 0],
            }],
            "walls": [{"dimensions": [6, 3, 0]}] * 4,
            "windows": [{"dimensions": [0.9, 1.2, 0.1], "transform": [1] * 16}],
            "doors": [], "openings": [], "objects": [], "sections": [],
        }
        (pkg / "roomplan_optional" / "roomplan.json").write_text(json.dumps({"capturedRooms": [room], "segments": []}))
    return pkg


def test_a_flat_export_package_ingests_as_a_capture(tmp_path):
    pkg = _write_package(tmp_path, roomplan=True)
    root = home_registry.unpack_export(pkg, tmp_path / "work")
    assert root == pkg
    manifest = json.loads((pkg / "rooms" / "room-1" / "rebuild" / "manifest.json").read_text())
    frames = manifest["frames"]
    assert [f["id"] for f in frames] == ["frame_000000", "frame_000001", "frame_000002"]
    assert frames[1]["cameraTransform"][12:15] == [1.5, 1.5, 2.0], "translation lands where the bundle expects"
    assert frames[0]["intrinsics"] == [500.0, 0.0, 0.0, 0.0, 500.0, 0.0, 320.0, 240.0, 1.0]
    assert frames[0]["imageResolution"] == [640, 480]
    assert (pkg / "rooms" / "room-1" / "rebuild" / "images" / "frame_000002.jpg").is_file()
    index = load_bundle(pkg)
    assert index.bundle_id == "abc123" and len(index.rooms) == 1
    assert index.rooms[0].window_count == 1 and index.rooms[0].window_openings == [(0.9, 1.2)]
    assert index.mesh_bounds == {"widthMeters": 7.3, "heightMeters": 3.7, "lengthMeters": 6.7}
    assert index.rooms[0].frame_ids, "frames were assigned to the capture"
    # A scan-folder zip is untouched by the adapter.
    assert home_registry.adapt_metashape_package(tmp_path / "nothing-here") is None


@pytest.mark.parametrize("path_kind", ["absolute", "traversal", "symlink"])
def test_flat_package_cannot_import_files_outside_its_images(tmp_path, path_kind):
    pkg = _write_package(tmp_path, roomplan=False)
    outside = tmp_path / "private.jpg"
    outside.write_bytes(b"private data must not be uploaded")
    frames_file = pkg / "metadata" / "frames.json"
    frames = json.loads(frames_file.read_text())
    if path_kind == "absolute":
        frames[0]["filename"] = str(outside)
    elif path_kind == "traversal":
        frames[0]["filename"] = "../private.jpg"
    else:
        (pkg / "images" / "escape.jpg").symlink_to(outside)
        frames[0]["filename"] = "images/escape.jpg"
    frames_file.write_text(json.dumps(frames))
    home_registry.unpack_export(pkg, tmp_path / "work")
    images = list((pkg / "rooms" / "room-1" / "rebuild" / "images").iterdir())
    assert len(images) == 2
    assert all(p.read_bytes() != outside.read_bytes() for p in images)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_nonfinite_mesh_bounds_are_ignored(value):
    state = FlowState(thread_id="t")
    packet = _packet().model_copy(update={"meshSummary": {"boundsMeters": {**BOUNDS, "widthMeters": value}}})
    flow_runtime._remember_mesh_bounds(state, _request(packet))
    assert state.scan_mesh_bounds is None


def test_switching_homes_clears_capture_appearance_and_extent(monkeypatch):
    monkeypatch.setattr(home_registry, "load_index", lambda _: None)
    state = FlowState(thread_id="t", home_id="old", scan_appearance=dict(GARAGE), scan_mesh_bounds=dict(BOUNDS))
    request = _request(_packet()).model_copy(update={"homeId": "new"})
    flow_runtime._reconcile_home(state, request)
    assert state.scan_appearance is None and state.scan_mesh_bounds is None


@pytest.mark.asyncio
async def test_a_package_without_roomplan_still_gets_its_photos_looked_at(tmp_path):
    pkg = _write_package(tmp_path, roomplan=False)
    home_registry.unpack_export(pkg, tmp_path / "work")
    assert json.loads((pkg / "rooms" / "room-1" / "room.json").read_text()) == {}
    index = load_bundle(pkg)
    assert len(index.rooms) == 1 and index.rooms[0].area_sqft == 0

    async def fake(content):
        assert sum(1 for b in content if b["type"] == "image") == 3
        return json.dumps(GARAGE)

    doc = await room_context.build(pkg, "room-1", caller=fake)
    assert doc["setting"] == "exterior" and doc["structure"] == "detached garage"
    assert doc["coverage"] != "geometry_only" and len(doc["frames"]) == 3
