"""Build a HomeAIContextPacket — with REAL keyframe photos — from an
ARKitScenes 3dod scene (github.com/apple/ARKitScenes).

Purpose: stand-in for TakeShape sample bundles while we wait for them —
real rooms, real camera trajectories, real furniture (with ground-truth
labels, which makes opener-grounding checkable). Keyframe selection mirrors
the iOS builder: farthest-point sampling over camera positions.

LICENSE NOTE: ARKitScenes ships under Apple's research-oriented license.
Use for internal validation only — never in client-facing demos or anything
shipped. Swap to TakeShape bundles the moment they arrive.

Usage:
    python scripts/arkitscenes_context.py <scene_dir> [--out context.json]
where <scene_dir> is the extracted 3dod folder containing
``{video_id}_frames/`` etc.
"""

from __future__ import annotations

import base64
import io
import json
import math
import struct
import sys
from pathlib import Path
from typing import Any

SQM_TO_SQFT = 10.7639


# ---------------------------------------------------------------- trajectory
def load_trajectory(traj_path: Path) -> list[tuple[str, list[float]]]:
    """Rows: timestamp rx ry rz tx ty tz (axis-angle + translation).
    Returns (timestamp_string, [tx, ty, tz]) per frame."""
    rows = []
    for line in traj_path.read_text(encoding="utf-8").splitlines():
        parts = line.split()
        if len(parts) >= 7:
            rows.append((parts[0], [float(parts[4]), float(parts[5]), float(parts[6])]))
    return rows


def _axis_angle_to_matrix(rx: float, ry: float, rz: float) -> list[list[float]]:
    theta = math.sqrt(rx * rx + ry * ry + rz * rz)
    if theta < 1e-9:
        return [[1, 0, 0], [0, 1, 0], [0, 0, 1]]
    kx, ky, kz = rx / theta, ry / theta, rz / theta
    c, s = math.cos(theta), math.sin(theta)
    v = 1 - c
    return [
        [kx * kx * v + c, kx * ky * v - kz * s, kx * kz * v + ky * s],
        [ky * kx * v + kz * s, ky * ky * v + c, ky * kz * v - kx * s],
        [kz * kx * v - ky * s, kz * ky * v + kx * s, kz * kz * v + c],
    ]


def camera_transform_column_major(row: list[float]) -> list[float]:
    """Full traj row [rx,ry,rz,tx,ty,tz] → 16-float column-major 4x4."""
    rot = _axis_angle_to_matrix(row[0], row[1], row[2])
    tx, ty, tz = row[3], row[4], row[5]
    cols = []
    for c in range(3):
        cols.extend([rot[0][c], rot[1][c], rot[2][c], 0.0])
    cols.extend([tx, ty, tz, 1.0])
    return cols


def farthest_point_sample(rows: list[tuple[str, list[float]]], count: int) -> list[int]:
    """Mirror of the iOS diverseKeyframes: first + greedy max-min-distance."""
    if len(rows) <= count:
        return list(range(len(rows)))
    chosen = [0, len(rows) - 1]
    while len(chosen) < count:
        best_index, best_distance = None, -1.0
        for i, (_, p) in enumerate(rows):
            if i in chosen:
                continue
            d = min(
                sum((p[k] - rows[j][1][k]) ** 2 for k in range(3)) for j in chosen
            )
            if d > best_distance:
                best_distance, best_index = d, i
        chosen.append(best_index)
    return sorted(chosen)


# -------------------------------------------------------------------- assets
def _frames_dir(scene_dir: Path) -> Path:
    frames = next(scene_dir.glob("*_frames"), None)
    if frames is None:
        raise FileNotFoundError(f"No *_frames directory under {scene_dir}")
    return frames


def png_to_jpeg_base64(png_path: Path, quality: int = 86) -> tuple[str, list[int]]:
    from PIL import Image

    with Image.open(png_path) as im:
        rgb = im.convert("RGB")
        # ARKitScenes lowres frames are stored sensor-landscape for portrait
        # captures; rotate so rooms read upright to a vision model.
        rgb = rgb.rotate(-90, expand=True)
        buf = io.BytesIO()
        rgb.save(buf, format="JPEG", quality=quality)
        return base64.b64encode(buf.getvalue()).decode("ascii"), [rgb.width, rgb.height]


def load_annotation_objects(scene_dir: Path) -> list[dict[str, Any]]:
    ann_path = next(scene_dir.glob("*_3dod_annotation.json"), None)
    if ann_path is None:
        return []
    data = json.loads(ann_path.read_text(encoding="utf-8"))
    objects = []
    for item in data.get("data", []):
        label = item.get("label", "object")
        lengths = (
            item.get("segments", {}).get("obbAligned", {}).get("axesLengths", [0, 0, 0])
        )
        objects.append(
            {
                "category": str(label),
                "widthMeters": round(float(lengths[0]), 2),
                "heightMeters": round(float(lengths[1]), 2),
                "depthMeters": round(float(lengths[2]), 2),
            }
        )
    return objects


def mesh_floor_area_sqm(scene_dir: Path) -> float | None:
    """XY extent of the ARKit mesh (binary little-endian PLY) as a rough
    footprint. Minimal parser: reads the header, then x,y,z per vertex."""
    ply_path = next(scene_dir.glob("*_3dod_mesh.ply"), None)
    if ply_path is None:
        return None
    try:
        with ply_path.open("rb") as f:
            vertex_count, properties, fmt = 0, [], ""
            while True:
                line = f.readline().decode("ascii", errors="replace").strip()
                if line.startswith("format"):
                    fmt = line.split()[1]
                elif line.startswith("element vertex"):
                    vertex_count = int(line.split()[-1])
                    in_vertex = True
                elif line.startswith("element"):
                    in_vertex = False
                elif line.startswith("property") and vertex_count and in_vertex:
                    properties.append(line.split()[-2:])
                elif line == "end_header":
                    break
            if fmt != "binary_little_endian" or not vertex_count:
                return None
            type_sizes = {"float": 4, "uchar": 1, "int": 4, "double": 8}
            stride = sum(type_sizes.get(t, 4) for t, _ in properties)
            step = max(1, vertex_count // 20000)  # sample for speed
            xs, ys = [], []
            payload = f.read(vertex_count * stride)
            for i in range(0, vertex_count, step):
                offset = i * stride
                x, y = struct.unpack_from("<ff", payload, offset)[0], struct.unpack_from(
                    "<f", payload, offset + 4
                )[0]
                xs.append(x)
                ys.append(y)
            if not xs:
                return None
            area = (max(xs) - min(xs)) * (max(ys) - min(ys))
            return area if 2.0 < area < 2000.0 else None
    except Exception:  # noqa: BLE001 — footprint is a nice-to-have
        return None


# ------------------------------------------------------------------- context
def context_from_scene(scene_dir: Path, *, keyframes: int = 4) -> dict[str, Any]:
    scene_dir = Path(scene_dir)
    frames = _frames_dir(scene_dir)
    traj = load_trajectory(frames / "lowres_wide.traj")
    rgb_dir = frames / "lowres_wide"

    # Map traj timestamps to frame files ({video}_{timestamp}.png, 3 decimals).
    available = {p.stem.split("_")[-1]: p for p in rgb_dir.glob("*.png")}
    rows = [(ts, pos) for ts, pos in traj if f"{float(ts):.3f}" in available]
    if not rows:  # timestamp formats occasionally differ; fall back to order
        files = sorted(rgb_dir.glob("*.png"))
        rows = [(p.stem.split("_")[-1], [i * 0.1, 0, 0]) for i, p in enumerate(files)]
        available = {ts: p for (ts, _), p in zip(rows, files)}

    selected = farthest_point_sample(rows, keyframes)
    full_traj = {
        parts[0]: [float(v) for v in parts[1:7]]
        for parts in (
            line.split()
            for line in (frames / "lowres_wide.traj").read_text().splitlines()
        )
        if len(parts) >= 7
    }
    selected_keyframes = []
    for index in selected:
        ts, _ = rows[index]
        path = available.get(f"{float(ts):.3f}") or available.get(ts)
        jpeg_b64, resolution = png_to_jpeg_base64(path)
        selected_keyframes.append(
            {
                "id": f"arkit-{ts}",
                "capturedAt": None,
                "timestamp": float(ts),
                "cameraTransform": camera_transform_column_major(
                    full_traj.get(ts, [0, 0, 0, 0, 0, 0])
                ),
                "imageResolution": resolution,
                "jpegBase64": jpeg_b64,
            }
        )

    objects = load_annotation_objects(scene_dir)
    area_sqm = mesh_floor_area_sqm(scene_dir)
    room: dict[str, Any] = {
        "id": "room-1",
        "name": "Room 1",
        "type": "RoomPlan captured area",
        "objectCount": len(objects),
        "objects": objects[:12],
        "wallCount": 4,
        "doorCount": 0,
        "windowCount": 0,
        "openingCount": 0,
    }
    if area_sqm:
        room["floorAreaSquareMeters"] = round(area_sqm, 2)
    totals: dict[str, Any] = {"roomCount": 1, "objectCount": len(objects)}
    if area_sqm:
        totals["floorAreaSquareMeters"] = round(area_sqm, 2)
    return {
        "contextVersion": "home_ai_context_v1",
        "roomCount": 1,
        "rooms": [room],
        "totals": totals,
        "floorplanSummary": (
            f"1 captured area, about {round(area_sqm * SQM_TO_SQFT)} sq ft."
            if area_sqm
            else "1 captured area."
        ),
        "meshSummary": {
            "photorealStatus": "processing",
            "keyframeCount": len(selected_keyframes),
        },
        "selectedKeyframes": selected_keyframes,
        "notes": [],
    }


if __name__ == "__main__":
    scene = Path(sys.argv[1])
    out = None
    if "--out" in sys.argv:
        out = Path(sys.argv[sys.argv.index("--out") + 1])
    context = context_from_scene(scene)
    sizes = [len(k["jpegBase64"]) for k in context["selectedKeyframes"]]
    print(
        f"keyframes: {len(sizes)} (base64 sizes {sizes}); "
        f"objects: {[o['category'] for o in context['rooms'][0]['objects']]}; "
        f"summary: {context['floorplanSummary']}"
    )
    if out:
        out.write_text(json.dumps(context), encoding="utf-8")
        print(f"wrote {out} ({out.stat().st_size} bytes)")
