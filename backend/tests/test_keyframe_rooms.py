"""Tests for keyframe→room assignment, including against the real
CapturedRoom fixture's floor polygon."""

import json
from pathlib import Path

import pytest

from app.flow.keyframe_rooms import (
    assign_keyframes_to_rooms,
    point_in_polygon,
    world_floor_polygon,
)

FIXTURE = (
    Path(__file__).resolve().parents[3]
    / "takeshape-mobile"
    / "LidarAITests"
    / "captured-room.json"
)


def _identity_room(cx: float, cz: float, w: float, l: float, floor_y: float = 0.0):
    """Axis-aligned rectangular room centered at (cx, cz). The transform
    mirrors real RoomPlan floors: the local XY polygon plane maps onto the
    horizontal world XZ plane (local z becomes world-up y)."""
    return {
        "floors": [
            {
                # column-major: local x→world x, local y→world z, local z→world y
                "transform": [1, 0, 0, 0, 0, 0, 1, 0, 0, 1, 0, 0, cx, floor_y, cz, 1],
                "polygonCorners": [[-w / 2, -l / 2, 0], [w / 2, -l / 2, 0],
                                    [w / 2, l / 2, 0], [-w / 2, l / 2, 0]],
                "dimensions": [w, l, 0],
            }
        ]
    }


def _camera_at(x: float, y: float, z: float):
    return [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, x, y, z, 1]


def test_point_in_polygon_basics():
    square = [(0, 0), (4, 0), (4, 4), (0, 4)]
    assert point_in_polygon((2, 2), square)
    assert not point_in_polygon((5, 2), square)


def test_two_room_assignment():
    rooms = [_identity_room(0, 0, 4, 4), _identity_room(10, 0, 4, 4)]
    keyframes = [
        {"id": "in-room-0", "cameraTransform": _camera_at(0.5, 1.4, 0.5)},
        {"id": "in-room-1", "cameraTransform": _camera_at(10.2, 1.4, -0.7)},
        {"id": "hallway", "cameraTransform": _camera_at(5.0, 1.4, 0.0)},
    ]
    result = assign_keyframes_to_rooms(keyframes, rooms)
    assert result == {"in-room-0": 0, "in-room-1": 1, "hallway": None}


def test_story_disambiguation():
    ground = _identity_room(0, 0, 4, 4, floor_y=0.0)
    upstairs = _identity_room(0, 0, 4, 4, floor_y=2.8)  # same footprint, story up
    keyframes = [
        {"id": "downstairs", "cameraTransform": _camera_at(0, 1.4, 0)},
        {"id": "upstairs", "cameraTransform": _camera_at(0, 4.2, 0)},
    ]
    result = assign_keyframes_to_rooms(keyframes, [ground, upstairs])
    assert result == {"downstairs": 0, "upstairs": 1}


@pytest.mark.skipif(not FIXTURE.exists(), reason="fixture not present")
def test_real_fixture_floor_polygon_contains_room_center():
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    polygon, height = world_floor_polygon(data["floors"][0])
    assert len(polygon) >= 10  # a real, non-rectangular footprint
    center = (
        sum(p[0] for p in polygon) / len(polygon),
        sum(p[1] for p in polygon) / len(polygon),
    )
    assert point_in_polygon(center, polygon)
    # A camera standing in the room maps to this room.
    camera = [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, center[0], height + 1.4, center[1], 1]
    result = assign_keyframes_to_rooms(
        [{"id": "kf", "cameraTransform": camera}], [data]
    )
    assert result == {"kf": 0}
