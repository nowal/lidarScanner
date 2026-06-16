from __future__ import annotations

import asyncio
from array import array
import base64
import binascii
import importlib
import json
import logging
import math
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable, Protocol

from PIL import Image, ImageEnhance

from .models import JobStage, ScanPayloadEnvelope


logger = logging.getLogger("lidarai.pipeline")


class StageReporter(Protocol):
    async def __call__(self, stage: JobStage, progress: float, message: str) -> None: ...


class CancellationCheck(Protocol):
    def __call__(self) -> bool: ...


class PipelineStage(Protocol):
    name: JobStage

    async def run(self, job_dir: Path, report: StageReporter, is_cancelled: CancellationCheck) -> None: ...


@dataclass
class FusedMesh:
    vertices: list[tuple[float, float, float]]
    faces: list[tuple[int, int, int]]
    stats: dict


@dataclass
class ProjectionKeyframe:
    image: Image.Image
    width: int
    height: int
    world_to_camera: list[float]
    camera_position: tuple[float, float, float]
    intrinsics: list[float]
    pixels: object
    id: str | None = None
    path: str | None = None
    color_correction: dict | None = None


@dataclass
class TextureProjectionCandidate:
    keyframe: ProjectionKeyframe
    score: float
    visible_vertex_count: int
    center_projection: tuple[float, float, float]
    facing: float
    center_edge_margin: float


@dataclass
class DecodedDepthFrame:
    id: str | None
    color_keyframe_id: str | None
    path: str
    confidence_path: str | None
    width: int
    height: int
    intrinsics: list[float]
    camera_transform: list[float]
    timestamp: float | None


FALLBACK_COLOR = (148, 148, 144)
TEXTURE_ATLAS_MAX_SIZE = 4096
TEXTURE_TSDF_ATLAS_MAX_SIZE = 6144
TEXTURE_TILE_MAX_SIZE = 96
TEXTURE_TILE_MIN_SIZE = 4
TEXTURE_RENDER_TARGET_MIN_TILE_SIZE = 12
TEXTURE_RENDER_TARGET_FACE_COUNT = 120_000
TEXTURE_TSDF_RENDER_TARGET_FACE_COUNT = 150_000
TEXTURE_RENDER_MIN_CLUSTER_METERS = 0.006
TEXTURE_RENDER_MAX_CLUSTER_METERS = 0.08
TEXTURE_RENDER_SMOOTHING_ITERATIONS = 8
TEXTURE_RENDER_SMOOTHING_STRENGTH = 0.45
TEXTURE_RENDER_SMOOTHING_BOUNDARY_STRENGTH = 0.16
TEXTURE_RENDER_SMOOTHING_HARD_EDGE_WEIGHT = 0.08
TEXTURE_RENDER_SMOOTHING_NORMAL_COSINE = 0.72
TEXTURE_RENDER_SMOOTHING_MAX_TOTAL_DISPLACEMENT_METERS = 0.10
TEXTURE_ISLAND_DILATION_PIXELS = 4
TEXTURE_COLOR_SATURATION_BOOST = 1.12
TEXTURE_COLOR_CONTRAST_BOOST = 1.05
TEXTURE_BLEND_MAX_FACE_CANDIDATES = 6
TEXTURE_BLEND_MAX_PIXEL_SAMPLES = 5
TEXTURE_BLEND_MIN_FACING = 0.18
TEXTURE_BLEND_EDGE_MARGIN_RATIO = 0.018
TEXTURE_REJECT_OVEREXPOSED_LUMINANCE = 245
TEXTURE_REJECT_UNDEREXPOSED_LUMINANCE = 8
TEXTURE_REJECT_LOW_DETAIL_RANGE = 10
RGBD_VOXEL_LENGTH_METERS = 0.05
RGBD_SDF_TRUNC_METERS = 0.16
RGBD_DEPTH_TRUNC_METERS = 6.0
RGBD_TSDF_SMOOTHING_ITERATIONS = 8
RGBD_TSDF_MIN_COMPONENT_TRIANGLES = 300
RGBD_TSDF_MIN_COMPONENT_FACE_RATIO = 0.0015
RGBD_TSDF_MIN_COMPONENT_AREA_M2 = 0.015
RGBD_DEPTH_MESH_MAX_FRAMES = 36
RGBD_DEPTH_MESH_TARGET_SAMPLES_PER_FRAME = 16_384
RGBD_DEPTH_MESH_VERTEX_QUANTIZATION = 0.006


class ValidationStage:
    name = JobStage.preprocessing

    async def run(self, job_dir: Path, report: StageReporter, is_cancelled: CancellationCheck) -> None:
        await report(self.name, 5, "Validating uploaded payload")
        payload_path = job_dir / "upload" / "scan_payload.json"
        raw = payload_path.read_text(encoding="utf-8")
        payload = ScanPayloadEnvelope.model_validate(json.loads(raw))
        mesh_count = len(payload.meshAnchors)
        keyframe_count = len(payload.images)
        depth_frame_count = len(payload.depthFrames or [])
        summary = {
            "schemaVersion": payload.schemaVersion,
            "createdAt": payload.createdAt.isoformat(),
            "meshAnchorCount": mesh_count,
            "keyframeCount": keyframe_count,
            "depthFrameCount": depth_frame_count,
            "hasRoomPlan": payload.roomJSONBase64 is not None,
            "roomPlanAreaCount": len(payload.roomJSONBase64List) or (1 if payload.roomJSONBase64 else 0),
            "hasCapturedStructure": payload.structureJSONBase64 is not None,
            "roomPlanSegmentCount": len(payload.roomPlanSegments),
        }
        (job_dir / "work" / "payload_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
        if is_cancelled():
            raise asyncio.CancelledError
        await report(self.name, 20, f"Validated {mesh_count} mesh anchors and {keyframe_count} keyframes")


class GeometryFusionStage:
    name = JobStage.meshing

    async def run(self, job_dir: Path, report: StageReporter, is_cancelled: CancellationCheck) -> None:
        await report(self.name, 35, "Fusing ARKit mesh anchors into a cleaned world-space mesh")
        src = job_dir / "upload" / "scan_payload.json"
        payload = json.loads(src.read_text(encoding="utf-8"))
        mesh_anchors = payload.get("meshAnchors", [])
        (job_dir / "work" / "raw_mesh_anchors.json").write_text(json.dumps(mesh_anchors, indent=2), encoding="utf-8")

        mesh = fuse_mesh_anchors(mesh_anchors)
        write_fused_mesh_json(mesh, job_dir / "work" / "fused_mesh.json")
        write_fused_mesh_json(mesh, job_dir / "work" / "arkit_fused_mesh.json")
        obj_path = job_dir / "work" / "fused_mesh.obj"
        write_obj(mesh, obj_path)
        write_obj(mesh, job_dir / "work" / "arkit_fused_mesh.obj")
        (job_dir / "work" / "mesh_stats.json").write_text(json.dumps(mesh.stats, indent=2), encoding="utf-8")
        (job_dir / "work" / "arkit_mesh_stats.json").write_text(json.dumps(mesh.stats, indent=2), encoding="utf-8")
        await asyncio.sleep(0.2)
        if is_cancelled():
            raise asyncio.CancelledError
        await report(
            self.name,
            50,
            f"Fused OBJ exported with {mesh.stats['vertexCount']} vertices and {mesh.stats['faceCount']} faces",
        )


class KeyframeDecodeStage:
    name = JobStage.texturing

    async def run(self, job_dir: Path, report: StageReporter, is_cancelled: CancellationCheck) -> None:
        await report(self.name, 58, "Decoding camera keyframes")
        payload = json.loads((job_dir / "upload" / "scan_payload.json").read_text(encoding="utf-8"))
        keyframe_dir = job_dir / "work" / "keyframes"
        keyframe_dir.mkdir(parents=True, exist_ok=True)

        manifest = []
        for index, image in enumerate(payload.get("images", []), start=1):
            if is_cancelled():
                raise asyncio.CancelledError

            raw_base64 = image.get("jpegBase64")
            if not raw_base64:
                continue

            try:
                image_bytes = base64.b64decode(raw_base64, validate=True)
            except (binascii.Error, ValueError) as exc:
                raise ValueError(f"Invalid base64 JPEG for keyframe {index}") from exc

            filename = f"keyframe_{index:03d}.jpg"
            (keyframe_dir / filename).write_bytes(image_bytes)
            manifest.append({
                "id": image.get("id"),
                "capturedAt": image.get("capturedAt"),
                "timestamp": image.get("timestamp"),
                "cameraTransform": image.get("cameraTransform"),
                "intrinsics": image.get("intrinsics"),
                "imageResolution": image.get("imageResolution"),
                "path": f"keyframes/{filename}",
                "byteCount": len(image_bytes),
            })

        (job_dir / "work" / "keyframe_manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        await asyncio.sleep(0.1)
        await report(self.name, 66, f"Decoded {len(manifest)} keyframes")


class DepthFrameDecodeStage:
    name = JobStage.preprocessing

    async def run(self, job_dir: Path, report: StageReporter, is_cancelled: CancellationCheck) -> None:
        await report(self.name, 68, "Decoding compact LiDAR depth frames")
        payload = json.loads((job_dir / "upload" / "scan_payload.json").read_text(encoding="utf-8"))
        depth_dir = job_dir / "work" / "depth_frames"
        depth_dir.mkdir(parents=True, exist_ok=True)

        manifest = []
        for index, depth_frame in enumerate(payload.get("depthFrames") or [], start=1):
            if is_cancelled():
                raise asyncio.CancelledError

            resolution = depth_frame.get("depthResolution") or []
            if len(resolution) != 2:
                continue
            width = int(resolution[0])
            height = int(resolution[1])
            if width <= 0 or height <= 0:
                continue

            raw_base64 = depth_frame.get("depthBase64")
            if not raw_base64:
                continue
            try:
                depth_bytes = base64.b64decode(raw_base64, validate=True)
            except (binascii.Error, ValueError) as exc:
                raise ValueError(f"Invalid base64 depth frame {index}") from exc
            expected_depth_bytes = width * height * 4
            if len(depth_bytes) != expected_depth_bytes:
                raise ValueError(
                    f"Depth frame {index} byte count mismatch: expected={expected_depth_bytes} actual={len(depth_bytes)}"
                )

            depth_filename = f"depth_{index:03d}.f32"
            (depth_dir / depth_filename).write_bytes(depth_bytes)
            confidence_filename = None
            confidence_base64 = depth_frame.get("confidenceBase64")
            if confidence_base64:
                try:
                    confidence_bytes = base64.b64decode(confidence_base64, validate=True)
                except (binascii.Error, ValueError) as exc:
                    raise ValueError(f"Invalid base64 confidence frame {index}") from exc
                if len(confidence_bytes) == width * height:
                    confidence_filename = f"depth_{index:03d}.confidence.u8"
                    (depth_dir / confidence_filename).write_bytes(confidence_bytes)

            manifest.append({
                "id": depth_frame.get("id"),
                "colorKeyframeId": depth_frame.get("colorKeyframeId"),
                "capturedAt": depth_frame.get("capturedAt"),
                "timestamp": depth_frame.get("timestamp"),
                "cameraTransform": depth_frame.get("cameraTransform"),
                "intrinsics": depth_frame.get("intrinsics"),
                "depthResolution": [width, height],
                "depthFormat": depth_frame.get("depthFormat"),
                "path": f"depth_frames/{depth_filename}",
                "confidenceFormat": depth_frame.get("confidenceFormat") if confidence_filename else None,
                "confidencePath": f"depth_frames/{confidence_filename}" if confidence_filename else None,
                "metersPerUnit": depth_frame.get("metersPerUnit", 1),
                "byteCount": len(depth_bytes),
            })

        (job_dir / "work" / "depth_frame_manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        await report(self.name, 70, f"Decoded {len(manifest)} depth frames")


class RGBDGeometryFusionStage:
    name = JobStage.meshing

    async def run(self, job_dir: Path, report: StageReporter, is_cancelled: CancellationCheck) -> None:
        await report(self.name, 72, "Trying RGBD TSDF fusion")
        keyframes = json.loads((job_dir / "work" / "keyframe_manifest.json").read_text(encoding="utf-8"))
        depth_frames = json.loads((job_dir / "work" / "depth_frame_manifest.json").read_text(encoding="utf-8"))
        stats_path = job_dir / "work" / "rgbd_fusion_stats.json"

        if not depth_frames:
            stats_path.write_text(json.dumps({
                "available": False,
                "used": False,
                "reason": "No compact depth frames were uploaded.",
                "geometrySource": "arkit_mesh_anchor_fusion",
            }, indent=2), encoding="utf-8")
            await report(self.name, 74, "No depth frames; using ARKit mesh anchor fusion")
            return

        try:
            stats = write_rgbd_tsdf_mesh(
                keyframes=keyframes,
                depth_frames=depth_frames,
                work_dir=job_dir / "work",
                output_obj_path=job_dir / "work" / "rgbd_fused_mesh.obj",
                output_json_path=job_dir / "work" / "rgbd_fused_mesh.json",
            )
        except RGBDFusionUnavailable as tsdf_exc:
            try:
                stats = write_rgbd_keyframe_depth_mesh(
                    keyframes=keyframes,
                    depth_frames=depth_frames,
                    work_dir=job_dir / "work",
                    output_obj_path=job_dir / "work" / "rgbd_fused_mesh.obj",
                    output_json_path=job_dir / "work" / "rgbd_fused_mesh.json",
                    tsdf_unavailable_reason=str(tsdf_exc),
                )
            except RGBDFusionUnavailable as fallback_exc:
                stats = {
                    "available": False,
                    "used": False,
                    "reason": str(fallback_exc),
                    "tsdfUnavailableReason": str(tsdf_exc),
                    "depthFrameCount": len(depth_frames),
                    "geometrySource": "arkit_mesh_anchor_fusion",
                }

        stats_path.write_text(json.dumps(stats, indent=2), encoding="utf-8")
        if stats.get("used"):
            shutil.copyfile(job_dir / "work" / "rgbd_fused_mesh.obj", job_dir / "work" / "fused_mesh.obj")
            shutil.copyfile(job_dir / "work" / "rgbd_fused_mesh.json", job_dir / "work" / "fused_mesh.json")
            mesh = read_fused_mesh_json(job_dir / "work" / "fused_mesh.json")
            mesh.stats.update({
                "geometrySource": stats.get("geometrySource", "rgbd_fusion"),
                "rgbdFusion": stats,
            })
            write_fused_mesh_json(mesh, job_dir / "work" / "fused_mesh.json")
            write_obj(mesh, job_dir / "work" / "fused_mesh.obj")
            (job_dir / "work" / "mesh_stats.json").write_text(json.dumps(mesh.stats, indent=2), encoding="utf-8")
            await report(self.name, 78, f"RGBD TSDF mesh fused with {stats['vertexCount']} vertices")
        else:
            await report(self.name, 76, f"RGBD fusion skipped: {stats.get('reason', 'unavailable')}")


class TexturedMeshStage:
    name = JobStage.texturing

    async def run(self, job_dir: Path, report: StageReporter, is_cancelled: CancellationCheck) -> None:
        await report(self.name, 72, "Projecting keyframes into a texture atlas")
        keyframes = json.loads((job_dir / "work" / "keyframe_manifest.json").read_text(encoding="utf-8"))
        mesh = read_fused_mesh_json(job_dir / "work" / "fused_mesh.json")
        loaded_keyframes = load_projection_keyframes(keyframes, job_dir / "work" / "keyframes")

        colored_stats = await write_vertex_colored_ply(
            vertices=mesh.vertices,
            faces=mesh.faces,
            keyframes=loaded_keyframes,
            output_path=job_dir / "work" / "colored_mesh.ply",
            report_progress=lambda progress, message: report(self.name, progress, message),
            is_cancelled=is_cancelled,
        )
        texture_mesh = make_texture_render_mesh(mesh)
        render_mesh_stats = texture_mesh.stats.get("textureRenderMesh", {})
        await report(
            self.name,
            82,
            f"Allocating texture atlas for {len(texture_mesh.faces)} render faces",
        )
        textured_stats = await write_textured_obj(
            mesh=texture_mesh,
            keyframes=loaded_keyframes,
            output_obj_path=job_dir / "work" / "textured_mesh.obj",
            output_mtl_path=job_dir / "work" / "textured_mesh.mtl",
            output_texture_path=job_dir / "work" / "textured_mesh_texture.png",
            output_debug_path=job_dir / "work" / "texture_debug.json",
            output_debug_preview_path=job_dir / "work" / "texture_debug_preview.png",
            report_progress=lambda progress, message: report(self.name, progress, message),
            is_cancelled=is_cancelled,
        )

        (job_dir / "work" / "uv_map.json").write_text(json.dumps({
            "strategy": textured_stats["uvStrategy"],
            "atlasWidth": textured_stats["atlasWidth"],
            "atlasHeight": textured_stats["atlasHeight"],
            "tileSize": textured_stats["tileSize"],
            "tilePadding": textured_stats["tilePadding"],
            "uvCoordinateCount": textured_stats["uvCoordinateCount"],
            "renderMesh": render_mesh_stats,
            "debugPath": "texture_debug.json",
            "note": "Textured OBJ uses a display render mesh with larger per-face atlas islands, padding, and dilation. Raw fused mesh artifacts remain available separately.",
        }, indent=2), encoding="utf-8")
        (job_dir / "work" / "texture_manifest.json").write_text(json.dumps({
            "sourceKeyframes": len(keyframes),
            "usableProjectionKeyframes": len(loaded_keyframes),
            "debugVertexColorPreview": {
                "format": "ply",
                "path": "colored_mesh.ply",
                "coloredVertexCount": colored_stats["coloredVertexCount"],
                "coverage": colored_stats["coverage"],
            },
            "texturedMesh": {
                **textured_stats,
                "format": "obj",
                "objPath": "textured_mesh.obj",
                "mtlPath": "textured_mesh.mtl",
                "texturePath": "textured_mesh_texture.png",
            },
            "textureDebug": {
                "format": "json",
                "path": "texture_debug.json",
                "previewPath": "texture_debug_preview.png",
                "stats": textured_stats["diagnostics"],
            },
            "usdz": {
                "available": False,
                "path": None,
                "reason": "USDZ conversion is represented in the manifest but not enabled in this backend milestone.",
            },
            "glb": {
                "available": False,
                "path": None,
                "reason": "GLB export is not supported by the current iOS viewer.",
            },
        }, indent=2), encoding="utf-8")
        await asyncio.sleep(0.2)
        if is_cancelled():
            raise asyncio.CancelledError
        await report(
            self.name,
            94,
            f"Textured {int(textured_stats['projectionCoverage'] * 100)}% of mesh faces into OBJ atlas",
        )


class ExportStage:
    name = JobStage.postprocessing

    async def run(self, job_dir: Path, report: StageReporter, is_cancelled: CancellationCheck) -> None:
        await report(self.name, 95, "Exporting result bundle")
        result_dir = job_dir / "result"
        result_dir.mkdir(parents=True, exist_ok=True)
        work_dir = job_dir / "work"
        summary = json.loads((work_dir / "payload_summary.json").read_text(encoding="utf-8"))
        mesh_stats = json.loads((work_dir / "mesh_stats.json").read_text(encoding="utf-8"))
        keyframes = json.loads((work_dir / "keyframe_manifest.json").read_text(encoding="utf-8"))
        texture_manifest = json.loads((work_dir / "texture_manifest.json").read_text(encoding="utf-8"))

        for filename in [
            "fused_mesh.obj",
            "arkit_fused_mesh.obj",
            "rgbd_fused_mesh.obj",
            "colored_mesh.ply",
            "textured_mesh.obj",
            "textured_mesh.mtl",
            "textured_mesh_texture.png",
            "texture_debug.json",
            "texture_debug_preview.png",
        ]:
            src = work_dir / filename
            if src.exists():
                shutil.copyfile(src, result_dir / filename)

        (result_dir / "keyframe_manifest.json").write_text(json.dumps(keyframes, indent=2), encoding="utf-8")
        for optional_manifest in ["arkit_mesh_stats.json", "depth_frame_manifest.json", "rgbd_fusion_stats.json"]:
            src = work_dir / optional_manifest
            if src.exists():
                (result_dir / optional_manifest).write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
        rgbd_stats = json.loads((work_dir / "rgbd_fusion_stats.json").read_text(encoding="utf-8")) if (work_dir / "rgbd_fusion_stats.json").exists() else {
            "available": False,
            "used": False,
            "reason": "RGBD fusion stage did not run.",
        }
        preferred_photoreal = "textured_obj" if (result_dir / "textured_mesh.obj").exists() else "vertex_colored_ply"
        artifact_manifest = {
            "version": "v1",
            "preferredPhotorealArtifact": preferred_photoreal,
            "artifacts": {
                "rawFusedMesh": {
                    "role": "raw_fused_mesh",
                    "format": "obj",
                    "path": "fused_mesh.obj",
                    "stats": mesh_stats,
                },
                "arkitFusedMeshDebug": {
                    "role": "arkit_mesh_anchor_debug",
                    "format": "obj",
                    "path": "arkit_fused_mesh.obj",
                    "statsPath": "arkit_mesh_stats.json",
                    "available": (result_dir / "arkit_fused_mesh.obj").exists(),
                },
                "rgbdFusedMesh": {
                    "role": "rgbd_tsdf_fused_mesh",
                    "format": "obj",
                    "path": "rgbd_fused_mesh.obj",
                    "stats": rgbd_stats,
                    "available": (result_dir / "rgbd_fused_mesh.obj").exists(),
                },
                "vertexColoredPlyDebugPreview": {
                    "role": "vertex_colored_debug_preview",
                    "format": "ply",
                    "path": "colored_mesh.ply",
                    "stats": texture_manifest["debugVertexColorPreview"],
                },
                "texturedObj": {
                    "role": "photoreal_textured_mesh",
                    "format": "obj",
                    "objPath": "textured_mesh.obj",
                    "mtlPath": "textured_mesh.mtl",
                    "texturePath": "textured_mesh_texture.png",
                    "stats": texture_manifest["texturedMesh"],
                },
                "textureDebug": {
                    "role": "texture_diagnostics",
                    "format": "json",
                    "path": "texture_debug.json",
                    "previewPath": "texture_debug_preview.png",
                    "available": (result_dir / "texture_debug.json").exists(),
                    "stats": texture_manifest.get("textureDebug", {}).get("stats", {}),
                },
                "usdz": {
                    "role": "photoreal_textured_mesh",
                    "format": "usdz",
                    **texture_manifest["usdz"],
                },
                "glb": {
                    "role": "photoreal_textured_mesh",
                    "format": "glb",
                    **texture_manifest["glb"],
                },
            },
            "artifactFiles": [
                "reconstructed_scene.json",
                "manifest.json",
                "fused_mesh.obj",
                "arkit_fused_mesh.obj",
                "rgbd_fused_mesh.obj",
                "colored_mesh.ply",
                "textured_mesh.obj",
                "textured_mesh.mtl",
                "textured_mesh_texture.png",
                "texture_debug.json",
                "texture_debug_preview.png",
                "keyframe_manifest.json",
                "arkit_mesh_stats.json",
                "depth_frame_manifest.json",
                "rgbd_fusion_stats.json",
            ],
        }
        (result_dir / "reconstructed_scene.json").write_text(json.dumps({
            "version": "v1",
            "summary": summary,
            "mesh": {
                "rawFusedMesh": artifact_manifest["artifacts"]["rawFusedMesh"],
                "arkitFusedMeshDebug": artifact_manifest["artifacts"]["arkitFusedMeshDebug"],
                "rgbdFusedMesh": artifact_manifest["artifacts"]["rgbdFusedMesh"],
            },
            "debugPreview": artifact_manifest["artifacts"]["vertexColoredPlyDebugPreview"],
            "photoreal": {
                "preferredArtifact": preferred_photoreal,
                "texturedObj": artifact_manifest["artifacts"]["texturedObj"],
                "textureDebug": artifact_manifest["artifacts"]["textureDebug"],
                "usdz": artifact_manifest["artifacts"]["usdz"],
                "glb": artifact_manifest["artifacts"]["glb"],
            },
            "keyframes": keyframes,
            "preferredPreview": "textured_mesh.obj" if preferred_photoreal == "textured_obj" else "colored_mesh.ply",
        }, indent=2), encoding="utf-8")
        (result_dir / "manifest.json").write_text(json.dumps(artifact_manifest, indent=2), encoding="utf-8")
        if is_cancelled():
            raise asyncio.CancelledError
        await report(self.name, 100, "Export complete")


DEFAULT_PIPELINE: list[PipelineStage] = [
    ValidationStage(),
    GeometryFusionStage(),
    KeyframeDecodeStage(),
    DepthFrameDecodeStage(),
    RGBDGeometryFusionStage(),
    TexturedMeshStage(),
    ExportStage(),
]


class RGBDFusionUnavailable(RuntimeError):
    pass


def pair_rgbd_frames(
    keyframes: list[dict],
    depth_frames: list[dict],
    work_dir: Path,
) -> list[tuple[dict, dict, Path, Path]]:
    keyframe_by_id = {str(keyframe.get("id")): keyframe for keyframe in keyframes if keyframe.get("id")}
    paired_frames = []

    for depth_frame in depth_frames:
        color_id = depth_frame.get("colorKeyframeId")
        keyframe = keyframe_by_id.get(str(color_id)) if color_id else closest_keyframe(depth_frame, keyframes)
        if not keyframe:
            continue
        if len(depth_frame.get("cameraTransform") or []) != 16 or len(depth_frame.get("intrinsics") or []) != 9:
            continue
        color_path = work_dir / "keyframes" / Path(keyframe.get("path", "")).name
        depth_path = work_dir / Path(depth_frame.get("path", ""))
        if not color_path.exists() or not depth_path.exists():
            continue
        paired_frames.append((keyframe, depth_frame, color_path, depth_path))

    return paired_frames


def evenly_sample_items(items: list, limit: int) -> list:
    if limit <= 0 or len(items) <= limit:
        return items

    if limit == 1:
        return [items[len(items) // 2]]

    last_index = len(items) - 1
    return [
        items[round(index * last_index / (limit - 1))]
        for index in range(limit)
    ]


def write_rgbd_keyframe_depth_mesh(
    keyframes: list[dict],
    depth_frames: list[dict],
    work_dir: Path,
    output_obj_path: Path,
    output_json_path: Path,
    tsdf_unavailable_reason: str,
) -> dict:
    paired_frames = pair_rgbd_frames(keyframes, depth_frames, work_dir)
    if not paired_frames:
        raise RGBDFusionUnavailable("No RGB/depth frames could be paired for fallback depth mesh fusion.")

    selected_frames = evenly_sample_items(paired_frames, RGBD_DEPTH_MESH_MAX_FRAMES)
    vertices: list[tuple[float, float, float]] = []
    faces: list[tuple[int, int, int]] = []
    vertex_lookup: dict[tuple[int, int, int], int] = {}
    seen_faces: set[tuple[int, int, int]] = set()
    integrated_count = 0
    skipped_frame_count = 0
    invalid_depth_count = 0
    duplicate_vertex_count = 0
    rejected_face_count = 0
    sample_steps: list[int] = []

    for _keyframe, depth_frame, _color_path, depth_path in selected_frames:
        width, height = [int(value) for value in depth_frame["depthResolution"]]
        intrinsics = depth_frame.get("intrinsics") or []
        transform = depth_frame.get("cameraTransform") or []
        if width < 2 or height < 2 or len(intrinsics) != 9 or len(transform) != 16:
            skipped_frame_count += 1
            continue

        depth_values = read_float32_depth_values(depth_path, width, height)
        confidence_values = read_confidence_values(depth_frame, work_dir, width, height)
        fx = float(intrinsics[0])
        fy = float(intrinsics[4])
        cx = float(intrinsics[6])
        cy = float(intrinsics[7])
        if abs(fx) < 1e-6 or abs(fy) < 1e-6:
            skipped_frame_count += 1
            continue

        sample_step = max(1, int(math.ceil(math.sqrt((width * height) / RGBD_DEPTH_MESH_TARGET_SAMPLES_PER_FRAME))))
        sample_steps.append(sample_step)
        x_samples = list(range(0, width, sample_step))
        y_samples = list(range(0, height, sample_step))
        grid: list[list[int | None]] = []
        depth_grid: list[list[float]] = []

        for source_y in y_samples:
            row: list[int | None] = []
            depth_row: list[float] = []
            for source_x in x_samples:
                source_index = source_y * width + source_x
                depth = float(depth_values[source_index])
                if confidence_values is not None and confidence_values[source_index] == 0:
                    depth = 0
                if not math.isfinite(depth) or depth <= 0 or depth > RGBD_DEPTH_TRUNC_METERS:
                    invalid_depth_count += 1
                    row.append(None)
                    depth_row.append(0)
                    continue

                world = backproject_depth_sample_to_world(
                    source_x=source_x,
                    source_y=source_y,
                    depth=depth,
                    intrinsics=intrinsics,
                    camera_transform=transform,
                )
                if not all(math.isfinite(component) for component in world):
                    invalid_depth_count += 1
                    row.append(None)
                    depth_row.append(0)
                    continue

                key = quantized_vertex_key(world, RGBD_DEPTH_MESH_VERTEX_QUANTIZATION)
                vertex_index = vertex_lookup.get(key)
                if vertex_index is None:
                    vertex_index = len(vertices)
                    vertex_lookup[key] = vertex_index
                    vertices.append(world)
                else:
                    duplicate_vertex_count += 1

                row.append(vertex_index)
                depth_row.append(depth)

            grid.append(row)
            depth_grid.append(depth_row)

        frame_face_count_before = len(faces)
        for y in range(len(grid) - 1):
            for x in range(len(grid[y]) - 1):
                top_left = grid[y][x]
                top_right = grid[y][x + 1]
                bottom_left = grid[y + 1][x]
                bottom_right = grid[y + 1][x + 1]
                d_tl = depth_grid[y][x]
                d_tr = depth_grid[y][x + 1]
                d_bl = depth_grid[y + 1][x]
                d_br = depth_grid[y + 1][x + 1]
                rejected_face_count += add_depth_mesh_face(
                    vertices,
                    faces,
                    seen_faces,
                    (top_left, bottom_left, top_right),
                    (d_tl, d_bl, d_tr),
                )
                rejected_face_count += add_depth_mesh_face(
                    vertices,
                    faces,
                    seen_faces,
                    (top_right, bottom_left, bottom_right),
                    (d_tr, d_bl, d_br),
                )

        if len(faces) > frame_face_count_before:
            integrated_count += 1
        else:
            skipped_frame_count += 1

    if not vertices or not faces or integrated_count == 0:
        raise RGBDFusionUnavailable("Fallback depth mesh fusion did not produce any connected surfaces.")

    fused_mesh = FusedMesh(vertices=vertices, faces=faces, stats={
        "geometrySource": "rgbd_keyframe_depth_mesh",
        "vertexCount": len(vertices),
        "faceCount": len(faces),
        "depthFrameCount": len(depth_frames),
        "pairedFrameCount": len(paired_frames),
        "integratedDepthFrameCount": integrated_count,
        "sampledDepthFrameCount": len(selected_frames),
        "skippedDepthFrameCount": skipped_frame_count,
        "invalidDepthSampleCount": invalid_depth_count,
        "duplicateVertexCount": duplicate_vertex_count,
        "rejectedFaceCount": rejected_face_count,
        "vertexQuantizationMeters": RGBD_DEPTH_MESH_VERTEX_QUANTIZATION,
        "targetSamplesPerFrame": RGBD_DEPTH_MESH_TARGET_SAMPLES_PER_FRAME,
        "averagePixelStride": (sum(sample_steps) / len(sample_steps)) if sample_steps else 0,
        "tsdfUnavailableReason": tsdf_unavailable_reason,
    })
    write_fused_mesh_json(fused_mesh, output_json_path)
    write_obj(fused_mesh, output_obj_path)
    return {
        "available": True,
        "used": True,
        "geometrySource": "rgbd_keyframe_depth_mesh",
        "algorithm": "depth_frame_grid_backprojection",
        "vertexCount": len(vertices),
        "faceCount": len(faces),
        "depthFrameCount": len(depth_frames),
        "pairedFrameCount": len(paired_frames),
        "sampledDepthFrameCount": len(selected_frames),
        "integratedDepthFrameCount": integrated_count,
        "skippedDepthFrameCount": skipped_frame_count,
        "invalidDepthSampleCount": invalid_depth_count,
        "duplicateVertexCount": duplicate_vertex_count,
        "rejectedFaceCount": rejected_face_count,
        "vertexQuantizationMeters": RGBD_DEPTH_MESH_VERTEX_QUANTIZATION,
        "targetSamplesPerFrame": RGBD_DEPTH_MESH_TARGET_SAMPLES_PER_FRAME,
        "averagePixelStride": (sum(sample_steps) / len(sample_steps)) if sample_steps else 0,
        "tsdfUnavailableReason": tsdf_unavailable_reason,
        "path": "rgbd_fused_mesh.obj",
    }


def read_float32_depth_values(depth_path: Path, width: int, height: int) -> array:
    values = array("f")
    values.frombytes(depth_path.read_bytes())
    if sys.byteorder != "little":
        values.byteswap()
    expected_count = width * height
    if len(values) != expected_count:
        raise RGBDFusionUnavailable(
            f"Depth frame {depth_path.name} sample count mismatch: expected={expected_count} actual={len(values)}"
        )
    return values


def read_confidence_values(depth_frame: dict, work_dir: Path, width: int, height: int) -> bytes | None:
    confidence_path = depth_frame.get("confidencePath")
    if not confidence_path:
        return None

    confidence_file = work_dir / confidence_path
    if not confidence_file.exists():
        return None

    values = confidence_file.read_bytes()
    return values if len(values) == width * height else None


def backproject_depth_sample_to_world(
    source_x: int,
    source_y: int,
    depth: float,
    intrinsics: list[float],
    camera_transform: list[float],
) -> tuple[float, float, float]:
    fx = float(intrinsics[0])
    fy = float(intrinsics[4])
    cx = float(intrinsics[6])
    cy = float(intrinsics[7])
    camera_x = (source_x - cx) * depth / fx
    camera_y = (cy - source_y) * depth / fy
    return transform_point(camera_transform, [camera_x, camera_y, -depth])


def add_depth_mesh_face(
    vertices: list[tuple[float, float, float]],
    faces: list[tuple[int, int, int]],
    seen_faces: set[tuple[int, int, int]],
    indices: tuple[int | None, int | None, int | None],
    depths: tuple[float, float, float],
) -> int:
    if any(index is None for index in indices) or not should_connect_depth_samples(depths):
        return 1

    face = (int(indices[0]), int(indices[1]), int(indices[2]))
    if len(set(face)) != 3:
        return 1

    if triangle_area(vertices[face[0]], vertices[face[1]], vertices[face[2]]) <= 1e-10:
        return 1

    face_key = tuple(sorted(face))
    if face_key in seen_faces:
        return 1

    seen_faces.add(face_key)
    faces.append(face)
    return 0


def should_connect_depth_samples(depths: tuple[float, float, float]) -> bool:
    min_depth = min(depths)
    max_depth = max(depths)
    if min_depth <= 0:
        return False

    return (max_depth - min_depth) <= max(0.08, min_depth * 0.08)


def write_rgbd_tsdf_mesh(
    keyframes: list[dict],
    depth_frames: list[dict],
    work_dir: Path,
    output_obj_path: Path,
    output_json_path: Path,
) -> dict:
    np, o3d = load_open3d_modules()
    paired_frames = pair_rgbd_frames(keyframes, depth_frames, work_dir)
    if not paired_frames:
        raise RGBDFusionUnavailable("No RGB/depth frames could be paired for TSDF fusion.")

    volume = o3d.pipelines.integration.ScalableTSDFVolume(
        voxel_length=RGBD_VOXEL_LENGTH_METERS,
        sdf_trunc=RGBD_SDF_TRUNC_METERS,
        color_type=o3d.pipelines.integration.TSDFVolumeColorType.RGB8,
    )
    integrated_count = 0

    for keyframe, depth_frame, color_path, depth_path in paired_frames:
        width, height = [int(value) for value in depth_frame["depthResolution"]]
        depth_array = np.frombuffer(depth_path.read_bytes(), dtype=np.dtype("<f4")).reshape((height, width))
        depth_array = np.nan_to_num(depth_array, nan=0, posinf=0, neginf=0).astype(np.float32)
        depth_array[(depth_array <= 0) | (depth_array > RGBD_DEPTH_TRUNC_METERS)] = 0

        confidence_path = depth_frame.get("confidencePath")
        if confidence_path:
            confidence_file = work_dir / confidence_path
            if confidence_file.exists():
                confidence = np.frombuffer(confidence_file.read_bytes(), dtype=np.uint8).reshape((height, width))
                depth_array[confidence == 0] = 0

        if int(np.count_nonzero(depth_array)) < max(64, width * height * 0.01):
            continue

        color_array = np.asarray(Image.open(color_path).convert("RGB").resize((width, height), Image.Resampling.BILINEAR))
        rgbd = o3d.geometry.RGBDImage.create_from_color_and_depth(
            o3d.geometry.Image(color_array),
            o3d.geometry.Image(depth_array),
            depth_scale=1.0,
            depth_trunc=RGBD_DEPTH_TRUNC_METERS,
            convert_rgb_to_intensity=False,
        )
        intrinsic = open3d_intrinsic(o3d, depth_frame["intrinsics"], width, height)
        extrinsic = open3d_extrinsic_from_arkit_transform(depth_frame["cameraTransform"], np)
        volume.integrate(rgbd, intrinsic, extrinsic)
        integrated_count += 1

    if integrated_count == 0:
        raise RGBDFusionUnavailable("Depth frames existed, but none had enough valid depth samples for TSDF fusion.")

    mesh = volume.extract_triangle_mesh()
    mesh, postprocess_stats = postprocess_open3d_tsdf_mesh(mesh, o3d, np)
    vertices = [tuple(float(component) for component in vertex) for vertex in np.asarray(mesh.vertices)]
    faces = [tuple(int(component) for component in face) for face in np.asarray(mesh.triangles)]
    if not vertices or not faces:
        raise RGBDFusionUnavailable("Open3D extracted an empty TSDF mesh.")

    fused_mesh = FusedMesh(vertices=vertices, faces=faces, stats={
        "geometrySource": "rgbd_tsdf_open3d",
        "vertexCount": len(vertices),
        "faceCount": len(faces),
        "depthFrameCount": len(depth_frames),
        "integratedDepthFrameCount": integrated_count,
        "voxelLengthMeters": RGBD_VOXEL_LENGTH_METERS,
        "sdfTruncMeters": RGBD_SDF_TRUNC_METERS,
        "depthTruncMeters": RGBD_DEPTH_TRUNC_METERS,
        "postprocess": postprocess_stats,
    })
    write_fused_mesh_json(fused_mesh, output_json_path)
    write_obj(fused_mesh, output_obj_path)
    return {
        "available": True,
        "used": True,
        "geometrySource": "rgbd_tsdf_open3d",
        "vertexCount": len(vertices),
        "faceCount": len(faces),
        "depthFrameCount": len(depth_frames),
        "pairedFrameCount": len(paired_frames),
        "integratedDepthFrameCount": integrated_count,
        "voxelLengthMeters": RGBD_VOXEL_LENGTH_METERS,
        "sdfTruncMeters": RGBD_SDF_TRUNC_METERS,
        "depthTruncMeters": RGBD_DEPTH_TRUNC_METERS,
        "postprocess": postprocess_stats,
        "path": "rgbd_fused_mesh.obj",
    }


def load_open3d_modules():
    try:
        np = importlib.import_module("numpy")
        o3d = importlib.import_module("open3d")
    except ImportError as exc:
        raise RGBDFusionUnavailable(
            "Open3D RGBD fusion requires numpy and open3d. Install backend requirements to enable the dense path."
        ) from exc
    return np, o3d


def closest_keyframe(depth_frame: dict, keyframes: list[dict]) -> dict | None:
    timestamp = depth_frame.get("timestamp")
    if timestamp is None or not keyframes:
        return None
    return min(
        keyframes,
        key=lambda keyframe: abs(float(keyframe.get("timestamp") or 0) - float(timestamp)),
    )


def open3d_intrinsic(o3d, intrinsics: list[float], width: int, height: int):
    return o3d.camera.PinholeCameraIntrinsic(
        width,
        height,
        float(intrinsics[0]),
        float(intrinsics[4]),
        float(intrinsics[6]),
        float(intrinsics[7]),
    )


def column_major_transform(matrix: list[float], np):
    return np.array(matrix, dtype=np.float64).reshape((4, 4), order="F")


def open3d_extrinsic_from_arkit_transform(matrix: list[float], np):
    arkit_camera_to_world = column_major_transform(matrix, np)
    open3d_camera_to_arkit_camera = np.diag([1.0, -1.0, -1.0, 1.0])
    open3d_camera_to_world = arkit_camera_to_world @ open3d_camera_to_arkit_camera
    return np.linalg.inv(open3d_camera_to_world)


def postprocess_open3d_tsdf_mesh(mesh, o3d, np) -> tuple[object, dict]:
    raw_vertex_count = len(mesh.vertices)
    raw_triangle_count = len(mesh.triangles)
    mesh.compute_vertex_normals()
    clean_open3d_mesh(mesh)
    cleaned_vertex_count = len(mesh.vertices)
    cleaned_triangle_count = len(mesh.triangles)

    component_stats = remove_small_open3d_components(mesh, np)
    after_component_vertex_count = len(mesh.vertices)
    after_component_triangle_count = len(mesh.triangles)

    smoothing_enabled = after_component_triangle_count > 0 and RGBD_TSDF_SMOOTHING_ITERATIONS > 0
    if smoothing_enabled:
        mesh = mesh.filter_smooth_taubin(number_of_iterations=RGBD_TSDF_SMOOTHING_ITERATIONS)
        clean_open3d_mesh(mesh)
        mesh.compute_vertex_normals()

    return mesh, {
        "rawVertexCount": raw_vertex_count,
        "rawFaceCount": raw_triangle_count,
        "cleanedVertexCount": cleaned_vertex_count,
        "cleanedFaceCount": cleaned_triangle_count,
        "afterComponentVertexCount": after_component_vertex_count,
        "afterComponentFaceCount": after_component_triangle_count,
        "finalVertexCount": len(mesh.vertices),
        "finalFaceCount": len(mesh.triangles),
        "componentFiltering": component_stats,
        "smoothing": {
            "enabled": smoothing_enabled,
            "algorithm": "open3d_taubin",
            "iterations": RGBD_TSDF_SMOOTHING_ITERATIONS if smoothing_enabled else 0,
        },
    }


def remove_small_open3d_components(mesh, np) -> dict:
    triangle_count = len(mesh.triangles)
    if triangle_count == 0:
        return {
            "enabled": False,
            "reason": "empty mesh",
            "removedComponentCount": 0,
            "removedFaceCount": 0,
        }

    triangle_clusters, cluster_n_triangles, cluster_area = mesh.cluster_connected_triangles()
    triangle_clusters = np.asarray(triangle_clusters)
    cluster_n_triangles = np.asarray(cluster_n_triangles)
    cluster_area = np.asarray(cluster_area)
    if len(cluster_n_triangles) == 0:
        return {
            "enabled": False,
            "reason": "no connected components found",
            "removedComponentCount": 0,
            "removedFaceCount": 0,
        }

    min_triangles = max(
        RGBD_TSDF_MIN_COMPONENT_TRIANGLES,
        int(math.ceil(triangle_count * RGBD_TSDF_MIN_COMPONENT_FACE_RATIO)),
    )
    keep_cluster_ids = {
        int(index)
        for index, face_count in enumerate(cluster_n_triangles)
        if int(face_count) >= min_triangles or float(cluster_area[index]) >= RGBD_TSDF_MIN_COMPONENT_AREA_M2
    }
    if not keep_cluster_ids:
        keep_cluster_ids = {int(cluster_n_triangles.argmax())}

    remove_mask = [
        int(cluster_id) not in keep_cluster_ids
        for cluster_id in triangle_clusters
    ]
    removed_face_count = sum(1 for should_remove in remove_mask if should_remove)
    removed_component_count = len(cluster_n_triangles) - len(keep_cluster_ids)
    if removed_face_count > 0:
        mesh.remove_triangles_by_mask(remove_mask)
        mesh.remove_unreferenced_vertices()
        clean_open3d_mesh(mesh)

    return {
        "enabled": True,
        "componentCount": int(len(cluster_n_triangles)),
        "keptComponentCount": int(len(keep_cluster_ids)),
        "removedComponentCount": int(removed_component_count),
        "removedFaceCount": int(removed_face_count),
        "minComponentTriangles": int(min_triangles),
        "minComponentAreaM2": RGBD_TSDF_MIN_COMPONENT_AREA_M2,
        "largestComponentTriangles": int(cluster_n_triangles.max()) if len(cluster_n_triangles) else 0,
    }


def clean_open3d_mesh(mesh) -> object:
    mesh.remove_duplicated_vertices()
    mesh.remove_degenerate_triangles()
    mesh.remove_duplicated_triangles()
    mesh.remove_non_manifold_edges()
    mesh.remove_unreferenced_vertices()
    return mesh


def fuse_mesh_anchors(mesh_anchors: list[dict], quantization: float = 1e-5) -> FusedMesh:
    vertices: list[tuple[float, float, float]] = []
    faces: list[tuple[int, int, int]] = []
    vertex_lookup: dict[tuple[int, int, int], int] = {}
    seen_faces: set[tuple[int, int, int]] = set()
    original_vertex_count = 0
    original_face_count = 0
    duplicate_vertex_count = 0
    invalid_vertex_count = 0
    invalid_face_count = 0
    duplicate_face_count = 0

    for anchor in mesh_anchors:
        transform = anchor.get("transform") or []
        local_vertices = anchor.get("vertices") or []
        indices = anchor.get("triangleIndices") or []
        if len(transform) != 16:
            invalid_vertex_count += len(local_vertices)
            invalid_face_count += len(indices) // 3
            continue

        local_to_fused: list[int | None] = []
        for vertex in local_vertices:
            original_vertex_count += 1
            if len(vertex) != 3:
                invalid_vertex_count += 1
                local_to_fused.append(None)
                continue

            world = transform_point(transform, vertex)
            if not all(math.isfinite(component) for component in world):
                invalid_vertex_count += 1
                local_to_fused.append(None)
                continue

            key = quantized_vertex_key(world, quantization)
            existing = vertex_lookup.get(key)
            if existing is not None:
                duplicate_vertex_count += 1
                local_to_fused.append(existing)
                continue

            fused_index = len(vertices)
            vertex_lookup[key] = fused_index
            vertices.append(world)
            local_to_fused.append(fused_index)

        for index in range(0, len(indices) - 2, 3):
            original_face_count += 1
            try:
                raw_a = int(indices[index])
                raw_b = int(indices[index + 1])
                raw_c = int(indices[index + 2])
            except (TypeError, ValueError):
                invalid_face_count += 1
                continue

            if min(raw_a, raw_b, raw_c) < 0 or max(raw_a, raw_b, raw_c) >= len(local_to_fused):
                invalid_face_count += 1
                continue

            resolved = (local_to_fused[raw_a], local_to_fused[raw_b], local_to_fused[raw_c])
            if resolved[0] is None or resolved[1] is None or resolved[2] is None:
                invalid_face_count += 1
                continue

            face = (int(resolved[0]), int(resolved[1]), int(resolved[2]))
            if len(set(face)) != 3 or triangle_area(vertices[face[0]], vertices[face[1]], vertices[face[2]]) <= 1e-10:
                invalid_face_count += 1
                continue

            face_key = tuple(sorted(face))
            if face_key in seen_faces:
                duplicate_face_count += 1
                continue

            seen_faces.add(face_key)
            faces.append(face)

    stats = {
        "geometrySource": "arkit_mesh_anchor_fusion",
        "anchorCount": len(mesh_anchors),
        "originalVertexCount": original_vertex_count,
        "originalFaceCount": original_face_count,
        "vertexCount": len(vertices),
        "faceCount": len(faces),
        "duplicateVertexCount": duplicate_vertex_count,
        "invalidVertexCount": invalid_vertex_count,
        "invalidFaceCount": invalid_face_count,
        "duplicateFaceCount": duplicate_face_count,
        "cleanup": {
            "duplicateVertexQuantizationMeters": quantization,
            "removedInvalidFaces": invalid_face_count,
            "removedDuplicateFaces": duplicate_face_count,
        },
    }
    return FusedMesh(vertices=vertices, faces=faces, stats=stats)


def quantized_vertex_key(vertex: tuple[float, float, float], quantization: float) -> tuple[int, int, int]:
    return (
        int(round(vertex[0] / quantization)),
        int(round(vertex[1] / quantization)),
        int(round(vertex[2] / quantization)),
    )


def write_fused_mesh_json(mesh: FusedMesh, output_path: Path) -> None:
    output_path.write_text(json.dumps({
        "vertices": [[x, y, z] for x, y, z in mesh.vertices],
        "faces": [[a, b, c] for a, b, c in mesh.faces],
        "stats": mesh.stats,
    }, indent=2), encoding="utf-8")


def read_fused_mesh_json(path: Path) -> FusedMesh:
    raw = json.loads(path.read_text(encoding="utf-8"))
    vertices = [tuple(float(component) for component in vertex[:3]) for vertex in raw.get("vertices", []) if len(vertex) == 3]
    faces = [tuple(int(component) for component in face[:3]) for face in raw.get("faces", []) if len(face) == 3]
    return FusedMesh(vertices=vertices, faces=faces, stats=raw.get("stats", {}))


def write_obj(mesh: FusedMesh, output_path: Path) -> None:
    lines = [
        "# LidarAI fused ARKit scene reconstruction mesh",
        "# Units are meters in ARKit world space",
        "o fused_mesh",
    ]
    for x, y, z in mesh.vertices:
        lines.append(f"v {x:.6f} {y:.6f} {z:.6f}")
    for a, b, c in mesh.faces:
        lines.append(f"f {a + 1} {b + 1} {c + 1}")
    output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def transform_point(matrix: list[float], vertex: list[float]) -> tuple[float, float, float]:
    x, y, z = float(vertex[0]), float(vertex[1]), float(vertex[2])
    return (
        matrix[0] * x + matrix[4] * y + matrix[8] * z + matrix[12],
        matrix[1] * x + matrix[5] * y + matrix[9] * z + matrix[13],
        matrix[2] * x + matrix[6] * y + matrix[10] * z + matrix[14],
    )


async def write_vertex_colored_ply(
    vertices: list[tuple[float, float, float]],
    faces: list[tuple[int, int, int]],
    keyframes: list[ProjectionKeyframe],
    output_path: Path,
    report_progress: Callable[[float, str], Awaitable[None]] | None = None,
    is_cancelled: CancellationCheck | None = None,
) -> dict:
    colors = []
    progress_interval = max(1, min(len(vertices) // 20, 1_000))
    for index, vertex in enumerate(vertices):
        if is_cancelled is not None and is_cancelled():
            raise asyncio.CancelledError
        colors.append(project_vertex_color(vertex, keyframes))
        if report_progress is not None and (index % progress_interval == 0 or index == len(vertices) - 1):
            fraction = ((index + 1) / len(vertices)) if vertices else 1
            await report_progress(78 + fraction * 3, f"Coloring debug mesh vertices {index + 1} / {len(vertices)}")
            await asyncio.sleep(0)

    colored_count = sum(1 for color in colors if color is not None)
    resolved_colors = [color or FALLBACK_COLOR for color in colors]

    lines = [
        "ply",
        "format ascii 1.0",
        "comment LidarAI first-pass vertex color projection",
        f"element vertex {len(vertices)}",
        "property float x",
        "property float y",
        "property float z",
        "property uchar red",
        "property uchar green",
        "property uchar blue",
        f"element face {len(faces)}",
        "property list uchar int vertex_indices",
        "end_header",
    ]

    for vertex, color in zip(vertices, resolved_colors):
        lines.append(
            f"{vertex[0]:.6f} {vertex[1]:.6f} {vertex[2]:.6f} {color[0]} {color[1]} {color[2]}"
        )

    for a, b, c in faces:
        lines.append(f"3 {a} {b} {c}")

    output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {
        "vertexCount": len(vertices),
        "faceCount": len(faces),
        "coloredVertexCount": colored_count,
        "coverage": (colored_count / len(vertices)) if vertices else 0,
    }


def load_projection_keyframes(keyframes: list[dict], keyframe_dir: Path) -> list[ProjectionKeyframe]:
    pending = []
    for keyframe in keyframes:
        image_path = keyframe_dir / Path(keyframe.get("path", "")).name
        transform = keyframe.get("cameraTransform") or []
        intrinsics = keyframe.get("intrinsics") or []
        if not image_path.exists() or len(transform) != 16 or len(intrinsics) != 9:
            continue

        image = Image.open(image_path).convert("RGB")
        pending.append((keyframe, image_path, transform, intrinsics, image))

    color_correction = build_keyframe_color_correction([item[4] for item in pending])
    loaded = []
    for image_index, (keyframe, image_path, transform, intrinsics, image) in enumerate(pending):
        image = apply_keyframe_color_correction(image, color_correction, image_index=image_index)
        loaded.append(ProjectionKeyframe(
            image=image,
            width=image.width,
            height=image.height,
            world_to_camera=invert_rigid_transform(transform),
            camera_position=(float(transform[12]), float(transform[13]), float(transform[14])),
            intrinsics=intrinsics,
            pixels=image.load(),
            id=str(keyframe.get("id")) if keyframe.get("id") else None,
            path=str(keyframe.get("path")) if keyframe.get("path") else image_path.name,
            color_correction=color_correction,
        ))
    return loaded


def build_keyframe_color_correction(images: list[Image.Image], max_samples: int = 160_000) -> dict:
    if not images:
        return {
            "enabled": False,
            "reason": "no usable keyframes",
            "sampleCount": 0,
            "sourceMeanRgb": [0, 0, 0],
            "sourceMeanLuminance": 0,
            "sourceMedianLuminance": 0,
            "channelScales": [1, 1, 1],
            "perKeyframeExposure": [],
            "saturationBoost": 1,
            "contrastBoost": 1,
        }

    sample_budget_per_image = max(1, max_samples // len(images))
    count = 0
    sum_r = 0
    sum_g = 0
    sum_b = 0
    sum_luminance = 0.0
    luminance_samples: list[float] = []
    per_image_stats: list[dict] = []
    for image in images:
        total_pixels = max(image.width * image.height, 1)
        stride = max(1, int(math.sqrt(total_pixels / sample_budget_per_image)))
        pixels = image.load()
        image_count = 0
        image_sum_r = 0
        image_sum_g = 0
        image_sum_b = 0
        image_sum_luminance = 0.0
        image_luminance_samples: list[float] = []
        for y in range(0, image.height, stride):
            for x in range(0, image.width, stride):
                r, g, b = pixels[x, y]
                r = int(r)
                g = int(g)
                b = int(b)
                luminance = rgb_luminance((r, g, b))
                sum_r += r
                sum_g += g
                sum_b += b
                sum_luminance += luminance
                luminance_samples.append(luminance)
                count += 1
                image_count += 1
                image_sum_r += r
                image_sum_g += g
                image_sum_b += b
                image_sum_luminance += luminance
                image_luminance_samples.append(luminance)

        per_image_stats.append({
            "sampleCount": image_count,
            "meanRgb": [
                round(image_sum_r / image_count, 2) if image_count else 0,
                round(image_sum_g / image_count, 2) if image_count else 0,
                round(image_sum_b / image_count, 2) if image_count else 0,
            ],
            "meanLuminance": round(image_sum_luminance / image_count, 2) if image_count else 0,
            "medianLuminance": round(median_float(image_luminance_samples), 2) if image_luminance_samples else 0,
        })

    if count == 0:
        return {
            "enabled": False,
            "reason": "no sampled keyframe pixels",
            "sampleCount": 0,
            "sourceMeanRgb": [0, 0, 0],
            "sourceMeanLuminance": 0,
            "sourceMedianLuminance": 0,
            "channelScales": [1, 1, 1],
            "perKeyframeExposure": [],
            "saturationBoost": 1,
            "contrastBoost": 1,
        }

    mean_r = sum_r / count
    mean_g = sum_g / count
    mean_b = sum_b / count
    target = max((mean_r + mean_g + mean_b) / 3, 1)
    target_luminance = max(sum_luminance / count, 1)
    target_median_luminance = max(median_float(luminance_samples), 1)
    scales = [
        clamp_float(target / max(mean_r, 1), 0.90, 1.14),
        clamp_float(target / max(mean_g, 1), 0.90, 1.10),
        clamp_float(target / max(mean_b, 1), 0.88, 1.08),
    ]
    per_keyframe_exposure = []
    for image_index, stats in enumerate(per_image_stats):
        source_luminance = float(stats.get("medianLuminance") or stats.get("meanLuminance") or target_median_luminance)
        luminance_scale = clamp_float(target_median_luminance / max(source_luminance, 1), 0.75, 1.25)
        per_keyframe_exposure.append({
            "imageIndex": image_index,
            "sampleCount": stats["sampleCount"],
            "meanRgb": stats["meanRgb"],
            "meanLuminance": stats["meanLuminance"],
            "medianLuminance": stats["medianLuminance"],
            "luminanceScale": round(luminance_scale, 4),
        })

    return {
        "enabled": True,
        "algorithm": "gray_world_channel_balance_with_bounded_per_keyframe_exposure",
        "sampleCount": count,
        "sourceMeanRgb": [round(mean_r, 2), round(mean_g, 2), round(mean_b, 2)],
        "sourceMeanLuminance": round(target_luminance, 2),
        "sourceMedianLuminance": round(target_median_luminance, 2),
        "targetMeanRgb": round(target, 2),
        "targetLuminance": round(target_luminance, 2),
        "targetMedianLuminance": round(target_median_luminance, 2),
        "channelScales": [round(scale, 4) for scale in scales],
        "perKeyframeExposure": per_keyframe_exposure,
        "exposureScaleBounds": [0.75, 1.25],
        "saturationBoost": TEXTURE_COLOR_SATURATION_BOOST,
        "contrastBoost": TEXTURE_COLOR_CONTRAST_BOOST,
    }


def apply_keyframe_color_correction(image: Image.Image, correction: dict, image_index: int | None = None) -> Image.Image:
    if not correction.get("enabled"):
        return image

    scales = correction.get("channelScales") or [1, 1, 1]
    exposure_scale = keyframe_exposure_scale(correction, image_index)
    red, green, blue = image.split()
    red = red.point(lambda value: clamp_color(value * float(scales[0]) * exposure_scale))
    green = green.point(lambda value: clamp_color(value * float(scales[1]) * exposure_scale))
    blue = blue.point(lambda value: clamp_color(value * float(scales[2]) * exposure_scale))
    corrected = Image.merge("RGB", (red, green, blue))

    contrast = float(correction.get("contrastBoost") or 1)
    if abs(contrast - 1) > 1e-4:
        corrected = ImageEnhance.Contrast(corrected).enhance(contrast)

    saturation = float(correction.get("saturationBoost") or 1)
    if abs(saturation - 1) > 1e-4:
        corrected = ImageEnhance.Color(corrected).enhance(saturation)

    return corrected


def keyframe_exposure_scale(correction: dict, image_index: int | None) -> float:
    if image_index is None:
        return 1.0

    for entry in correction.get("perKeyframeExposure") or []:
        if int(entry.get("imageIndex", -1)) == image_index:
            return float(entry.get("luminanceScale") or 1.0)

    return 1.0


def texture_color_correction_for_keyframes(keyframes: list[ProjectionKeyframe]) -> dict:
    for keyframe in keyframes:
        if keyframe.color_correction:
            return keyframe.color_correction
    return {
        "enabled": False,
        "reason": "no usable keyframes",
        "sampleCount": 0,
        "sourceMeanRgb": [0, 0, 0],
        "channelScales": [1, 1, 1],
        "perKeyframeExposure": [],
        "saturationBoost": 1,
        "contrastBoost": 1,
    }


def project_vertex_color(vertex: tuple[float, float, float], keyframes: list[ProjectionKeyframe]) -> tuple[int, int, int] | None:
    samples: list[tuple[float, tuple[int, int, int]]] = []
    for keyframe in keyframes:
        projection = project_world_point(vertex, keyframe)
        if projection is None:
            continue

        u, v, depth = projection
        edge_margin = min(u, v, keyframe.width - u, keyframe.height - v)
        center_bias = max(0.05, min(edge_margin / max(min(keyframe.width, keyframe.height) * 0.25, 1), 1))
        distance_weight = 1 / max(depth, 0.2)
        weight = center_bias * distance_weight
        samples.append((weight, sample_image_nearest(keyframe, u, v)))

    if not samples:
        return None

    total_weight = sum(weight for weight, _ in samples)
    r = sum(weight * color[0] for weight, color in samples) / total_weight
    g = sum(weight * color[1] for weight, color in samples) / total_weight
    b = sum(weight * color[2] for weight, color in samples) / total_weight
    return (clamp_color(r), clamp_color(g), clamp_color(b))


def make_texture_render_mesh(mesh: FusedMesh) -> FusedMesh:
    source_face_count = len(mesh.faces)
    atlas_max_size = texture_atlas_max_size_for_mesh(mesh)
    preferred_target_face_count = (
        TEXTURE_TSDF_RENDER_TARGET_FACE_COUNT
        if is_open3d_tsdf_mesh(mesh)
        else TEXTURE_RENDER_TARGET_FACE_COUNT
    )
    target_face_count = texture_render_target_face_count(
        atlas_max_size=atlas_max_size,
        preferred_target_face_count=preferred_target_face_count,
    )
    base_stats = {
        "sourceVertexCount": len(mesh.vertices),
        "sourceFaceCount": source_face_count,
        "targetFaceCount": target_face_count,
        "targetMinTileSize": TEXTURE_RENDER_TARGET_MIN_TILE_SIZE,
        "atlasMaxSize": atlas_max_size,
    }

    if source_face_count <= target_face_count:
        render_stats = {
            **base_stats,
            "used": False,
            "algorithm": "none",
            "renderVertexCount": len(mesh.vertices),
            "renderFaceCount": source_face_count,
            "reason": "source mesh already fits the atlas tile budget",
        }
        return FusedMesh(
            vertices=mesh.vertices,
            faces=mesh.faces,
            stats={**mesh.stats, "textureRenderMesh": render_stats},
        )

    qem_unavailable_reason = None
    if is_open3d_tsdf_mesh(mesh):
        try:
            simplified_mesh, simplify_stats = simplify_mesh_by_open3d_quadric_decimation(mesh, target_face_count)
            render_stats = {
                **base_stats,
                **simplify_stats,
                "used": len(simplified_mesh.faces) < source_face_count,
                "renderVertexCount": len(simplified_mesh.vertices),
                "renderFaceCount": len(simplified_mesh.faces),
                "faceReductionRatio": round(1 - (len(simplified_mesh.faces) / source_face_count), 4) if source_face_count else 0,
                "smoothing": {
                    "enabled": False,
                    "reason": "TSDF mesh was already smoothed before render decimation; preserving planar surfaces.",
                },
            }
            return FusedMesh(
                vertices=simplified_mesh.vertices,
                faces=simplified_mesh.faces,
                stats={**mesh.stats, "textureRenderMesh": render_stats},
            )
        except Exception as exc:  # pragma: no cover - exercised only when Open3D fails on a specific mesh.
            qem_unavailable_reason = str(exc)
            logger.warning("Open3D render mesh decimation failed; falling back to vertex clustering: %s", exc)

    simplified_mesh, simplify_stats = simplify_mesh_by_vertex_clustering(mesh, target_face_count)
    smoothing_max_step = min(0.035, max(float(simplify_stats.get("clusterVoxelMeters") or 0.0) * 0.35, 0.006))
    smoothed_mesh, smoothing_stats = smooth_texture_render_mesh(
        simplified_mesh,
        iterations=TEXTURE_RENDER_SMOOTHING_ITERATIONS,
        strength=TEXTURE_RENDER_SMOOTHING_STRENGTH,
        boundary_strength=TEXTURE_RENDER_SMOOTHING_BOUNDARY_STRENGTH,
        hard_edge_weight=TEXTURE_RENDER_SMOOTHING_HARD_EDGE_WEIGHT,
        normal_cosine_threshold=TEXTURE_RENDER_SMOOTHING_NORMAL_COSINE,
        max_step_meters=smoothing_max_step,
        max_total_displacement_meters=TEXTURE_RENDER_SMOOTHING_MAX_TOTAL_DISPLACEMENT_METERS,
    )
    render_stats = {
        **base_stats,
        **simplify_stats,
        "used": len(smoothed_mesh.faces) < source_face_count,
        "renderVertexCount": len(smoothed_mesh.vertices),
        "renderFaceCount": len(smoothed_mesh.faces),
        "faceReductionRatio": round(1 - (len(smoothed_mesh.faces) / source_face_count), 4) if source_face_count else 0,
        "smoothing": smoothing_stats,
    }
    if qem_unavailable_reason:
        render_stats["preferredAlgorithmUnavailableReason"] = qem_unavailable_reason
    return FusedMesh(
        vertices=smoothed_mesh.vertices,
        faces=smoothed_mesh.faces,
        stats={**mesh.stats, "textureRenderMesh": render_stats},
    )


def texture_atlas_max_size_for_mesh(mesh: FusedMesh) -> int:
    return TEXTURE_TSDF_ATLAS_MAX_SIZE if is_open3d_tsdf_mesh(mesh) else TEXTURE_ATLAS_MAX_SIZE


def is_open3d_tsdf_mesh(mesh: FusedMesh) -> bool:
    geometry_source = mesh.stats.get("geometrySource")
    if geometry_source == "rgbd_tsdf_open3d":
        return True

    rgbd_fusion = mesh.stats.get("rgbdFusion") if isinstance(mesh.stats, dict) else None
    return isinstance(rgbd_fusion, dict) and rgbd_fusion.get("geometrySource") == "rgbd_tsdf_open3d"


def texture_render_target_face_count(
    atlas_max_size: int = TEXTURE_ATLAS_MAX_SIZE,
    preferred_target_face_count: int = TEXTURE_RENDER_TARGET_FACE_COUNT,
) -> int:
    atlas_budget = max(1, (atlas_max_size // TEXTURE_RENDER_TARGET_MIN_TILE_SIZE) ** 2)
    return max(1, min(preferred_target_face_count, atlas_budget))


def simplify_mesh_by_open3d_quadric_decimation(mesh: FusedMesh, target_face_count: int) -> tuple[FusedMesh, dict]:
    np, o3d = load_open3d_modules()
    triangle_mesh = fused_mesh_to_open3d_triangle_mesh(mesh, np, o3d)
    clean_open3d_mesh(triangle_mesh)
    source_vertex_count = len(triangle_mesh.vertices)
    source_face_count = len(triangle_mesh.triangles)
    if source_face_count == 0:
        raise RGBDFusionUnavailable("Open3D render decimation received an empty mesh.")

    simplified = triangle_mesh.simplify_quadric_decimation(target_number_of_triangles=max(1, target_face_count))
    clean_open3d_mesh(simplified)
    simplified.compute_vertex_normals()
    vertices = [tuple(float(component) for component in vertex) for vertex in np.asarray(simplified.vertices)]
    faces = [tuple(int(component) for component in face) for face in np.asarray(simplified.triangles)]
    if not vertices or not faces:
        raise RGBDFusionUnavailable("Open3D render decimation produced an empty mesh.")

    stats = {
        "algorithm": "open3d_quadric_decimation",
        "sourceVertexCount": len(mesh.vertices),
        "sourceFaceCount": len(mesh.faces),
        "open3dCleanedSourceVertexCount": source_vertex_count,
        "open3dCleanedSourceFaceCount": source_face_count,
        "targetFaceCount": target_face_count,
        "renderVertexCount": len(vertices),
        "renderFaceCount": len(faces),
        "simplificationRatio": round(1 - (len(faces) / max(len(mesh.faces), 1)), 4),
        "vertexReductionRatio": round(1 - (len(vertices) / max(len(mesh.vertices), 1)), 4),
    }
    return FusedMesh(vertices=vertices, faces=faces, stats=stats), stats


def fused_mesh_to_open3d_triangle_mesh(mesh: FusedMesh, np, o3d):
    triangle_mesh = o3d.geometry.TriangleMesh()
    triangle_mesh.vertices = o3d.utility.Vector3dVector(np.asarray(mesh.vertices, dtype=np.float64))
    triangle_mesh.triangles = o3d.utility.Vector3iVector(np.asarray(mesh.faces, dtype=np.int32))
    triangle_mesh.compute_vertex_normals()
    return triangle_mesh


def simplify_mesh_by_vertex_clustering(mesh: FusedMesh, target_face_count: int) -> tuple[FusedMesh, dict]:
    bounds = mesh_bounds(mesh.vertices)
    ratio = math.sqrt(max(len(mesh.faces), 1) / max(target_face_count, 1))
    initial_voxel = clamp_float(
        (bounds["diagonalMeters"] / 650) * ratio,
        TEXTURE_RENDER_MIN_CLUSTER_METERS,
        TEXTURE_RENDER_MAX_CLUSTER_METERS,
    )
    attempts: list[dict] = []
    candidates: list[tuple[FusedMesh, dict]] = []
    voxel_size = initial_voxel

    for _ in range(7):
        candidate, stats = cluster_mesh_vertices(mesh, voxel_size)
        attempts.append(stats)
        candidates.append((candidate, stats))
        if len(candidate.faces) <= target_face_count:
            break
        voxel_size = min(voxel_size * 1.32, TEXTURE_RENDER_MAX_CLUSTER_METERS)
        if attempts[-1]["clusterVoxelMeters"] >= TEXTURE_RENDER_MAX_CLUSTER_METERS:
            break

    under_budget = [(candidate, stats) for candidate, stats in candidates if len(candidate.faces) <= target_face_count]
    if under_budget:
        selected_mesh, selected_stats = max(under_budget, key=lambda item: len(item[0].faces))
    else:
        selected_mesh, selected_stats = min(candidates, key=lambda item: len(item[0].faces))

    return selected_mesh, {
        **selected_stats,
        "algorithm": "vertex_clustering_render_mesh",
        "attempts": attempts,
        "bounds": bounds,
    }


def cluster_mesh_vertices(mesh: FusedMesh, voxel_size: float) -> tuple[FusedMesh, dict]:
    accumulators: dict[tuple[int, int, int], list[float]] = {}
    vertex_to_cluster: list[int] = []
    cluster_index_by_key: dict[tuple[int, int, int], int] = {}

    for vertex in mesh.vertices:
        key = (
            int(math.floor(vertex[0] / voxel_size)),
            int(math.floor(vertex[1] / voxel_size)),
            int(math.floor(vertex[2] / voxel_size)),
        )
        cluster_index = cluster_index_by_key.get(key)
        if cluster_index is None:
            cluster_index = len(cluster_index_by_key)
            cluster_index_by_key[key] = cluster_index
            accumulators[key] = [0.0, 0.0, 0.0, 0.0]
        accumulators[key][0] += vertex[0]
        accumulators[key][1] += vertex[1]
        accumulators[key][2] += vertex[2]
        accumulators[key][3] += 1
        vertex_to_cluster.append(cluster_index)

    cluster_vertices: list[tuple[float, float, float]] = [(0.0, 0.0, 0.0)] * len(cluster_index_by_key)
    for key, cluster_index in cluster_index_by_key.items():
        sum_x, sum_y, sum_z, count = accumulators[key]
        cluster_vertices[cluster_index] = (sum_x / count, sum_y / count, sum_z / count)

    faces: list[tuple[int, int, int]] = []
    seen_faces: set[tuple[int, int, int]] = set()
    degenerate_face_count = 0
    duplicate_face_count = 0

    for face in mesh.faces:
        remapped = (
            vertex_to_cluster[face[0]],
            vertex_to_cluster[face[1]],
            vertex_to_cluster[face[2]],
        )
        if len(set(remapped)) != 3 or triangle_area(
            cluster_vertices[remapped[0]],
            cluster_vertices[remapped[1]],
            cluster_vertices[remapped[2]],
        ) <= 1e-10:
            degenerate_face_count += 1
            continue

        face_key = tuple(sorted(remapped))
        if face_key in seen_faces:
            duplicate_face_count += 1
            continue

        seen_faces.add(face_key)
        faces.append(remapped)

    stats = {
        "clusterVoxelMeters": round(voxel_size, 5),
        "sourceVertexCount": len(mesh.vertices),
        "sourceFaceCount": len(mesh.faces),
        "renderVertexCount": len(cluster_vertices),
        "renderFaceCount": len(faces),
        "removedDegenerateFaceCount": degenerate_face_count,
        "removedDuplicateFaceCount": duplicate_face_count,
        "vertexReductionRatio": round(1 - (len(cluster_vertices) / len(mesh.vertices)), 4) if mesh.vertices else 0,
    }
    return FusedMesh(vertices=cluster_vertices, faces=faces, stats=stats), stats


def smooth_texture_render_mesh(
    mesh: FusedMesh,
    *,
    iterations: int,
    strength: float,
    boundary_strength: float,
    hard_edge_weight: float,
    normal_cosine_threshold: float,
    max_step_meters: float,
    max_total_displacement_meters: float,
) -> tuple[FusedMesh, dict]:
    if iterations <= 0 or not mesh.vertices or not mesh.faces:
        return mesh, {
            "enabled": False,
            "reason": "empty mesh or zero iterations",
            "iterations": 0,
        }

    adjacency, boundary_vertices = weighted_mesh_adjacency(
        mesh,
        hard_edge_weight=hard_edge_weight,
        normal_cosine_threshold=normal_cosine_threshold,
    )
    original_vertices = mesh.vertices
    vertices = [tuple(vertex) for vertex in mesh.vertices]

    for _ in range(iterations):
        updated_vertices = vertices.copy()
        for index, vertex in enumerate(vertices):
            neighbors = adjacency[index] if index < len(adjacency) else {}
            if not neighbors:
                continue

            total_weight = sum(neighbors.values())
            if total_weight <= 1e-8:
                continue

            target = (
                sum(vertices[neighbor][0] * weight for neighbor, weight in neighbors.items()) / total_weight,
                sum(vertices[neighbor][1] * weight for neighbor, weight in neighbors.items()) / total_weight,
                sum(vertices[neighbor][2] * weight for neighbor, weight in neighbors.items()) / total_weight,
            )
            local_strength = boundary_strength if index in boundary_vertices else strength
            delta = multiply(subtract(target, vertex), local_strength)
            delta = clamp_vector_length(delta, max_step_meters)
            candidate = (
                vertex[0] + delta[0],
                vertex[1] + delta[1],
                vertex[2] + delta[2],
            )
            total_delta = clamp_vector_length(
                subtract(candidate, original_vertices[index]),
                max_total_displacement_meters,
            )
            updated_vertices[index] = (
                original_vertices[index][0] + total_delta[0],
                original_vertices[index][1] + total_delta[1],
                original_vertices[index][2] + total_delta[2],
            )
        vertices = updated_vertices

    displacements = [length(subtract(after, before)) for before, after in zip(original_vertices, vertices)]
    moved = [value for value in displacements if value > 1e-5]
    stats = {
        "enabled": True,
        "algorithm": "weighted_laplacian_feature_aware",
        "iterations": iterations,
        "strength": strength,
        "boundaryStrength": boundary_strength,
        "hardEdgeNeighborWeight": hard_edge_weight,
        "normalCosineThreshold": normal_cosine_threshold,
        "maxStepMeters": round(max_step_meters, 5),
        "maxTotalDisplacementMeters": round(max_total_displacement_meters, 5),
        "boundaryVertexCount": len(boundary_vertices),
        "movedVertexCount": len(moved),
        "meanDisplacementMeters": round((sum(displacements) / len(displacements)) if displacements else 0, 5),
        "maxDisplacementMeters": round(max(displacements) if displacements else 0, 5),
    }
    return FusedMesh(vertices=vertices, faces=mesh.faces, stats={**mesh.stats, "smoothing": stats}), stats


def weighted_mesh_adjacency(
    mesh: FusedMesh,
    *,
    hard_edge_weight: float,
    normal_cosine_threshold: float,
) -> tuple[list[dict[int, float]], set[int]]:
    adjacency: list[dict[int, float]] = [dict() for _ in mesh.vertices]
    edge_to_faces: dict[tuple[int, int], list[int]] = {}
    face_normals = [
        triangle_normal(mesh.vertices[face[0]], mesh.vertices[face[1]], mesh.vertices[face[2]])
        for face in mesh.faces
    ]

    for face_index, face in enumerate(mesh.faces):
        for edge in ((face[0], face[1]), (face[1], face[2]), (face[2], face[0])):
            edge_key = tuple(sorted(edge))
            edge_to_faces.setdefault(edge_key, []).append(face_index)

    boundary_vertices: set[int] = set()
    for (a, b), face_indices in edge_to_faces.items():
        if len(face_indices) == 1:
            boundary_vertices.add(a)
            boundary_vertices.add(b)
            weight = 0.45
        else:
            weight = smooth_edge_weight(face_indices, face_normals, hard_edge_weight, normal_cosine_threshold)

        adjacency[a][b] = max(adjacency[a].get(b, 0), weight)
        adjacency[b][a] = max(adjacency[b].get(a, 0), weight)

    return adjacency, boundary_vertices


def smooth_edge_weight(
    face_indices: list[int],
    face_normals: list[tuple[float, float, float]],
    hard_edge_weight: float,
    normal_cosine_threshold: float,
) -> float:
    if len(face_indices) < 2:
        return 0.45

    best_alignment = -1.0
    for left_index, left_face_index in enumerate(face_indices[:-1]):
        for right_face_index in face_indices[left_index + 1:]:
            alignment = abs(dot(face_normals[left_face_index], face_normals[right_face_index]))
            best_alignment = max(best_alignment, alignment)

    return 1.0 if best_alignment >= normal_cosine_threshold else hard_edge_weight


def mesh_bounds(vertices: list[tuple[float, float, float]]) -> dict:
    if not vertices:
        return {
            "min": [0, 0, 0],
            "max": [0, 0, 0],
            "widthMeters": 0,
            "heightMeters": 0,
            "depthMeters": 0,
            "diagonalMeters": 1,
        }

    min_x = min(vertex[0] for vertex in vertices)
    min_y = min(vertex[1] for vertex in vertices)
    min_z = min(vertex[2] for vertex in vertices)
    max_x = max(vertex[0] for vertex in vertices)
    max_y = max(vertex[1] for vertex in vertices)
    max_z = max(vertex[2] for vertex in vertices)
    width = max_x - min_x
    height = max_y - min_y
    depth = max_z - min_z
    diagonal = max(math.sqrt(width * width + height * height + depth * depth), 1e-6)
    return {
        "min": [round(min_x, 4), round(min_y, 4), round(min_z, 4)],
        "max": [round(max_x, 4), round(max_y, 4), round(max_z, 4)],
        "widthMeters": round(width, 4),
        "heightMeters": round(height, 4),
        "depthMeters": round(depth, 4),
        "diagonalMeters": round(diagonal, 4),
    }


def clamp_float(value: float, lower: float, upper: float) -> float:
    return max(lower, min(value, upper))


def median_float(values: list[float]) -> float:
    if not values:
        return 0.0

    sorted_values = sorted(values)
    midpoint = len(sorted_values) // 2
    if len(sorted_values) % 2 == 1:
        return float(sorted_values[midpoint])

    return float((sorted_values[midpoint - 1] + sorted_values[midpoint]) / 2)


class ColorStatsAccumulator:
    def __init__(self) -> None:
        self.count = 0
        self.sum_r = 0
        self.sum_g = 0
        self.sum_b = 0
        self.sum_saturation = 0.0
        self.sum_blue_minus_red = 0.0
        self.min_r = 255
        self.min_g = 255
        self.min_b = 255
        self.max_r = 0
        self.max_g = 0
        self.max_b = 0
        self.near_white_count = 0
        self.pure_white_count = 0
        self.fallback_color_count = 0
        self.colorful_count = 0
        self.blue_dominant_count = 0
        self.quantized_colors: set[tuple[int, int, int]] = set()

    def add(self, color: tuple[int, int, int] | tuple[int, int, int, int]) -> None:
        r = clamp_color(color[0])
        g = clamp_color(color[1])
        b = clamp_color(color[2])
        self.count += 1
        self.sum_r += r
        self.sum_g += g
        self.sum_b += b
        self.min_r = min(self.min_r, r)
        self.min_g = min(self.min_g, g)
        self.min_b = min(self.min_b, b)
        self.max_r = max(self.max_r, r)
        self.max_g = max(self.max_g, g)
        self.max_b = max(self.max_b, b)
        high = max(r, g, b)
        low = min(r, g, b)
        saturation = ((high - low) / high) if high > 0 else 0
        self.sum_saturation += saturation
        self.sum_blue_minus_red += b - r
        if min(r, g, b) >= 245:
            self.near_white_count += 1
        if min(r, g, b) >= 252:
            self.pure_white_count += 1
        if is_fallback_color((r, g, b)):
            self.fallback_color_count += 1
        if high - low >= 20:
            self.colorful_count += 1
        if b > r + 8 and b > g + 4:
            self.blue_dominant_count += 1
        self.quantized_colors.add((r // 16, g // 16, b // 16))

    def to_dict(self) -> dict:
        if self.count == 0:
            return {
                "sampleCount": 0,
                "meanRgb": [0, 0, 0],
                "minRgb": [0, 0, 0],
                "maxRgb": [0, 0, 0],
                "nearWhiteCount": 0,
                "nearWhiteRatio": 0,
                "nonWhiteRatio": 0,
                "pureWhiteRatio": 0,
                "fallbackColorRatio": 0,
                "colorfulRatio": 0,
                "meanSaturation": 0,
                "meanBlueMinusRed": 0,
                "blueDominantRatio": 0,
                "uniqueColorEstimate": 0,
            }

        near_white_ratio = self.near_white_count / self.count
        return {
            "sampleCount": self.count,
            "meanRgb": [
                round(self.sum_r / self.count, 2),
                round(self.sum_g / self.count, 2),
                round(self.sum_b / self.count, 2),
            ],
            "minRgb": [self.min_r, self.min_g, self.min_b],
            "maxRgb": [self.max_r, self.max_g, self.max_b],
            "nearWhiteCount": self.near_white_count,
            "nearWhiteRatio": round(near_white_ratio, 4),
            "nonWhiteRatio": round(1 - near_white_ratio, 4),
            "pureWhiteRatio": round(self.pure_white_count / self.count, 4),
            "fallbackColorRatio": round(self.fallback_color_count / self.count, 4),
            "colorfulRatio": round(self.colorful_count / self.count, 4),
            "meanSaturation": round(self.sum_saturation / self.count, 4),
            "meanBlueMinusRed": round(self.sum_blue_minus_red / self.count, 2),
            "blueDominantRatio": round(self.blue_dominant_count / self.count, 4),
            "uniqueColorEstimate": len(self.quantized_colors),
        }


async def write_textured_obj(
    mesh: FusedMesh,
    keyframes: list[ProjectionKeyframe],
    output_obj_path: Path,
    output_mtl_path: Path,
    output_texture_path: Path,
    output_debug_path: Path | None = None,
    output_debug_preview_path: Path | None = None,
    report_progress: Callable[[float, str], Awaitable[None]] | None = None,
    is_cancelled: CancellationCheck | None = None,
) -> dict:
    face_count = len(mesh.faces)
    atlas_max_size = texture_atlas_max_size_for_mesh(mesh)
    atlas_width, atlas_height, tile_size, columns = atlas_layout(face_count, atlas_max_size=atlas_max_size)
    tile_padding = atlas_tile_padding(tile_size)
    dilation_pixels = texture_dilation_pixels(tile_size)
    texture = Image.new("RGB", (atlas_width, atlas_height), FALLBACK_COLOR)
    texture_mask = Image.new("L", (atlas_width, atlas_height), 0)
    texture_pixels = texture.load()
    mask_pixels = texture_mask.load()
    vt_lines: list[str] = []
    face_lines: list[str] = []
    textured_face_count = 0
    fallback_face_count = 0
    rasterized_pixel_count = 0
    projected_pixel_count = 0
    fallback_pixel_count = 0
    dilated_pixel_count = 0
    uv_vertex_sample_stats = ColorStatsAccumulator()
    uv_face_interior_sample_stats = ColorStatsAccumulator()
    selected_keyframe_face_counts: dict[str, int] = {}
    keyframe_contribution_counts: dict[str, int] = {}
    blended_pixel_count = 0
    single_sample_pixel_count = 0
    accepted_projection_sample_count = 0
    rejected_overexposed_sample_count = 0
    rejected_underexposed_sample_count = 0
    rejected_edge_sample_count = 0
    rejected_grazing_sample_count = 0
    rejected_invalid_projection_sample_count = 0
    color_correction = texture_color_correction_for_keyframes(keyframes)
    uv_min_u = math.inf
    uv_min_v = math.inf
    uv_max_u = -math.inf
    uv_max_v = -math.inf
    uv_out_of_range_count = 0
    uv_non_finite_count = 0
    progress_interval = max(1, min(face_count // 50, 1_000))

    for face_index, face in enumerate(mesh.faces):
        if is_cancelled is not None and is_cancelled():
            raise asyncio.CancelledError

        tile = atlas_tile(face_index, tile_size, columns)
        atlas_triangle = atlas_triangle_points(tile, tile_size)
        uv_start = face_index * 3 + 1
        for point in atlas_triangle:
            u = (point[0] + 0.5) / atlas_width
            v = 1 - ((point[1] + 0.5) / atlas_height)
            if not math.isfinite(u) or not math.isfinite(v):
                uv_non_finite_count += 1
            else:
                uv_min_u = min(uv_min_u, u)
                uv_min_v = min(uv_min_v, v)
                uv_max_u = max(uv_max_u, u)
                uv_max_v = max(uv_max_v, v)
                if not (0 <= u <= 1 and 0 <= v <= 1):
                    uv_out_of_range_count += 1
            vt_lines.append(f"vt {u:.8f} {v:.8f}")

        face_vertices = [mesh.vertices[face[0]], mesh.vertices[face[1]], mesh.vertices[face[2]]]
        candidates = texture_projection_candidates(face_vertices, keyframes)
        if candidates:
            selected_key = keyframe_debug_id(candidates[0].keyframe)
            selected_keyframe_face_counts[selected_key] = selected_keyframe_face_counts.get(selected_key, 0) + 1
        fallback_color: tuple[int, int, int] | None = None

        def resolve_fallback_color() -> tuple[int, int, int]:
            nonlocal fallback_color
            if fallback_color is None:
                fallback_color = average_projected_color(face_vertices, keyframes) or FALLBACK_COLOR
            return fallback_color

        raster_stats = rasterize_face_texture(
            texture_pixels,
            mask_pixels,
            atlas_triangle,
            face_vertices,
            candidates,
            resolve_fallback_color,
        )
        for key, value in raster_stats["keyframeContributionCounts"].items():
            keyframe_contribution_counts[key] = keyframe_contribution_counts.get(key, 0) + value
        dilated_pixel_count += dilate_texture_tile(
            texture_pixels,
            mask_pixels,
            tile,
            tile_size,
            dilation_pixels,
        )
        rasterized_pixel_count += raster_stats["filledPixelCount"]
        projected_pixel_count += raster_stats["projectedPixelCount"]
        fallback_pixel_count += raster_stats["fallbackPixelCount"]
        blended_pixel_count += raster_stats["blendedPixelCount"]
        single_sample_pixel_count += raster_stats["singleSamplePixelCount"]
        accepted_projection_sample_count += raster_stats["acceptedProjectionSampleCount"]
        rejected_overexposed_sample_count += raster_stats["rejectedOverexposedSampleCount"]
        rejected_underexposed_sample_count += raster_stats["rejectedUnderexposedSampleCount"]
        rejected_edge_sample_count += raster_stats["rejectedEdgeSampleCount"]
        rejected_grazing_sample_count += raster_stats["rejectedGrazingSampleCount"]
        rejected_invalid_projection_sample_count += raster_stats["rejectedInvalidProjectionSampleCount"]
        if raster_stats["projectedPixelCount"] > 0:
            textured_face_count += 1
        else:
            fallback_face_count += 1

        for point in atlas_triangle:
            uv_vertex_sample_stats.add(sample_texture_at_atlas_point(texture_pixels, atlas_width, atlas_height, point[0], point[1]))
        for point in atlas_interior_sample_points(atlas_triangle):
            uv_face_interior_sample_stats.add(sample_texture_at_atlas_point(texture_pixels, atlas_width, atlas_height, point[0], point[1]))

        face_lines.append(
            f"f {face[0] + 1}/{uv_start} {face[1] + 1}/{uv_start + 1} {face[2] + 1}/{uv_start + 2}"
        )
        if report_progress is not None and (face_index % progress_interval == 0 or face_index == face_count - 1):
            fraction = ((face_index + 1) / face_count) if face_count else 1
            coverage = int((textured_face_count / (face_index + 1)) * 100) if face_index >= 0 else 0
            await report_progress(
                83 + fraction * 10,
                f"Texturing atlas faces {face_index + 1} / {face_count} ({coverage}% projected)",
            )
            await asyncio.sleep(0)

    texture_diagnostics = build_texture_diagnostics(
        texture=texture,
        face_count=face_count,
        tile_size=tile_size,
        atlas_max_size=atlas_max_size,
        tile_padding=tile_padding,
        dilation_pixels=dilation_pixels,
        uv_coordinate_count=len(vt_lines),
        uv_min_u=uv_min_u,
        uv_min_v=uv_min_v,
        uv_max_u=uv_max_u,
        uv_max_v=uv_max_v,
        uv_out_of_range_count=uv_out_of_range_count,
        uv_non_finite_count=uv_non_finite_count,
        uv_vertex_sample_stats=uv_vertex_sample_stats,
        uv_face_interior_sample_stats=uv_face_interior_sample_stats,
        textured_face_count=textured_face_count,
        fallback_face_count=fallback_face_count,
        rasterized_pixel_count=rasterized_pixel_count,
        projected_pixel_count=projected_pixel_count,
        fallback_pixel_count=fallback_pixel_count,
        dilated_pixel_count=dilated_pixel_count,
        selected_keyframe_face_counts=selected_keyframe_face_counts,
        keyframe_contribution_counts=keyframe_contribution_counts,
        blended_pixel_count=blended_pixel_count,
        single_sample_pixel_count=single_sample_pixel_count,
        accepted_projection_sample_count=accepted_projection_sample_count,
        rejected_overexposed_sample_count=rejected_overexposed_sample_count,
        rejected_underexposed_sample_count=rejected_underexposed_sample_count,
        rejected_edge_sample_count=rejected_edge_sample_count,
        rejected_grazing_sample_count=rejected_grazing_sample_count,
        rejected_invalid_projection_sample_count=rejected_invalid_projection_sample_count,
        render_mesh_stats=mesh.stats.get("textureRenderMesh", {}),
        color_correction=color_correction,
    )

    texture.save(output_texture_path)
    if output_debug_preview_path is not None:
        write_texture_debug_preview(texture, output_debug_preview_path)
    if output_debug_path is not None:
        output_debug_path.write_text(json.dumps(texture_diagnostics, indent=2), encoding="utf-8")
    output_mtl_path.write_text("\n".join([
        "newmtl LidarAI_Textured_Material",
        "Ka 1.000000 1.000000 1.000000",
        "Kd 1.000000 1.000000 1.000000",
        "Ks 0.000000 0.000000 0.000000",
        "d 1.0",
        "illum 1",
        f"map_Kd {output_texture_path.name}",
        "",
    ]), encoding="utf-8")

    invalid_uv_reference_count = 0
    if face_count > 0 and len(vt_lines) != face_count * 3:
        invalid_uv_reference_count = abs((face_count * 3) - len(vt_lines))

    texture_diagnostics["objSyntax"] = {
        **texture_diagnostics["objSyntax"],
        "invalidUVReferenceCount": invalid_uv_reference_count,
    }
    if output_debug_path is not None:
        output_debug_path.write_text(json.dumps(texture_diagnostics, indent=2), encoding="utf-8")

    lines = [
        "# LidarAI textured ARKit mesh",
        "# OBJ references textured_mesh.mtl and textured_mesh_texture.png",
        f"mtllib {output_mtl_path.name}",
        "o textured_mesh",
        "usemtl LidarAI_Textured_Material",
    ]
    for x, y, z in mesh.vertices:
        lines.append(f"v {x:.6f} {y:.6f} {z:.6f}")
    lines.extend(vt_lines)
    lines.extend(face_lines)
    output_obj_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    logger.info(
        "TEXTURE_DIAGNOSTICS faces=%s facesWithUV=%s uvOutOfRange=%s textureNonWhite=%.4f "
        "uvVertexNonWhite=%.4f uvInteriorNonWhite=%.4f projectionCoverage=%.4f tileSize=%s debug=%s",
        face_count,
        texture_diagnostics["objSyntax"]["faceWithUVIndexCount"],
        uv_out_of_range_count,
        texture_diagnostics["textureAtlas"]["sampledPixels"]["nonWhiteRatio"],
        texture_diagnostics["uvVertexSamples"]["nonWhiteRatio"],
        texture_diagnostics["uvFaceInteriorSamples"]["nonWhiteRatio"],
        (textured_face_count / face_count) if face_count else 0,
        tile_size,
        str(output_debug_path) if output_debug_path else "none",
    )

    return {
        "uvStrategy": "render_mesh_per_face_atlas_padded",
        "atlasWidth": atlas_width,
        "atlasHeight": atlas_height,
        "atlasMaxSize": atlas_max_size,
        "tileSize": tile_size,
        "tilePadding": tile_padding,
        "dilationPixels": dilation_pixels,
        "uvCoordinateCount": len(vt_lines),
        "faceCount": face_count,
        "texturedFaceCount": textured_face_count,
        "fallbackFaceCount": fallback_face_count,
        "projectionCoverage": (textured_face_count / face_count) if face_count else 0,
        "renderMesh": mesh.stats.get("textureRenderMesh", {}),
        "diagnostics": texture_diagnostics,
    }


def atlas_layout(face_count: int, atlas_max_size: int = TEXTURE_ATLAS_MAX_SIZE) -> tuple[int, int, int, int]:
    if face_count <= 0:
        return 64, 64, 32, 1

    columns = max(1, math.ceil(math.sqrt(face_count)))
    tile_size = max(TEXTURE_TILE_MIN_SIZE, min(TEXTURE_TILE_MAX_SIZE, atlas_max_size // columns))
    rows = max(1, math.ceil(face_count / columns))
    return columns * tile_size, rows * tile_size, tile_size, columns


def atlas_tile(face_index: int, tile_size: int, columns: int) -> tuple[int, int]:
    column = face_index % columns
    row = face_index // columns
    return column * tile_size, row * tile_size


def atlas_triangle_points(tile_origin: tuple[int, int], tile_size: int) -> list[tuple[float, float]]:
    margin = atlas_tile_padding(tile_size)
    left = tile_origin[0] + margin
    top = tile_origin[1] + margin
    right = tile_origin[0] + tile_size - margin - 1
    bottom = tile_origin[1] + tile_size - margin - 1
    return [(left, bottom), (right, bottom), (left, top)]


def atlas_tile_padding(tile_size: int) -> int:
    if tile_size < 6:
        return 0
    return min(4, max(1, tile_size // 6))


def texture_dilation_pixels(tile_size: int) -> int:
    return min(12, max(TEXTURE_ISLAND_DILATION_PIXELS, tile_size // 6))


def atlas_interior_sample_points(atlas_triangle: list[tuple[float, float]]) -> list[tuple[float, float]]:
    weights = [
        (1 / 3, 1 / 3, 1 / 3),
        (0.6, 0.2, 0.2),
        (0.2, 0.6, 0.2),
        (0.2, 0.2, 0.6),
    ]
    return [
        (
            atlas_triangle[0][0] * a + atlas_triangle[1][0] * b + atlas_triangle[2][0] * c,
            atlas_triangle[0][1] * a + atlas_triangle[1][1] * b + atlas_triangle[2][1] * c,
        )
        for a, b, c in weights
    ]


def sample_texture_at_atlas_point(
    texture_pixels: object,
    width: int,
    height: int,
    x: float,
    y: float,
) -> tuple[int, int, int]:
    pixel_x = max(0, min(int(round(x)), width - 1))
    pixel_y = max(0, min(int(round(y)), height - 1))
    color = texture_pixels[pixel_x, pixel_y]
    return (int(color[0]), int(color[1]), int(color[2]))


def build_texture_diagnostics(
    texture: Image.Image,
    face_count: int,
    tile_size: int,
    atlas_max_size: int,
    tile_padding: int,
    dilation_pixels: int,
    uv_coordinate_count: int,
    uv_min_u: float,
    uv_min_v: float,
    uv_max_u: float,
    uv_max_v: float,
    uv_out_of_range_count: int,
    uv_non_finite_count: int,
    uv_vertex_sample_stats: ColorStatsAccumulator,
    uv_face_interior_sample_stats: ColorStatsAccumulator,
    textured_face_count: int,
    fallback_face_count: int,
    rasterized_pixel_count: int,
    projected_pixel_count: int,
    fallback_pixel_count: int,
    dilated_pixel_count: int,
    selected_keyframe_face_counts: dict[str, int],
    keyframe_contribution_counts: dict[str, int],
    blended_pixel_count: int,
    single_sample_pixel_count: int,
    accepted_projection_sample_count: int,
    rejected_overexposed_sample_count: int,
    rejected_underexposed_sample_count: int,
    rejected_edge_sample_count: int,
    rejected_grazing_sample_count: int,
    rejected_invalid_projection_sample_count: int,
    render_mesh_stats: dict,
    color_correction: dict,
) -> dict:
    uv_bounds = {
        "minU": round(uv_min_u, 8) if math.isfinite(uv_min_u) else None,
        "minV": round(uv_min_v, 8) if math.isfinite(uv_min_v) else None,
        "maxU": round(uv_max_u, 8) if math.isfinite(uv_max_u) else None,
        "maxV": round(uv_max_v, 8) if math.isfinite(uv_max_v) else None,
    }
    texture_stats = sampled_texture_stats(texture)
    uv_vertex_stats = uv_vertex_sample_stats.to_dict()
    uv_face_stats = uv_face_interior_sample_stats.to_dict()
    projection_coverage = (textured_face_count / face_count) if face_count else 0
    raster_projection_ratio = (projected_pixel_count / rasterized_pixel_count) if rasterized_pixel_count else 0
    fallback_raster_ratio = (fallback_pixel_count / rasterized_pixel_count) if rasterized_pixel_count else 0
    mean_samples_per_projected_pixel = (
        accepted_projection_sample_count / projected_pixel_count
        if projected_pixel_count
        else 0
    )
    blended_projection_coverage = (
        blended_pixel_count / projected_pixel_count
        if projected_pixel_count
        else 0
    )
    diagnostics = {
        "version": "v2",
        "renderMesh": render_mesh_stats,
        "objSyntax": {
            "faceCount": face_count,
            "faceWithUVIndexCount": face_count,
            "faceWithoutUVIndexCount": 0,
            "uvCoordinateCount": uv_coordinate_count,
            "expectedUVCoordinateCount": face_count * 3,
            "invalidUVReferenceCount": 0,
        },
        "uv": {
            "bounds": uv_bounds,
            "nonFiniteCoordinateCount": uv_non_finite_count,
            "outOfRangeCoordinateCount": uv_out_of_range_count,
            "outOfRangeCoordinateRatio": round((uv_out_of_range_count / uv_coordinate_count) if uv_coordinate_count else 0, 4),
        },
        "textureAtlas": {
            "width": texture.width,
            "height": texture.height,
            "maxSize": atlas_max_size,
            "tileSize": tile_size,
            "tilePadding": tile_padding,
            "dilationPixels": dilation_pixels,
            "fallbackColor": list(FALLBACK_COLOR),
            "rasterizedPixelCount": rasterized_pixel_count,
            "projectedPixelCount": projected_pixel_count,
            "fallbackPixelCount": fallback_pixel_count,
            "fallbackRasterPixelRatio": round(fallback_raster_ratio, 4),
            "dilatedPixelCount": dilated_pixel_count,
            "projectedRasterPixelRatio": round(raster_projection_ratio, 4),
            "sampledPixels": texture_stats,
        },
        "projection": {
            "texturedFaceCount": textured_face_count,
            "fallbackFaceCount": fallback_face_count,
            "projectionCoverage": round(projection_coverage, 4),
            "selectedKeyframeFaceCounts": top_keyframe_counts(selected_keyframe_face_counts),
            "keyframeContributionCounts": top_keyframe_counts(keyframe_contribution_counts, value_label="sampleCount"),
            "blendedPixelCount": blended_pixel_count,
            "singleSamplePixelCount": single_sample_pixel_count,
            "blendedProjectionCoverage": round(blended_projection_coverage, 4),
            "acceptedProjectionSampleCount": accepted_projection_sample_count,
            "meanSamplesPerProjectedPixel": round(mean_samples_per_projected_pixel, 3),
            "rejectedOverexposedSampleCount": rejected_overexposed_sample_count,
            "rejectedUnderexposedSampleCount": rejected_underexposed_sample_count,
            "rejectedEdgeSampleCount": rejected_edge_sample_count,
            "rejectedGrazingSampleCount": rejected_grazing_sample_count,
            "rejectedInvalidProjectionSampleCount": rejected_invalid_projection_sample_count,
            "maxFaceCandidates": TEXTURE_BLEND_MAX_FACE_CANDIDATES,
            "maxPixelSamples": TEXTURE_BLEND_MAX_PIXEL_SAMPLES,
        },
        "colorCorrection": color_correction,
        "uvVertexSamples": uv_vertex_stats,
        "uvFaceInteriorSamples": uv_face_stats,
    }
    diagnostics["hints"] = texture_diagnostic_hints(diagnostics)
    return diagnostics


def sampled_texture_stats(texture: Image.Image, max_samples: int = 200_000) -> dict:
    width = max(texture.width, 1)
    height = max(texture.height, 1)
    total_pixels = width * height
    stride = max(1, int(math.sqrt(total_pixels / max_samples))) if total_pixels > max_samples else 1
    pixels = texture.load()
    accumulator = ColorStatsAccumulator()
    for y in range(0, height, stride):
        for x in range(0, width, stride):
            color = pixels[x, y]
            accumulator.add((int(color[0]), int(color[1]), int(color[2])))
    return {
        **accumulator.to_dict(),
        "totalPixelCount": total_pixels,
        "sampleStride": stride,
    }


def top_keyframe_counts(
    counts: dict[str, int],
    limit: int = 12,
    value_label: str = "faceCount",
) -> list[dict[str, int | str]]:
    return [
        {"keyframe": key, value_label: value}
        for key, value in sorted(counts.items(), key=lambda item: item[1], reverse=True)[:limit]
    ]


def texture_diagnostic_hints(diagnostics: dict) -> list[str]:
    hints: list[str] = []
    obj_syntax = diagnostics["objSyntax"]
    uv = diagnostics["uv"]
    texture = diagnostics["textureAtlas"]["sampledPixels"]
    atlas = diagnostics["textureAtlas"]
    uv_vertex = diagnostics["uvVertexSamples"]
    uv_face = diagnostics["uvFaceInteriorSamples"]
    render_mesh = diagnostics.get("renderMesh") or {}

    if atlas.get("tileSize", 0) < TEXTURE_RENDER_TARGET_MIN_TILE_SIZE:
        hints.append("Texture atlas tiles are still below the target size; increase render mesh simplification or atlas size if triangle patterning remains visible.")
    if render_mesh.get("used"):
        hints.append("Photoreal OBJ uses a simplified render mesh for larger texture islands; raw fused geometry remains available as fused_mesh.obj and rgbd_fused_mesh.obj.")
    if obj_syntax["faceWithUVIndexCount"] < obj_syntax["faceCount"]:
        hints.append("Some OBJ faces are missing UV indices; SceneKit may render those faces with a flat material color.")
    if obj_syntax["invalidUVReferenceCount"] > 0:
        hints.append("Some OBJ faces reference missing UV coordinates; check face/uv index generation.")
    if uv["outOfRangeCoordinateCount"] > 0 or uv["nonFiniteCoordinateCount"] > 0:
        hints.append("Some UV coordinates are invalid or outside 0...1; texture sampling can clamp to a border color.")
    if texture["nonWhiteRatio"] > 0.2 and uv_face["nonWhiteRatio"] < 0.2:
        hints.append("The atlas has color, but the face interior UV samples are mostly white; inspect UV indexing or texture-coordinate import.")
    if texture["nonWhiteRatio"] < 0.2:
        hints.append("The atlas itself is mostly near-white; inspect keyframe projection, selected views, and source image exposure.")
    if uv_vertex["fallbackColorRatio"] > 0.5 or uv_face["fallbackColorRatio"] > 0.5:
        hints.append("Most UV samples hit the fallback color; many faces are not receiving projected image pixels.")
    if uv_face.get("blueDominantRatio", 0) > 0.45 or uv_face.get("meanBlueMinusRed", 0) > 18:
        hints.append("Projected texture samples are still blue-heavy after correction; inspect keyframe white balance and room lighting.")
    if uv_face.get("meanSaturation", 0) < 0.08:
        hints.append("Projected texture samples have low saturation; source keyframes may be underexposed or over-smoothed.")
    if not hints:
        hints.append("Backend UVs and atlas samples look plausible; if iOS is still white, inspect SceneKit texture-coordinate sources and material rendering.")
    return hints


def write_texture_debug_preview(texture: Image.Image, output_path: Path, max_size: int = 1024) -> None:
    preview = texture.copy()
    preview.thumbnail((max_size, max_size), Image.Resampling.LANCZOS)
    preview.save(output_path)


def is_fallback_color(color: tuple[int, int, int], tolerance: int = 3) -> bool:
    return all(abs(int(color[index]) - FALLBACK_COLOR[index]) <= tolerance for index in range(3))


def select_face_keyframe(
    face_vertices: list[tuple[float, float, float]],
    keyframes: list[ProjectionKeyframe],
) -> ProjectionKeyframe | None:
    candidates = texture_projection_candidates(face_vertices, keyframes, max_candidates=1)
    return candidates[0].keyframe if candidates else None


def texture_projection_candidates(
    face_vertices: list[tuple[float, float, float]],
    keyframes: list[ProjectionKeyframe],
    max_candidates: int = TEXTURE_BLEND_MAX_FACE_CANDIDATES,
) -> list[TextureProjectionCandidate]:
    if not keyframes:
        return []

    center = triangle_center(face_vertices[0], face_vertices[1], face_vertices[2])
    normal = triangle_normal(face_vertices[0], face_vertices[1], face_vertices[2])
    candidates: list[TextureProjectionCandidate] = []
    for keyframe in keyframes:
        projection = project_world_point(center, keyframe)
        if projection is None:
            continue

        u, v, depth = projection
        visible_vertex_count = sum(1 for vertex in face_vertices if project_world_point(vertex, keyframe) is not None)
        if visible_vertex_count == 0:
            continue

        edge_margin = min(u, v, keyframe.width - u, keyframe.height - v)
        if edge_margin < projection_edge_margin_threshold(keyframe):
            continue

        center_bias = max(0.05, min(edge_margin / max(min(keyframe.width, keyframe.height) * 0.25, 1), 1))
        view_vector = normalize(subtract(keyframe.camera_position, center))
        facing = abs(dot(normal, view_vector)) if normal != (0.0, 0.0, 0.0) else 0.25
        if facing < TEXTURE_BLEND_MIN_FACING:
            continue

        score = center_bias * (visible_vertex_count / 3) * max(facing, 0.15) / max(depth, 0.2)
        candidates.append(TextureProjectionCandidate(
            keyframe=keyframe,
            score=score,
            visible_vertex_count=visible_vertex_count,
            center_projection=projection,
            facing=facing,
            center_edge_margin=edge_margin,
        ))

    candidates.sort(key=lambda candidate: candidate.score, reverse=True)
    return candidates[:max_candidates]


def rasterize_face_texture(
    texture_pixels: object,
    mask_pixels: object,
    atlas_triangle: list[tuple[float, float]],
    face_vertices: list[tuple[float, float, float]],
    candidates: list[TextureProjectionCandidate],
    fallback_color: Callable[[], tuple[int, int, int]],
) -> dict:
    min_x = max(0, int(math.floor(min(point[0] for point in atlas_triangle))))
    max_x = int(math.ceil(max(point[0] for point in atlas_triangle)))
    min_y = max(0, int(math.floor(min(point[1] for point in atlas_triangle))))
    max_y = int(math.ceil(max(point[1] for point in atlas_triangle)))
    filled_pixel_count = 0
    projected_pixel_count = 0
    fallback_pixel_count = 0
    blended_pixel_count = 0
    single_sample_pixel_count = 0
    accepted_projection_sample_count = 0
    rejected_overexposed_sample_count = 0
    rejected_underexposed_sample_count = 0
    rejected_edge_sample_count = 0
    rejected_grazing_sample_count = 0
    rejected_invalid_projection_sample_count = 0
    keyframe_contribution_counts: dict[str, int] = {}

    for y in range(min_y, max_y + 1):
        for x in range(min_x, max_x + 1):
            bary = barycentric((x + 0.5, y + 0.5), atlas_triangle[0], atlas_triangle[1], atlas_triangle[2])
            if bary is None or min(bary) < -1e-5:
                continue

            color: tuple[int, int, int] | None = None
            if candidates:
                world_point = interpolate_triangle(face_vertices, bary)
                blend = blend_projected_texture_sample(world_point, candidates)
                accepted_count = blend["acceptedSampleCount"]
                if accepted_count > 0:
                    color = blend["color"]
                    projected_pixel_count += 1
                    accepted_projection_sample_count += accepted_count
                    rejected_overexposed_sample_count += blend["rejectedOverexposedSampleCount"]
                    rejected_underexposed_sample_count += blend["rejectedUnderexposedSampleCount"]
                    rejected_edge_sample_count += blend["rejectedEdgeSampleCount"]
                    rejected_grazing_sample_count += blend["rejectedGrazingSampleCount"]
                    rejected_invalid_projection_sample_count += blend["rejectedInvalidProjectionSampleCount"]
                    if accepted_count > 1:
                        blended_pixel_count += 1
                    else:
                        single_sample_pixel_count += 1
                    for key, value in blend["keyframeContributionCounts"].items():
                        keyframe_contribution_counts[key] = keyframe_contribution_counts.get(key, 0) + value
                else:
                    rejected_overexposed_sample_count += blend["rejectedOverexposedSampleCount"]
                    rejected_underexposed_sample_count += blend["rejectedUnderexposedSampleCount"]
                    rejected_edge_sample_count += blend["rejectedEdgeSampleCount"]
                    rejected_grazing_sample_count += blend["rejectedGrazingSampleCount"]
                    rejected_invalid_projection_sample_count += blend["rejectedInvalidProjectionSampleCount"]
                    fallback_pixel_count += 1
            else:
                fallback_pixel_count += 1
            if color is None:
                color = fallback_color()
            texture_pixels[x, y] = color
            mask_pixels[x, y] = 255
            filled_pixel_count += 1

    return {
        "filledPixelCount": filled_pixel_count,
        "projectedPixelCount": projected_pixel_count,
        "fallbackPixelCount": fallback_pixel_count,
        "blendedPixelCount": blended_pixel_count,
        "singleSamplePixelCount": single_sample_pixel_count,
        "acceptedProjectionSampleCount": accepted_projection_sample_count,
        "rejectedOverexposedSampleCount": rejected_overexposed_sample_count,
        "rejectedUnderexposedSampleCount": rejected_underexposed_sample_count,
        "rejectedEdgeSampleCount": rejected_edge_sample_count,
        "rejectedGrazingSampleCount": rejected_grazing_sample_count,
        "rejectedInvalidProjectionSampleCount": rejected_invalid_projection_sample_count,
        "keyframeContributionCounts": keyframe_contribution_counts,
    }


def blend_projected_texture_sample(
    world_point: tuple[float, float, float],
    candidates: list[TextureProjectionCandidate],
) -> dict:
    samples: list[tuple[float, tuple[int, int, int], str]] = []
    rejected_overexposed_count = 0
    rejected_underexposed_count = 0
    rejected_edge_count = 0
    rejected_grazing_count = 0
    rejected_invalid_projection_count = 0

    for candidate in candidates[:TEXTURE_BLEND_MAX_PIXEL_SAMPLES]:
        if candidate.facing < TEXTURE_BLEND_MIN_FACING:
            rejected_grazing_count += 1
            continue

        keyframe = candidate.keyframe
        projection = project_world_point(world_point, keyframe)
        if projection is None:
            rejected_invalid_projection_count += 1
            continue

        u, v, depth = projection
        edge_margin = min(u, v, keyframe.width - u, keyframe.height - v)
        if edge_margin < projection_edge_margin_threshold(keyframe):
            rejected_edge_count += 1
            continue

        color = sample_image_bilinear(keyframe, u, v)
        luminance = rgb_luminance(color)
        detail_range = max(color) - min(color)
        if luminance > TEXTURE_REJECT_OVEREXPOSED_LUMINANCE and detail_range <= TEXTURE_REJECT_LOW_DETAIL_RANGE:
            rejected_overexposed_count += 1
            continue
        if luminance < TEXTURE_REJECT_UNDEREXPOSED_LUMINANCE:
            rejected_underexposed_count += 1
            continue

        edge_weight = max(0.05, min(edge_margin / max(min(keyframe.width, keyframe.height) * 0.18, 1), 1))
        weight = max(candidate.score, 1e-6) * edge_weight / max(depth, 0.2)
        samples.append((weight, color, keyframe_debug_id(keyframe)))

    if not samples:
        return {
            "color": FALLBACK_COLOR,
            "acceptedSampleCount": 0,
            "keyframeContributionCounts": {},
            "rejectedOverexposedSampleCount": rejected_overexposed_count,
            "rejectedUnderexposedSampleCount": rejected_underexposed_count,
            "rejectedEdgeSampleCount": rejected_edge_count,
            "rejectedGrazingSampleCount": rejected_grazing_count,
            "rejectedInvalidProjectionSampleCount": rejected_invalid_projection_count,
        }

    total_weight = sum(weight for weight, _color, _key in samples)
    if total_weight <= 1e-8:
        total_weight = float(len(samples))
        samples = [(1.0, color, key) for _weight, color, key in samples]

    linear_r = sum(weight * srgb_to_linear(color[0]) for weight, color, _key in samples) / total_weight
    linear_g = sum(weight * srgb_to_linear(color[1]) for weight, color, _key in samples) / total_weight
    linear_b = sum(weight * srgb_to_linear(color[2]) for weight, color, _key in samples) / total_weight
    contribution_counts: dict[str, int] = {}
    for _weight, _color, key in samples:
        contribution_counts[key] = contribution_counts.get(key, 0) + 1

    return {
        "color": (
            linear_to_srgb(linear_r),
            linear_to_srgb(linear_g),
            linear_to_srgb(linear_b),
        ),
        "acceptedSampleCount": len(samples),
        "keyframeContributionCounts": contribution_counts,
        "rejectedOverexposedSampleCount": rejected_overexposed_count,
        "rejectedUnderexposedSampleCount": rejected_underexposed_count,
        "rejectedEdgeSampleCount": rejected_edge_count,
        "rejectedGrazingSampleCount": rejected_grazing_count,
        "rejectedInvalidProjectionSampleCount": rejected_invalid_projection_count,
    }


def dilate_texture_tile(
    texture_pixels: object,
    mask_pixels: object,
    tile_origin: tuple[int, int],
    tile_size: int,
    iterations: int,
) -> int:
    left = tile_origin[0]
    top = tile_origin[1]
    right = left + tile_size
    bottom = top + tile_size
    total_updates = 0

    for _ in range(iterations):
        updates: list[tuple[int, int, tuple[int, int, int]]] = []
        for y in range(top, bottom):
            for x in range(left, right):
                if mask_pixels[x, y] != 0:
                    continue

                neighbor_colors: list[tuple[int, int, int]] = []
                for offset_y in (-1, 0, 1):
                    for offset_x in (-1, 0, 1):
                        if offset_x == 0 and offset_y == 0:
                            continue
                        neighbor_x = x + offset_x
                        neighbor_y = y + offset_y
                        if not (left <= neighbor_x < right and top <= neighbor_y < bottom):
                            continue
                        if mask_pixels[neighbor_x, neighbor_y] == 0:
                            continue
                        color = texture_pixels[neighbor_x, neighbor_y]
                        neighbor_colors.append((int(color[0]), int(color[1]), int(color[2])))

                if not neighbor_colors:
                    continue

                updates.append((
                    x,
                    y,
                    (
                        clamp_color(sum(color[0] for color in neighbor_colors) / len(neighbor_colors)),
                        clamp_color(sum(color[1] for color in neighbor_colors) / len(neighbor_colors)),
                        clamp_color(sum(color[2] for color in neighbor_colors) / len(neighbor_colors)),
                    ),
                ))

        if not updates:
            break

        for x, y, color in updates:
            texture_pixels[x, y] = color
            mask_pixels[x, y] = 128
        total_updates += len(updates)

    return total_updates


def average_projected_color(
    vertices: list[tuple[float, float, float]],
    keyframes: list[ProjectionKeyframe],
) -> tuple[int, int, int] | None:
    colors = [project_vertex_color(vertex, keyframes) for vertex in vertices]
    colors = [color for color in colors if color is not None]
    if not colors:
        return None

    count = len(colors)
    return (
        clamp_color(sum(color[0] for color in colors) / count),
        clamp_color(sum(color[1] for color in colors) / count),
        clamp_color(sum(color[2] for color in colors) / count),
    )


def projection_edge_margin_threshold(keyframe: ProjectionKeyframe) -> float:
    return max(2.0, min(keyframe.width, keyframe.height) * TEXTURE_BLEND_EDGE_MARGIN_RATIO)


def keyframe_debug_id(keyframe: ProjectionKeyframe) -> str:
    return keyframe.id or keyframe.path or "unknown"


def project_world_point(
    vertex: tuple[float, float, float],
    keyframe: ProjectionKeyframe,
) -> tuple[float, float, float] | None:
    x, y, z = transform_point(keyframe.world_to_camera, list(vertex))
    depth = -z
    if depth <= 0.05:
        return None

    intrinsics = keyframe.intrinsics
    fx = float(intrinsics[0])
    fy = float(intrinsics[4])
    cx = float(intrinsics[6])
    cy = float(intrinsics[7])
    u = fx * x / depth + cx
    v = cy - fy * y / depth
    if not (0 <= u < keyframe.width and 0 <= v < keyframe.height):
        return None

    return u, v, depth


def sample_image_nearest(keyframe: ProjectionKeyframe, u: float, v: float) -> tuple[int, int, int]:
    x = max(0, min(int(round(u)), keyframe.width - 1))
    y = max(0, min(int(round(v)), keyframe.height - 1))
    return keyframe.pixels[x, y]


def sample_image_bilinear(keyframe: ProjectionKeyframe, u: float, v: float) -> tuple[int, int, int]:
    x0 = max(0, min(int(math.floor(u)), keyframe.width - 1))
    y0 = max(0, min(int(math.floor(v)), keyframe.height - 1))
    x1 = max(0, min(x0 + 1, keyframe.width - 1))
    y1 = max(0, min(y0 + 1, keyframe.height - 1))
    dx = u - x0
    dy = v - y0
    c00 = keyframe.pixels[x0, y0]
    c10 = keyframe.pixels[x1, y0]
    c01 = keyframe.pixels[x0, y1]
    c11 = keyframe.pixels[x1, y1]
    return (
        clamp_color((1 - dx) * (1 - dy) * c00[0] + dx * (1 - dy) * c10[0] + (1 - dx) * dy * c01[0] + dx * dy * c11[0]),
        clamp_color((1 - dx) * (1 - dy) * c00[1] + dx * (1 - dy) * c10[1] + (1 - dx) * dy * c01[1] + dx * dy * c11[1]),
        clamp_color((1 - dx) * (1 - dy) * c00[2] + dx * (1 - dy) * c10[2] + (1 - dx) * dy * c01[2] + dx * dy * c11[2]),
    )


def rgb_luminance(color: tuple[int, int, int]) -> float:
    return 0.2126 * int(color[0]) + 0.7152 * int(color[1]) + 0.0722 * int(color[2])


def srgb_to_linear(value: int) -> float:
    normalized = max(0.0, min(float(value) / 255.0, 1.0))
    if normalized <= 0.04045:
        return normalized / 12.92
    return ((normalized + 0.055) / 1.055) ** 2.4


def linear_to_srgb(value: float) -> int:
    value = max(0.0, min(value, 1.0))
    if value <= 0.0031308:
        normalized = value * 12.92
    else:
        normalized = 1.055 * (value ** (1 / 2.4)) - 0.055
    return clamp_color(normalized * 255.0)


def barycentric(
    point: tuple[float, float],
    a: tuple[float, float],
    b: tuple[float, float],
    c: tuple[float, float],
) -> tuple[float, float, float] | None:
    denominator = (b[1] - c[1]) * (a[0] - c[0]) + (c[0] - b[0]) * (a[1] - c[1])
    if abs(denominator) < 1e-8:
        return None

    w0 = ((b[1] - c[1]) * (point[0] - c[0]) + (c[0] - b[0]) * (point[1] - c[1])) / denominator
    w1 = ((c[1] - a[1]) * (point[0] - c[0]) + (a[0] - c[0]) * (point[1] - c[1])) / denominator
    w2 = 1 - w0 - w1
    return w0, w1, w2


def interpolate_triangle(
    vertices: list[tuple[float, float, float]],
    weights: tuple[float, float, float],
) -> tuple[float, float, float]:
    return (
        vertices[0][0] * weights[0] + vertices[1][0] * weights[1] + vertices[2][0] * weights[2],
        vertices[0][1] * weights[0] + vertices[1][1] * weights[1] + vertices[2][1] * weights[2],
        vertices[0][2] * weights[0] + vertices[1][2] * weights[1] + vertices[2][2] * weights[2],
    )


def triangle_center(
    a: tuple[float, float, float],
    b: tuple[float, float, float],
    c: tuple[float, float, float],
) -> tuple[float, float, float]:
    return ((a[0] + b[0] + c[0]) / 3, (a[1] + b[1] + c[1]) / 3, (a[2] + b[2] + c[2]) / 3)


def triangle_area(
    a: tuple[float, float, float],
    b: tuple[float, float, float],
    c: tuple[float, float, float],
) -> float:
    cross_value = cross(subtract(b, a), subtract(c, a))
    return 0.5 * length(cross_value)


def triangle_normal(
    a: tuple[float, float, float],
    b: tuple[float, float, float],
    c: tuple[float, float, float],
) -> tuple[float, float, float]:
    return normalize(cross(subtract(b, a), subtract(c, a)))


def subtract(
    a: tuple[float, float, float],
    b: tuple[float, float, float],
) -> tuple[float, float, float]:
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def multiply(
    value: tuple[float, float, float],
    scalar: float,
) -> tuple[float, float, float]:
    return (value[0] * scalar, value[1] * scalar, value[2] * scalar)


def clamp_vector_length(
    value: tuple[float, float, float],
    max_length: float,
) -> tuple[float, float, float]:
    current_length = length(value)
    if max_length <= 0 or current_length <= max_length:
        return value

    scale = max_length / max(current_length, 1e-8)
    return multiply(value, scale)


def cross(
    a: tuple[float, float, float],
    b: tuple[float, float, float],
) -> tuple[float, float, float]:
    return (
        a[1] * b[2] - a[2] * b[1],
        a[2] * b[0] - a[0] * b[2],
        a[0] * b[1] - a[1] * b[0],
    )


def dot(a: tuple[float, float, float], b: tuple[float, float, float]) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def length(value: tuple[float, float, float]) -> float:
    return math.sqrt(dot(value, value))


def normalize(value: tuple[float, float, float]) -> tuple[float, float, float]:
    magnitude = length(value)
    if magnitude <= 1e-8:
        return (0.0, 0.0, 0.0)
    return (value[0] / magnitude, value[1] / magnitude, value[2] / magnitude)


def invert_rigid_transform(matrix: list[float]) -> list[float]:
    # ARKit camera transforms are rigid column-major matrices. The inverse is R^T and -R^T t.
    r00, r01, r02 = matrix[0], matrix[4], matrix[8]
    r10, r11, r12 = matrix[1], matrix[5], matrix[9]
    r20, r21, r22 = matrix[2], matrix[6], matrix[10]
    tx, ty, tz = matrix[12], matrix[13], matrix[14]

    inv_tx = -(r00 * tx + r10 * ty + r20 * tz)
    inv_ty = -(r01 * tx + r11 * ty + r21 * tz)
    inv_tz = -(r02 * tx + r12 * ty + r22 * tz)

    return [
        r00, r01, r02, 0,
        r10, r11, r12, 0,
        r20, r21, r22, 0,
        inv_tx, inv_ty, inv_tz, 1,
    ]


def clamp_color(value: float) -> int:
    if math.isnan(value):
        return 0
    return max(0, min(int(round(value)), 255))
