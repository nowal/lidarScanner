"""Pick the few frames that read as "a picture of this room".

Ported from `takeshape/src/frame_select.py`, which scored ARKitScenes frames
against annotated 3D boxes. A TakeShape export needs no annotation file: RoomPlan
`objects` are already oriented boxes with labels, in the same world frame as the
keyframe camera poses, so the scoring runs on the bundle alone.

Everything downstream inherits this choice, so the selection is deterministic and
the geometry is checked rather than assumed:

* The pose convention is **detected**, not declared. ARKit stores camera-to-world
  with -Z forward, but a bundle that disagrees would silently score zero objects
  in every frame; `detect_convention` tries both and `select_frames` raises when
  neither projects anything.
* Frame orientation comes from **gravity**, not from a model or a metadata field.
  Bundle frames are `arkit_captured_image_native_unrotated`; uncorrected they do
  not fail loudly, they make a VLM describe the room sideways and swap floor for
  ceiling -- the layer painting and flooring quotes are built on.

No numpy: `numpy` lives in `requirements-rgbd.txt` and is absent from the chat
backend's environment. The maths here is small enough not to justify pulling it
into the deployed image.
"""

from __future__ import annotations

import math
from typing import Any, Iterable, Sequence

# ponytail: 32x24 occupancy grid for union-of-boxes area. Exact polygon union is
# the "right" answer and buys nothing at this resolution.
GRID_ROWS, GRID_COLS = 24, 32

# Selection defaults. Four images is what the appearance pass uses; the prompt
# path caps attachments separately.
DEFAULT_COUNT = 4
POSITION_THRESHOLD = 1.5  # metres between kept viewpoints
ANGLE_THRESHOLD = 40.0  # degrees between kept view directions
IDEAL_STANDOFF = 1.5  # metres -- where a frame reads as a room, not a close-up

Vec3 = tuple[float, float, float]


# --------------------------------------------------------------------------
# Column-major 4x4 helpers (RoomPlan / ARKit convention)
# --------------------------------------------------------------------------


def _columns(transform: Sequence[float]) -> tuple[Vec3, Vec3, Vec3, Vec3]:
    """Basis vectors and translation of a column-major 4x4."""
    return (
        (transform[0], transform[1], transform[2]),
        (transform[4], transform[5], transform[6]),
        (transform[8], transform[9], transform[10]),
        (transform[12], transform[13], transform[14]),
    )


def _to_world(transform: Sequence[float], point: Vec3) -> Vec3:
    cx, cy, cz, t = _columns(transform)
    x, y, z = point
    return (
        cx[0] * x + cy[0] * y + cz[0] * z + t[0],
        cx[1] * x + cy[1] * y + cz[1] * z + t[1],
        cx[2] * x + cy[2] * y + cz[2] * z + t[2],
    )


def _to_camera(transform: Sequence[float], point: Vec3) -> Vec3:
    """Inverse of a rigid camera-to-world transform, applied to a world point.

    Rotation is orthonormal, so the inverse is the transpose: each output
    component is the dot product of the world offset with one basis column.
    """
    cx, cy, cz, t = _columns(transform)
    dx, dy, dz = point[0] - t[0], point[1] - t[1], point[2] - t[2]
    return (
        cx[0] * dx + cx[1] * dy + cx[2] * dz,
        cy[0] * dx + cy[1] * dy + cy[2] * dz,
        cz[0] * dx + cz[1] * dy + cz[2] * dz,
    )


def camera_pose(transform: Sequence[float]) -> tuple[Vec3, Vec3]:
    """-> (camera centre in world, view direction in world).

    ARKit cameras look down their local -Z, so the view direction is the negated
    third basis column.
    """
    _, _, cz, t = _columns(transform)
    return t, (-cz[0], -cz[1], -cz[2])


def upright_rotation(transform: Sequence[float]) -> int:
    """CCW quarter-turns that make this frame upright.

    Gravity is world +Y in ARKit. Projecting world-up into camera axes recovers
    the true up regardless of how the phone was held. In camera space +Y is up
    while image +y is down, so the image-space up vector is (ux, -uy) and an
    upright frame is the one where that points at (0, -1).
    """
    cx, cy, _, _ = _columns(transform)
    # World up (0, 1, 0) into camera axes picks the y-component of each column.
    up_x, up_y = cx[1], cy[1]
    if abs(up_x) < 1e-9 and abs(up_y) < 1e-9:
        return 0  # camera pointing straight up or down: no meaningful roll
    return int(round(math.atan2(up_x, up_y) / (math.pi / 2))) % 4


# --------------------------------------------------------------------------
# Bundle readers
# --------------------------------------------------------------------------


def object_boxes(room: dict[str, Any]) -> list[tuple[str, list[Vec3]]]:
    """RoomPlan `objects` -> [(label, 8 world corners)].

    This is what the ARKitScenes port needed an annotation file for. RoomPlan
    gives the same thing directly: a transform plus half-extents per object.
    """
    boxes: list[tuple[str, list[Vec3]]] = []
    for obj in room.get("objects") or []:
        transform = obj.get("transform") or []
        dimensions = obj.get("dimensions") or []
        if len(transform) != 16 or len(dimensions) < 3:
            continue
        half = [float(d) / 2.0 for d in dimensions[:3]]
        if not all(math.isfinite(h) for h in half):
            continue
        corners = [
            _to_world(transform, (sx * half[0], sy * half[1], sz * half[2]))
            for sx in (-1, 1)
            for sy in (-1, 1)
            for sz in (-1, 1)
        ]
        boxes.append((_category(obj.get("category")), corners))
    return boxes


def _category(raw: Any) -> str:
    """RoomPlan emits a category as "sink" or {"sink": {}} by version; both
    appear in real exports. Same normalisation `home_index._category` uses."""
    if isinstance(raw, str):
        return raw
    if isinstance(raw, dict) and raw:
        return next(iter(raw.keys()))
    return "unknown"


def frame_records(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    """`rebuild/manifest.json` frames that carry everything projection needs."""
    out = []
    for frame in manifest.get("frames") or []:
        transform = frame.get("cameraTransform") or []
        intrinsics = frame.get("intrinsics") or []
        if not frame.get("id") or len(transform) != 16 or len(intrinsics) != 9:
            continue
        out.append(frame)
    return out


def _pixel_scale(frame: dict[str, Any]) -> tuple[float, float]:
    """Intrinsics are quoted against a reference resolution that need not match
    the stored image. Returns the (x, y) factor to bring them onto the image."""
    reference = frame.get("intrinsicsReferenceResolution") or []
    image = frame.get("imageResolution") or []
    if len(reference) == 2 and len(image) == 2 and reference[0] and reference[1]:
        return image[0] / reference[0], image[1] / reference[1]
    return 1.0, 1.0


def _image_size(frame: dict[str, Any]) -> tuple[float, float]:
    image = frame.get("imageResolution") or frame.get("originalImageResolution") or []
    if len(image) == 2 and image[0] and image[1]:
        return float(image[0]), float(image[1])
    return 0.0, 0.0


# --------------------------------------------------------------------------
# Projection and scoring
# --------------------------------------------------------------------------


def project(
    corners: Iterable[Vec3],
    transform: Sequence[float],
    intrinsics: Sequence[float],
    scale: tuple[float, float],
    *,
    negate_z: bool,
) -> list[tuple[float, float, float]]:
    """World corners -> [(u, v, depth)].

    `negate_z` selects the pose convention: ARKit looks down -Z, so forward
    depth is -z_cam and image +y is -y_cam. A bundle written the other way round
    is caught by `detect_convention` rather than assumed away.
    """
    # Column-major 3x3: fx, fy on the diagonal, principal point in the last column.
    fx, fy = intrinsics[0] * scale[0], intrinsics[4] * scale[1]
    cx, cy = intrinsics[6] * scale[0], intrinsics[7] * scale[1]
    out = []
    for corner in corners:
        x, y, z = _to_camera(transform, corner)
        if negate_z:
            depth, y = -z, -y
        else:
            depth = z
        if depth <= 0.05:
            out.append((0.0, 0.0, depth))
            continue
        out.append((fx * x / depth + cx, fy * y / depth + cy, depth))
    return out


def score_frame(
    boxes: Sequence[tuple[str, list[Vec3]]],
    frame: dict[str, Any],
    *,
    negate_z: bool,
) -> tuple[int, float, float, list[str]]:
    """-> (visible object count, area fraction, nearest object depth, labels)."""
    width, height = _image_size(frame)
    if width <= 0 or height <= 0:
        return 0, 0.0, 0.0, []
    transform = frame["cameraTransform"]
    intrinsics = frame["intrinsics"]
    scale = _pixel_scale(frame)

    grid = [[False] * GRID_COLS for _ in range(GRID_ROWS)]
    seen: list[str] = []
    depths: list[float] = []

    for label, corners in boxes:
        projected = project(corners, transform, intrinsics, scale, negate_z=negate_z)
        front = [p for p in projected if p[2] > 0.05]
        if len(front) < 4:
            continue
        us = [p[0] for p in front]
        vs = [p[1] for p in front]
        u0, u1, v0, v1 = min(us), max(us), min(vs), max(vs)
        if u1 < 0 or v1 < 0 or u0 > width or v0 > height:
            continue
        seen.append(label)
        ordered = sorted(p[2] for p in front)
        depths.append(ordered[len(ordered) // 2])  # median depth of the box

        gu0 = int(min(max(u0 / width, 0.0), 1.0) * (GRID_COLS - 1))
        gu1 = int(min(max(u1 / width, 0.0), 1.0) * (GRID_COLS - 1))
        gv0 = int(min(max(v0 / height, 0.0), 1.0) * (GRID_ROWS - 1))
        gv1 = int(min(max(v1 / height, 0.0), 1.0) * (GRID_ROWS - 1))
        for row in range(gv0, gv1 + 1):
            for col in range(gu0, gu1 + 1):
                grid[row][col] = True

    area = sum(sum(1 for cell in row if cell) for row in grid) / (GRID_ROWS * GRID_COLS)
    return len(seen), area, (min(depths) if depths else 0.0), seen


def distance_weight(near: float, ideal: float = IDEAL_STANDOFF) -> float:
    """Favour room-legible standoff over close-ups and through-doorway shots.

    A frame 0.6m from a cupboard door scores well on area; a frame 2.8m away
    through a doorway scores well on object count. Neither is a picture of the
    room. Peaks at `ideal` metres, falls off either side.
    """
    if near <= 0.05:
        return 0.0
    return 1.0 / (1.0 + abs(math.log(near / ideal)))


def frame_score(count: int, area: float, near: float) -> float:
    return count * area * distance_weight(near)


def detect_convention(
    boxes: Sequence[tuple[str, list[Vec3]]], frames: Sequence[dict[str, Any]]
) -> bool | None:
    """Which sign convention actually puts objects in frame.

    Returns the `negate_z` value to use, or None when neither projects anything
    -- which means the geometry and the poses are not in the same space and no
    selection is trustworthy. The original port called this the dot test.
    """
    if not boxes or not frames:
        return None
    step = max(1, len(frames) // 40)
    sample = list(frames)[::step]
    hits = {}
    for negate_z in (True, False):
        hits[negate_z] = sum(
            score_frame(boxes, frame, negate_z=negate_z)[0] for frame in sample
        )
    if max(hits.values()) == 0:
        return None
    return True if hits[True] >= hits[False] else False


def suppress(rows: list[dict[str, Any]], count: int) -> list[dict[str, Any]]:
    """Greedy non-max suppression over camera pose.

    Top-N by score alone returns near-duplicate frames -- a scan yields ~10 frames
    per second and the best viewpoint wins all of them. Keep the best frame per
    distinct (position, direction) cluster instead.
    """
    cos_threshold = math.cos(math.radians(ANGLE_THRESHOLD))
    kept: list[dict[str, Any]] = []
    # Score descending, then frame id: ties must not depend on dict ordering.
    ordered = sorted(rows, key=lambda r: (-r["score"], r["id"]))
    for row in ordered:
        centre, direction = row["centre"], row["direction"]
        duplicate = False
        for other in kept:
            gap = math.dist(centre, other["centre"])
            alignment = sum(a * b for a, b in zip(direction, other["direction"]))
            if gap < POSITION_THRESHOLD and alignment > cos_threshold:
                duplicate = True
                break
        if duplicate:
            continue
        kept.append(row)
        if len(kept) == count:
            break
    return kept


class ConventionError(RuntimeError):
    """Geometry and camera poses do not project into the same space."""


def score_frames(
    room: dict[str, Any], manifest: dict[str, Any]
) -> list[dict[str, Any]]:
    """Every posed frame that sees at least one object, scored."""
    boxes = object_boxes(room)
    frames = frame_records(manifest)
    negate_z = detect_convention(boxes, frames)
    if negate_z is None:
        raise ConventionError(
            f"no object projects into any frame ({len(boxes)} boxes, {len(frames)} frames)"
        )

    rows = []
    for frame in frames:
        count, area, near, labels = score_frame(boxes, frame, negate_z=negate_z)
        if not count:
            continue
        centre, direction = camera_pose(frame["cameraTransform"])
        rows.append(
            {
                "id": frame["id"],
                "objects": count,
                "area": area,
                "near": near,
                "labels": labels,
                "centre": centre,
                "direction": direction,
                "turns": upright_rotation(frame["cameraTransform"]),
                "score": frame_score(count, area, near),
            }
        )
    return rows


def spread_frames(manifest: dict[str, Any], count: int = DEFAULT_COUNT) -> list[dict[str, Any]]:
    """Fallback for a room with NO detected objects: there is nothing to
    score against, so pick posed frames spread across the walk instead --
    farthest-point sampling over camera position, seeded with the first
    frame so the choice is deterministic. Rows carry the same keys the
    scored path produces (``id``, ``turns``, ``centre``, ``direction``,
    ``score``) so the appearance pass can consume either."""
    frames = frame_records(manifest)
    if not frames:
        return []
    rows = []
    for frame in frames:
        centre, direction = camera_pose(frame["cameraTransform"])
        rows.append({"id": frame["id"], "objects": 0, "area": 0.0, "near": 0.0, "labels": [],
                     "centre": centre, "direction": direction,
                     "turns": upright_rotation(frame["cameraTransform"]), "score": 0.0})
    rows.sort(key=lambda r: r["id"])
    chosen = [rows[0]]
    while len(chosen) < min(count, len(rows)):
        best = max(
            (r for r in rows if r not in chosen),
            key=lambda r: (min(math.dist(r["centre"], c["centre"]) for c in chosen), r["id"]),
        )
        chosen.append(best)
    return chosen


def select_frames(
    room: dict[str, Any], manifest: dict[str, Any], count: int = DEFAULT_COUNT
) -> list[dict[str, Any]]:
    """The entry point: the `count` most room-legible, non-duplicate frames.

    Deterministic for a given bundle -- the appearance pass is cached per scan,
    so a selection that wobbled between runs would produce a context document
    that disagreed with itself.
    """
    return suppress(score_frames(room, manifest), count)
