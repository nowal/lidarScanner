"""Keyframe → room assignment (Noah's "bridge the keyframe and floorplan
data" idea, Sprint-1-safe version).

RoomPlan floor polygons and ARKit keyframe camera poses already live in the
same world coordinate frame — no registration or reconstruction needed
(SOW §6 keeps that out of scope). A camera position tested against each
room's world-space floor polygon tells us *which room a photo was taken in*,
which lets the agent say "in the kitchen view" truthfully in multi-room
homes and lets grounding pick the right room's keyframes.

Inputs are the raw parsed `CapturedRoom` dicts (from `roomJSONBase64List`)
and keyframes carrying the 16-float column-major `cameraTransform` the app
already sends. ARKit world is y-up: floors vary in x/z, so containment is a
2D point-in-polygon on (x, z), with a story check on y when multiple
stories exist.
"""

from __future__ import annotations

from typing import Any

Point2 = tuple[float, float]


def _apply_transform(transform: list[float], point: list[float]) -> tuple[float, float, float]:
    """Column-major 4x4 (RoomPlan/ARKit convention) applied to a 3D point."""
    x, y, z = point[0], point[1], (point[2] if len(point) > 2 else 0.0)
    return (
        transform[0] * x + transform[4] * y + transform[8] * z + transform[12],
        transform[1] * x + transform[5] * y + transform[9] * z + transform[13],
        transform[2] * x + transform[6] * y + transform[10] * z + transform[14],
    )


def world_floor_polygon(floor: dict[str, Any]) -> tuple[list[Point2], float]:
    """A floor surface's polygon in world (x, z), plus its world y height.
    Falls back to the bounding rectangle when polygonCorners is absent."""
    transform = floor.get("transform") or [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1]
    corners = floor.get("polygonCorners")
    if not corners or len(corners) < 3:
        half_w = float(floor.get("dimensions", [0, 0, 0])[0]) / 2
        half_l = float(floor.get("dimensions", [0, 0, 0])[1]) / 2
        corners = [
            [-half_w, -half_l, 0], [half_w, -half_l, 0],
            [half_w, half_l, 0], [-half_w, half_l, 0],
        ]
    world = [_apply_transform(transform, c) for c in corners]
    height = sum(p[1] for p in world) / len(world)
    return [(p[0], p[2]) for p in world], height


def point_in_polygon(point: Point2, polygon: list[Point2]) -> bool:
    """Ray casting; boundary points count as inside."""
    x, y = point
    inside = False
    n = len(polygon)
    for i in range(n):
        x1, y1 = polygon[i]
        x2, y2 = polygon[(i + 1) % n]
        if (y1 > y) != (y2 > y):
            x_cross = x1 + (y - y1) * (x2 - x1) / (y2 - y1)
            if x <= x_cross:
                inside = not inside
    return inside


def camera_position(camera_transform: list[float]) -> tuple[float, float, float]:
    """Translation of a column-major camera-to-world transform."""
    return camera_transform[12], camera_transform[13], camera_transform[14]


def assign_keyframes_to_rooms(
    keyframes: list[dict[str, Any]],
    captured_rooms: list[dict[str, Any]],
    *,
    max_story_height_delta: float = 2.2,
) -> dict[str, int | None]:
    """Map keyframe id → index into captured_rooms (or None if outside all).

    When floor polygons overlap across stories, the room whose floor height
    sits below the camera within one storey wins.
    """
    room_polygons: list[tuple[int, list[Point2], float]] = []
    for index, room in enumerate(captured_rooms):
        for floor in room.get("floors", []):
            polygon, height = world_floor_polygon(floor)
            if polygon:
                room_polygons.append((index, polygon, height))

    assignments: dict[str, int | None] = {}
    for keyframe in keyframes:
        transform = keyframe.get("cameraTransform") or []
        if len(transform) != 16:
            assignments[keyframe.get("id", "?")] = None
            continue
        cx, cy, cz = camera_position(transform)
        best: tuple[float, int] | None = None
        for room_index, polygon, floor_y in room_polygons:
            if not point_in_polygon((cx, cz), polygon):
                continue
            rise = cy - floor_y  # camera should be above its floor
            if rise < -0.5 or rise > max_story_height_delta + 1.0:
                continue
            score = abs(rise - 1.4)  # operator eye height, per the iOS code
            if best is None or score < best[0]:
                best = (score, room_index)
        assignments[keyframe.get("id", "?")] = best[1] if best else None
    return assignments
