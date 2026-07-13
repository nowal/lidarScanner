from __future__ import annotations

import asyncio
from array import array
import base64
import binascii
from collections import deque
from concurrent.futures import ProcessPoolExecutor
import importlib
from io import BytesIO
import json
import logging
import math
import multiprocessing
import os
import shutil
import struct
import sys
import zipfile
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Awaitable, Callable, NamedTuple, Protocol

from PIL import Image, ImageDraw, ImageEnhance

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


FaceUVs = list[tuple[tuple[float, float], tuple[float, float], tuple[float, float]]]


@dataclass
class ProjectionDepthFrame:
    id: str | None
    color_keyframe_id: str | None
    width: int
    height: int
    world_to_camera: list[float]
    intrinsics: list[float]
    depth_values: array
    confidence_values: bytes | None = None
    timestamp: float | None = None
    path: str | None = None
    confidence_path: str | None = None
    world_to_camera_values: tuple[float, ...] = field(init=False, repr=False)
    fx: float = field(init=False)
    fy: float = field(init=False)
    cx: float = field(init=False)
    cy: float = field(init=False)
    debug_id: str = field(init=False)

    def __post_init__(self) -> None:
        self.world_to_camera_values = tuple(float(value) for value in self.world_to_camera[:16])
        self.fx = float(self.intrinsics[0]) if len(self.intrinsics) > 0 else 0.0
        self.fy = float(self.intrinsics[4]) if len(self.intrinsics) > 4 else 0.0
        self.cx = float(self.intrinsics[6]) if len(self.intrinsics) > 6 else 0.0
        self.cy = float(self.intrinsics[7]) if len(self.intrinsics) > 7 else 0.0
        self.debug_id = self.id or self.color_keyframe_id or "unknown-depth"


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
    timestamp: float | None = None
    captured_at: str | None = None
    color_correction: dict | None = None
    depth_frame: ProjectionDepthFrame | None = None
    mesh_visibility_mask: bytearray | None = None
    mesh_visibility_stats: dict | None = None
    world_to_camera_values: tuple[float, ...] = field(init=False, repr=False)
    fx: float = field(init=False)
    fy: float = field(init=False)
    cx: float = field(init=False)
    cy: float = field(init=False)
    edge_margin_threshold: float = field(init=False)
    center_bias_denominator: float = field(init=False)
    blend_edge_denominator: float = field(init=False)
    debug_id: str = field(init=False)

    def __post_init__(self) -> None:
        self.world_to_camera_values = tuple(float(value) for value in self.world_to_camera[:16])
        self.fx = float(self.intrinsics[0]) if len(self.intrinsics) > 0 else 0.0
        self.fy = float(self.intrinsics[4]) if len(self.intrinsics) > 4 else 0.0
        self.cx = float(self.intrinsics[6]) if len(self.intrinsics) > 6 else 0.0
        self.cy = float(self.intrinsics[7]) if len(self.intrinsics) > 7 else 0.0
        min_dimension = min(self.width, self.height)
        self.edge_margin_threshold = max(2.0, min_dimension * TEXTURE_BLEND_EDGE_MARGIN_RATIO)
        self.center_bias_denominator = max(min_dimension * 0.25, 1)
        self.blend_edge_denominator = max(min_dimension * 0.18, 1)
        self.debug_id = self.id or self.path or "unknown"


@dataclass
class TextureProjectionCandidate:
    keyframe: ProjectionKeyframe
    keyframe_debug_id: str
    score: float
    visible_vertex_count: int
    center_projection: tuple[float, float, float]
    facing: float
    center_edge_margin: float


class TextureBlendResult(NamedTuple):
    color: tuple[int, int, int]
    accepted_sample_count: int
    keyframe_contribution_keys: tuple[str, ...]
    rejected_overexposed_sample_count: int
    rejected_underexposed_sample_count: int
    rejected_edge_sample_count: int
    rejected_grazing_sample_count: int
    rejected_invalid_projection_sample_count: int
    rejected_depth_edge_sample_count: int
    rejected_occluded_sample_count: int
    depth_tested_sample_count: int
    missing_depth_sample_count: int


class TextureFaceResult(NamedTuple):
    face_index: int
    tile_bytes: bytes
    selected_keyframe: str | None
    filled_pixel_count: int
    projected_pixel_count: int
    fallback_pixel_count: int
    blended_pixel_count: int
    single_sample_pixel_count: int
    accepted_projection_sample_count: int
    rejected_overexposed_sample_count: int
    rejected_underexposed_sample_count: int
    rejected_edge_sample_count: int
    rejected_grazing_sample_count: int
    rejected_invalid_projection_sample_count: int
    rejected_depth_edge_sample_count: int
    rejected_occluded_sample_count: int
    depth_tested_sample_count: int
    missing_depth_sample_count: int
    dilated_pixel_count: int
    keyframe_contribution_keys: tuple[str, ...]
    uv_vertex_sample_colors: tuple[tuple[int, int, int], ...]
    uv_face_interior_sample_colors: tuple[tuple[int, int, int], ...]


class DepthVisibilityResult(NamedTuple):
    status: str
    weight: float
    projected_depth: float | None
    sampled_depth: float | None
    confidence: int | None


@dataclass
class PlanarTextureChart:
    chart_id: int
    face_indices: list[int]
    normal: tuple[float, float, float]
    plane_offset: float
    axis_u: tuple[float, float, float]
    axis_v: tuple[float, float, float]
    min_u: float
    max_u: float
    min_v: float
    max_v: float
    width: int
    height: int
    x: int = 0
    y: int = 0
    source_plane_index: int | None = None


@dataclass
class TextureAtlasLayout:
    width: int
    height: int
    tile_size: int
    columns: int
    tile_start_y: int
    planar_charts: list[PlanarTextureChart]
    face_to_chart: dict[int, PlanarTextureChart]
    face_to_tile_index: dict[int, int]
    strategy: str
    stats: dict


@dataclass
class SourceImageAtlasPlacement:
    keyframe: ProjectionKeyframe
    x: int
    y: int
    tile_size: int
    image_x: int
    image_y: int
    image_width: int
    image_height: int
    source_scale: float
    owner_face_count: int


@dataclass
class SourceImageAtlasLayout:
    width: int
    height: int
    tile_size: int
    columns: int
    rows: int
    tile_start_y: int
    placements: dict[str, SourceImageAtlasPlacement]
    stats: dict


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
TEXTURE_UNOBSERVED_COLOR = (255, 255, 255)
TEXTURE_ATLAS_MAX_SIZE = 4096
TEXTURE_TSDF_ATLAS_MAX_SIZE = 6144
TEXTURE_TILE_MAX_SIZE = 96
TEXTURE_TILE_MIN_SIZE = 4
TEXTURE_RENDER_TARGET_MIN_TILE_SIZE = 12
TEXTURE_RENDER_TARGET_FACE_COUNT = 120_000
TEXTURE_TSDF_RENDER_TARGET_FACE_COUNT = 150_000
FAST_ONBOARDING_TEXTURE_RENDER_TARGET_FACE_COUNT = 45_000
FAST_ONBOARDING_TEXTURE_TSDF_RENDER_TARGET_FACE_COUNT = 50_000
FAST_ONBOARDING_FALLBACK_TEXTURE_FACE_LIMIT = 10_000
FAST_ONBOARDING_TEXTURE_SURFACE_DENSIFY_MAX_EDGE_METERS = 0.045
FAST_ONBOARDING_TEXTURE_SURFACE_DENSIFY_MAX_ITERATIONS = 10
DENSE_SINGLE_VIEW_HERO_MAX_FACE_SAMPLES = 20_000
DENSE_SINGLE_VIEW_EDGE_MARGIN_PIXELS = 1.0
DENSE_SINGLE_VIEW_MIN_FACING = 0.02
TEXTURE_RENDER_MIN_CLUSTER_METERS = 0.006
TEXTURE_RENDER_MAX_CLUSTER_METERS = 0.08
TEXTURE_RENDER_SMOOTHING_ITERATIONS = 8
TEXTURE_RENDER_SMOOTHING_STRENGTH = 0.45
TEXTURE_RENDER_SMOOTHING_BOUNDARY_STRENGTH = 0.16
TEXTURE_RENDER_SMOOTHING_HARD_EDGE_WEIGHT = 0.08
TEXTURE_RENDER_SMOOTHING_NORMAL_COSINE = 0.72
TEXTURE_RENDER_SMOOTHING_MAX_TOTAL_DISPLACEMENT_METERS = 0.10
TEXTURE_RENDER_PLANE_REGULARIZATION_ENABLED = True
TEXTURE_RENDER_PLANE_MAX_PLANES = 8
TEXTURE_RENDER_PLANE_DISTANCE_THRESHOLD_METERS = 0.035
TEXTURE_RENDER_PLANE_NORMAL_ALIGNMENT = 0.68
TEXTURE_RENDER_PLANE_MIN_VERTEX_RATIO = 0.035
TEXTURE_RENDER_PLANE_MIN_VERTICES = 1_200
TEXTURE_RENDER_PLANE_STRENGTH = 0.65
TEXTURE_RENDER_PLANE_MAX_DISPLACEMENT_METERS = 0.04
TEXTURE_TSDF_RENDER_EXTRA_SMOOTHING_ITERATIONS = 5
TEXTURE_TSDF_RENDER_EXTRA_SMOOTHING_STRENGTH = 0.28
TEXTURE_TSDF_RENDER_EXTRA_SMOOTHING_BOUNDARY_STRENGTH = 0.08
TEXTURE_TSDF_RENDER_EXTRA_SMOOTHING_HARD_EDGE_WEIGHT = 0.18
TEXTURE_TSDF_RENDER_EXTRA_SMOOTHING_NORMAL_COSINE = 0.84
TEXTURE_TSDF_RENDER_EXTRA_SMOOTHING_MAX_TOTAL_DISPLACEMENT_METERS = 0.055
TEXTURE_PLANAR_CHARTS_ENABLED = True
TEXTURE_PLANAR_CHART_MAX_COUNT = 8
TEXTURE_PLANAR_CHART_MIN_FACE_COUNT = 500
TEXTURE_PLANAR_CHART_MIN_AREA_M2 = 0.45
TEXTURE_PLANAR_CHART_DISTANCE_METERS = 0.055
TEXTURE_PLANAR_CHART_NORMAL_ALIGNMENT = 0.72
TEXTURE_PLANAR_CHART_PIXELS_PER_METER = 360
TEXTURE_PLANAR_CHART_MIN_SIZE = 256
TEXTURE_PLANAR_CHART_MAX_SIZE = 1792
TEXTURE_PLANAR_CHART_PADDING_METERS = 0.06
TEXTURE_PLANAR_CHART_ATLAS_HEIGHT_RATIO = 0.68
TEXTURE_PLANAR_CHART_DIRECT_MAX_CANDIDATES = 3
TEXTURE_PLANAR_CHART_DIRECT_EDGE_MARGIN_SCALE = 0.35
TEXTURE_PLANAR_CHART_NEIGHBOR_FILL_ENABLED = True
TEXTURE_PLANAR_CHART_LOCAL_FILL_MAX_RADIUS_PIXELS = 2
TEXTURE_PLANAR_CHART_SECONDARY_FILL_ENABLED = True
TEXTURE_PLANAR_CHART_SECONDARY_MIN_REGION_PIXELS = 256
TEXTURE_PLANAR_CHART_SECONDARY_MIN_COVERAGE_RATIO = 0.55
TEXTURE_PLANAR_CHART_SECONDARY_MAX_REGIONS = 12
TEXTURE_PLANAR_CHART_SECONDARY_MAX_SAMPLE_POINTS = 512
TEXTURE_ISLAND_DILATION_PIXELS = 4
TEXTURE_SOURCE_IMAGE_ATLAS_ENABLED = True
TEXTURE_SOURCE_IMAGE_ATLAS_MAX_TILE_SIZE = 768
TEXTURE_SOURCE_IMAGE_ATLAS_MIN_TILE_SIZE = 128
TEXTURE_SOURCE_IMAGE_ATLAS_TILE_STEP = 16
TEXTURE_SOURCE_IMAGE_ATLAS_PADDING = 8
TEXTURE_COLOR_SATURATION_BOOST = 1.12
TEXTURE_COLOR_CONTRAST_BOOST = 1.05
TEXTURE_BLEND_MAX_FACE_CANDIDATES = 6
TEXTURE_BLEND_MAX_PIXEL_SAMPLES = 5
TEXTURE_BLEND_MIN_FACING = 0.18
TEXTURE_BLEND_EDGE_MARGIN_RATIO = 0.018
TEXTURE_VISIBILITY_RASTER_MAX_SIZE = 768
TEXTURE_VISIBILITY_DEPTH_TOLERANCE_METERS = 0.035
TEXTURE_COHERENT_LABEL_MAX_CANDIDATES = 4
TEXTURE_COHERENT_LABEL_ITERATIONS = 3
TEXTURE_COHERENT_LABEL_SMOOTHNESS_WEIGHT = 0.18
TEXTURE_COHERENT_LABEL_SWITCH_TOLERANCE = 0.72
ADAPTIVE_KEYFRAME_PROXY_FACE_SAMPLES = 6_000
ADAPTIVE_KEYFRAME_MIN_MARGINAL_BENEFIT_RATIO = 0.006
ADAPTIVE_KEYFRAME_MIN_NEW_COVERAGE_RATIO = 0.0025
ADAPTIVE_KEYFRAME_MIN_VISIBLE_SAMPLE_RATIO = 0.002
ADAPTIVE_KEYFRAME_POSE_DEDUPE_TRANSLATION_METERS = 0.045
ADAPTIVE_KEYFRAME_POSE_DEDUPE_ANGLE_DEGREES = 4.0
ADAPTIVE_KEYFRAME_QUALITY_GAIN_EPSILON = 0.015
TEXTURE_DEPTH_OCCLUSION_BASE_TOLERANCE_METERS = 0.08
TEXTURE_DEPTH_OCCLUSION_RELATIVE_TOLERANCE = 0.035
TEXTURE_DEPTH_EDGE_ABSOLUTE_METERS = 0.12
TEXTURE_DEPTH_EDGE_RELATIVE = 0.08
TEXTURE_DEPTH_UNKNOWN_SAMPLE_WEIGHT = 0.72
TEXTURE_DEPTH_MISMATCH_MIN_WEIGHT = 0.35
TEXTURE_DEPTH_NEIGHBORHOOD_RADIUS = 1
TEXTURE_REJECT_OVEREXPOSED_LUMINANCE = 245
TEXTURE_REJECT_UNDEREXPOSED_LUMINANCE = 8
TEXTURE_REJECT_LOW_DETAIL_RANGE = 10
TEXTURE_PARALLEL_MIN_FACE_COUNT = 10_000
TEXTURE_PARALLEL_CHUNK_SIZE = 384
TEXTURE_PARALLEL_MAX_AUTO_WORKERS = 4
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
RGBD_DIAGNOSTIC_MAX_POINT_SAMPLES = 180_000
RGBD_DIAGNOSTIC_OVERLAY_MAX_SAMPLES = 16_000
RGBD_DIAGNOSTIC_MESH_TARGET_SAMPLES = 40_000
RGBD_HERO_PATCH_TARGET_SAMPLES = 120_000
RGBD_HERO_PATCH_CANDIDATE_POOL_SIZE = 8
RGBD_HERO_PATCH_MAX_PATCHES = 3
RGBD_HERO_PATCH_HOLE_FILL_PASSES = 3
RGBD_HERO_PATCH_HOLE_FILL_RADIUS = 2
RGBD_HERO_PATCH_FACE_ABSOLUTE_TOLERANCE_METERS = 0.35
RGBD_HERO_PATCH_FACE_RELATIVE_TOLERANCE = 0.45
RGBD_HERO_PATCH_SUPPLEMENT_MIN_TIME_DELTA_SECONDS = 2.0
RGBD_HERO_PATCH_SUPPLEMENT_TARGET_TIME_DELTA_SECONDS = 2.5
RGBD_HERO_PATCH_SUPPLEMENT_TARGET_TIME_DELTAS_SECONDS = (2.5, 4.5)
RGBD_HERO_PATCH_SUPPLEMENT_MAX_PREFERRED_TIME_DELTA_SECONDS = 3.25
RGBD_HERO_PATCH_SUPPLEMENT_SELECTION_WINDOW_SECONDS = 0.85
RGBD_HERO_PATCH_PRIMARY_SELECTION_WINDOW_SECONDS = 1.0
RGBD_HERO_PATCH_PRIMARY_OWNER_CONFIDENCE_MIN = 1
RGBD_HERO_PATCH_PRIMARY_OWNER_ABSOLUTE_TOLERANCE_METERS = 0.12
RGBD_HERO_PATCH_PRIMARY_OWNER_RELATIVE_TOLERANCE = 0.10
RGBD_HERO_PATCH_SECONDARY_CLAIM_RADIUS_PIXELS = 1
RGBD_HERO_PATCH_PRIMARY_OWNER_DEPTH_EDGE_ABSOLUTE_METERS = 0.18
RGBD_HERO_PATCH_PRIMARY_OWNER_DEPTH_EDGE_RELATIVE = 0.16
RGBD_HERO_PATCH_PRIMARY_OWNER_MIN_VALID_NEIGHBORS = 3
RGBD_ONBOARDING_WINDOW_SECONDS = 3.0
RGBD_ONBOARDING_TARGET_SAMPLES = 16_384
RGBD_ONBOARDING_FACE_ABSOLUTE_TOLERANCE_METERS = 0.18
RGBD_ONBOARDING_FACE_RELATIVE_TOLERANCE = 0.22
RGBD_ONBOARDING_LIDAR_SUPPORT_DISTANCE_METERS = 0.15
RGBD_ONBOARDING_LIDAR_DEPTH_SUPPORT_METERS = 0.15
RGBD_ONBOARDING_LIDAR_HARD_REJECT_DEPTH_METERS = 0.32
RGBD_ONBOARDING_LIDAR_HARD_REJECT_DISTANCE_METERS = 0.28
RGBD_ONBOARDING_MAX_FACE_EDGE_METERS = 0.75
RGBD_ONBOARDING_MIN_COMPONENT_FACES = 8
RGBD_ONBOARDING_MIN_COMPONENT_FACE_RATIO = 0.003
RGBD_ONBOARDING_LIDAR_PIXEL_BUCKETS = 96
RGBD_ONBOARDING_LIDAR_MAX_SUPPORT_CANDIDATES = 64


@dataclass(frozen=True)
class ProcessingProfile:
    name: str
    max_keyframes: int | None
    max_depth_frames: int | None
    max_rgbd_frames: int | None
    use_rgbd_geometry: bool
    write_vertex_colored_debug: bool
    write_texture_debug_preview: bool
    texture_render_target_faces: int
    texture_tsdf_render_target_faces: int
    planar_chart_raster_stride: int
    planar_chart_projection_mode: str = "blend"
    fallback_texture_face_limit: int | None = None
    single_frame_diagnostic: bool = False
    preserve_texture_render_mesh: bool = False
    densify_texture_render_mesh: bool = False
    dense_single_view_texture: bool = False
    rgbd_hero_patch_texture: bool = False
    rgbd_onboarding_mesh: bool = False


PROCESSING_PROFILES: dict[str, ProcessingProfile] = {
    "fast_onboarding": ProcessingProfile(
        name="fast_onboarding",
        max_keyframes=None,
        max_depth_frames=None,
        max_rgbd_frames=0,
        use_rgbd_geometry=False,
        write_vertex_colored_debug=False,
        write_texture_debug_preview=False,
        texture_render_target_faces=FAST_ONBOARDING_TEXTURE_RENDER_TARGET_FACE_COUNT,
        texture_tsdf_render_target_faces=FAST_ONBOARDING_TEXTURE_TSDF_RENDER_TARGET_FACE_COUNT,
        planar_chart_raster_stride=2,
        planar_chart_projection_mode="direct",
        fallback_texture_face_limit=None,
        preserve_texture_render_mesh=True,
        densify_texture_render_mesh=True,
        dense_single_view_texture=False,
        rgbd_hero_patch_texture=False,
        rgbd_onboarding_mesh=True,
    ),
    "full_quality": ProcessingProfile(
        name="full_quality",
        max_keyframes=None,
        max_depth_frames=None,
        max_rgbd_frames=36,
        use_rgbd_geometry=False,
        write_vertex_colored_debug=True,
        write_texture_debug_preview=True,
        texture_render_target_faces=TEXTURE_RENDER_TARGET_FACE_COUNT,
        texture_tsdf_render_target_faces=TEXTURE_TSDF_RENDER_TARGET_FACE_COUNT,
        planar_chart_raster_stride=1,
        planar_chart_projection_mode="direct",
        fallback_texture_face_limit=None,
    ),
    "rgbd_one_keyframe_diagnostic": ProcessingProfile(
        name="rgbd_one_keyframe_diagnostic",
        max_keyframes=None,
        max_depth_frames=None,
        max_rgbd_frames=1,
        use_rgbd_geometry=False,
        write_vertex_colored_debug=False,
        write_texture_debug_preview=False,
        texture_render_target_faces=FAST_ONBOARDING_TEXTURE_RENDER_TARGET_FACE_COUNT,
        texture_tsdf_render_target_faces=FAST_ONBOARDING_TEXTURE_TSDF_RENDER_TARGET_FACE_COUNT,
        planar_chart_raster_stride=2,
        planar_chart_projection_mode="direct",
        fallback_texture_face_limit=None,
        single_frame_diagnostic=True,
    ),
}

DEFAULT_PROCESSING_PROFILE = os.getenv("LIDARAI_DEFAULT_PROCESSING_PROFILE", "fast_onboarding")


class ValidationStage:
    name = JobStage.preprocessing

    async def run(self, job_dir: Path, report: StageReporter, is_cancelled: CancellationCheck) -> None:
        await report(self.name, 5, "Validating uploaded payload")
        payload_path = job_dir / "upload" / "scan_payload.json"
        raw = payload_path.read_text(encoding="utf-8")
        payload = ScanPayloadEnvelope.model_validate(json.loads(raw))
        profile = processing_profile_from_payload(payload)
        write_processing_profile(profile, job_dir / "work" / "processing_profile.json")
        mesh_count = len(payload.meshAnchors)
        keyframe_count = len(payload.images)
        depth_frame_count = len(payload.depthFrames or [])
        summary = {
            "schemaVersion": payload.schemaVersion,
            "createdAt": payload.createdAt.isoformat(),
            "processingProfile": processing_profile_stats(profile),
            "scanPurpose": payload.scanPurpose,
            "alignmentContext": payload.alignmentContext,
            "clientCaptureSelection": payload.captureSelection,
            "meshAnchorCount": mesh_count,
            "keyframeCount": keyframe_count,
            "depthFrameCount": depth_frame_count,
            "hasRoomPlan": payload.roomJSONBase64 is not None,
            "roomPlanAreaCount": len(payload.roomJSONBase64List) or (1 if payload.roomJSONBase64 else 0),
            "hasCapturedStructure": payload.structureJSONBase64 is not None,
            "roomPlanSegmentCount": len(payload.roomPlanSegments),
        }
        (job_dir / "work" / "payload_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
        (job_dir / "work" / "capture_data_validation.json").write_text(
            json.dumps(build_capture_data_validation(payload), indent=2),
            encoding="utf-8",
        )
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
        integrity_report = build_mesh_integrity_report(mesh)
        mesh.stats["integrity"] = {
            "bounds": integrity_report["bounds"],
            "surfaceAreaM2": integrity_report["surfaceAreaM2"],
            "topology": integrity_report["topology"],
            "connectedComponents": integrity_report["connectedComponents"],
            "validation": integrity_report["validation"],
        }
        mesh.stats["diagnosticArtifacts"] = write_geometry_diagnostic_artifacts(mesh, job_dir / "work")
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
        profile = read_processing_profile(job_dir / "work")
        mesh_path = job_dir / "work" / "arkit_fused_mesh.json"
        mesh = read_fused_mesh_json(mesh_path) if mesh_path.exists() else None
        selected_images, selection_stats = select_keyframes_for_profile(
            payload.get("images", []),
            profile,
            depth_frames=payload.get("depthFrames") or [],
            mesh=mesh,
        )
        keyframe_dir = job_dir / "work" / "keyframes"
        keyframe_dir.mkdir(parents=True, exist_ok=True)

        manifest = []
        for index, image in enumerate(selected_images, start=1):
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
                "originalImageResolution": image.get("originalImageResolution"),
                "imageOrientation": image.get("imageOrientation"),
                "intrinsicsReferenceResolution": image.get("intrinsicsReferenceResolution"),
                "trackingState": image.get("trackingState"),
                "trackingReason": image.get("trackingReason"),
                "exposureDurationSeconds": image.get("exposureDurationSeconds"),
                "iso": image.get("iso"),
                "ambientIntensity": image.get("ambientIntensity"),
                "ambientColorTemperature": image.get("ambientColorTemperature"),
                "sharpnessScore": image.get("sharpnessScore"),
                "path": f"keyframes/{filename}",
                "byteCount": len(image_bytes),
            })

        (job_dir / "work" / "keyframe_manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        (job_dir / "work" / "keyframe_selection.json").write_text(json.dumps({
            **selection_stats,
            "decodedKeyframeCount": len(manifest),
        }, indent=2), encoding="utf-8")
        await asyncio.sleep(0.1)
        await report(
            self.name,
            66,
            f"Decoded {len(manifest)} / {selection_stats['originalKeyframeCount']} keyframes for {profile.name}",
        )


class DepthFrameDecodeStage:
    name = JobStage.preprocessing

    async def run(self, job_dir: Path, report: StageReporter, is_cancelled: CancellationCheck) -> None:
        await report(self.name, 68, "Decoding compact LiDAR depth frames")
        payload = json.loads((job_dir / "upload" / "scan_payload.json").read_text(encoding="utf-8"))
        profile = read_processing_profile(job_dir / "work")
        keyframes = json.loads((job_dir / "work" / "keyframe_manifest.json").read_text(encoding="utf-8"))
        selected_depth_frames, selection_stats = select_depth_frames_for_profile(
            payload.get("depthFrames") or [],
            keyframes,
            profile,
        )
        depth_dir = job_dir / "work" / "depth_frames"
        depth_dir.mkdir(parents=True, exist_ok=True)

        manifest = []
        for index, depth_frame in enumerate(selected_depth_frames, start=1):
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
        (job_dir / "work" / "depth_frame_selection.json").write_text(json.dumps({
            **selection_stats,
            "decodedDepthFrameCount": len(manifest),
        }, indent=2), encoding="utf-8")
        await report(
            self.name,
            70,
            f"Decoded {len(manifest)} / {selection_stats['originalDepthFrameCount']} depth frames for {profile.name}",
        )


class RGBDSingleFrameDiagnosticStage:
    name = JobStage.texturing

    async def run(self, job_dir: Path, report: StageReporter, is_cancelled: CancellationCheck) -> None:
        profile = read_processing_profile(job_dir / "work")
        if not profile.single_frame_diagnostic:
            return

        await report(self.name, 71, "Writing one-keyframe RGB-D diagnostic artifacts")
        keyframes = json.loads((job_dir / "work" / "keyframe_manifest.json").read_text(encoding="utf-8"))
        depth_frames = json.loads((job_dir / "work" / "depth_frame_manifest.json").read_text(encoding="utf-8"))
        stats = write_rgbd_single_frame_artifacts(
            keyframes=keyframes,
            depth_frames=depth_frames,
            work_dir=job_dir / "work",
        )
        if is_cancelled():
            raise asyncio.CancelledError

        message = (
            f"RGB-D diagnostic selected {stats.get('selectedDepthFrameId')}"
            if stats.get("available")
            else f"RGB-D diagnostic unavailable: {', '.join(stats.get('warnings') or ['no paired frame'])}"
        )
        await report(self.name, 74, message)


class RGBDGeometryFusionStage:
    name = JobStage.meshing

    async def run(self, job_dir: Path, report: StageReporter, is_cancelled: CancellationCheck) -> None:
        profile = read_processing_profile(job_dir / "work")
        await report(self.name, 72, f"Trying RGBD TSDF fusion for {profile.name}")
        keyframes = json.loads((job_dir / "work" / "keyframe_manifest.json").read_text(encoding="utf-8"))
        depth_frames = json.loads((job_dir / "work" / "depth_frame_manifest.json").read_text(encoding="utf-8"))
        stats_path = job_dir / "work" / "rgbd_fusion_stats.json"

        arkit_mesh = read_fused_mesh_json(job_dir / "work" / "arkit_fused_mesh.json")
        if profile.single_frame_diagnostic:
            stats_path.write_text(json.dumps({
                "available": bool(depth_frames),
                "used": False,
                "reason": "Full multi-frame RGB-D fusion skipped by rgbd_one_keyframe_diagnostic profile.",
                "depthFrameCount": len(depth_frames),
                "keyframeCount": len(keyframes),
                "geometrySource": "single_frame_rgbd_diagnostic",
                "profile": processing_profile_stats(profile),
            }, indent=2), encoding="utf-8")
            await report(self.name, 76, "Full RGB-D fusion skipped for one-keyframe diagnostic")
            return

        if not profile.use_rgbd_geometry and arkit_mesh.faces:
            fused_mesh = read_fused_mesh_json(job_dir / "work" / "fused_mesh.json")
            geometry_preservation = mesh_geometry_preservation_stats(arkit_mesh, fused_mesh)
            fused_mesh.stats.update({
                "geometrySource": "arkit_mesh_anchor_fusion",
                "geometryPreserved": geometry_preservation["geometryPreserved"],
                "geometryPreservation": geometry_preservation,
            })
            write_fused_mesh_json(fused_mesh, job_dir / "work" / "fused_mesh.json")
            write_obj(fused_mesh, job_dir / "work" / "fused_mesh.obj")
            (job_dir / "work" / "mesh_stats.json").write_text(json.dumps(fused_mesh.stats, indent=2), encoding="utf-8")
            stats_path.write_text(json.dumps({
                "available": bool(depth_frames),
                "used": False,
                "reason": f"RGBD geometry skipped by {profile.name}; using ARKit mesh as authoritative geometry.",
                "depthFrameCount": len(depth_frames),
                "keyframeCount": len(keyframes),
                "geometrySource": "arkit_mesh_anchor_fusion",
                "geometryPreserved": geometry_preservation["geometryPreserved"],
                "geometryPreservation": geometry_preservation,
                "profile": processing_profile_stats(profile),
            }, indent=2), encoding="utf-8")
            await report(self.name, 76, f"RGBD fusion skipped for {profile.name}")
            return

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
                profile=profile,
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
                    profile=profile,
                )
            except RGBDFusionUnavailable as fallback_exc:
                stats = {
                    "available": False,
                    "used": False,
                    "reason": str(fallback_exc),
                    "tsdfUnavailableReason": str(tsdf_exc),
                    "depthFrameCount": len(depth_frames),
                    "geometrySource": "arkit_mesh_anchor_fusion",
                    "profile": processing_profile_stats(profile),
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
        profile = read_processing_profile(job_dir / "work")
        if profile.single_frame_diagnostic:
            return

        if profile.rgbd_onboarding_mesh:
            await report(self.name, 72, f"Preparing single-keyframe RGB-D onboarding mesh for {profile.name}")
        else:
            await report(self.name, 72, f"Projecting keyframes into a texture atlas for {profile.name}")
        keyframes = json.loads((job_dir / "work" / "keyframe_manifest.json").read_text(encoding="utf-8"))
        depth_manifest_path = job_dir / "work" / "depth_frame_manifest.json"
        depth_frames = json.loads(depth_manifest_path.read_text(encoding="utf-8")) if depth_manifest_path.exists() else []
        mesh = read_fused_mesh_json(job_dir / "work" / "fused_mesh.json")
        if profile.rgbd_onboarding_mesh:
            await report(self.name, 74, "Building single-keyframe RGB-D onboarding mesh")
            onboarding_stats = write_single_keyframe_rgbd_onboarding_mesh(
                keyframes=keyframes,
                depth_frames=depth_frames,
                work_dir=job_dir / "work",
                arkit_mesh=mesh,
                output_obj_path=job_dir / "work" / "rgbd_onboarding_mesh.obj",
                output_mtl_path=job_dir / "work" / "rgbd_onboarding_mesh.mtl",
                output_texture_path=job_dir / "work" / "rgbd_onboarding_texture.png",
                output_debug_path=job_dir / "work" / "rgbd_onboarding_diagnostics.json",
                output_overlay_path=job_dir / "work" / "rgbd_onboarding_overlay.png",
                output_usdz_path=None,
                profile=profile,
            )
            if onboarding_stats.get("available"):
                await report(
                    self.name,
                    80,
                    (
                        "Built RGB-D onboarding mesh "
                        f"({onboarding_stats.get('prunedFaceCount', 0)} faces from "
                        f"{onboarding_stats.get('selectedKeyframeId')})"
                    ),
                )
            else:
                await report(
                    self.name,
                    80,
                    f"RGB-D onboarding mesh unavailable: {onboarding_stats.get('reason', 'no usable RGB-D frame')}",
                )
            return
        if not mesh.faces or not keyframes:
            await report(self.name, 72, "Texture projection skipped because mesh faces or keyframes are missing")
            return

        loaded_keyframes = load_projection_keyframes(
            keyframes,
            job_dir / "work" / "keyframes",
            depth_frames=depth_frames,
            work_dir=job_dir / "work",
        )
        if not loaded_keyframes:
            await report(self.name, 72, "Texture projection skipped because no keyframes decoded successfully")
            return

        if profile.rgbd_hero_patch_texture:
            try:
                await report(self.name, 80, "Building permissive RGB-D hero patch texture")
                textured_stats = write_rgbd_hero_patch_textured_obj(
                    keyframes=keyframes,
                    depth_frames=depth_frames,
                    loaded_keyframes=loaded_keyframes,
                    work_dir=job_dir / "work",
                    output_obj_path=job_dir / "work" / "textured_mesh.obj",
                    output_mtl_path=job_dir / "work" / "textured_mesh.mtl",
                    output_texture_path=job_dir / "work" / "textured_mesh_texture.png",
                    output_debug_path=job_dir / "work" / "texture_debug.json",
                    output_projection_overlay_dir=(
                        job_dir / "work"
                        if is_fast_rgbd_hero_patch_profile(profile)
                        else None
                    ),
                    profile=profile,
                )
                colored_stats = {
                    "available": False,
                    "vertexCount": len(mesh.vertices),
                    "faceCount": len(mesh.faces),
                    "coloredVertexCount": 0,
                    "coverage": 0,
                    "reason": f"Vertex-colored debug PLY skipped because {profile.name} emitted an RGB-D hero patch.",
                }
                (job_dir / "work" / "uv_map.json").write_text(json.dumps({
                    "strategy": textured_stats["uvStrategy"],
                    "atlasWidth": textured_stats["atlasWidth"],
                    "atlasHeight": textured_stats["atlasHeight"],
                    "tileSize": textured_stats["tileSize"],
                    "tilePadding": textured_stats["tilePadding"],
                    "uvCoordinateCount": textured_stats["uvCoordinateCount"],
                    "renderMesh": textured_stats["renderMesh"],
                    "atlasLayout": textured_stats.get("atlasLayout", {}),
                    "debugPath": "texture_debug.json",
                    "note": "Fast photoreal OBJ is a permissive RGB-D hero patch with direct source-image UVs. Raw LiDAR mesh artifacts remain available separately.",
                }, indent=2), encoding="utf-8")
                (job_dir / "work" / "texture_manifest.json").write_text(json.dumps({
                    "sourceKeyframes": len(keyframes),
                    "usableProjectionKeyframes": len(loaded_keyframes),
                    "processingProfile": processing_profile_stats(profile),
                    "debugVertexColorPreview": {
                        "format": "ply",
                        "path": "colored_mesh.ply",
                        "available": False,
                        "coloredVertexCount": colored_stats["coloredVertexCount"],
                        "coverage": colored_stats["coverage"],
                        "reason": colored_stats.get("reason"),
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
                        "previewPath": None,
                        "stats": textured_stats["diagnostics"],
                    },
                    "usdz": {
                        "path": None,
                        "available": False,
                        "reason": "USDZ export was not requested.",
                    },
                    "glb": {
                        "available": False,
                        "path": None,
                        "reason": "GLB export is not supported by the current iOS viewer.",
                    },
                }, indent=2), encoding="utf-8")
                await report(
                    self.name,
                    94,
                    (
                        "Textured RGB-D hero patch "
                        f"({textured_stats['faceCount']} faces, direct source-image UVs)"
                    ),
                )
                return
            except RGBDFusionUnavailable as exc:
                logger.warning("RGB-D hero patch texture unavailable; falling back to LiDAR atlas: %s", exc)
                await report(self.name, 80, f"RGB-D hero patch unavailable; falling back to LiDAR atlas: {exc}")

        if profile.write_vertex_colored_debug:
            colored_stats = await write_vertex_colored_ply(
                vertices=mesh.vertices,
                faces=mesh.faces,
                keyframes=loaded_keyframes,
                output_path=job_dir / "work" / "colored_mesh.ply",
                report_progress=lambda progress, message: report(self.name, progress, message),
                is_cancelled=is_cancelled,
            )
        else:
            colored_stats = {
                "available": False,
                "vertexCount": len(mesh.vertices),
                "faceCount": len(mesh.faces),
                "coloredVertexCount": 0,
                "coverage": 0,
                "reason": f"Vertex-colored debug PLY skipped by {profile.name}.",
            }
        texture_mesh = make_texture_render_mesh(mesh, profile=profile)
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
            output_debug_preview_path=(
                job_dir / "work" / "texture_debug_preview.png"
                if profile.write_texture_debug_preview
                else None
            ),
            output_projection_overlay_dir=job_dir / "work",
            output_textured_usdz_path=job_dir / "work" / "textured_mesh.usdz",
            output_textured_glb_path=job_dir / "work" / "textured_mesh.glb",
            output_uv_checker_glb_path=job_dir / "work" / "uv_checker.glb",
            output_coverage_debug_glb_path=job_dir / "work" / "coverage_debug.glb",
            output_coverage_debug_report_path=job_dir / "work" / "coverage_debug_report.json",
            report_progress=lambda progress, message: report(self.name, progress, message),
            is_cancelled=is_cancelled,
            profile=profile,
        )

        (job_dir / "work" / "uv_map.json").write_text(json.dumps({
            "strategy": textured_stats["uvStrategy"],
            "atlasWidth": textured_stats["atlasWidth"],
            "atlasHeight": textured_stats["atlasHeight"],
            "tileSize": textured_stats["tileSize"],
            "tilePadding": textured_stats["tilePadding"],
            "uvCoordinateCount": textured_stats["uvCoordinateCount"],
            "renderMesh": render_mesh_stats,
            "atlasLayout": textured_stats.get("atlasLayout", {}),
            "debugPath": "texture_debug.json",
            "note": "Textured OBJ uses a display render mesh with larger per-face atlas islands, padding, and dilation. Raw fused mesh artifacts remain available separately.",
        }, indent=2), encoding="utf-8")
        (job_dir / "work" / "texture_manifest.json").write_text(json.dumps({
            "sourceKeyframes": len(keyframes),
            "usableProjectionKeyframes": len(loaded_keyframes),
            "processingProfile": processing_profile_stats(profile),
            "debugVertexColorPreview": {
                "format": "ply",
                "path": "colored_mesh.ply",
                "available": profile.write_vertex_colored_debug and (job_dir / "work" / "colored_mesh.ply").exists(),
                "coloredVertexCount": colored_stats["coloredVertexCount"],
                "coverage": colored_stats["coverage"],
                "reason": colored_stats.get("reason"),
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
                "previewPath": "texture_debug_preview.png" if profile.write_texture_debug_preview else None,
                "stats": textured_stats["diagnostics"],
            },
            "usdz": {
                **textured_stats["usdz"],
                "role": "photoreal_textured_mesh",
            },
            "glb": {
                **textured_stats["glb"],
                "role": "photoreal_textured_mesh",
            },
            "diagnosticGlbs": textured_stats["diagnosticGlbs"],
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
        texture_manifest = json.loads((work_dir / "texture_manifest.json").read_text(encoding="utf-8")) if (work_dir / "texture_manifest.json").exists() else {
            "debugVertexColorPreview": {
                "available": False,
                "reason": "TexturedMeshStage disabled for raw relocalization testing.",
            },
            "texturedMesh": {
                "available": False,
                "reason": "TexturedMeshStage disabled for raw relocalization testing.",
            },
            "textureDebug": {
                "previewPath": None,
                "stats": {},
            },
            "usdz": {
                "available": False,
                "reason": "TexturedMeshStage disabled for raw relocalization testing.",
            },
            "glb": {
                "available": False,
                "reason": "TexturedMeshStage disabled for raw relocalization testing.",
            },
            "diagnosticGlbs": {},
        }
        processing_profile = json.loads((work_dir / "processing_profile.json").read_text(encoding="utf-8")) if (work_dir / "processing_profile.json").exists() else {
            "name": "full_quality",
        }
        keyframe_selection = json.loads((work_dir / "keyframe_selection.json").read_text(encoding="utf-8")) if (work_dir / "keyframe_selection.json").exists() else {}
        depth_frame_selection = json.loads((work_dir / "depth_frame_selection.json").read_text(encoding="utf-8")) if (work_dir / "depth_frame_selection.json").exists() else {}

        for filename in [
            "fused_mesh.obj",
            "arkit_fused_mesh.obj",
            "rgbd_fused_mesh.obj",
            "rgbd_single_frame_points.ply",
            "rgbd_single_frame_mesh.obj",
            "rgbd_single_frame_overlay.png",
            "rgbd_single_frame_depth.png",
            "rgbd_single_frame_confidence.png",
            "rgbd_single_frame_diagnostics.json",
            "rgbd_onboarding_mesh.obj",
            "rgbd_onboarding_mesh.mtl",
            "rgbd_onboarding_texture.png",
            "rgbd_onboarding_overlay.png",
            "rgbd_onboarding_diagnostics.json",
            "colored_mesh.ply",
            "textured_mesh.obj",
            "textured_mesh.mtl",
            "textured_mesh_texture.png",
            "textured_mesh.usdz",
            "textured_mesh.glb",
            "geometry_only.glb",
            "geometry_culled.glb",
            "uv_checker.glb",
            "coverage_debug.glb",
            "texture_debug.json",
            "texture_debug_preview.png",
            "mesh_integrity_report.json",
            "coverage_debug_report.json",
            "two_keyframe_projection_0.png",
            "two_keyframe_projection_1.png",
            "stage_timings.json",
            "keyframe_selection.json",
            "depth_frame_selection.json",
            "processing_profile.json",
            "capture_data_validation.json",
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
        rgbd_diagnostic = json.loads((work_dir / "rgbd_single_frame_diagnostics.json").read_text(encoding="utf-8")) if (work_dir / "rgbd_single_frame_diagnostics.json").exists() else {
            "available": False,
            "reason": "RGB-D single-frame diagnostic did not run.",
        }
        rgbd_onboarding_diagnostic = json.loads((work_dir / "rgbd_onboarding_diagnostics.json").read_text(encoding="utf-8")) if (work_dir / "rgbd_onboarding_diagnostics.json").exists() else {
            "available": False,
            "reason": "RGB-D onboarding mesh did not run.",
        }
        rgbd_onboarding_available = (
            bool(rgbd_onboarding_diagnostic.get("available"))
            and (result_dir / "rgbd_onboarding_mesh.obj").exists()
        )
        if processing_profile.get("name") == "fast_onboarding" and rgbd_onboarding_available:
            preferred_photoreal = "rgbd_onboarding_mesh"
        elif (result_dir / "textured_mesh.usdz").exists():
            preferred_photoreal = "usdz"
        elif (result_dir / "textured_mesh.obj").exists():
            preferred_photoreal = "textured_obj"
        elif (result_dir / "rgbd_single_frame_mesh.obj").exists():
            preferred_photoreal = "rgbd_single_frame_mesh"
        else:
            preferred_photoreal = "vertex_colored_ply"
        preferred_preview = (
            "rgbd_onboarding_mesh.obj"
            if preferred_photoreal == "rgbd_onboarding_mesh"
            else "textured_mesh.usdz"
            if preferred_photoreal == "usdz"
            else "textured_mesh.obj"
            if preferred_photoreal == "textured_obj"
            else "rgbd_single_frame_mesh.obj"
            if preferred_photoreal == "rgbd_single_frame_mesh"
            else "rgbd_fused_mesh.obj"
            if preferred_photoreal == "rgbd_fused_mesh"
            else "colored_mesh.ply"
            if preferred_photoreal == "vertex_colored_ply"
            else "fused_mesh.obj"
        )
        coordinate_transforms = {
            "convention": "column_major_4x4",
            "sourceCoordinateSpace": "arkit_world",
            "modelCoordinateSpace": "processed_model",
            "modelFromARKitWorld": [
                1.0, 0.0, 0.0, 0.0,
                0.0, 1.0, 0.0, 0.0,
                0.0, 0.0, 1.0, 0.0,
                0.0, 0.0, 0.0, 1.0,
            ],
            "arkitWorldFromModel": [
                1.0, 0.0, 0.0, 0.0,
                0.0, 1.0, 0.0, 0.0,
                0.0, 0.0, 1.0, 0.0,
                0.0, 0.0, 0.0, 1.0,
            ],
            "note": "Current fused and textured artifacts preserve ARKit world coordinates. Non-identity transforms can be emitted here if future processing recenters or rescales models.",
        }
        artifact_manifest = {
            "version": "v1",
            "preferredPhotorealArtifact": preferred_photoreal,
            "preferredPreview": preferred_preview,
            "processingProfile": processing_profile,
            "captureSelection": {
                "client": summary.get("clientCaptureSelection"),
                "backendKeyframes": keyframe_selection,
                "backendDepthFrames": depth_frame_selection,
            },
            "coordinateTransforms": coordinate_transforms,
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
                "meshIntegrityReport": {
                    "role": "mesh_integrity_report",
                    "format": "json",
                    "path": "mesh_integrity_report.json",
                    "available": (result_dir / "mesh_integrity_report.json").exists(),
                },
                "geometryOnlyGlb": {
                    "role": "complete_arkit_geometry_no_texture",
                    "format": "glb",
                    "path": "geometry_only.glb",
                    "available": (result_dir / "geometry_only.glb").exists(),
                    "stats": mesh_stats.get("diagnosticArtifacts", {}).get("geometryOnlyGlb", {}),
                },
                "geometryCulledGlb": {
                    "role": "complete_arkit_geometry_backface_culling_check",
                    "format": "glb",
                    "path": "geometry_culled.glb",
                    "available": (result_dir / "geometry_culled.glb").exists(),
                    "stats": mesh_stats.get("diagnosticArtifacts", {}).get("geometryCulledGlb", {}),
                },
                "rgbdFusedMesh": {
                    "role": "rgbd_tsdf_fused_mesh",
                    "format": "obj",
                    "path": "rgbd_fused_mesh.obj",
                    "stats": rgbd_stats,
                    "available": (result_dir / "rgbd_fused_mesh.obj").exists(),
                },
                "rgbdSingleFrameDiagnostic": {
                    "role": "single_frame_rgbd_alignment_diagnostic",
                    "format": "mixed",
                    "pointsPath": "rgbd_single_frame_points.ply",
                    "meshPath": "rgbd_single_frame_mesh.obj",
                    "overlayPath": "rgbd_single_frame_overlay.png",
                    "depthPath": "rgbd_single_frame_depth.png",
                    "confidencePath": "rgbd_single_frame_confidence.png",
                    "diagnosticsPath": "rgbd_single_frame_diagnostics.json",
                    "available": bool(rgbd_diagnostic.get("available")),
                    "stats": rgbd_diagnostic,
                },
                "rgbdOnboardingMesh": {
                    "role": "single_keyframe_rgbd_onboarding_photoreal_mesh",
                    "format": "obj",
                    "objPath": "rgbd_onboarding_mesh.obj",
                    "mtlPath": "rgbd_onboarding_mesh.mtl",
                    "texturePath": "rgbd_onboarding_texture.png",
                    "overlayPath": "rgbd_onboarding_overlay.png",
                    "usdzPath": rgbd_onboarding_diagnostic.get("artifacts", {}).get("usdz", {}).get("path"),
                    "diagnosticsPath": "rgbd_onboarding_diagnostics.json",
                    "available": rgbd_onboarding_available,
                    "preferred": preferred_photoreal == "rgbd_onboarding_mesh",
                    "stats": rgbd_onboarding_diagnostic,
                },
                "vertexColoredPlyDebugPreview": {
                    "role": "vertex_colored_debug_preview",
                    "format": "ply",
                    "path": "colored_mesh.ply",
                    "available": (result_dir / "colored_mesh.ply").exists(),
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
                    "previewPath": texture_manifest.get("textureDebug", {}).get("previewPath"),
                    "available": (result_dir / "texture_debug.json").exists(),
                    "previewAvailable": (result_dir / "texture_debug_preview.png").exists(),
                    "stats": texture_manifest.get("textureDebug", {}).get("stats", {}),
                },
                "uvCheckerGlb": {
                    "role": "uv_checker_complete_texture_mesh",
                    "format": "glb",
                    "path": "uv_checker.glb",
                    "available": (result_dir / "uv_checker.glb").exists(),
                    "stats": texture_manifest.get("diagnosticGlbs", {}).get("uvCheckerGlb", {}),
                },
                "coverageDebugGlb": {
                    "role": "texture_coverage_debug_mesh",
                    "format": "glb",
                    "path": "coverage_debug.glb",
                    "reportPath": "coverage_debug_report.json",
                    "available": (result_dir / "coverage_debug.glb").exists(),
                    "reportAvailable": (result_dir / "coverage_debug_report.json").exists(),
                    "stats": texture_manifest.get("diagnosticGlbs", {}).get("coverageDebugGlb", {}),
                },
                "stageTimings": {
                    "role": "processing_timing_diagnostics",
                    "format": "json",
                    "path": "stage_timings.json",
                    "available": (result_dir / "stage_timings.json").exists(),
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
                "rgbd_single_frame_points.ply",
                "rgbd_single_frame_mesh.obj",
                "rgbd_single_frame_overlay.png",
                "rgbd_single_frame_depth.png",
                "rgbd_single_frame_confidence.png",
                "rgbd_single_frame_diagnostics.json",
                "rgbd_onboarding_mesh.obj",
                "rgbd_onboarding_mesh.mtl",
                "rgbd_onboarding_texture.png",
                "rgbd_onboarding_overlay.png",
                "rgbd_onboarding_diagnostics.json",
                "colored_mesh.ply",
                "textured_mesh.obj",
                "textured_mesh.mtl",
                "textured_mesh_texture.png",
                "textured_mesh.usdz",
                "textured_mesh.glb",
                "geometry_only.glb",
                "geometry_culled.glb",
                "uv_checker.glb",
                "coverage_debug.glb",
                "texture_debug.json",
                "texture_debug_preview.png",
                "mesh_integrity_report.json",
                "coverage_debug_report.json",
                "two_keyframe_projection_0.png",
                "two_keyframe_projection_1.png",
                "keyframe_manifest.json",
                "keyframe_selection.json",
                "depth_frame_selection.json",
                "processing_profile.json",
                "capture_data_validation.json",
                "stage_timings.json",
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
                "meshIntegrityReport": artifact_manifest["artifacts"]["meshIntegrityReport"],
                "geometryOnlyGlb": artifact_manifest["artifacts"]["geometryOnlyGlb"],
                "geometryCulledGlb": artifact_manifest["artifacts"]["geometryCulledGlb"],
                "rgbdFusedMesh": artifact_manifest["artifacts"]["rgbdFusedMesh"],
                "rgbdSingleFrameDiagnostic": artifact_manifest["artifacts"]["rgbdSingleFrameDiagnostic"],
                "rgbdOnboardingMesh": artifact_manifest["artifacts"]["rgbdOnboardingMesh"],
            },
            "debugPreview": artifact_manifest["artifacts"]["vertexColoredPlyDebugPreview"],
            "photoreal": {
                "preferredArtifact": preferred_photoreal,
                "rgbdOnboardingMesh": artifact_manifest["artifacts"]["rgbdOnboardingMesh"],
                "texturedObj": artifact_manifest["artifacts"]["texturedObj"],
                "textureDebug": artifact_manifest["artifacts"]["textureDebug"],
                "uvCheckerGlb": artifact_manifest["artifacts"]["uvCheckerGlb"],
                "coverageDebugGlb": artifact_manifest["artifacts"]["coverageDebugGlb"],
                "usdz": artifact_manifest["artifacts"]["usdz"],
                "glb": artifact_manifest["artifacts"]["glb"],
            },
            "processingProfile": processing_profile,
            "captureSelection": artifact_manifest["captureSelection"],
            "coordinateTransforms": coordinate_transforms,
            "keyframes": keyframes,
            "preferredPreview": preferred_preview,
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
    RGBDSingleFrameDiagnosticStage(),
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
        keyframe = keyframe_by_id.get(str(color_id)) if color_id else None
        if not keyframe:
            keyframe = closest_keyframe(depth_frame, keyframes)
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


def write_rgbd_single_frame_artifacts(
    keyframes: list[dict],
    depth_frames: list[dict],
    work_dir: Path,
) -> dict:
    output_path = work_dir / "rgbd_single_frame_diagnostics.json"
    warnings: list[str] = []
    paired_frames = pair_rgbd_frames(keyframes, depth_frames, work_dir)
    if not paired_frames:
        stats = unavailable_rgbd_diagnostic_stats(
            keyframes=keyframes,
            depth_frames=depth_frames,
            paired_count=0,
            warnings=["No RGB/depth frame pair could be matched by colorKeyframeId or timestamp."],
        )
        output_path.write_text(json.dumps(stats, indent=2), encoding="utf-8")
        return stats

    timestamps = [
        float(frame[1].get("timestamp") or frame[0].get("timestamp") or 0.0)
        for frame in paired_frames
    ]
    scan_midpoint = (min(timestamps) + max(timestamps)) / 2 if timestamps else 0.0
    scan_span = max(max(timestamps) - min(timestamps), 1e-6) if timestamps else 1.0

    candidates = []
    for index, pair in enumerate(paired_frames):
        try:
            candidates.append(rgbd_diagnostic_candidate_metrics(
                index=index,
                pair=pair,
                work_dir=work_dir,
                scan_midpoint=scan_midpoint,
                scan_span=scan_span,
            ))
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"Skipped RGB-D diagnostic candidate {index + 1}: {exc}")

    if not candidates:
        stats = unavailable_rgbd_diagnostic_stats(
            keyframes=keyframes,
            depth_frames=depth_frames,
            paired_count=len(paired_frames),
            warnings=warnings or ["No RGB/depth candidate had valid depth bytes, transforms, and intrinsics."],
        )
        output_path.write_text(json.dumps(stats, indent=2), encoding="utf-8")
        return stats

    candidates.sort(key=lambda item: item["score"], reverse=True)
    selected = candidates[0]
    keyframe, depth_frame, color_path, depth_path = selected["pair"]
    width, height = [int(value) for value in depth_frame["depthResolution"]]
    rgb_image = Image.open(color_path).convert("RGB")
    depth_values = read_float32_depth_values(depth_path, width, height)
    confidence_values = read_confidence_values(depth_frame, work_dir, width, height)
    projection_keyframe = diagnostic_projection_keyframe(keyframe, rgb_image)

    samples = collect_rgbd_diagnostic_samples(
        keyframe=keyframe,
        depth_frame=depth_frame,
        projection_keyframe=projection_keyframe,
        rgb_image=rgb_image,
        depth_values=depth_values,
        confidence_values=confidence_values,
        max_samples=RGBD_DIAGNOSTIC_MAX_POINT_SAMPLES,
    )
    point_stats = write_rgbd_colored_point_cloud_ply(
        samples["points"],
        work_dir / "rgbd_single_frame_points.ply",
    )
    mesh_stats = write_rgbd_single_frame_mesh_obj(
        keyframe=keyframe,
        depth_frame=depth_frame,
        projection_keyframe=projection_keyframe,
        rgb_image=rgb_image,
        depth_values=depth_values,
        confidence_values=confidence_values,
        output_path=work_dir / "rgbd_single_frame_mesh.obj",
    )
    overlay_stats = write_rgbd_overlay_png(
        rgb_image=rgb_image,
        samples=samples["points"],
        output_path=work_dir / "rgbd_single_frame_overlay.png",
    )
    depth_viz_stats = write_depth_visualization_png(
        depth_values=depth_values,
        width=width,
        height=height,
        output_path=work_dir / "rgbd_single_frame_depth.png",
    )
    confidence_viz_stats = write_confidence_visualization_png(
        confidence_values=confidence_values,
        width=width,
        height=height,
        output_path=work_dir / "rgbd_single_frame_confidence.png",
    )

    warnings.extend(selected.get("warnings", []))
    if selected["validDepthRatio"] < 0.05:
        warnings.append("Selected depth frame has very low valid-depth coverage.")
    if confidence_values is not None and selected["highConfidenceRatio"] < 0.05:
        warnings.append("Selected depth frame has very low high-confidence coverage.")
    if samples["reprojection"]["inBoundsRatio"] < 0.65:
        warnings.append("Many backprojected depth samples reproject outside the paired RGB image.")
    if samples["reprojection"].get("medianExpectedPixelError") is not None and samples["reprojection"]["medianExpectedPixelError"] > 2.0:
        warnings.append("Median depth-to-RGB reprojection error is above two pixels; check intrinsics scaling and image orientation.")

    timestamp_delta = timestamp_delta_seconds(depth_frame, keyframe)
    stats = {
        "available": True,
        "profile": "rgbd_one_keyframe_diagnostic",
        "strategy": "best_single_rgbd_pair_by_exact_pairing_depth_confidence_sharpness_and_mid_scan_tie_break",
        "candidateCount": len(candidates),
        "pairedFrameCount": len(paired_frames),
        "selectedCandidateRank": 1,
        "selectedKeyframeId": keyframe.get("id"),
        "selectedDepthFrameId": depth_frame.get("id"),
        "selectedColorKeyframeId": depth_frame.get("colorKeyframeId"),
        "colorKeyframeIdMatched": bool(
            depth_frame.get("colorKeyframeId")
            and keyframe.get("id")
            and str(depth_frame.get("colorKeyframeId")) == str(keyframe.get("id"))
        ),
        "timestamps": {
            "rgbTimestamp": keyframe.get("timestamp"),
            "depthTimestamp": depth_frame.get("timestamp"),
            "deltaSeconds": timestamp_delta,
        },
        "rgb": {
            "resolution": [rgb_image.width, rgb_image.height],
            "declaredResolution": keyframe.get("imageResolution"),
            "intrinsics": keyframe.get("intrinsics"),
            "sharpnessScore": selected.get("rgbSharpnessScore"),
        },
        "depth": {
            "resolution": [width, height],
            "intrinsics": depth_frame.get("intrinsics"),
            "format": depth_frame.get("depthFormat"),
            "metersPerUnit": depth_frame.get("metersPerUnit", 1),
            "validPixelCount": selected["validDepthCount"],
            "totalPixelCount": selected["totalDepthPixelCount"],
            "validDepthRatio": selected["validDepthRatio"],
            "minDepthMeters": selected.get("minDepthMeters"),
            "medianDepthMeters": selected.get("medianDepthMeters"),
            "maxDepthMeters": selected.get("maxDepthMeters"),
        },
        "confidence": {
            "available": confidence_values is not None,
            "format": depth_frame.get("confidenceFormat") if confidence_values is not None else None,
            "histogram": selected["confidenceHistogram"],
            "highConfidenceRatio": selected["highConfidenceRatio"],
        },
        "artifacts": {
            "pointsPly": point_stats,
            "meshObj": mesh_stats,
            "overlayPng": overlay_stats,
            "depthPng": depth_viz_stats,
            "confidencePng": confidence_viz_stats,
        },
        "reprojection": samples["reprojection"],
        "coordinateConvention": {
            "cameraTransform": "ARKit camera-to-world, column-major 4x4",
            "cameraForward": "ARKit camera looks down local -Z",
            "backprojection": "x=(u-cx)*depth/fx, y=(cy-v)*depth/fy, local=(x,y,-depth), world=cameraTransform*local",
            "projection": "worldToCamera=inverse(cameraTransform), depth=-z, u=fx*x/depth+cx, v=cy-fy*y/depth",
            "worldCoordinateSpace": "ARKit world meters",
        },
        "warnings": dedupe_preserve_order(warnings),
        "candidateSummary": [
            {
                key: candidate[key]
                for key in [
                    "keyframeId",
                    "depthFrameId",
                    "colorKeyframeIdMatched",
                    "timestampDeltaSeconds",
                    "validDepthRatio",
                    "highConfidenceRatio",
                    "rgbSharpnessScore",
                    "middleTieBreakScore",
                    "score",
                ]
            }
            for candidate in candidates[:8]
        ],
    }
    output_path.write_text(json.dumps(stats, indent=2), encoding="utf-8")
    return stats


def unavailable_rgbd_diagnostic_stats(
    *,
    keyframes: list[dict],
    depth_frames: list[dict],
    paired_count: int,
    warnings: list[str],
) -> dict:
    return {
        "available": False,
        "profile": "rgbd_one_keyframe_diagnostic",
        "keyframeCount": len(keyframes),
        "depthFrameCount": len(depth_frames),
        "pairedFrameCount": paired_count,
        "artifacts": {
            "pointsPly": {"path": "rgbd_single_frame_points.ply", "available": False},
            "meshObj": {"path": "rgbd_single_frame_mesh.obj", "available": False},
            "overlayPng": {"path": "rgbd_single_frame_overlay.png", "available": False},
            "depthPng": {"path": "rgbd_single_frame_depth.png", "available": False},
            "confidencePng": {"path": "rgbd_single_frame_confidence.png", "available": False},
        },
        "coordinateConvention": {
            "cameraTransform": "ARKit camera-to-world, column-major 4x4",
            "cameraForward": "ARKit camera looks down local -Z",
        },
        "warnings": dedupe_preserve_order(warnings),
    }


def rgbd_diagnostic_candidate_metrics(
    *,
    index: int,
    pair: tuple[dict, dict, Path, Path],
    work_dir: Path,
    scan_midpoint: float,
    scan_span: float,
) -> dict:
    keyframe, depth_frame, color_path, depth_path = pair
    resolution = depth_frame.get("depthResolution") or []
    intrinsics = depth_frame.get("intrinsics") or []
    transform = depth_frame.get("cameraTransform") or []
    keyframe_intrinsics = keyframe.get("intrinsics") or []
    keyframe_transform = keyframe.get("cameraTransform") or []
    if len(resolution) != 2 or len(intrinsics) != 9 or len(transform) != 16:
        raise ValueError("depth frame is missing resolution, intrinsics, or camera transform")
    if len(keyframe_intrinsics) != 9 or len(keyframe_transform) != 16:
        raise ValueError("keyframe is missing intrinsics or camera transform")
    width, height = int(resolution[0]), int(resolution[1])
    if width <= 0 or height <= 0:
        raise ValueError("depth frame has invalid resolution")

    depth_values = read_float32_depth_values(depth_path, width, height)
    confidence_values = read_confidence_values(depth_frame, work_dir, width, height)
    valid_depths = [
        float(value)
        for value in depth_values
        if math.isfinite(float(value)) and 0 < float(value) <= RGBD_DEPTH_TRUNC_METERS
    ]
    total_pixels = width * height
    valid_ratio = len(valid_depths) / total_pixels if total_pixels else 0.0
    confidence_histogram = confidence_histogram_for_values(confidence_values)
    high_confidence_ratio = (
        confidence_histogram.get("2", 0) / total_pixels
        if confidence_values is not None and total_pixels
        else 0.0
    )
    color_match = bool(
        depth_frame.get("colorKeyframeId")
        and keyframe.get("id")
        and str(depth_frame.get("colorKeyframeId")) == str(keyframe.get("id"))
    )
    timestamp = float(depth_frame.get("timestamp") or keyframe.get("timestamp") or 0.0)
    middle_score = 1.0 - min(abs(timestamp - scan_midpoint) / (scan_span / 2), 1.0)
    timestamp_delta = timestamp_delta_seconds(depth_frame, keyframe)
    sharpness = rgb_sharpness_score(color_path)
    score = (
        (2.0 if color_match else 0.0)
        + valid_ratio * 6.0
        + high_confidence_ratio * 2.5
        + min(sharpness / 28.0, 1.0) * 0.75
        + middle_score * 0.35
        - min(timestamp_delta, 1.0) * 0.5
    )
    sorted_depths = sorted(valid_depths)
    warnings = []
    warnings.extend(intrinsics_resolution_warnings("rgb", keyframe.get("intrinsics") or [], keyframe.get("imageResolution") or []))
    warnings.extend(intrinsics_resolution_warnings("depth", intrinsics, [width, height]))
    return {
        "index": index,
        "pair": pair,
        "keyframeId": keyframe.get("id"),
        "depthFrameId": depth_frame.get("id"),
        "colorKeyframeIdMatched": color_match,
        "sourceTimestamp": timestamp,
        "timestampDeltaSeconds": timestamp_delta,
        "validDepthCount": len(valid_depths),
        "totalDepthPixelCount": total_pixels,
        "validDepthRatio": valid_ratio,
        "confidenceHistogram": confidence_histogram,
        "highConfidenceRatio": high_confidence_ratio,
        "rgbSharpnessScore": sharpness,
        "middleTieBreakScore": middle_score,
        "minDepthMeters": round(sorted_depths[0], 4) if sorted_depths else None,
        "medianDepthMeters": round(median_sorted(sorted_depths), 4) if sorted_depths else None,
        "maxDepthMeters": round(sorted_depths[-1], 4) if sorted_depths else None,
        "score": round(score, 6),
        "warnings": warnings,
    }


def diagnostic_projection_keyframe(keyframe: dict, image: Image.Image) -> ProjectionKeyframe:
    transform = keyframe.get("cameraTransform") or []
    intrinsics = keyframe.get("intrinsics") or []
    return ProjectionKeyframe(
        image=image,
        width=image.width,
        height=image.height,
        world_to_camera=invert_rigid_transform(transform),
        camera_position=(float(transform[12]), float(transform[13]), float(transform[14])),
        intrinsics=intrinsics,
        pixels=image.load(),
        id=str(keyframe.get("id")) if keyframe.get("id") else None,
        path=str(keyframe.get("path")) if keyframe.get("path") else None,
    )


def collect_rgbd_diagnostic_samples(
    *,
    keyframe: dict,
    depth_frame: dict,
    projection_keyframe: ProjectionKeyframe,
    rgb_image: Image.Image,
    depth_values: array,
    confidence_values: bytes | None,
    max_samples: int,
) -> dict:
    width, height = [int(value) for value in depth_frame["depthResolution"]]
    intrinsics = depth_frame.get("intrinsics") or []
    transform = depth_frame.get("cameraTransform") or []
    total_pixels = width * height
    sample_step = max(1, int(math.ceil(math.sqrt(total_pixels / max(max_samples, 1)))))
    rgb_pixels = rgb_image.load()
    points = []
    valid_sample_count = 0
    in_bounds_count = 0
    confidence_rejected_count = 0
    reprojection_errors = []

    for source_y in range(0, height, sample_step):
        for source_x in range(0, width, sample_step):
            source_index = source_y * width + source_x
            depth = float(depth_values[source_index])
            if not math.isfinite(depth) or depth <= 0 or depth > RGBD_DEPTH_TRUNC_METERS:
                continue
            confidence = int(confidence_values[source_index]) if confidence_values is not None else None
            if confidence == 0:
                confidence_rejected_count += 1
                continue

            valid_sample_count += 1
            world = backproject_depth_sample_to_world(
                source_x=source_x,
                source_y=source_y,
                depth=depth,
                intrinsics=intrinsics,
                camera_transform=transform,
            )
            projection = project_world_point(world, projection_keyframe)
            expected = expected_rgb_pixel_for_depth_sample(source_x, source_y, depth_frame, keyframe)
            if projection is None:
                continue

            u, v, projected_depth = projection
            in_bounds_count += 1
            if expected is not None:
                error = math.hypot(u - expected[0], v - expected[1])
                reprojection_errors.append(error)
            else:
                error = None

            color = rgb_pixels[
                max(0, min(int(round(u)), rgb_image.width - 1)),
                max(0, min(int(round(v)), rgb_image.height - 1)),
            ]
            points.append({
                "world": world,
                "color": (int(color[0]), int(color[1]), int(color[2])),
                "source": (source_x, source_y),
                "depth": depth,
                "confidence": confidence,
                "projection": (u, v, projected_depth),
                "expectedProjection": expected,
                "expectedPixelError": error,
            })

    errors_sorted = sorted(reprojection_errors)
    return {
        "points": points,
        "reprojection": {
            "sampleStep": sample_step,
            "validSampleCount": valid_sample_count,
            "confidenceRejectedSampleCount": confidence_rejected_count,
            "sampleCount": valid_sample_count + confidence_rejected_count,
            "inBoundsCount": in_bounds_count,
            "inBoundsRatio": in_bounds_count / valid_sample_count if valid_sample_count else 0.0,
            "medianExpectedPixelError": round(median_sorted(errors_sorted), 4) if errors_sorted else None,
            "p95ExpectedPixelError": round(percentile_sorted(errors_sorted, 0.95), 4) if errors_sorted else None,
        },
    }


def write_rgbd_colored_point_cloud_ply(samples: list[dict], output_path: Path) -> dict:
    lines = [
        "ply",
        "format ascii 1.0",
        "comment LidarAI one-keyframe RGB-D diagnostic point cloud",
        "comment Units are meters in ARKit world space",
        f"element vertex {len(samples)}",
        "property float x",
        "property float y",
        "property float z",
        "property uchar red",
        "property uchar green",
        "property uchar blue",
        "end_header",
    ]
    for sample in samples:
        x, y, z = sample["world"]
        r, g, b = sample["color"]
        lines.append(f"{x:.6f} {y:.6f} {z:.6f} {r} {g} {b}")
    output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {
        "path": output_path.name,
        "format": "ply",
        "available": output_path.exists(),
        "pointCount": len(samples),
    }


def write_rgbd_single_frame_mesh_obj(
    *,
    keyframe: dict,
    depth_frame: dict,
    projection_keyframe: ProjectionKeyframe,
    rgb_image: Image.Image,
    depth_values: array,
    confidence_values: bytes | None,
    output_path: Path,
) -> dict:
    width, height = [int(value) for value in depth_frame["depthResolution"]]
    intrinsics = depth_frame.get("intrinsics") or []
    transform = depth_frame.get("cameraTransform") or []
    total_pixels = width * height
    sample_step = max(1, int(math.ceil(math.sqrt(total_pixels / RGBD_DIAGNOSTIC_MESH_TARGET_SAMPLES))))
    x_samples = list(range(0, width, sample_step))
    y_samples = list(range(0, height, sample_step))
    rgb_pixels = rgb_image.load()
    vertices: list[tuple[float, float, float]] = []
    colors: list[tuple[int, int, int]] = []
    faces: list[tuple[int, int, int]] = []
    seen_faces: set[tuple[int, int, int]] = set()
    rejected_face_count = 0
    invalid_depth_count = 0
    out_of_bounds_color_count = 0
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
            projection = project_world_point(world, projection_keyframe)
            if projection is None:
                out_of_bounds_color_count += 1
                row.append(None)
                depth_row.append(0)
                continue

            u, v, _projected_depth = projection
            color = rgb_pixels[
                max(0, min(int(round(u)), rgb_image.width - 1)),
                max(0, min(int(round(v)), rgb_image.height - 1)),
            ]
            row.append(len(vertices))
            depth_row.append(depth)
            vertices.append(world)
            colors.append((int(color[0]), int(color[1]), int(color[2])))
        grid.append(row)
        depth_grid.append(depth_row)

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

    lines = [
        "# LidarAI one-keyframe RGB-D diagnostic mesh",
        "# Vertex RGB values are written with the common OBJ vertex-color extension",
        "# Units are meters in ARKit world space",
        "o rgbd_single_frame_mesh",
    ]
    for (x, y, z), (r, g, b) in zip(vertices, colors):
        lines.append(f"v {x:.6f} {y:.6f} {z:.6f} {r / 255:.6f} {g / 255:.6f} {b / 255:.6f}")
    for a, b, c in faces:
        lines.append(f"f {a + 1} {b + 1} {c + 1}")
    output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {
        "path": output_path.name,
        "format": "obj",
        "available": output_path.exists(),
        "vertexCount": len(vertices),
        "faceCount": len(faces),
        "sampleStep": sample_step,
        "invalidDepthSampleCount": invalid_depth_count,
        "outOfBoundsColorSampleCount": out_of_bounds_color_count,
        "rejectedFaceCount": rejected_face_count,
    }


def write_single_keyframe_rgbd_onboarding_mesh(
    *,
    keyframes: list[dict],
    depth_frames: list[dict],
    work_dir: Path,
    arkit_mesh: FusedMesh,
    output_obj_path: Path,
    output_mtl_path: Path,
    output_texture_path: Path,
    output_debug_path: Path,
    output_overlay_path: Path | None = None,
    output_usdz_path: Path | None = None,
    profile: ProcessingProfile | None = None,
) -> dict:
    paired_frames = pair_rgbd_frames(keyframes, depth_frames, work_dir)
    if not paired_frames:
        stats = unavailable_rgbd_onboarding_stats(
            keyframes=keyframes,
            depth_frames=depth_frames,
            paired_count=0,
            reason="No RGB/depth frame pair could be matched by colorKeyframeId or timestamp.",
            profile=profile,
        )
        output_debug_path.write_text(json.dumps(stats, indent=2), encoding="utf-8")
        return stats

    timestamps = [
        float(frame[1].get("timestamp") or frame[0].get("timestamp") or 0.0)
        for frame in paired_frames
    ]
    first_timestamp = min(timestamps) if timestamps else 0.0
    scan_midpoint = (min(timestamps) + max(timestamps)) / 2 if timestamps else 0.0
    scan_span = max(max(timestamps) - min(timestamps), 1e-6) if timestamps else 1.0

    warnings: list[str] = []
    candidates: list[dict] = []
    skipped_candidates: list[dict] = []
    for index, pair in enumerate(paired_frames):
        try:
            candidates.append(rgbd_onboarding_candidate_metrics(
                index=index,
                pair=pair,
                work_dir=work_dir,
                first_timestamp=first_timestamp,
                scan_midpoint=scan_midpoint,
                scan_span=scan_span,
            ))
        except Exception as exc:  # noqa: BLE001
            keyframe, depth_frame, _color_path, _depth_path = pair
            skipped_candidates.append({
                "index": index,
                "keyframeId": keyframe.get("id"),
                "depthFrameId": depth_frame.get("id"),
                "reason": str(exc),
            })

    if not candidates:
        stats = unavailable_rgbd_onboarding_stats(
            keyframes=keyframes,
            depth_frames=depth_frames,
            paired_count=len(paired_frames),
            reason="No RGB-D onboarding candidate had valid depth bytes, transforms, and intrinsics.",
            profile=profile,
        )
        stats["skippedCandidates"] = skipped_candidates
        output_debug_path.write_text(json.dumps(stats, indent=2), encoding="utf-8")
        return stats

    window_candidates = [
        candidate for candidate in candidates
        if candidate["withinOnboardingWindow"] and int(candidate.get("meshablePixelCount") or 0) >= 3
    ]
    if not window_candidates:
        stats = unavailable_rgbd_onboarding_stats(
            keyframes=keyframes,
            depth_frames=depth_frames,
            paired_count=len(paired_frames),
            reason="No RGB-D candidate in the first onboarding window had enough meshable depth pixels.",
            profile=profile,
        )
        stats.update({
            "firstTimestamp": first_timestamp,
            "onboardingWindowSeconds": RGBD_ONBOARDING_WINDOW_SECONDS,
            "candidateCount": len(candidates),
            "candidateSummary": [
                rgbd_onboarding_candidate_summary(candidate, selected=False)
                for candidate in sorted(candidates, key=lambda item: item["score"], reverse=True)
            ],
            "skippedCandidates": skipped_candidates,
        })
        output_debug_path.write_text(json.dumps(stats, indent=2), encoding="utf-8")
        return stats

    selected = max(
        window_candidates,
        key=lambda candidate: (
            float(candidate.get("score") or 0),
            float(candidate.get("validDepthRatio") or 0),
            float(candidate.get("highConfidenceRatio") or 0),
            -float(candidate.get("secondsFromCaptureStart") or 0),
        ),
    )
    keyframe, depth_frame, color_path, depth_path = selected["pair"]
    width, height = [int(value) for value in depth_frame["depthResolution"]]
    rgb_image = Image.open(color_path).convert("RGB")
    depth_values = read_float32_depth_values(depth_path, width, height)
    confidence_values = read_confidence_values(depth_frame, work_dir, width, height)
    projection_keyframe = diagnostic_projection_keyframe(keyframe, rgb_image)
    prepared_depth = prepare_rgbd_hero_patch_depth_grid(
        depth_values=depth_values,
        confidence_values=confidence_values,
        width=width,
        height=height,
    )

    mesh_result = build_single_keyframe_rgbd_onboarding_mesh(
        keyframe=keyframe,
        depth_frame=depth_frame,
        projection_keyframe=projection_keyframe,
        depth_values=prepared_depth["depthValues"],
        width=width,
        height=height,
    )
    raw_mesh = mesh_result["mesh"]
    raw_uv_coordinates = mesh_result["uvCoordinates"]
    if not raw_mesh.faces:
        stats = unavailable_rgbd_onboarding_stats(
            keyframes=keyframes,
            depth_frames=depth_frames,
            paired_count=len(paired_frames),
            reason="Selected RGB-D onboarding keyframe did not produce any connected faces.",
            profile=profile,
        )
        stats.update({
            "selectedKeyframeId": keyframe.get("id"),
            "selectedDepthFrameId": depth_frame.get("id"),
            "candidateSummary": [
                rgbd_onboarding_candidate_summary(candidate, selected=(candidate is selected))
                for candidate in sorted(candidates, key=lambda item: item["score"], reverse=True)
            ],
            "mesh": mesh_result["stats"],
        })
        output_debug_path.write_text(json.dumps(stats, indent=2), encoding="utf-8")
        return stats

    prune_result = prune_rgbd_onboarding_mesh_with_lidar(
        mesh=raw_mesh,
        uv_coordinates=raw_uv_coordinates,
        face_source_pixels=mesh_result["faceSourcePixels"],
        depth_frame=depth_frame,
        arkit_mesh=arkit_mesh,
    )
    final_mesh = prune_result["mesh"]
    final_uv_coordinates = prune_result["uvCoordinates"]
    if not final_mesh.faces:
        stats = unavailable_rgbd_onboarding_stats(
            keyframes=keyframes,
            depth_frames=depth_frames,
            paired_count=len(paired_frames),
            reason="RGB-D onboarding mesh was pruned to zero faces.",
            profile=profile,
        )
        stats.update({
            "selectedKeyframeId": keyframe.get("id"),
            "selectedDepthFrameId": depth_frame.get("id"),
            "rawVertexCount": len(raw_mesh.vertices),
            "rawFaceCount": len(raw_mesh.faces),
            "pruning": prune_result["stats"],
        })
        output_debug_path.write_text(json.dumps(stats, indent=2), encoding="utf-8")
        return stats

    output_texture_path.parent.mkdir(parents=True, exist_ok=True)
    rgb_image.save(output_texture_path)
    output_mtl_path.write_text("\n".join([
        "newmtl LidarAI_RGBD_Hero_Patch",
        "Ka 1.000000 1.000000 1.000000",
        "Kd 1.000000 1.000000 1.000000",
        "Ks 0.000000 0.000000 0.000000",
        "d 1.0",
        "illum 1",
        f"map_Kd {output_texture_path.name}",
        "",
    ]), encoding="utf-8")
    write_textured_mesh_obj_with_uvs(
        mesh=final_mesh,
        uv_coordinates=final_uv_coordinates,
        output_obj_path=output_obj_path,
        output_mtl_path=output_mtl_path,
        object_name="rgbd_onboarding_mesh",
    )

    overlay_stats = None
    if output_overlay_path is not None:
        samples = collect_rgbd_diagnostic_samples(
            keyframe=keyframe,
            depth_frame=depth_frame,
            projection_keyframe=projection_keyframe,
            rgb_image=rgb_image,
            depth_values=depth_values,
            confidence_values=confidence_values,
            max_samples=RGBD_DIAGNOSTIC_OVERLAY_MAX_SAMPLES,
        )
        overlay_stats = write_rgbd_overlay_png(
            rgb_image=rgb_image,
            samples=samples["points"],
            output_path=output_overlay_path,
        )

    face_uvs = face_uvs_from_vertex_uvs(final_mesh, final_uv_coordinates)
    usdz_stats = {
        "format": "usdz",
        "path": output_usdz_path.name if output_usdz_path is not None else None,
        "available": False,
        "reason": "USDZ export was not requested.",
    }
    if output_usdz_path is not None:
        usdz_stats = write_textured_usdz(
            final_mesh,
            output_usdz_path,
            face_uvs=face_uvs,
            texture_path=output_texture_path,
            name="rgbd_onboarding",
        )

    confidence_histogram = confidence_histogram_for_values(confidence_values)
    candidate_summary = [
        rgbd_onboarding_candidate_summary(candidate, selected=(candidate is selected))
        for candidate in sorted(candidates, key=lambda item: item["score"], reverse=True)
    ]
    stats = {
        "available": True,
        "profile": profile.name if profile is not None else "fast_onboarding",
        "strategy": "best_single_rgbd_keyframe_from_first_three_seconds_with_lidar_support_pruning",
        "preferredPreviewCandidate": True,
        "becamePreferredPreview": True,
        "pairedFrameCount": len(paired_frames),
        "candidateCount": len(window_candidates),
        "totalCandidateCount": len(candidates),
        "firstTimestamp": first_timestamp,
        "onboardingWindowSeconds": RGBD_ONBOARDING_WINDOW_SECONDS,
        "selectedCandidateRank": 1,
        "selectedKeyframeId": keyframe.get("id"),
        "selectedDepthFrameId": depth_frame.get("id"),
        "selectedColorKeyframeId": depth_frame.get("colorKeyframeId"),
        "selectedTimestamp": selected.get("sourceTimestamp"),
        "secondsFromCaptureStart": selected.get("secondsFromCaptureStart"),
        "selectedScore": selected.get("score"),
        "selectedScoreBreakdown": selected.get("scoreBreakdown"),
        "validDepthRatio": selected.get("validDepthRatio"),
        "centralCoverageRatio": selected.get("centralCoverageRatio"),
        "depthEdgeChaosRatio": selected.get("depthEdgeChaosRatio"),
        "confidenceHistogram": confidence_histogram,
        "highConfidenceRatio": selected.get("highConfidenceRatio"),
        "rawVertexCount": len(raw_mesh.vertices),
        "rawFaceCount": len(raw_mesh.faces),
        "prunedVertexCount": len(final_mesh.vertices),
        "prunedFaceCount": len(final_mesh.faces),
        "textureSize": [rgb_image.width, rgb_image.height],
        "sourceDepthResolution": [width, height],
        "depthPreparation": prepared_depth["stats"],
        "mesh": {
            **mesh_result["stats"],
            "rawVertexCount": len(raw_mesh.vertices),
            "rawFaceCount": len(raw_mesh.faces),
            "finalVertexCount": len(final_mesh.vertices),
            "finalFaceCount": len(final_mesh.faces),
        },
        "pruning": prune_result["stats"],
        "artifacts": {
            "obj": {
                "format": "obj",
                "path": output_obj_path.name,
                "available": output_obj_path.exists(),
                "vertexCount": len(final_mesh.vertices),
                "uvCoordinateCount": len(final_uv_coordinates),
                "faceCount": len(final_mesh.faces),
            },
            "mtl": {
                "format": "mtl",
                "path": output_mtl_path.name,
                "available": output_mtl_path.exists(),
            },
            "texture": {
                "format": "png",
                "path": output_texture_path.name,
                "available": output_texture_path.exists(),
                "width": rgb_image.width,
                "height": rgb_image.height,
            },
            "overlay": overlay_stats or {
                "format": "png",
                "path": output_overlay_path.name if output_overlay_path is not None else None,
                "available": False,
                "reason": "Overlay export was not requested.",
            },
            "usdz": usdz_stats,
            "diagnostics": {
                "format": "json",
                "path": output_debug_path.name,
                "available": True,
            },
        },
        "candidateSummary": candidate_summary,
        "skippedCandidates": skipped_candidates,
        "warnings": dedupe_preserve_order(warnings + selected.get("warnings", [])),
    }
    output_debug_path.write_text(json.dumps(stats, indent=2), encoding="utf-8")
    return stats


def unavailable_rgbd_onboarding_stats(
    *,
    keyframes: list[dict],
    depth_frames: list[dict],
    paired_count: int,
    reason: str,
    profile: ProcessingProfile | None = None,
) -> dict:
    return {
        "available": False,
        "profile": profile.name if profile is not None else "fast_onboarding",
        "strategy": "best_single_rgbd_keyframe_from_first_three_seconds_with_lidar_support_pruning",
        "reason": reason,
        "keyframeCount": len(keyframes),
        "depthFrameCount": len(depth_frames),
        "pairedFrameCount": paired_count,
        "onboardingWindowSeconds": RGBD_ONBOARDING_WINDOW_SECONDS,
        "becamePreferredPreview": False,
        "artifacts": {
            "obj": {"format": "obj", "path": "rgbd_onboarding_mesh.obj", "available": False},
            "mtl": {"format": "mtl", "path": "rgbd_onboarding_mesh.mtl", "available": False},
            "texture": {"format": "png", "path": "rgbd_onboarding_texture.png", "available": False},
            "overlay": {"format": "png", "path": "rgbd_onboarding_overlay.png", "available": False},
            "usdz": {"format": "usdz", "path": None, "available": False},
            "diagnostics": {"format": "json", "path": "rgbd_onboarding_diagnostics.json", "available": True},
        },
    }


def rgbd_onboarding_candidate_metrics(
    *,
    index: int,
    pair: tuple[dict, dict, Path, Path],
    work_dir: Path,
    first_timestamp: float,
    scan_midpoint: float,
    scan_span: float,
) -> dict:
    base = rgbd_diagnostic_candidate_metrics(
        index=index,
        pair=pair,
        work_dir=work_dir,
        scan_midpoint=scan_midpoint,
        scan_span=scan_span,
    )
    keyframe, depth_frame, color_path, depth_path = pair
    width, height = [int(value) for value in depth_frame["depthResolution"]]
    depth_values = read_float32_depth_values(depth_path, width, height)
    confidence_values = read_confidence_values(depth_frame, work_dir, width, height)
    color_quality = image_quality_stats_for_path(color_path)
    central_coverage = central_valid_depth_ratio(depth_values, confidence_values, width, height)
    edge_chaos = depth_edge_chaos_ratio(depth_values, width, height)
    meshable_pixels = sum(
        1
        for value in depth_values
        if math.isfinite(float(value)) and 0 < float(value) <= RGBD_DEPTH_TRUNC_METERS
    )
    timestamp = float(base.get("sourceTimestamp") or 0.0)
    seconds_from_start = max(0.0, timestamp - first_timestamp)
    within_window = seconds_from_start <= RGBD_ONBOARDING_WINDOW_SECONDS + 1e-6
    meshable_ratio = meshable_pixels / max(width * height, 1)
    valid_depth = float(base.get("validDepthRatio") or 0.0)
    high_confidence = float(base.get("highConfidenceRatio") or 0.0)
    sharpness = min(float(base.get("rgbSharpnessScore") or 0.0) / 28.0, 1.0)
    exposure = float(color_quality.get("exposureScore") or 0.0)
    non_overexposed = 1.0 - float(color_quality.get("overexposedRatio") or 0.0)
    non_underexposed = 1.0 - float(color_quality.get("underexposedRatio") or 0.0)
    chaos_score = 1.0 - edge_chaos
    central_score = central_coverage
    color_match_bonus = 0.5 if base.get("colorKeyframeIdMatched") else 0.0
    pose_score = tracking_pose_confidence(keyframe)
    score_breakdown = {
        "validDepth": round(valid_depth * 3.0, 6),
        "highConfidence": round(high_confidence * 1.2, 6),
        "centralCoverage": round(central_score * 1.35, 6),
        "meshablePixels": round(meshable_ratio * 1.0, 6),
        "nonOverexposedColor": round(non_overexposed * 0.45, 6),
        "nonUnderexposedColor": round(non_underexposed * 0.45, 6),
        "exposure": round(exposure * 0.45, 6),
        "sharpness": round(sharpness * 0.65, 6),
        "lowDepthEdgeChaos": round(chaos_score * 0.85, 6),
        "pose": round(pose_score * 0.25, 6),
        "colorDepthPairing": round(color_match_bonus, 6),
    }
    score = sum(score_breakdown.values())
    rejection_reasons = []
    if not within_window:
        rejection_reasons.append("outside_first_three_seconds")
    if meshable_pixels < 3:
        rejection_reasons.append("not_enough_meshable_pixels")
    if valid_depth < 0.02:
        rejection_reasons.append("very_low_valid_depth_ratio")
    if edge_chaos > 0.85:
        rejection_reasons.append("high_depth_edge_chaos")
    return {
        **base,
        "score": round(score, 6),
        "scoreBreakdown": score_breakdown,
        "secondsFromCaptureStart": round(seconds_from_start, 4),
        "withinOnboardingWindow": within_window,
        "colorQuality": color_quality,
        "nonOverexposedRatio": round(non_overexposed, 6),
        "nonUnderexposedRatio": round(non_underexposed, 6),
        "centralCoverageRatio": round(central_coverage, 6),
        "depthEdgeChaosRatio": round(edge_chaos, 6),
        "meshablePixelCount": meshable_pixels,
        "meshablePixelRatio": round(meshable_ratio, 6),
        "poseConfidence": round(pose_score, 4),
        "rejectionReasons": rejection_reasons,
    }


def rgbd_onboarding_candidate_summary(candidate: dict, *, selected: bool) -> dict:
    reasons = list(candidate.get("rejectionReasons") or [])
    if selected:
        reasons = ["selected"]
    elif not reasons:
        reasons = ["lower_score_than_selected_candidate"]
    return {
        "index": candidate.get("index"),
        "keyframeId": candidate.get("keyframeId"),
        "depthFrameId": candidate.get("depthFrameId"),
        "sourceTimestamp": candidate.get("sourceTimestamp"),
        "secondsFromCaptureStart": candidate.get("secondsFromCaptureStart"),
        "withinOnboardingWindow": candidate.get("withinOnboardingWindow"),
        "score": candidate.get("score"),
        "scoreBreakdown": candidate.get("scoreBreakdown"),
        "validDepthRatio": candidate.get("validDepthRatio"),
        "highConfidenceRatio": candidate.get("highConfidenceRatio"),
        "centralCoverageRatio": candidate.get("centralCoverageRatio"),
        "depthEdgeChaosRatio": candidate.get("depthEdgeChaosRatio"),
        "meshablePixelCount": candidate.get("meshablePixelCount"),
        "nonOverexposedRatio": candidate.get("nonOverexposedRatio"),
        "nonUnderexposedRatio": candidate.get("nonUnderexposedRatio"),
        "rgbSharpnessScore": candidate.get("rgbSharpnessScore"),
        "poseConfidence": candidate.get("poseConfidence"),
        "rejectionReasons": reasons,
    }


def image_quality_stats_for_path(path: Path, thumbnail_max: int = 112) -> dict:
    try:
        image = Image.open(path).convert("L")
        image.thumbnail((thumbnail_max, thumbnail_max), Image.Resampling.BILINEAR)
        width, height = image.size
        values = image.tobytes()
    except Exception:
        return {
            "available": False,
            "exposureScore": 0.55,
            "meanLuminance": None,
            "overexposedRatio": 0.0,
            "underexposedRatio": 0.0,
        }

    if not values:
        return {
            "available": False,
            "exposureScore": 0.55,
            "meanLuminance": None,
            "overexposedRatio": 0.0,
            "underexposedRatio": 0.0,
        }

    mean_luminance = sum(values) / len(values)
    overexposed_ratio = sum(1 for value in values if value >= 248) / len(values)
    underexposed_ratio = sum(1 for value in values if value <= 6) / len(values)
    exposure_score = clamp_float(
        1.0
        - (abs(mean_luminance - 128.0) / 170.0)
        - overexposed_ratio * 0.75
        - underexposed_ratio * 0.9,
        0.15,
        1.0,
    )
    return {
        "available": True,
        "exposureScore": round(exposure_score, 4),
        "meanLuminance": round(mean_luminance, 2),
        "overexposedRatio": round(overexposed_ratio, 4),
        "underexposedRatio": round(underexposed_ratio, 4),
        "sampledWidth": width,
        "sampledHeight": height,
    }


def central_valid_depth_ratio(
    depth_values: array,
    confidence_values: bytes | None,
    width: int,
    height: int,
) -> float:
    x0 = int(width * 0.2)
    x1 = max(x0 + 1, int(math.ceil(width * 0.8)))
    y0 = int(height * 0.2)
    y1 = max(y0 + 1, int(math.ceil(height * 0.8)))
    total = 0
    valid = 0
    for y in range(max(0, y0), min(height, y1)):
        for x in range(max(0, x0), min(width, x1)):
            total += 1
            index = y * width + x
            depth = float(depth_values[index])
            if confidence_values is not None and int(confidence_values[index]) == 0:
                continue
            if math.isfinite(depth) and 0 < depth <= RGBD_DEPTH_TRUNC_METERS:
                valid += 1
    return valid / total if total else 0.0


def depth_edge_chaos_ratio(depth_values: array, width: int, height: int) -> float:
    total_pixels = width * height
    step = max(1, int(math.ceil(math.sqrt(total_pixels / 20_000))))
    checked = 0
    chaotic = 0
    for y in range(0, height, step):
        for x in range(0, width, step):
            index = y * width + x
            depth = float(depth_values[index])
            if not math.isfinite(depth) or depth <= 0 or depth > RGBD_DEPTH_TRUNC_METERS:
                continue
            for nx, ny in ((x + step, y), (x, y + step)):
                if nx >= width or ny >= height:
                    continue
                neighbor = float(depth_values[ny * width + nx])
                if not math.isfinite(neighbor) or neighbor <= 0 or neighbor > RGBD_DEPTH_TRUNC_METERS:
                    continue
                checked += 1
                if abs(neighbor - depth) > max(0.12, min(depth, neighbor) * 0.14):
                    chaotic += 1
    return chaotic / checked if checked else 0.0


def build_single_keyframe_rgbd_onboarding_mesh(
    *,
    keyframe: dict,
    depth_frame: dict,
    projection_keyframe: ProjectionKeyframe,
    depth_values: array,
    width: int,
    height: int,
) -> dict:
    intrinsics = depth_frame.get("intrinsics") or []
    transform = depth_frame.get("cameraTransform") or []
    total_pixels = width * height
    sample_step = max(1, int(math.ceil(math.sqrt(total_pixels / RGBD_ONBOARDING_TARGET_SAMPLES))))
    x_samples = sampled_depth_indices(width, sample_step)
    y_samples = sampled_depth_indices(height, sample_step)
    vertices: list[tuple[float, float, float]] = []
    uv_coordinates: list[tuple[float, float]] = []
    vertex_source_pixels: list[tuple[float, float]] = []
    faces: list[tuple[int, int, int]] = []
    face_source_pixels: list[tuple[tuple[float, float], tuple[float, float], tuple[float, float]]] = []
    seen_faces: set[tuple[int, int, int]] = set()
    grid: list[list[int | None]] = []
    depth_grid: list[list[float]] = []
    invalid_depth_count = 0
    rejected_face_reasons: dict[str, int] = {}

    for source_y in y_samples:
        row: list[int | None] = []
        depth_row: list[float] = []
        for source_x in x_samples:
            source_index = source_y * width + source_x
            depth = float(depth_values[source_index])
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
            rgb_pixel = expected_rgb_pixel_for_depth_sample(source_x, source_y, depth_frame, keyframe)
            if rgb_pixel is None:
                rgb_pixel = (
                    (source_x / max(width - 1, 1)) * max(projection_keyframe.width - 1, 1),
                    (source_y / max(height - 1, 1)) * max(projection_keyframe.height - 1, 1),
                )
            u_pixel = clamp_float(float(rgb_pixel[0]), 0.0, max(projection_keyframe.width - 1, 1))
            v_pixel = clamp_float(float(rgb_pixel[1]), 0.0, max(projection_keyframe.height - 1, 1))
            row.append(len(vertices))
            depth_row.append(depth)
            vertices.append(world)
            uv_coordinates.append((
                clamp_float(u_pixel / max(projection_keyframe.width - 1, 1), 0.0, 1.0),
                1.0 - clamp_float(v_pixel / max(projection_keyframe.height - 1, 1), 0.0, 1.0),
            ))
            vertex_source_pixels.append((float(source_x), float(source_y)))
        grid.append(row)
        depth_grid.append(depth_row)

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
            add_rgbd_onboarding_face(
                vertices=vertices,
                faces=faces,
                seen_faces=seen_faces,
                vertex_source_pixels=vertex_source_pixels,
                face_source_pixels=face_source_pixels,
                indices=(top_left, bottom_left, top_right),
                depths=(d_tl, d_bl, d_tr),
                rejected_face_reasons=rejected_face_reasons,
            )
            add_rgbd_onboarding_face(
                vertices=vertices,
                faces=faces,
                seen_faces=seen_faces,
                vertex_source_pixels=vertex_source_pixels,
                face_source_pixels=face_source_pixels,
                indices=(top_right, bottom_left, bottom_right),
                depths=(d_tr, d_bl, d_br),
                rejected_face_reasons=rejected_face_reasons,
            )

    mesh = FusedMesh(vertices=vertices, faces=faces, stats={
        "geometrySource": "single_keyframe_rgbd_onboarding_depth_mesh",
        "vertexCount": len(vertices),
        "faceCount": len(faces),
        "selectedKeyframeId": keyframe.get("id"),
        "selectedDepthFrameId": depth_frame.get("id"),
        "sampleStep": sample_step,
        "sourceDepthResolution": [width, height],
        "targetSamples": RGBD_ONBOARDING_TARGET_SAMPLES,
        "depthConnectionAbsoluteToleranceMeters": RGBD_ONBOARDING_FACE_ABSOLUTE_TOLERANCE_METERS,
        "depthConnectionRelativeTolerance": RGBD_ONBOARDING_FACE_RELATIVE_TOLERANCE,
    })
    return {
        "mesh": mesh,
        "uvCoordinates": uv_coordinates,
        "faceSourcePixels": face_source_pixels,
        "stats": {
            "vertexCount": len(vertices),
            "faceCount": len(faces),
            "sampleStep": sample_step,
            "sampledColumnCount": len(x_samples),
            "sampledRowCount": len(y_samples),
            "targetSamples": RGBD_ONBOARDING_TARGET_SAMPLES,
            "invalidDepthSampleCount": invalid_depth_count,
            "rejectedFaceCount": sum(rejected_face_reasons.values()),
            "rejectedFaceReasons": rejected_face_reasons,
            "acceptedFaceCount": len(faces),
            "depthConnectionAbsoluteToleranceMeters": RGBD_ONBOARDING_FACE_ABSOLUTE_TOLERANCE_METERS,
            "depthConnectionRelativeTolerance": RGBD_ONBOARDING_FACE_RELATIVE_TOLERANCE,
            "maxFaceEdgeMeters": RGBD_ONBOARDING_MAX_FACE_EDGE_METERS,
        },
    }


def add_rgbd_onboarding_face(
    *,
    vertices: list[tuple[float, float, float]],
    faces: list[tuple[int, int, int]],
    seen_faces: set[tuple[int, int, int]],
    vertex_source_pixels: list[tuple[float, float]],
    face_source_pixels: list[tuple[tuple[float, float], tuple[float, float], tuple[float, float]]],
    indices: tuple[int | None, int | None, int | None],
    depths: tuple[float, float, float],
    rejected_face_reasons: dict[str, int],
) -> None:
    reason = rgbd_onboarding_face_rejection_reason(vertices, seen_faces, indices, depths)
    if reason is not None:
        rejected_face_reasons[reason] = rejected_face_reasons.get(reason, 0) + 1
        return

    face = (int(indices[0]), int(indices[1]), int(indices[2]))
    seen_faces.add(tuple(sorted(face)))
    faces.append(face)
    face_source_pixels.append((
        vertex_source_pixels[face[0]],
        vertex_source_pixels[face[1]],
        vertex_source_pixels[face[2]],
    ))


def rgbd_onboarding_face_rejection_reason(
    vertices: list[tuple[float, float, float]],
    seen_faces: set[tuple[int, int, int]],
    indices: tuple[int | None, int | None, int | None],
    depths: tuple[float, float, float],
) -> str | None:
    if any(index is None for index in indices):
        return "invalid_depth_corner"
    if not should_connect_depth_samples(
        depths,
        absolute_tolerance=RGBD_ONBOARDING_FACE_ABSOLUTE_TOLERANCE_METERS,
        relative_tolerance=RGBD_ONBOARDING_FACE_RELATIVE_TOLERANCE,
    ):
        return "depth_discontinuity"

    face = (int(indices[0]), int(indices[1]), int(indices[2]))
    if len(set(face)) != 3:
        return "degenerate_indices"
    face_key = tuple(sorted(face))
    if face_key in seen_faces:
        return "duplicate_face"

    a, b, c = (vertices[face[0]], vertices[face[1]], vertices[face[2]])
    if triangle_area(a, b, c) <= 1e-10:
        return "degenerate_area"
    max_edge = max(length(subtract(a, b)), length(subtract(b, c)), length(subtract(c, a)))
    if max_edge > RGBD_ONBOARDING_MAX_FACE_EDGE_METERS:
        return "extreme_edge_length"
    return None


def prune_rgbd_onboarding_mesh_with_lidar(
    *,
    mesh: FusedMesh,
    uv_coordinates: list[tuple[float, float]],
    face_source_pixels: list[tuple[tuple[float, float], tuple[float, float], tuple[float, float]]],
    depth_frame: dict,
    arkit_mesh: FusedMesh,
) -> dict:
    support_index = build_lidar_projection_support_index(arkit_mesh, depth_frame)
    if not support_index["available"]:
        return {
            "mesh": mesh,
            "uvCoordinates": uv_coordinates,
            "stats": {
                "enabled": False,
                "reason": support_index["reason"],
                "rawVertexCount": len(mesh.vertices),
                "rawFaceCount": len(mesh.faces),
                "finalVertexCount": len(mesh.vertices),
                "finalFaceCount": len(mesh.faces),
                "pruneReasonCounts": {"lidar_support_unavailable_keep": len(mesh.faces)},
                "lidarSupportIndex": lidar_support_index_summary(support_index),
                "averageLidarSupportDistanceMeters": None,
                "medianLidarSupportDistanceMeters": None,
                "disconnectedComponentCounts": None,
            },
        }

    kept_faces: list[tuple[int, int, int]] = []
    face_support_reasons: list[str] = []
    prune_reason_counts: dict[str, int] = {}
    support_distances: list[float] = []
    depth_support_distances: list[float] = []
    for face_index, face in enumerate(mesh.faces):
        decision = rgbd_onboarding_lidar_face_decision(
            mesh=mesh,
            face=face,
            face_source_pixels=face_source_pixels[face_index] if face_index < len(face_source_pixels) else None,
            support_index=support_index,
        )
        reason = decision["reason"]
        prune_reason_counts[reason] = prune_reason_counts.get(reason, 0) + 1
        if decision.get("nearestDistanceMeters") is not None:
            support_distances.append(float(decision["nearestDistanceMeters"]))
        if decision.get("nearestDepthDeltaMeters") is not None:
            depth_support_distances.append(float(decision["nearestDepthDeltaMeters"]))
        if decision["keep"]:
            kept_faces.append(face)
            face_support_reasons.append(reason)

    compact = compact_mesh_to_faces(mesh, uv_coordinates, kept_faces)
    component_result = filter_rgbd_onboarding_components(
        mesh=compact["mesh"],
        uv_coordinates=compact["uvCoordinates"],
        face_support_reasons=face_support_reasons,
    )
    component_stats = component_result["stats"]
    if component_stats.get("removedFaceCount", 0):
        prune_reason_counts["tiny_disconnected_island"] = (
            prune_reason_counts.get("tiny_disconnected_island", 0)
            + int(component_stats["removedFaceCount"])
        )

    final_mesh = component_result["mesh"]
    final_uv_coordinates = component_result["uvCoordinates"]
    return {
        "mesh": final_mesh,
        "uvCoordinates": final_uv_coordinates,
        "stats": {
            "enabled": True,
            "algorithm": "camera_projection_lidar_vertex_support_with_sparse_keep",
            "rawVertexCount": len(mesh.vertices),
            "rawFaceCount": len(mesh.faces),
            "afterLidarFaceCount": len(kept_faces),
            "finalVertexCount": len(final_mesh.vertices),
            "finalFaceCount": len(final_mesh.faces),
            "prunedFaceCount": len(mesh.faces) - len(final_mesh.faces),
            "prunedVertexCount": len(mesh.vertices) - len(final_mesh.vertices),
            "pruneReasonCounts": prune_reason_counts,
            "supportDistanceMeters": RGBD_ONBOARDING_LIDAR_SUPPORT_DISTANCE_METERS,
            "supportDepthDeltaMeters": RGBD_ONBOARDING_LIDAR_DEPTH_SUPPORT_METERS,
            "hardRejectDepthDeltaMeters": RGBD_ONBOARDING_LIDAR_HARD_REJECT_DEPTH_METERS,
            "hardRejectDistanceMeters": RGBD_ONBOARDING_LIDAR_HARD_REJECT_DISTANCE_METERS,
            "maxSupportCandidatesPerFace": RGBD_ONBOARDING_LIDAR_MAX_SUPPORT_CANDIDATES,
            "averageLidarSupportDistanceMeters": (
                round(sum(support_distances) / len(support_distances), 5)
                if support_distances else None
            ),
            "medianLidarSupportDistanceMeters": (
                round(median_sorted(sorted(support_distances)), 5)
                if support_distances else None
            ),
            "averageLidarDepthDeltaMeters": (
                round(sum(depth_support_distances) / len(depth_support_distances), 5)
                if depth_support_distances else None
            ),
            "medianLidarDepthDeltaMeters": (
                round(median_sorted(sorted(depth_support_distances)), 5)
                if depth_support_distances else None
            ),
            "lidarSupportIndex": lidar_support_index_summary(support_index),
            "disconnectedComponentCounts": component_stats,
        },
    }


def build_lidar_projection_support_index(arkit_mesh: FusedMesh, depth_frame: dict) -> dict:
    resolution = depth_frame.get("depthResolution") or []
    intrinsics = depth_frame.get("intrinsics") or []
    transform = depth_frame.get("cameraTransform") or []
    if not arkit_mesh.vertices:
        return {"available": False, "reason": "arkit_mesh_has_no_vertices"}
    if len(resolution) != 2 or len(intrinsics) != 9 or len(transform) != 16:
        return {"available": False, "reason": "selected_depth_frame_missing_projection_metadata"}

    width, height = int(resolution[0]), int(resolution[1])
    if width <= 0 or height <= 0:
        return {"available": False, "reason": "selected_depth_frame_invalid_resolution"}

    world_to_camera = invert_rigid_transform(transform)
    cell_size = max(1, int(math.ceil(max(width, height) / RGBD_ONBOARDING_LIDAR_PIXEL_BUCKETS)))
    buckets: dict[tuple[int, int], list[dict]] = {}
    projected_count = 0
    for index, vertex in enumerate(arkit_mesh.vertices):
        projection = project_world_point_values(vertex, world_to_camera, intrinsics, width, height)
        if projection is None:
            continue
        u, v, depth = projection
        cell = (int(u) // cell_size, int(v) // cell_size)
        buckets.setdefault(cell, []).append({
            "vertexIndex": index,
            "world": vertex,
            "u": u,
            "v": v,
            "depth": depth,
        })
        projected_count += 1

    if not buckets:
        return {
            "available": False,
            "reason": "no_arkit_vertices_project_into_selected_rgbd_frame",
            "inputVertexCount": len(arkit_mesh.vertices),
            "inputFaceCount": len(arkit_mesh.faces),
            "projectedVertexCount": projected_count,
        }

    return {
        "available": True,
        "width": width,
        "height": height,
        "intrinsics": intrinsics,
        "worldToCamera": world_to_camera,
        "cellSizePixels": cell_size,
        "bucketCount": len(buckets),
        "pixelBucketTarget": RGBD_ONBOARDING_LIDAR_PIXEL_BUCKETS,
        "maxCandidatesPerLookup": RGBD_ONBOARDING_LIDAR_MAX_SUPPORT_CANDIDATES,
        "inputVertexCount": len(arkit_mesh.vertices),
        "inputFaceCount": len(arkit_mesh.faces),
        "projectedVertexCount": projected_count,
        "buckets": buckets,
    }


def lidar_support_index_summary(support_index: dict) -> dict:
    return {
        key: value
        for key, value in support_index.items()
        if key not in {"buckets", "worldToCamera", "intrinsics"}
    }


def rgbd_onboarding_lidar_face_decision(
    *,
    mesh: FusedMesh,
    face: tuple[int, int, int],
    face_source_pixels: tuple[tuple[float, float], tuple[float, float], tuple[float, float]] | None,
    support_index: dict,
) -> dict:
    vertices = [mesh.vertices[face[0]], mesh.vertices[face[1]], mesh.vertices[face[2]]]
    centroid = triangle_center(vertices[0], vertices[1], vertices[2])
    projection = project_world_point_values(
        centroid,
        support_index["worldToCamera"],
        support_index["intrinsics"],
        int(support_index["width"]),
        int(support_index["height"]),
    )
    if projection is None:
        return {"keep": True, "reason": "rgbd_face_unprojected_keep"}

    projected_u, projected_v, projected_depth = projection
    if face_source_pixels is not None:
        projected_u = sum(pixel[0] for pixel in face_source_pixels) / 3.0
        projected_v = sum(pixel[1] for pixel in face_source_pixels) / 3.0
    candidates = nearby_lidar_support_candidates(support_index, projected_u, projected_v)
    if not candidates:
        return {"keep": True, "reason": "lidar_sparse_keep"}

    nearest_distance = math.inf
    nearest_depth_delta = math.inf
    for candidate in candidates:
        nearest_distance = min(nearest_distance, length(subtract(candidate["world"], centroid)))
        nearest_depth_delta = min(nearest_depth_delta, abs(float(candidate["depth"]) - projected_depth))
    if (
        nearest_distance <= RGBD_ONBOARDING_LIDAR_SUPPORT_DISTANCE_METERS
        or nearest_depth_delta <= RGBD_ONBOARDING_LIDAR_DEPTH_SUPPORT_METERS
    ):
        return {
            "keep": True,
            "reason": "lidar_supported_keep",
            "nearestDistanceMeters": round(nearest_distance, 5),
            "nearestDepthDeltaMeters": round(nearest_depth_delta, 5),
        }
    if (
        nearest_depth_delta >= RGBD_ONBOARDING_LIDAR_HARD_REJECT_DEPTH_METERS
        and nearest_distance >= RGBD_ONBOARDING_LIDAR_HARD_REJECT_DISTANCE_METERS
    ):
        return {
            "keep": False,
            "reason": "lidar_depth_disagreement_prune",
            "nearestDistanceMeters": round(nearest_distance, 5),
            "nearestDepthDeltaMeters": round(nearest_depth_delta, 5),
        }
    return {
        "keep": True,
        "reason": "lidar_ambiguous_keep",
        "nearestDistanceMeters": round(nearest_distance, 5),
        "nearestDepthDeltaMeters": round(nearest_depth_delta, 5),
    }


def nearby_lidar_support_candidates(support_index: dict, u: float, v: float) -> list[dict]:
    cell_size = max(int(support_index["cellSizePixels"]), 1)
    center_cell = (int(u) // cell_size, int(v) // cell_size)
    buckets: dict[tuple[int, int], list[dict]] = support_index["buckets"]
    candidates: list[dict] = []
    max_candidates = max(1, int(support_index.get("maxCandidatesPerLookup") or RGBD_ONBOARDING_LIDAR_MAX_SUPPORT_CANDIDATES))
    for radius in (1, 2):
        candidates.clear()
        for cy in range(center_cell[1] - radius, center_cell[1] + radius + 1):
            for cx in range(center_cell[0] - radius, center_cell[0] + radius + 1):
                candidates.extend(buckets.get((cx, cy), []))
        if candidates:
            candidates.sort(key=lambda candidate: (float(candidate["u"]) - u) ** 2 + (float(candidate["v"]) - v) ** 2)
            return list(candidates[:max_candidates])
    return []


def compact_mesh_to_faces(
    mesh: FusedMesh,
    uv_coordinates: list[tuple[float, float]],
    kept_faces: list[tuple[int, int, int]],
) -> dict:
    referenced = sorted({index for face in kept_faces for index in face})
    remap = {old_index: new_index for new_index, old_index in enumerate(referenced)}
    vertices = [mesh.vertices[old_index] for old_index in referenced]
    uvs = [uv_coordinates[old_index] for old_index in referenced]
    faces = [
        (remap[face[0]], remap[face[1]], remap[face[2]])
        for face in kept_faces
        if face[0] in remap and face[1] in remap and face[2] in remap
    ]
    return {
        "mesh": FusedMesh(vertices=vertices, faces=faces, stats={**mesh.stats, "vertexCount": len(vertices), "faceCount": len(faces)}),
        "uvCoordinates": uvs,
        "remap": remap,
    }


def filter_rgbd_onboarding_components(
    *,
    mesh: FusedMesh,
    uv_coordinates: list[tuple[float, float]],
    face_support_reasons: list[str],
) -> dict:
    components = mesh_connected_face_components(mesh)
    if not components:
        return {
            "mesh": mesh,
            "uvCoordinates": uv_coordinates,
            "stats": {
                "enabled": False,
                "reason": "no_faces",
                "componentCount": 0,
                "removedComponentCount": 0,
                "removedFaceCount": 0,
            },
        }

    total_faces = len(mesh.faces)
    min_faces = max(
        RGBD_ONBOARDING_MIN_COMPONENT_FACES,
        int(math.ceil(total_faces * RGBD_ONBOARDING_MIN_COMPONENT_FACE_RATIO)),
    )
    if total_faces <= min_faces * 2:
        return {
            "mesh": mesh,
            "uvCoordinates": uv_coordinates,
            "stats": {
                "enabled": False,
                "reason": "mesh_too_small_for_component_pruning",
                "componentCount": len(components),
                "largestComponentFaces": max(len(component) for component in components),
                "removedComponentCount": 0,
                "removedFaceCount": 0,
                "minComponentFaces": min_faces,
            },
        }

    keep_face_indexes: set[int] = set()
    removed_component_count = 0
    removed_face_count = 0
    component_summaries = []
    for component in components:
        has_lidar_support = any(
            face_index < len(face_support_reasons)
            and face_support_reasons[face_index] == "lidar_supported_keep"
            for face_index in component
        )
        keep = len(component) >= min_faces or has_lidar_support
        if keep:
            keep_face_indexes.update(component)
        else:
            removed_component_count += 1
            removed_face_count += len(component)
        component_summaries.append({
            "faceCount": len(component),
            "hasLidarSupport": has_lidar_support,
            "kept": keep,
        })

    kept_faces = [
        face for index, face in enumerate(mesh.faces)
        if index in keep_face_indexes
    ]
    compact = compact_mesh_to_faces(mesh, uv_coordinates, kept_faces)
    return {
        "mesh": compact["mesh"],
        "uvCoordinates": compact["uvCoordinates"],
        "stats": {
            "enabled": True,
            "componentCount": len(components),
            "keptComponentCount": len(components) - removed_component_count,
            "removedComponentCount": removed_component_count,
            "removedFaceCount": removed_face_count,
            "minComponentFaces": min_faces,
            "largestComponentFaces": max(len(component) for component in components),
            "components": component_summaries[:40],
        },
    }


def mesh_connected_face_components(mesh: FusedMesh) -> list[list[int]]:
    vertex_to_faces: dict[int, list[int]] = {}
    for face_index, face in enumerate(mesh.faces):
        for vertex_index in face:
            vertex_to_faces.setdefault(vertex_index, []).append(face_index)

    visited: set[int] = set()
    components: list[list[int]] = []
    for start_index in range(len(mesh.faces)):
        if start_index in visited:
            continue
        component: list[int] = []
        stack = [start_index]
        visited.add(start_index)
        while stack:
            face_index = stack.pop()
            component.append(face_index)
            for vertex_index in mesh.faces[face_index]:
                for neighbor in vertex_to_faces.get(vertex_index, []):
                    if neighbor in visited:
                        continue
                    visited.add(neighbor)
                    stack.append(neighbor)
        components.append(component)
    return components


def face_uvs_from_vertex_uvs(mesh: FusedMesh, uv_coordinates: list[tuple[float, float]]) -> FaceUVs:
    return [
        (uv_coordinates[face[0]], uv_coordinates[face[1]], uv_coordinates[face[2]])
        for face in mesh.faces
        if face[0] < len(uv_coordinates) and face[1] < len(uv_coordinates) and face[2] < len(uv_coordinates)
    ]


def write_rgbd_hero_patch_textured_obj(
    *,
    keyframes: list[dict],
    depth_frames: list[dict],
    loaded_keyframes: list[ProjectionKeyframe],
    work_dir: Path,
    output_obj_path: Path,
    output_mtl_path: Path,
    output_texture_path: Path,
    output_debug_path: Path,
    output_projection_overlay_dir: Path | None = None,
    profile: ProcessingProfile,
) -> dict:
    paired_frames = pair_rgbd_frames(keyframes, depth_frames, work_dir)
    if not paired_frames:
        raise RGBDFusionUnavailable("No RGB/depth frame pair could be matched for RGB-D hero patch texturing.")

    timestamps = [
        float(frame[1].get("timestamp") or frame[0].get("timestamp") or 0.0)
        for frame in paired_frames
    ]
    scan_midpoint = (min(timestamps) + max(timestamps)) / 2 if timestamps else 0.0
    scan_span = max(max(timestamps) - min(timestamps), 1e-6) if timestamps else 1.0
    candidates = [
        rgbd_diagnostic_candidate_metrics(
            index=index,
            pair=pair,
            work_dir=work_dir,
            scan_midpoint=scan_midpoint,
            scan_span=scan_span,
        )
        for index, pair in enumerate(paired_frames)
    ]
    if not candidates:
        raise RGBDFusionUnavailable("No RGB-D hero patch candidate had valid depth bytes, transforms, and intrinsics.")

    candidates.sort(
        key=lambda item: (
            float(item["validDepthRatio"]),
            float(item["highConfidenceRatio"]),
            float(item["score"]),
        ),
        reverse=True,
    )
    selected_candidates, selection_stats = select_rgbd_hero_patch_candidates(candidates)
    patch_results = []
    skipped_patch_reasons = []
    for patch_index, selected in enumerate(selected_candidates):
        keyframe, depth_frame, color_path, depth_path = selected["pair"]
        width, height = [int(value) for value in depth_frame["depthResolution"]]
        rgb_image = Image.open(color_path).convert("RGB")
        depth_values = read_float32_depth_values(depth_path, width, height)
        confidence_values = read_confidence_values(depth_frame, work_dir, width, height)
        projection_keyframe = diagnostic_projection_keyframe(keyframe, rgb_image)
        prepared_depth = prepare_rgbd_hero_patch_depth_grid(
            depth_values=depth_values,
            confidence_values=confidence_values,
            width=width,
            height=height,
        )
        mesh_result = build_rgbd_hero_patch_textured_mesh(
            keyframe=keyframe,
            depth_frame=depth_frame,
            projection_keyframe=projection_keyframe,
            depth_values=prepared_depth["depthValues"],
            width=width,
            height=height,
        )
        mesh = mesh_result["mesh"]
        if not mesh.faces:
            skipped_patch_reasons.append({
                "keyframeId": keyframe.get("id"),
                "depthFrameId": depth_frame.get("id"),
                "reason": "rgbd_hero_patch_did_not_produce_connected_faces",
            })
            continue

        patch_results.append({
            "patchIndex": patch_index,
            "candidate": selected,
            "keyframe": keyframe,
            "depthFrame": depth_frame,
            "rgbImage": rgb_image,
            "ownershipDepthFrame": ProjectionDepthFrame(
                id=str(depth_frame.get("id")) if depth_frame.get("id") else None,
                color_keyframe_id=(
                    str(depth_frame.get("colorKeyframeId"))
                    if depth_frame.get("colorKeyframeId")
                    else None
                ),
                width=width,
                height=height,
                world_to_camera=invert_rigid_transform(depth_frame.get("cameraTransform") or []),
                intrinsics=depth_frame.get("intrinsics") or [],
                depth_values=depth_values,
                confidence_values=confidence_values,
                timestamp=safe_float(depth_frame.get("timestamp")),
                path=str(depth_frame.get("path")) if depth_frame.get("path") else None,
                confidence_path=(
                    str(depth_frame.get("confidencePath"))
                    if depth_frame.get("confidencePath")
                    else None
                ),
            ),
            "preparedDepthStats": prepared_depth["stats"],
            "mesh": mesh,
            "uvCoordinates": mesh_result["uvCoordinates"],
            "meshStats": mesh_result["stats"],
        })

    if not patch_results:
        raise RGBDFusionUnavailable("RGB-D hero patches did not produce connected textured faces.")

    combined = combine_rgbd_hero_patch_meshes(patch_results)
    mesh = combined["mesh"]
    uv_coordinates = combined["uvCoordinates"]
    atlas_image = combined["texture"]
    atlas_image.save(output_texture_path)
    output_mtl_path.write_text("\n".join([
        "newmtl LidarAI_RGBD_Hero_Patch",
        "Ka 1.000000 1.000000 1.000000",
        "Kd 1.000000 1.000000 1.000000",
        "Ks 0.000000 0.000000 0.000000",
        "d 1.0",
        "illum 1",
        f"map_Kd {output_texture_path.name}",
        "",
    ]), encoding="utf-8")
    write_textured_mesh_obj_with_uvs(
        mesh=mesh,
        uv_coordinates=uv_coordinates,
        output_obj_path=output_obj_path,
        output_mtl_path=output_mtl_path,
        object_name="rgbd_hero_patch_mesh",
    )

    texture_diagnostics = build_rgbd_hero_patch_texture_diagnostics(
        keyframes=loaded_keyframes,
        selected_patches=combined["patches"],
        mesh=mesh,
        uv_coordinates=uv_coordinates,
        atlas_image=atlas_image,
        candidate_stats=candidates,
        selection_stats=selection_stats,
        skipped_patch_reasons=skipped_patch_reasons,
        profile=profile,
    )
    if output_projection_overlay_dir is not None:
        texture_diagnostics["projectionOverlays"] = write_two_keyframe_projection_overlays(
            mesh,
            loaded_keyframes,
            output_projection_overlay_dir,
        )
    output_debug_path.write_text(json.dumps(texture_diagnostics, indent=2), encoding="utf-8")

    return {
        "uvStrategy": "rgbd_hero_patch_direct_image_uv",
        "atlasWidth": atlas_image.width,
        "atlasHeight": atlas_image.height,
        "atlasMaxSize": max(atlas_image.width, atlas_image.height),
        "tileSize": 0,
        "tilePadding": 0,
        "dilationPixels": 0,
        "uvCoordinateCount": len(uv_coordinates),
        "faceCount": len(mesh.faces),
        "texturedFaceCount": len(mesh.faces),
        "fallbackFaceCount": 0,
        "projectionCoverage": 1.0,
        "renderMesh": mesh.stats.get("textureRenderMesh", {}),
        "atlasLayout": {
            "strategy": "vertical_stack_rgbd_hero_patch_source_images",
            "enabled": len(combined["patches"]) > 1,
            "patchCount": len(combined["patches"]),
            "patches": [
                {
                    "patchIndex": patch["patchIndex"],
                    "keyframeId": patch["keyframe"].get("id"),
                    "depthFrameId": patch["depthFrame"].get("id"),
                    "atlasRect": patch["atlasRect"],
                    "sourceFaceCount": patch.get("sourceFaceCount"),
                    "keptFaceCount": patch.get("keptFaceCount"),
                    "culledFaceCount": patch.get("culledFaceCount"),
                }
                for patch in combined["patches"]
            ],
            "reason": (
                "Hero RGB-D patches use their source RGB images packed into one atlas with direct remapped UVs."
            ),
        },
        "textureWorkerCount": 1,
        "diagnostics": texture_diagnostics,
    }


def select_rgbd_hero_patch_candidates(candidates: list[dict]) -> tuple[list[dict], dict]:
    ordered = sorted(
        candidates,
        key=lambda item: (
            rgbd_hero_patch_candidate_timestamp(item),
            int(item.get("index") or 0),
        ),
    )
    if not ordered:
        return [], {
            "strategy": "no_rgbd_hero_patch_candidates",
            "selectedPatchCount": 0,
        }

    first_timestamp = rgbd_hero_patch_candidate_timestamp(ordered[0])
    primary_candidates = [
        candidate for candidate in ordered
        if rgbd_hero_patch_candidate_timestamp(candidate) - first_timestamp
        <= RGBD_HERO_PATCH_PRIMARY_SELECTION_WINDOW_SECONDS
    ] or [ordered[0]]
    primary = max(
        primary_candidates,
        key=lambda candidate: (
            rgbd_hero_patch_candidate_quality_score(candidate),
            -abs(rgbd_hero_patch_candidate_timestamp(candidate) - first_timestamp),
            -int(candidate.get("index") or 0),
        ),
    )
    if len(ordered) == 1 or RGBD_HERO_PATCH_MAX_PATCHES <= 1:
        return [primary], {
            "strategy": "best_primary_rgbd_hero_patch_candidate",
            "selectedPatchCount": 1,
            "selectedCandidateIndexes": [primary.get("index")],
            "supplementalSelection": None,
            "candidatePoolCount": len(candidates),
        }

    selected = [primary]
    remaining = [candidate for candidate in ordered if candidate is not primary]
    supplemental_selections = []
    max_patch_count = min(RGBD_HERO_PATCH_MAX_PATCHES, len(ordered))
    while len(selected) < max_patch_count and remaining:
        target_delta = rgbd_hero_patch_supplement_target_for_index(len(selected))
        window_candidates = rgbd_hero_patch_window_candidates(
            remaining,
            primary,
            target_delta,
        )
        supplemental = max(
            window_candidates,
            key=lambda candidate: supplemental_rgbd_hero_patch_candidate_score(
                candidate,
                primary,
                selected,
                target_delta,
            ),
        )
        remaining = [candidate for candidate in remaining if candidate is not supplemental]
        supplemental_score = supplemental_rgbd_hero_patch_candidate_score(
            supplemental,
            primary,
            selected,
            target_delta,
        )
        selected.append(supplemental)
        delta = max(
            0.0,
            rgbd_hero_patch_candidate_timestamp(supplemental) - rgbd_hero_patch_candidate_timestamp(primary),
        )
        supplemental_selections.append({
            "targetTimeDeltaSeconds": target_delta,
            "windowSeconds": RGBD_HERO_PATCH_SUPPLEMENT_SELECTION_WINDOW_SECONDS,
            "actualTimeDeltaSeconds": round(delta, 4),
            "score": round(supplemental_score, 6),
            "qualityScore": round(rgbd_hero_patch_candidate_quality_score(supplemental), 6),
            "selectedKeyframeId": supplemental.get("keyframeId"),
            "selectedDepthFrameId": supplemental.get("depthFrameId"),
            "candidateWindowCount": len(window_candidates),
        })

    return selected, {
        "strategy": "quality_aware_primary_plus_timed_supplemental_rgbd_hero_patches",
        "candidatePoolCount": len(candidates),
        "selectedPatchCount": len(selected),
        "selectedCandidateIndexes": [candidate.get("index") for candidate in selected],
        "selectedKeyframeIds": [candidate.get("keyframeId") for candidate in selected],
        "selectedDepthFrameIds": [candidate.get("depthFrameId") for candidate in selected],
        "primarySelection": {
            "windowSeconds": RGBD_HERO_PATCH_PRIMARY_SELECTION_WINDOW_SECONDS,
            "candidateWindowCount": len(primary_candidates),
            "qualityScore": round(rgbd_hero_patch_candidate_quality_score(primary), 6),
            "selectedKeyframeId": primary.get("keyframeId"),
            "selectedDepthFrameId": primary.get("depthFrameId"),
        },
        "supplementalSelection": supplemental_selections[0] if supplemental_selections else None,
        "supplementalSelections": supplemental_selections,
        "targetSupplementTimeDeltaSeconds": RGBD_HERO_PATCH_SUPPLEMENT_TARGET_TIME_DELTA_SECONDS,
        "targetSupplementTimeDeltasSeconds": list(RGBD_HERO_PATCH_SUPPLEMENT_TARGET_TIME_DELTAS_SECONDS),
    }


def supplemental_rgbd_hero_patch_candidate_score(
    candidate: dict,
    primary: dict,
    selected: list[dict] | None = None,
    target_delta: float | None = None,
) -> float:
    target_delta = target_delta or RGBD_HERO_PATCH_SUPPLEMENT_TARGET_TIME_DELTA_SECONDS
    delta = max(0.0, rgbd_hero_patch_candidate_timestamp(candidate) - rgbd_hero_patch_candidate_timestamp(primary))
    time_score = timed_supplement_score(delta, target_delta)
    selected = selected or [primary]
    selected_indexes = {item.get("index") for item in selected if item.get("index") is not None}
    candidate_index = candidate.get("index")
    already_selected_penalty = -10.0 if candidate_index is not None and candidate_index in selected_indexes else 0.0
    return (
        time_score * 2.0
        + float(candidate.get("validDepthRatio") or 0) * 1.5
        + float(candidate.get("highConfidenceRatio") or 0) * 0.75
        + min(float(candidate.get("rgbSharpnessScore") or 0) / 28.0, 1.0) * 1.0
        + already_selected_penalty
    )


def rgbd_hero_patch_window_candidates(
    candidates: list[dict],
    primary: dict,
    target_delta: float,
) -> list[dict]:
    primary_timestamp = rgbd_hero_patch_candidate_timestamp(primary)
    in_window = [
        candidate for candidate in candidates
        if abs(
            max(0.0, rgbd_hero_patch_candidate_timestamp(candidate) - primary_timestamp)
            - target_delta
        ) <= RGBD_HERO_PATCH_SUPPLEMENT_SELECTION_WINDOW_SECONDS
    ]
    return in_window or candidates


def rgbd_hero_patch_candidate_quality_score(candidate: dict) -> float:
    return (
        float(candidate.get("validDepthRatio") or 0) * 2.0
        + float(candidate.get("highConfidenceRatio") or 0) * 1.0
        + min(float(candidate.get("rgbSharpnessScore") or 0) / 28.0, 1.0) * 1.0
    )


def rgbd_hero_patch_supplement_target_for_index(selected_count: int) -> float:
    target_index = max(0, selected_count - 1)
    if target_index < len(RGBD_HERO_PATCH_SUPPLEMENT_TARGET_TIME_DELTAS_SECONDS):
        return RGBD_HERO_PATCH_SUPPLEMENT_TARGET_TIME_DELTAS_SECONDS[target_index]
    last_target = RGBD_HERO_PATCH_SUPPLEMENT_TARGET_TIME_DELTAS_SECONDS[-1]
    return last_target + 2.0 * (target_index - len(RGBD_HERO_PATCH_SUPPLEMENT_TARGET_TIME_DELTAS_SECONDS) + 1)


def timed_supplement_score(time_delta: float, target_delta: float) -> float:
    preferred_half_width = 0.75
    preferred_min = max(RGBD_HERO_PATCH_SUPPLEMENT_MIN_TIME_DELTA_SECONDS, target_delta - preferred_half_width)
    preferred_max = target_delta + preferred_half_width
    if preferred_min <= time_delta <= preferred_max:
        return 2.0 - min(abs(time_delta - target_delta), 1.0)
    return max(0.0, 1.0 - abs(time_delta - target_delta) / 4.0)


def rgbd_hero_patch_candidate_timestamp(candidate: dict) -> float:
    timestamp = safe_float(candidate.get("sourceTimestamp"))
    if timestamp is not None:
        return timestamp
    pair = candidate.get("pair")
    if pair:
        keyframe, depth_frame, _color_path, _depth_path = pair
        timestamp = safe_float(depth_frame.get("timestamp"))
        if timestamp is not None:
            return timestamp
        timestamp = safe_float(keyframe.get("timestamp"))
        if timestamp is not None:
            return timestamp
    return 0.0


def combine_rgbd_hero_patch_meshes(patches: list[dict]) -> dict:
    atlas_width = max(patch["rgbImage"].width for patch in patches)
    atlas_height = sum(patch["rgbImage"].height for patch in patches)
    texture = Image.new("RGB", (atlas_width, atlas_height), FALLBACK_COLOR)
    vertices: list[tuple[float, float, float]] = []
    faces: list[tuple[int, int, int]] = []
    uv_coordinates: list[tuple[float, float]] = []
    patch_summaries = []
    kept_faces_by_patch, ownership_cull_stats = rgbd_hero_patch_owned_face_sets(patches)
    y_offset = 0

    for output_index, patch in enumerate(patches):
        rgb_image = patch["rgbImage"]
        mesh = patch["mesh"]
        source_uv_coordinates = patch["uvCoordinates"]
        kept_faces = kept_faces_by_patch[output_index]
        texture.paste(rgb_image, (0, y_offset))

        vertex_offset = len(vertices)
        vertices.extend(mesh.vertices)
        faces.extend(
            (a + vertex_offset, b + vertex_offset, c + vertex_offset)
            for a, b, c in kept_faces
        )
        for source_u, source_obj_v in source_uv_coordinates:
            source_v = 1.0 - source_obj_v
            source_x = clamp_float(source_u, 0.0, 1.0) * max(rgb_image.width - 1, 1)
            source_y = clamp_float(source_v, 0.0, 1.0) * max(rgb_image.height - 1, 1)
            atlas_u = clamp_float(source_x / max(atlas_width - 1, 1), 0.0, 1.0)
            atlas_v = 1.0 - clamp_float((y_offset + source_y) / max(atlas_height - 1, 1), 0.0, 1.0)
            uv_coordinates.append((atlas_u, atlas_v))

        atlas_rect = {
            "x": 0,
            "y": y_offset,
            "width": rgb_image.width,
            "height": rgb_image.height,
        }
        patch_summaries.append({
            **patch,
            "patchIndex": output_index,
            "atlasRect": atlas_rect,
            "sourceFaceCount": len(mesh.faces),
            "keptFaceCount": len(kept_faces),
            "culledFaceCount": max(len(mesh.faces) - len(kept_faces), 0),
            "ownershipCull": ownership_cull_stats["patches"][output_index],
            "atlasUVBounds": {
                "minU": 0.0,
                "maxU": round((rgb_image.width - 1) / max(atlas_width - 1, 1), 6),
                "minV": round(1.0 - (y_offset + rgb_image.height - 1) / max(atlas_height - 1, 1), 6),
                "maxV": round(1.0 - y_offset / max(atlas_height - 1, 1), 6),
            },
        })
        y_offset += rgb_image.height

    selected_keyframe_ids = [patch["keyframe"].get("id") for patch in patch_summaries if patch["keyframe"].get("id")]
    selected_depth_frame_ids = [patch["depthFrame"].get("id") for patch in patch_summaries if patch["depthFrame"].get("id")]
    combined_mesh_stats = {
        "geometrySource": "rgbd_hero_patch_depth_mesh",
        "vertexCount": len(vertices),
        "faceCount": len(faces),
        "acceptedFaceCount": len(faces),
        "patchCount": len(patch_summaries),
        "selectedKeyframeId": selected_keyframe_ids[0] if selected_keyframe_ids else None,
        "selectedDepthFrameId": selected_depth_frame_ids[0] if selected_depth_frame_ids else None,
        "selectedKeyframeIds": selected_keyframe_ids,
        "selectedDepthFrameIds": selected_depth_frame_ids,
        "sourceDepthResolutions": [
            patch["depthFrame"].get("depthResolution")
            for patch in patch_summaries
        ],
        "ownershipCull": ownership_cull_stats,
        "targetSamplesPerPatch": RGBD_HERO_PATCH_TARGET_SAMPLES,
        "depthConnectionAbsoluteToleranceMeters": RGBD_HERO_PATCH_FACE_ABSOLUTE_TOLERANCE_METERS,
        "depthConnectionRelativeTolerance": RGBD_HERO_PATCH_FACE_RELATIVE_TOLERANCE,
        "textureRenderMesh": {
            "used": False,
            "reason": "Fast RGB-D hero patches are already the render mesh.",
        },
    }
    return {
        "mesh": FusedMesh(vertices=vertices, faces=faces, stats=combined_mesh_stats),
        "uvCoordinates": uv_coordinates,
        "texture": texture,
        "patches": patch_summaries,
    }


def rgbd_hero_patch_owned_face_sets(patches: list[dict]) -> tuple[list[list[tuple[int, int, int]]], dict]:
    kept_faces_by_patch = [list(patch["mesh"].faces) for patch in patches]
    patch_stats = [
        {
            "patchIndex": index,
            "role": "primary" if index == 0 else "supplemental",
            "sourceFaceCount": len(patch["mesh"].faces),
            "keptFaceCount": len(patch["mesh"].faces),
            "culledFaceCount": 0,
            "primaryOwnedDepthAgreementCount": 0,
            "primaryOwnedDepthDisagreementCount": 0,
            "primaryMissingOrLowConfidenceCount": 0,
            "primaryStretchedDepthEdgeCount": 0,
            "outsidePrimaryFrustumCount": 0,
            "replacedBySupplementalCount": 0,
        }
        for index, patch in enumerate(patches)
    ]
    stats = {
        "enabled": len(patches) > 1,
        "strategy": "primary_depth_confidence_ownership_mask",
        "primaryOwnerConfidenceMin": RGBD_HERO_PATCH_PRIMARY_OWNER_CONFIDENCE_MIN,
        "primaryOwnerAbsoluteToleranceMeters": RGBD_HERO_PATCH_PRIMARY_OWNER_ABSOLUTE_TOLERANCE_METERS,
        "primaryOwnerRelativeTolerance": RGBD_HERO_PATCH_PRIMARY_OWNER_RELATIVE_TOLERANCE,
        "primaryOwnerDepthEdgeAbsoluteMeters": RGBD_HERO_PATCH_PRIMARY_OWNER_DEPTH_EDGE_ABSOLUTE_METERS,
        "primaryOwnerDepthEdgeRelative": RGBD_HERO_PATCH_PRIMARY_OWNER_DEPTH_EDGE_RELATIVE,
        "primaryOwnerMinValidNeighbors": RGBD_HERO_PATCH_PRIMARY_OWNER_MIN_VALID_NEIGHBORS,
        "secondaryClaimRadiusPixels": RGBD_HERO_PATCH_SECONDARY_CLAIM_RADIUS_PIXELS,
        "patches": patch_stats,
    }
    if len(patches) <= 1:
        return kept_faces_by_patch, stats

    primary_depth_frame = patches[0]["ownershipDepthFrame"]
    secondary_claimed_cells: set[tuple[int, int]] = set()
    secondary_claimed_face_count = 0

    for patch_index, patch in enumerate(patches[1:], start=1):
        mesh = patch["mesh"]
        kept_faces: list[tuple[int, int, int]] = []
        patch_stat = patch_stats[patch_index]
        for face in mesh.faces:
            ownership = rgbd_hero_patch_primary_ownership_for_face(mesh, face, primary_depth_frame)
            status = ownership["status"]
            if status == "primary_owned_depth_agreement":
                patch_stat["primaryOwnedDepthAgreementCount"] += 1
            elif status == "primary_owned_depth_disagreement":
                patch_stat["primaryOwnedDepthDisagreementCount"] += 1
            elif status == "primary_missing_or_low_confidence":
                patch_stat["primaryMissingOrLowConfidenceCount"] += 1
            elif status == "primary_stretched_depth_edge":
                patch_stat["primaryStretchedDepthEdgeCount"] += 1
            elif status == "outside_primary_frustum":
                patch_stat["outsidePrimaryFrustumCount"] += 1

            if ownership["primaryOwns"]:
                continue

            kept_faces.append(face)
            secondary_claimed_face_count += 1
            cell = ownership.get("cell")
            if cell is not None:
                mark_rgbd_hero_patch_claimed_cells(
                    secondary_claimed_cells,
                    cell,
                    primary_depth_frame.width,
                    primary_depth_frame.height,
                )

        kept_faces_by_patch[patch_index] = kept_faces
        patch_stat["keptFaceCount"] = len(kept_faces)
        patch_stat["culledFaceCount"] = len(mesh.faces) - len(kept_faces)

    if secondary_claimed_cells:
        primary_mesh = patches[0]["mesh"]
        primary_kept_faces: list[tuple[int, int, int]] = []
        primary_stat = patch_stats[0]
        for face in primary_mesh.faces:
            ownership = rgbd_hero_patch_primary_ownership_for_face(primary_mesh, face, primary_depth_frame)
            status = ownership["status"]
            if status == "primary_owned_depth_agreement":
                primary_stat["primaryOwnedDepthAgreementCount"] += 1
            elif status == "primary_owned_depth_disagreement":
                primary_stat["primaryOwnedDepthDisagreementCount"] += 1
            elif status == "primary_missing_or_low_confidence":
                primary_stat["primaryMissingOrLowConfidenceCount"] += 1
            elif status == "primary_stretched_depth_edge":
                primary_stat["primaryStretchedDepthEdgeCount"] += 1
            elif status == "outside_primary_frustum":
                primary_stat["outsidePrimaryFrustumCount"] += 1

            cell = ownership.get("cell")
            if (
                not ownership["primaryOwns"]
                and cell is not None
                and cell in secondary_claimed_cells
            ):
                primary_stat["replacedBySupplementalCount"] += 1
                continue
            primary_kept_faces.append(face)

        kept_faces_by_patch[0] = primary_kept_faces
        primary_stat["keptFaceCount"] = len(primary_kept_faces)
        primary_stat["culledFaceCount"] = len(primary_mesh.faces) - len(primary_kept_faces)

    stats["secondaryClaimedFaceCount"] = secondary_claimed_face_count
    stats["secondaryClaimedCellCount"] = len(secondary_claimed_cells)
    stats["totalSourceFaceCount"] = sum(item["sourceFaceCount"] for item in patch_stats)
    stats["totalKeptFaceCount"] = sum(item["keptFaceCount"] for item in patch_stats)
    stats["totalCulledFaceCount"] = sum(item["culledFaceCount"] for item in patch_stats)
    return kept_faces_by_patch, stats


def rgbd_hero_patch_primary_ownership_for_face(
    mesh: FusedMesh,
    face: tuple[int, int, int],
    primary_depth_frame: ProjectionDepthFrame,
) -> dict:
    vertices = [mesh.vertices[face[0]], mesh.vertices[face[1]], mesh.vertices[face[2]]]
    center = triangle_center(vertices[0], vertices[1], vertices[2])
    projection = project_world_point_to_depth(center, primary_depth_frame)
    if projection is None:
        return {
            "status": "outside_primary_frustum",
            "primaryOwns": False,
            "cell": None,
        }

    u, v, projected_depth = projection
    cell = (
        max(0, min(int(round(u)), primary_depth_frame.width - 1)),
        max(0, min(int(round(v)), primary_depth_frame.height - 1)),
    )
    sampled = sample_depth_frame(primary_depth_frame, u, v)
    if sampled is None:
        return {
            "status": "primary_missing_or_low_confidence",
            "primaryOwns": False,
            "cell": cell,
            "projectedDepth": round(projected_depth, 4),
            "sampledDepth": None,
            "confidence": None,
        }

    sampled_depth, confidence = sampled
    confidence_reliable = (
        confidence is None
        or confidence >= RGBD_HERO_PATCH_PRIMARY_OWNER_CONFIDENCE_MIN
    )
    if not confidence_reliable:
        return {
            "status": "primary_missing_or_low_confidence",
            "primaryOwns": False,
            "cell": cell,
            "projectedDepth": round(projected_depth, 4),
            "sampledDepth": round(sampled_depth, 4),
            "confidence": confidence,
        }

    neighborhood = rgbd_hero_patch_depth_neighborhood_stats(primary_depth_frame, u, v)
    if neighborhood and neighborhood["validDepthCount"] >= RGBD_HERO_PATCH_PRIMARY_OWNER_MIN_VALID_NEIGHBORS:
        edge_tolerance = max(
            RGBD_HERO_PATCH_PRIMARY_OWNER_DEPTH_EDGE_ABSOLUTE_METERS,
            sampled_depth * RGBD_HERO_PATCH_PRIMARY_OWNER_DEPTH_EDGE_RELATIVE,
        )
        if neighborhood["depthSpanMeters"] > edge_tolerance:
            return {
                "status": "primary_stretched_depth_edge",
                "primaryOwns": False,
                "cell": cell,
                "projectedDepth": round(projected_depth, 4),
                "sampledDepth": round(sampled_depth, 4),
                "confidence": confidence,
                "depthSpanMeters": round(neighborhood["depthSpanMeters"], 4),
                "validNeighborCount": neighborhood["validDepthCount"],
                "depthEdgeToleranceMeters": round(edge_tolerance, 4),
            }

    tolerance = max(
        RGBD_HERO_PATCH_PRIMARY_OWNER_ABSOLUTE_TOLERANCE_METERS,
        projected_depth * RGBD_HERO_PATCH_PRIMARY_OWNER_RELATIVE_TOLERANCE,
    )
    depth_error = abs(sampled_depth - projected_depth)
    return {
        "status": (
            "primary_owned_depth_agreement"
            if depth_error <= tolerance
            else "primary_owned_depth_disagreement"
        ),
        "primaryOwns": True,
        "cell": cell,
        "projectedDepth": round(projected_depth, 4),
        "sampledDepth": round(sampled_depth, 4),
        "confidence": confidence,
        "depthErrorMeters": round(depth_error, 4),
        "toleranceMeters": round(tolerance, 4),
    }


def rgbd_hero_patch_depth_neighborhood_stats(
    depth_frame: ProjectionDepthFrame,
    u: float,
    v: float,
) -> dict | None:
    center_x = int(round(u))
    center_y = int(round(v))
    radius = TEXTURE_DEPTH_NEIGHBORHOOD_RADIUS
    values: list[float] = []
    confidence_values: list[int] = []

    for y in range(center_y - radius, center_y + radius + 1):
        if y < 0 or y >= depth_frame.height:
            continue
        for x in range(center_x - radius, center_x + radius + 1):
            if x < 0 or x >= depth_frame.width:
                continue
            index = y * depth_frame.width + x
            if depth_frame.confidence_values is not None:
                confidence = int(depth_frame.confidence_values[index])
                if confidence == 0:
                    continue
                confidence_values.append(confidence)
            depth = float(depth_frame.depth_values[index])
            if not math.isfinite(depth) or depth <= 0 or depth > RGBD_DEPTH_TRUNC_METERS:
                continue
            values.append(depth)

    if not values:
        return None

    min_depth = min(values)
    max_depth = max(values)
    return {
        "validDepthCount": len(values),
        "minDepth": min_depth,
        "maxDepth": max_depth,
        "medianDepth": median_float(values),
        "confidence": max(confidence_values) if confidence_values else None,
        "depthSpanMeters": max_depth - min_depth,
    }


def mark_rgbd_hero_patch_claimed_cells(
    claimed_cells: set[tuple[int, int]],
    cell: tuple[int, int],
    width: int,
    height: int,
) -> None:
    center_x, center_y = cell
    radius = RGBD_HERO_PATCH_SECONDARY_CLAIM_RADIUS_PIXELS
    for y in range(center_y - radius, center_y + radius + 1):
        if y < 0 or y >= height:
            continue
        for x in range(center_x - radius, center_x + radius + 1):
            if x < 0 or x >= width:
                continue
            claimed_cells.add((x, y))


def prepare_rgbd_hero_patch_depth_grid(
    *,
    depth_values: array,
    confidence_values: bytes | None,
    width: int,
    height: int,
) -> dict:
    total = width * height
    prepared = array("f", [0.0] * total)
    original_valid_count = 0
    accepted_low_confidence_count = 0
    invalid_depth_count = 0

    for index in range(total):
        depth = float(depth_values[index])
        if not math.isfinite(depth) or depth <= 0 or depth > RGBD_DEPTH_TRUNC_METERS:
            invalid_depth_count += 1
            continue

        prepared[index] = depth
        original_valid_count += 1
        if confidence_values is not None and int(confidence_values[index]) == 0:
            accepted_low_confidence_count += 1

    fill_updates_total = 0
    for _pass_index in range(RGBD_HERO_PATCH_HOLE_FILL_PASSES):
        updates: list[tuple[int, float]] = []
        for y in range(height):
            for x in range(width):
                index = y * width + x
                if prepared[index] > 0:
                    continue

                neighbor_depths: list[float] = []
                for offset_y in range(-RGBD_HERO_PATCH_HOLE_FILL_RADIUS, RGBD_HERO_PATCH_HOLE_FILL_RADIUS + 1):
                    ny = y + offset_y
                    if ny < 0 or ny >= height:
                        continue
                    for offset_x in range(-RGBD_HERO_PATCH_HOLE_FILL_RADIUS, RGBD_HERO_PATCH_HOLE_FILL_RADIUS + 1):
                        if offset_x == 0 and offset_y == 0:
                            continue
                        nx = x + offset_x
                        if nx < 0 or nx >= width:
                            continue
                        neighbor_depth = float(prepared[ny * width + nx])
                        if neighbor_depth > 0:
                            neighbor_depths.append(neighbor_depth)

                if not neighbor_depths:
                    continue
                updates.append((index, median_float(neighbor_depths)))

        if not updates:
            break

        for index, depth in updates:
            prepared[index] = float(depth)
        fill_updates_total += len(updates)

    remaining_invalid_count = sum(1 for value in prepared if float(value) <= 0)
    return {
        "depthValues": prepared,
        "stats": {
            "width": width,
            "height": height,
            "totalPixelCount": total,
            "originalValidDepthCount": original_valid_count,
            "originalValidDepthRatio": round(original_valid_count / total if total else 0, 4),
            "acceptedLowConfidenceDepthCount": accepted_low_confidence_count,
            "invalidDepthSampleCount": invalid_depth_count,
            "filledDepthHoleCount": fill_updates_total,
            "remainingInvalidDepthCount": remaining_invalid_count,
            "finalValidDepthCount": total - remaining_invalid_count,
            "finalValidDepthRatio": round((total - remaining_invalid_count) / total if total else 0, 4),
            "holeFillPasses": RGBD_HERO_PATCH_HOLE_FILL_PASSES,
            "holeFillRadius": RGBD_HERO_PATCH_HOLE_FILL_RADIUS,
            "confidenceThreshold": "accept_all_nonzero_depth_values",
        },
    }


def build_rgbd_hero_patch_textured_mesh(
    *,
    keyframe: dict,
    depth_frame: dict,
    projection_keyframe: ProjectionKeyframe,
    depth_values: array,
    width: int,
    height: int,
) -> dict:
    intrinsics = depth_frame.get("intrinsics") or []
    transform = depth_frame.get("cameraTransform") or []
    total_pixels = width * height
    sample_step = max(1, int(math.ceil(math.sqrt(total_pixels / RGBD_HERO_PATCH_TARGET_SAMPLES))))
    x_samples = sampled_depth_indices(width, sample_step)
    y_samples = sampled_depth_indices(height, sample_step)
    vertices: list[tuple[float, float, float]] = []
    uv_coordinates: list[tuple[float, float]] = []
    faces: list[tuple[int, int, int]] = []
    seen_faces: set[tuple[int, int, int]] = set()
    grid: list[list[int | None]] = []
    depth_grid: list[list[float]] = []
    invalid_depth_count = 0
    out_of_bounds_color_count = 0
    rejected_face_count = 0

    for source_y in y_samples:
        row: list[int | None] = []
        depth_row: list[float] = []
        for source_x in x_samples:
            source_index = source_y * width + source_x
            depth = float(depth_values[source_index])
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
            projection = project_world_point(world, projection_keyframe)
            if projection is None:
                out_of_bounds_color_count += 1
                row.append(None)
                depth_row.append(0)
                continue

            u, v, _projected_depth = projection
            row.append(len(vertices))
            depth_row.append(depth)
            vertices.append(world)
            uv_coordinates.append((
                clamp_float(u / max(projection_keyframe.width - 1, 1), 0.0, 1.0),
                1.0 - clamp_float(v / max(projection_keyframe.height - 1, 1), 0.0, 1.0),
            ))
        grid.append(row)
        depth_grid.append(depth_row)

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
                absolute_tolerance=RGBD_HERO_PATCH_FACE_ABSOLUTE_TOLERANCE_METERS,
                relative_tolerance=RGBD_HERO_PATCH_FACE_RELATIVE_TOLERANCE,
            )
            rejected_face_count += add_depth_mesh_face(
                vertices,
                faces,
                seen_faces,
                (top_right, bottom_left, bottom_right),
                (d_tr, d_bl, d_br),
                absolute_tolerance=RGBD_HERO_PATCH_FACE_ABSOLUTE_TOLERANCE_METERS,
                relative_tolerance=RGBD_HERO_PATCH_FACE_RELATIVE_TOLERANCE,
            )

    mesh = FusedMesh(vertices=vertices, faces=faces, stats={
        "geometrySource": "rgbd_hero_patch_depth_mesh",
        "vertexCount": len(vertices),
        "faceCount": len(faces),
        "selectedKeyframeId": keyframe.get("id"),
        "selectedDepthFrameId": depth_frame.get("id"),
        "sampleStep": sample_step,
        "sourceDepthResolution": [width, height],
        "targetSamples": RGBD_HERO_PATCH_TARGET_SAMPLES,
        "depthConnectionAbsoluteToleranceMeters": RGBD_HERO_PATCH_FACE_ABSOLUTE_TOLERANCE_METERS,
        "depthConnectionRelativeTolerance": RGBD_HERO_PATCH_FACE_RELATIVE_TOLERANCE,
        "textureRenderMesh": {
            "used": False,
            "reason": "Fast RGB-D hero patch is already the render mesh.",
        },
    })
    return {
        "mesh": mesh,
        "uvCoordinates": uv_coordinates,
        "stats": {
            "vertexCount": len(vertices),
            "faceCount": len(faces),
            "sampleStep": sample_step,
            "sampledColumnCount": len(x_samples),
            "sampledRowCount": len(y_samples),
            "invalidDepthSampleCount": invalid_depth_count,
            "outOfBoundsColorSampleCount": out_of_bounds_color_count,
            "rejectedFaceCount": rejected_face_count,
            "acceptedFaceCount": len(faces),
            "depthConnectionAbsoluteToleranceMeters": RGBD_HERO_PATCH_FACE_ABSOLUTE_TOLERANCE_METERS,
            "depthConnectionRelativeTolerance": RGBD_HERO_PATCH_FACE_RELATIVE_TOLERANCE,
        },
    }


def sampled_depth_indices(count: int, step: int) -> list[int]:
    indices = list(range(0, count, max(1, step)))
    if count > 0 and (not indices or indices[-1] != count - 1):
        indices.append(count - 1)
    return indices


def write_textured_mesh_obj_with_uvs(
    *,
    mesh: FusedMesh,
    uv_coordinates: list[tuple[float, float]],
    output_obj_path: Path,
    output_mtl_path: Path,
    object_name: str,
) -> None:
    lines = [
        "# LidarAI RGB-D hero patch textured mesh",
        "# Geometry comes from permissive RGB-D depth grids; texture uses remapped source RGB images.",
        f"mtllib {output_mtl_path.name}",
        f"o {object_name}",
        "usemtl LidarAI_RGBD_Hero_Patch",
    ]
    for x, y, z in mesh.vertices:
        lines.append(f"v {x:.6f} {y:.6f} {z:.6f}")
    for u, v in uv_coordinates:
        lines.append(f"vt {u:.8f} {v:.8f}")
    vertex_normals = compute_vertex_normals(mesh.vertices, mesh.faces)
    for x, y, z in vertex_normals:
        lines.append(f"vn {x:.6f} {y:.6f} {z:.6f}")
    for face in mesh.faces:
        a, b, c = (index + 1 for index in face)
        lines.append(f"f {a}/{a}/{a} {b}/{b}/{b} {c}/{c}/{c}")
    output_obj_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def build_rgbd_hero_patch_texture_diagnostics(
    *,
    keyframes: list[ProjectionKeyframe],
    selected_patches: list[dict],
    mesh: FusedMesh,
    uv_coordinates: list[tuple[float, float]],
    atlas_image: Image.Image,
    candidate_stats: list[dict],
    selection_stats: dict,
    skipped_patch_reasons: list[dict],
    profile: ProcessingProfile,
) -> dict:
    face_count = len(mesh.faces)
    primary_patch = selected_patches[0]
    selected_keyframe = primary_patch["keyframe"]
    selected_depth_frame = primary_patch["depthFrame"]
    prepared_depth_stats = primary_patch["preparedDepthStats"]
    mesh_stats = aggregate_rgbd_hero_patch_mesh_stats(selected_patches, mesh)
    selected_keyframe_ids = [patch["keyframe"].get("id") for patch in selected_patches if patch["keyframe"].get("id")]
    selected_depth_frame_ids = [patch["depthFrame"].get("id") for patch in selected_patches if patch["depthFrame"].get("id")]
    projected_pixel_count = sum(
        patch["rgbImage"].width * patch["rgbImage"].height
        for patch in selected_patches
    )
    fallback_pixel_count = max(atlas_image.width * atlas_image.height - projected_pixel_count, 0)
    final_valid_depth_count = sum(
        int(patch["preparedDepthStats"].get("finalValidDepthCount", 0))
        for patch in selected_patches
    )
    remaining_invalid_depth_count = sum(
        int(patch["preparedDepthStats"].get("remainingInvalidDepthCount", 0))
        for patch in selected_patches
    )
    total_depth_pixel_count = sum(
        int(patch["preparedDepthStats"].get("totalPixelCount", 0))
        for patch in selected_patches
    )
    return {
        "version": "v4_rgbd_hero_patch_multi_atlas",
        "uvStrategy": "rgbd_hero_patch_direct_image_uv",
        "renderMesh": mesh.stats.get("textureRenderMesh", {}),
        "processing": {
            "textureWorkerCount": 1,
            "parallelEnabled": False,
            "activeProjectionMode": "rgbd_hero_patch",
            "rgbdHeroPatchTexture": True,
            "denseSingleViewTexture": profile.dense_single_view_texture,
            "sourceKeyframeCount": len(keyframes),
            "activeTextureKeyframeCount": len(selected_patches),
            "confidenceThreshold": "accept_all_nonzero_depth_values",
            "holeFillPasses": RGBD_HERO_PATCH_HOLE_FILL_PASSES,
            "holeFillRadius": RGBD_HERO_PATCH_HOLE_FILL_RADIUS,
        },
        "geometry": texture_geometry_debug(mesh),
        "keyframes": projection_keyframe_debug_summaries(keyframes),
        "poseDelta": (
            projection_keyframe_pose_delta(keyframes[0], keyframes[1])
            if len(keyframes) >= 2
            else None
        ),
        "perKeyframeProjection": per_keyframe_mesh_projection_stats(mesh, keyframes),
        "rgbdHeroPatch": {
            "enabled": True,
            "strategy": "best_primary_plus_timed_supplemental_rgbd_patches_with_permissive_depth_hole_fill",
            "patchCount": len(selected_patches),
            "selectedKeyframeId": selected_keyframe.get("id"),
            "selectedDepthFrameId": selected_depth_frame.get("id"),
            "selectedKeyframeIds": selected_keyframe_ids,
            "selectedDepthFrameIds": selected_depth_frame_ids,
            "sourceDepthResolution": selected_depth_frame.get("depthResolution"),
            "sourceRgbResolution": [primary_patch["rgbImage"].width, primary_patch["rgbImage"].height],
            "atlasLayout": {
                "strategy": "vertical_stack_rgbd_hero_patch_source_images",
                "width": atlas_image.width,
                "height": atlas_image.height,
                "patches": [
                    {
                        "patchIndex": patch["patchIndex"],
                        "keyframeId": patch["keyframe"].get("id"),
                        "depthFrameId": patch["depthFrame"].get("id"),
                        "sourceRgbResolution": [patch["rgbImage"].width, patch["rgbImage"].height],
                        "sourceDepthResolution": patch["depthFrame"].get("depthResolution"),
                        "atlasRect": patch["atlasRect"],
                        "atlasUVBounds": patch["atlasUVBounds"],
                    }
                    for patch in selected_patches
                ],
            },
            "depthPreparation": prepared_depth_stats,
            "mesh": mesh_stats,
            "selection": selection_stats,
            "skippedPatches": skipped_patch_reasons,
            "patches": [
                rgbd_hero_patch_debug_summary(patch)
                for patch in selected_patches
            ],
            "candidateSummary": [
                {
                    key: candidate.get(key)
                    for key in [
                        "keyframeId",
                        "depthFrameId",
                        "colorKeyframeIdMatched",
                        "sourceTimestamp",
                        "timestampDeltaSeconds",
                        "validDepthRatio",
                        "highConfidenceRatio",
                        "rgbSharpnessScore",
                        "score",
                    ]
                }
                for candidate in candidate_stats[:8]
            ],
        },
        "objSyntax": {
            "faceCount": face_count,
            "faceWithUVIndexCount": face_count,
            "faceWithoutUVIndexCount": 0,
            "uvCoordinateCount": len(uv_coordinates),
            "expectedUVCoordinateCount": len(mesh.vertices),
            "normalCoordinateCount": len(mesh.vertices),
            "expectedNormalCoordinateCount": len(mesh.vertices),
            "invalidUVReferenceCount": 0,
        },
        "textureAtlas": {
            "width": atlas_image.width,
            "height": atlas_image.height,
            "maxSize": max(atlas_image.width, atlas_image.height),
            "tileSize": 0,
            "tilePadding": 0,
            "dilationPixels": 0,
            "fallbackColor": list(FALLBACK_COLOR),
            "unobservedColor": list(TEXTURE_UNOBSERVED_COLOR),
            "rasterizedPixelCount": atlas_image.width * atlas_image.height,
            "projectedPixelCount": projected_pixel_count,
            "fallbackPixelCount": fallback_pixel_count,
            "projectedRasterPixelRatio": round(
                projected_pixel_count / (atlas_image.width * atlas_image.height)
                if atlas_image.width and atlas_image.height
                else 0,
                4,
            ),
            "sampledPixels": sampled_texture_stats(atlas_image),
        },
        "projection": {
            "texturedFaceCount": face_count,
            "fallbackFaceCount": 0,
            "projectionCoverage": 1.0 if face_count else 0.0,
            "selectedKeyframeFaceCounts": [
                {"keyframe": patch["keyframe"].get("id"), "faceCount": patch.get("keptFaceCount", len(patch["mesh"].faces))}
                for patch in selected_patches
            ],
            "keyframeContributionCounts": [
                {
                    "keyframe": patch["keyframe"].get("id"),
                    "sampleCount": patch["rgbImage"].width * patch["rgbImage"].height,
                }
                for patch in selected_patches
            ],
            "singleSamplePixelCount": projected_pixel_count,
            "blendedPixelCount": 0,
            "acceptedProjectionSampleCount": projected_pixel_count,
            "meanSamplesPerProjectedPixel": 1.0,
            "faceRejectionReasons": {
                "overexposedSampleCount": 0,
                "underexposedSampleCount": 0,
                "edgeSampleCount": 0,
                "grazingSampleCount": 0,
                "invalidProjectionSampleCount": int(mesh_stats.get("outOfBoundsColorSampleCount", 0)),
                "occludedSampleCount": 0,
            },
            "depthVisibilityStats": {
                "depthTestedSampleCount": final_valid_depth_count,
                "missingDepthSampleCount": remaining_invalid_depth_count,
                "occludedSampleCount": 0,
                "depthVisibilityDecisionCount": total_depth_pixel_count,
            },
        },
        "colorCorrection": texture_color_correction_for_keyframes(keyframes),
        "hints": [
            "Fast photoreal output is one or two permissive RGB-D hero patches; raw LiDAR mesh remains available as fused_mesh.obj.",
        ],
    }


def aggregate_rgbd_hero_patch_mesh_stats(selected_patches: list[dict], mesh: FusedMesh) -> dict:
    return {
        "vertexCount": len(mesh.vertices),
        "faceCount": len(mesh.faces),
        "patchCount": len(selected_patches),
        "acceptedFaceCount": len(mesh.faces),
        "rejectedFaceCount": sum(
            int(patch["meshStats"].get("rejectedFaceCount", 0))
            for patch in selected_patches
        ),
        "ownershipCulledFaceCount": sum(
            int(patch.get("culledFaceCount", 0))
            for patch in selected_patches
        ),
        "ownershipKeptFaceCount": sum(
            int(patch.get("keptFaceCount", 0))
            for patch in selected_patches
        ),
        "sourcePatchFaceCount": sum(
            int(patch.get("sourceFaceCount", len(patch["mesh"].faces)))
            for patch in selected_patches
        ),
        "ownershipCull": mesh.stats.get("ownershipCull", {}),
        "invalidDepthSampleCount": sum(
            int(patch["meshStats"].get("invalidDepthSampleCount", 0))
            for patch in selected_patches
        ),
        "outOfBoundsColorSampleCount": sum(
            int(patch["meshStats"].get("outOfBoundsColorSampleCount", 0))
            for patch in selected_patches
        ),
        "targetSamplesPerPatch": RGBD_HERO_PATCH_TARGET_SAMPLES,
        "depthConnectionAbsoluteToleranceMeters": RGBD_HERO_PATCH_FACE_ABSOLUTE_TOLERANCE_METERS,
        "depthConnectionRelativeTolerance": RGBD_HERO_PATCH_FACE_RELATIVE_TOLERANCE,
    }


def rgbd_hero_patch_debug_summary(patch: dict) -> dict:
    keyframe = patch["keyframe"]
    depth_frame = patch["depthFrame"]
    candidate = patch["candidate"]
    rgb_image = patch["rgbImage"]
    return {
        "patchIndex": patch["patchIndex"],
        "candidateIndex": candidate.get("index"),
        "keyframeId": keyframe.get("id"),
        "depthFrameId": depth_frame.get("id"),
        "sourceTimestamp": candidate.get("sourceTimestamp"),
        "timestampDeltaSeconds": candidate.get("timestampDeltaSeconds"),
        "colorKeyframeIdMatched": candidate.get("colorKeyframeIdMatched"),
        "validDepthRatio": candidate.get("validDepthRatio"),
        "highConfidenceRatio": candidate.get("highConfidenceRatio"),
        "rgbSharpnessScore": candidate.get("rgbSharpnessScore"),
        "sourceRgbResolution": [rgb_image.width, rgb_image.height],
        "sourceDepthResolution": depth_frame.get("depthResolution"),
        "atlasRect": patch["atlasRect"],
        "atlasUVBounds": patch["atlasUVBounds"],
        "sourceFaceCount": patch.get("sourceFaceCount", len(patch["mesh"].faces)),
        "keptFaceCount": patch.get("keptFaceCount", len(patch["mesh"].faces)),
        "culledFaceCount": patch.get("culledFaceCount", 0),
        "ownershipCull": patch.get("ownershipCull", {}),
        "depthPreparation": patch["preparedDepthStats"],
        "mesh": patch["meshStats"],
    }


def write_rgbd_overlay_png(
    *,
    rgb_image: Image.Image,
    samples: list[dict],
    output_path: Path,
) -> dict:
    overlay = rgb_image.copy()
    draw = ImageDraw.Draw(overlay, "RGBA")
    if not samples:
        overlay.save(output_path)
        return {
            "path": output_path.name,
            "format": "png",
            "available": output_path.exists(),
            "drawnPointCount": 0,
        }

    depths = sorted(float(sample["depth"]) for sample in samples if math.isfinite(float(sample["depth"])))
    min_depth = percentile_sorted(depths, 0.05) if depths else 0.0
    max_depth = percentile_sorted(depths, 0.95) if depths else 1.0
    sample_step = max(1, math.ceil(len(samples) / RGBD_DIAGNOSTIC_OVERLAY_MAX_SAMPLES))
    radius = max(1, min(rgb_image.width, rgb_image.height) // 420)
    drawn = 0
    for sample in samples[::sample_step]:
        u, v, _depth = sample["projection"]
        confidence = sample.get("confidence")
        color = confidence_overlay_color(confidence) if confidence is not None else depth_overlay_color(sample["depth"], min_depth, max_depth)
        draw.ellipse(
            (u - radius, v - radius, u + radius, v + radius),
            fill=color,
        )
        drawn += 1
    overlay.save(output_path)
    return {
        "path": output_path.name,
        "format": "png",
        "available": output_path.exists(),
        "drawnPointCount": drawn,
        "sourcePointCount": len(samples),
        "overlaySampleStep": sample_step,
        "colorMode": "confidence" if any(sample.get("confidence") is not None for sample in samples) else "depth",
    }


def write_depth_visualization_png(
    *,
    depth_values: array,
    width: int,
    height: int,
    output_path: Path,
) -> dict:
    valid_depths = sorted(
        float(value)
        for value in depth_values
        if math.isfinite(float(value)) and 0 < float(value) <= RGBD_DEPTH_TRUNC_METERS
    )
    if valid_depths:
        min_depth = percentile_sorted(valid_depths, 0.05)
        max_depth = percentile_sorted(valid_depths, 0.95)
    else:
        min_depth = 0.0
        max_depth = 1.0
    image = Image.new("RGB", (width, height), (0, 0, 0))
    pixels = image.load()
    for y in range(height):
        for x in range(width):
            depth = float(depth_values[y * width + x])
            if math.isfinite(depth) and 0 < depth <= RGBD_DEPTH_TRUNC_METERS:
                pixels[x, y] = depth_rgb(depth, min_depth, max_depth)
    image.save(output_path)
    return {
        "path": output_path.name,
        "format": "png",
        "available": output_path.exists(),
        "minDepthMeters": round(valid_depths[0], 4) if valid_depths else None,
        "maxDepthMeters": round(valid_depths[-1], 4) if valid_depths else None,
        "normalization": "5th_to_95th_percentile_valid_depth",
    }


def write_confidence_visualization_png(
    *,
    confidence_values: bytes | None,
    width: int,
    height: int,
    output_path: Path,
) -> dict:
    if confidence_values is None:
        return {
            "path": output_path.name,
            "format": "png",
            "available": False,
            "reason": "No ARKit confidence map was uploaded for the selected depth frame.",
        }

    image = Image.new("RGB", (width, height), (0, 0, 0))
    pixels = image.load()
    for y in range(height):
        for x in range(width):
            confidence = int(confidence_values[y * width + x])
            if confidence <= 0:
                pixels[x, y] = (196, 52, 58)
            elif confidence == 1:
                pixels[x, y] = (238, 190, 72)
            else:
                pixels[x, y] = (74, 171, 102)
    image.save(output_path)
    return {
        "path": output_path.name,
        "format": "png",
        "available": output_path.exists(),
        "legend": {"0": "low/red", "1": "medium/yellow", "2": "high/green"},
    }


def expected_rgb_pixel_for_depth_sample(
    source_x: int,
    source_y: int,
    depth_frame: dict,
    keyframe: dict,
) -> tuple[float, float] | None:
    depth_intrinsics = depth_frame.get("intrinsics") or []
    rgb_intrinsics = keyframe.get("intrinsics") or []
    if len(depth_intrinsics) != 9 or len(rgb_intrinsics) != 9:
        return None
    depth_fx = float(depth_intrinsics[0])
    depth_fy = float(depth_intrinsics[4])
    if abs(depth_fx) < 1e-8 or abs(depth_fy) < 1e-8:
        return None
    ray_x = (source_x - float(depth_intrinsics[6])) / depth_fx
    ray_y = (float(depth_intrinsics[7]) - source_y) / depth_fy
    return (
        float(rgb_intrinsics[0]) * ray_x + float(rgb_intrinsics[6]),
        float(rgb_intrinsics[7]) - float(rgb_intrinsics[4]) * ray_y,
    )


def rgb_sharpness_score(image_path: Path) -> float:
    image = Image.open(image_path).convert("L")
    image.thumbnail((320, 320), Image.Resampling.BILINEAR)
    width, height = image.size
    if width < 2 or height < 2:
        return 0.0
    pixels = image.load()
    total = 0.0
    count = 0
    for y in range(0, height - 1, 2):
        for x in range(0, width - 1, 2):
            center = int(pixels[x, y])
            total += abs(center - int(pixels[x + 1, y]))
            total += abs(center - int(pixels[x, y + 1]))
            count += 2
    return round(total / count if count else 0.0, 4)


def confidence_histogram_for_values(confidence_values: bytes | None) -> dict[str, int]:
    histogram = {"0": 0, "1": 0, "2": 0, "other": 0}
    if confidence_values is None:
        return histogram
    for value in confidence_values:
        key = str(int(value))
        if key in histogram:
            histogram[key] += 1
        else:
            histogram["other"] += 1
    return histogram


def timestamp_delta_seconds(depth_frame: dict, keyframe: dict) -> float:
    try:
        return abs(float(depth_frame.get("timestamp")) - float(keyframe.get("timestamp")))
    except (TypeError, ValueError):
        return 0.0


def intrinsics_resolution_warnings(kind: str, intrinsics: list, resolution: list) -> list[str]:
    if len(intrinsics) != 9:
        return [f"{kind} intrinsics are missing or not a 3x3 matrix."]
    if len(resolution) != 2:
        return [f"{kind} resolution is missing."]
    width = float(resolution[0])
    height = float(resolution[1])
    fx = float(intrinsics[0])
    fy = float(intrinsics[4])
    cx = float(intrinsics[6])
    cy = float(intrinsics[7])
    warnings = []
    if fx <= 0 or fy <= 0:
        warnings.append(f"{kind} intrinsics have non-positive focal length.")
    if width > 0 and not (-width * 0.1 <= cx <= width * 1.1):
        warnings.append(f"{kind} principal point cx={cx:.2f} is outside the expected image width range.")
    if height > 0 and not (-height * 0.1 <= cy <= height * 1.1):
        warnings.append(f"{kind} principal point cy={cy:.2f} is outside the expected image height range.")
    return warnings


def confidence_overlay_color(confidence: int | None) -> tuple[int, int, int, int]:
    if confidence is None:
        return (70, 180, 255, 210)
    if confidence <= 0:
        return (226, 45, 65, 220)
    if confidence == 1:
        return (255, 205, 80, 220)
    return (62, 220, 120, 220)


def depth_overlay_color(depth: float, min_depth: float, max_depth: float) -> tuple[int, int, int, int]:
    r, g, b = depth_rgb(depth, min_depth, max_depth)
    return (r, g, b, 215)


def depth_rgb(depth: float, min_depth: float, max_depth: float) -> tuple[int, int, int]:
    denominator = max(max_depth - min_depth, 1e-6)
    t = clamp_float((depth - min_depth) / denominator, 0.0, 1.0)
    return (
        clamp_color(45 + 210 * t),
        clamp_color(220 - 130 * abs(t - 0.45)),
        clamp_color(255 - 215 * t),
    )


def median_sorted(values: list[float]) -> float:
    if not values:
        return 0.0
    mid = len(values) // 2
    if len(values) % 2:
        return float(values[mid])
    return (float(values[mid - 1]) + float(values[mid])) / 2


def percentile_sorted(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    if len(values) == 1:
        return float(values[0])
    position = clamp_float(percentile, 0.0, 1.0) * (len(values) - 1)
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return float(values[lower])
    weight = position - lower
    return float(values[lower]) * (1 - weight) + float(values[upper]) * weight


def dedupe_preserve_order(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def processing_profile_from_payload(payload: dict | ScanPayloadEnvelope | None) -> ProcessingProfile:
    raw_profile = None
    if isinstance(payload, ScanPayloadEnvelope):
        raw_profile = payload.processingProfile
    elif isinstance(payload, dict):
        raw_profile = payload.get("processingProfile")
    name = str(raw_profile or DEFAULT_PROCESSING_PROFILE).strip().lower()
    return PROCESSING_PROFILES.get(name) or PROCESSING_PROFILES.get(DEFAULT_PROCESSING_PROFILE) or PROCESSING_PROFILES["full_quality"]


def build_capture_data_validation(payload: dict | ScanPayloadEnvelope) -> dict:
    if isinstance(payload, ScanPayloadEnvelope):
        data = payload.model_dump(mode="json")
    else:
        data = payload

    mesh_anchors = data.get("meshAnchors") or []
    keyframes = data.get("images") or []
    depth_frames = data.get("depthFrames") or []
    keyframe_optional_fields = [
        "originalImageResolution",
        "imageOrientation",
        "intrinsicsReferenceResolution",
        "trackingState",
        "trackingReason",
        "exposureDurationSeconds",
        "iso",
        "ambientIntensity",
        "ambientColorTemperature",
        "sharpnessScore",
    ]
    depth_optional_fields = ["confidenceBase64", "confidenceFormat", "metersPerUnit"]
    missing_keyframe_fields = {
        field_name: sum(1 for keyframe in keyframes if keyframe.get(field_name) is None)
        for field_name in keyframe_optional_fields
    }
    missing_depth_fields = {
        field_name: sum(1 for frame in depth_frames if frame.get(field_name) is None)
        for field_name in depth_optional_fields
    }
    invalid_keyframe_projection_records = [
        {
            "index": index,
            "id": keyframe.get("id"),
            "reason": validation["reason"],
        }
        for index, keyframe in enumerate(keyframes)
        if not (validation := validate_keyframe_projection_metadata(keyframe))["valid"]
    ]
    mesh_anchor_field_counts = {
        "transform": sum(1 for anchor in mesh_anchors if len(anchor.get("transform") or []) == 16),
        "vertices": sum(1 for anchor in mesh_anchors if bool(anchor.get("vertices"))),
        "triangleIndices": sum(1 for anchor in mesh_anchors if bool(anchor.get("triangleIndices"))),
        "normals": sum(1 for anchor in mesh_anchors if bool(anchor.get("normals"))),
        "classifications": sum(1 for anchor in mesh_anchors if bool(anchor.get("classifications"))),
    }
    warnings = []
    if missing_keyframe_fields.get("originalImageResolution") == len(keyframes) and keyframes:
        warnings.append("RGB original/native image resolution is missing from all keyframes.")
    if missing_keyframe_fields.get("imageOrientation") == len(keyframes) and keyframes:
        warnings.append("RGB image orientation is missing from all keyframes.")
    if missing_keyframe_fields.get("trackingState") == len(keyframes) and keyframes:
        warnings.append("ARKit tracking state is missing from all keyframes.")
    if mesh_anchor_field_counts["normals"] == 0 and mesh_anchors:
        warnings.append("Mesh normals are not uploaded; backend recomputes normals from fused faces.")
    if invalid_keyframe_projection_records:
        warnings.append("Some keyframes have invalid transform, intrinsics, or image-resolution metadata.")

    return {
        "version": "v1",
        "coordinateConventions": {
            "matrixStorage": "column_major_4x4",
            "cameraTransform": "ARKit camera-to-world transform",
            "meshAnchorTransform": "ARKit mesh-anchor local-to-world transform",
            "worldToCamera": "rigid inverse of cameraTransform",
            "cameraForward": "ARKit camera looks down local -Z",
            "projection": "depth=-cameraZ; u=fx*x/depth+cx; v=cy-fy*y/depth",
            "units": "meters in ARKit world space",
            "rgbImageOrientationAssumption": "intrinsics are expected to match the encoded JPEG pixel orientation and resolution",
        },
        "mesh": {
            "anchorCount": len(mesh_anchors),
            "fieldCounts": mesh_anchor_field_counts,
            "savesVerticesAndFaces": mesh_anchor_field_counts["vertices"] == len(mesh_anchors)
            and mesh_anchor_field_counts["triangleIndices"] == len(mesh_anchors),
            "savesAnchorTransforms": mesh_anchor_field_counts["transform"] == len(mesh_anchors),
            "savesNormals": mesh_anchor_field_counts["normals"] == len(mesh_anchors) and bool(mesh_anchors),
            "savesClassifications": mesh_anchor_field_counts["classifications"] == len(mesh_anchors) and bool(mesh_anchors),
        },
        "keyframes": {
            "count": len(keyframes),
            "requiredFieldCounts": {
                "cameraTransform": sum(1 for item in keyframes if len(item.get("cameraTransform") or []) == 16),
                "intrinsics": sum(1 for item in keyframes if len(item.get("intrinsics") or []) == 9),
                "imageResolution": sum(1 for item in keyframes if len(item.get("imageResolution") or []) == 2),
                "jpegBase64": sum(1 for item in keyframes if bool(item.get("jpegBase64"))),
                "timestamp": sum(1 for item in keyframes if item.get("timestamp") is not None),
            },
            "missingOptionalFieldCounts": missing_keyframe_fields,
            "invalidProjectionMetadata": invalid_keyframe_projection_records[:80],
        },
        "depthFrames": {
            "count": len(depth_frames),
            "requiredFieldCounts": {
                "cameraTransform": sum(1 for item in depth_frames if len(item.get("cameraTransform") or []) == 16),
                "intrinsics": sum(1 for item in depth_frames if len(item.get("intrinsics") or []) == 9),
                "depthResolution": sum(1 for item in depth_frames if len(item.get("depthResolution") or []) == 2),
                "depthBase64": sum(1 for item in depth_frames if bool(item.get("depthBase64"))),
                "timestamp": sum(1 for item in depth_frames if item.get("timestamp") is not None),
            },
            "missingOptionalFieldCounts": missing_depth_fields,
        },
        "warnings": warnings,
    }


def write_processing_profile(profile: ProcessingProfile, output_path: Path) -> None:
    output_path.write_text(json.dumps(processing_profile_stats(profile), indent=2), encoding="utf-8")


def processing_profile_stats(profile: ProcessingProfile) -> dict:
    return {
        "name": profile.name,
        "maxKeyframes": profile.max_keyframes,
        "maxDepthFrames": profile.max_depth_frames,
        "maxRgbdFrames": profile.max_rgbd_frames,
        "useRgbdGeometry": profile.use_rgbd_geometry,
        "writeVertexColoredDebug": profile.write_vertex_colored_debug,
        "writeTextureDebugPreview": profile.write_texture_debug_preview,
        "textureRenderTargetFaces": profile.texture_render_target_faces,
        "textureTsdfRenderTargetFaces": profile.texture_tsdf_render_target_faces,
        "planarChartRasterStride": profile.planar_chart_raster_stride,
        "planarChartProjectionMode": profile.planar_chart_projection_mode,
        "fallbackTextureFaceLimit": profile.fallback_texture_face_limit,
        "singleFrameDiagnostic": profile.single_frame_diagnostic,
        "preserveTextureRenderMesh": profile.preserve_texture_render_mesh,
        "densifyTextureRenderMesh": profile.densify_texture_render_mesh,
        "denseSingleViewTexture": profile.dense_single_view_texture,
        "rgbdHeroPatchTexture": profile.rgbd_hero_patch_texture,
        "rgbdOnboardingMesh": profile.rgbd_onboarding_mesh,
    }


def read_processing_profile(work_dir: Path) -> ProcessingProfile:
    profile_path = work_dir / "processing_profile.json"
    if not profile_path.exists():
        return PROCESSING_PROFILES["full_quality"]
    try:
        data = json.loads(profile_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return PROCESSING_PROFILES["full_quality"]
    return PROCESSING_PROFILES.get(str(data.get("name", "")).lower(), PROCESSING_PROFILES["full_quality"])


def current_full_quality_profile() -> ProcessingProfile:
    return replace(
        PROCESSING_PROFILES["full_quality"],
        texture_render_target_faces=TEXTURE_RENDER_TARGET_FACE_COUNT,
        texture_tsdf_render_target_faces=TEXTURE_TSDF_RENDER_TARGET_FACE_COUNT,
    )


def select_keyframes_for_profile(
    keyframes: list[dict],
    profile: ProcessingProfile,
    *,
    depth_frames: list[dict] | None = None,
    mesh: FusedMesh | None = None,
) -> tuple[list[dict], dict]:
    if is_fast_rgbd_hero_patch_profile(profile):
        selected_pairs, pair_stats = select_rgbd_payload_candidate_pool_for_fast_profile(
            keyframes,
            depth_frames or [],
            limit=RGBD_HERO_PATCH_CANDIDATE_POOL_SIZE,
        )
        if selected_pairs:
            selected = [pair["keyframe"] for pair in selected_pairs]
            selected_ids = [item.get("id") for item in selected if item.get("id")]
            return selected, {
                "strategy": "rgbd_hero_patch_candidate_pool",
                "profile": profile.name,
                "originalKeyframeCount": len(keyframes),
                "selectedKeyframeCount": len(selected),
                "selectedKeyframeIds": selected_ids,
                **pair_stats,
            }

        selected = center_biased_items(keyframes, limit=profile.max_keyframes)
        selected_ids = [item.get("id") for item in selected if item.get("id")]
        return selected, {
            "strategy": "rgbd_hero_patch_candidate_pool_fallback",
            "profile": profile.name,
            "originalKeyframeCount": len(keyframes),
            "selectedKeyframeCount": len(selected),
            "selectedKeyframeIds": selected_ids,
                **pair_stats,
            }

    if profile.max_keyframes is None:
        selected = [
            keyframe for keyframe in keyframes
            if validate_keyframe_projection_metadata(keyframe)["valid"] and keyframe.get("jpegBase64")
        ]
        if selected:
            selected_ids = [item.get("id") for item in selected if item.get("id")]
            return selected, {
                "strategy": "all_uploaded_keyframes_coverage_first",
                "profile": profile.name,
                "originalKeyframeCount": len(keyframes),
                "selectedKeyframeCount": len(selected),
                "selectedKeyframeIds": selected_ids,
                "rejectedKeyframeCount": len(keyframes) - len(selected),
                "reason": "Profile has no maxKeyframes cap; texture coverage takes priority over adaptive pruning.",
            }

    if mesh is not None and mesh.faces:
        selected, adaptive_stats = select_adaptive_texture_keyframes(keyframes, mesh, profile=profile)
        if selected:
            selected_ids = [item.get("id") for item in selected if item.get("id")]
            return selected, {
                "strategy": "adaptive_mesh_coverage_quality",
                "profile": profile.name,
                "originalKeyframeCount": len(keyframes),
                "selectedKeyframeCount": len(selected),
                "selectedKeyframeIds": selected_ids,
                **adaptive_stats,
            }

    selected = pose_diverse_items(
        keyframes,
        limit=profile.max_keyframes,
        transform_getter=lambda item: item.get("cameraTransform") or [],
        timestamp_getter=lambda item: item.get("timestamp"),
    )
    selected_ids = [item.get("id") for item in selected if item.get("id")]
    return selected, {
        "strategy": "pose_diverse_backend_subset" if len(selected) < len(keyframes) else "all_uploaded_keyframes",
        "profile": profile.name,
        "originalKeyframeCount": len(keyframes),
        "selectedKeyframeCount": len(selected),
        "selectedKeyframeIds": selected_ids,
        "fallbackReason": "adaptive_mesh_selection_unavailable" if mesh is None or not mesh.faces else None,
    }


def select_adaptive_texture_keyframes(
    keyframes: list[dict],
    mesh: FusedMesh,
    *,
    profile: ProcessingProfile,
) -> tuple[list[dict], dict]:
    if not keyframes or not mesh.faces:
        return [], {
            "adaptiveSelectionAvailable": False,
            "fallbackReason": "missing_keyframes_or_mesh_faces",
        }

    samples = adaptive_mesh_surface_samples(mesh, max_samples=ADAPTIVE_KEYFRAME_PROXY_FACE_SAMPLES)
    if not samples:
        return [], {
            "adaptiveSelectionAvailable": False,
            "fallbackReason": "mesh_proxy_sampling_empty",
        }

    quality_stats_by_index = keyframe_image_quality_stats(keyframes)
    frame_records: list[dict] = []
    rejected: list[dict] = []
    for index, keyframe in enumerate(keyframes):
        record = adaptive_keyframe_record(
            keyframe,
            index=index,
            samples=samples,
            image_quality=quality_stats_by_index.get(index, {}),
        )
        if record.get("usable"):
            frame_records.append(record)
        else:
            rejected.append({
                "index": index,
                "id": keyframe.get("id"),
                "reason": record.get("rejectionReason", "unusable_keyframe"),
            })

    if not frame_records:
        return [], {
            "adaptiveSelectionAvailable": False,
            "fallbackReason": "no_keyframes_project_mesh_proxy",
            "proxySampleCount": len(samples),
            "rejectedKeyframes": rejected[:80],
        }

    total_proxy_area = max(sum(sample["area"] for sample in samples), 1e-9)
    total_potential = max(
        sum(max(record["sampleScores"].get(sample["sampleIndex"], 0.0) for record in frame_records) for sample in samples),
        1e-9,
    )
    selected_records: list[dict] = []
    selected_indices: set[int] = set()
    selected_sample_quality = {sample["sampleIndex"]: 0.0 for sample in samples}
    selected_sample_observed = {sample["sampleIndex"]: False for sample in samples}
    iterations: list[dict] = []

    while len(selected_indices) < len(frame_records):
        best_record: dict | None = None
        best_metrics: dict | None = None
        for record in frame_records:
            if int(record["index"]) in selected_indices:
                continue
            if is_redundant_adaptive_pose(record, selected_records):
                continue
            metrics = adaptive_keyframe_marginal_metrics(
                record,
                selected_sample_quality,
                selected_sample_observed,
                total_proxy_area=total_proxy_area,
                total_potential=total_potential,
            )
            if best_metrics is None or metrics["marginalBenefit"] > best_metrics["marginalBenefit"]:
                best_record = record
                best_metrics = metrics

        if best_record is None or best_metrics is None:
            break

        should_continue = (
            not selected_records
            or best_metrics["marginalBenefitRatio"] >= ADAPTIVE_KEYFRAME_MIN_MARGINAL_BENEFIT_RATIO
            or (
                best_metrics["newCoverageRatio"] >= ADAPTIVE_KEYFRAME_MIN_NEW_COVERAGE_RATIO
                and best_metrics["improvedSampleCount"] > 0
            )
        )
        if not should_continue:
            rejected.append({
                "index": best_record.get("index"),
                "id": best_record.get("id"),
                "reason": "below_marginal_benefit_threshold",
                "marginalBenefitRatio": round(best_metrics["marginalBenefitRatio"], 6),
                "newCoverageRatio": round(best_metrics["newCoverageRatio"], 6),
            })
            break

        selected_records.append(best_record)
        selected_indices.add(int(best_record["index"]))
        for sample_index, quality in best_record["sampleScores"].items():
            if quality > selected_sample_quality.get(sample_index, 0.0):
                selected_sample_quality[sample_index] = quality
            selected_sample_observed[sample_index] = True
        iterations.append({
            "iteration": len(selected_records),
            "index": best_record["index"],
            "id": best_record.get("id"),
            "visibleSampleCount": best_record["visibleSampleCount"],
            "visibleCoverageRatio": round(best_record["visibleArea"] / total_proxy_area, 5),
            "marginalBenefitRatio": round(best_metrics["marginalBenefitRatio"], 6),
            "newCoverageRatio": round(best_metrics["newCoverageRatio"], 6),
            "improvedSampleCount": best_metrics["improvedSampleCount"],
        })

    selected = [keyframes[int(record["index"])] for record in selected_records]
    selected_coverage_area = sum(
        sample["area"]
        for sample in samples
        if selected_sample_observed.get(sample["sampleIndex"], False)
    )
    for record in frame_records:
        if int(record["index"]) not in selected_indices:
            rejected.append({
                "index": record.get("index"),
                "id": record.get("id"),
                "reason": "not_selected_after_greedy_optimization",
                "visibleSampleCount": record.get("visibleSampleCount"),
                "visibleCoverageRatio": round(record.get("visibleArea", 0.0) / total_proxy_area, 5),
            })

    return selected, {
        "adaptiveSelectionAvailable": True,
        "algorithm": "greedy_proxy_mesh_coverage_quality",
        "proxySampleCount": len(samples),
        "proxyFaceStride": adaptive_proxy_face_stride(mesh, ADAPTIVE_KEYFRAME_PROXY_FACE_SAMPLES),
        "candidateKeyframeCount": len(frame_records),
        "rejectedKeyframeCount": len(rejected),
        "selectedCoverageRatio": round(selected_coverage_area / total_proxy_area, 5),
        "selectedQualityPotentialRatio": round(sum(selected_sample_quality.values()) / total_potential, 5),
        "minMarginalBenefitRatio": ADAPTIVE_KEYFRAME_MIN_MARGINAL_BENEFIT_RATIO,
        "minNewCoverageRatio": ADAPTIVE_KEYFRAME_MIN_NEW_COVERAGE_RATIO,
        "iterations": iterations,
        "candidateSummary": [
            adaptive_keyframe_record_summary(record, total_proxy_area)
            for record in sorted(frame_records, key=lambda item: item["qualitySum"], reverse=True)[:80]
        ],
        "rejectedKeyframes": rejected[:160],
    }


def adaptive_proxy_face_stride(mesh: FusedMesh, max_samples: int) -> int:
    return max(1, math.ceil(len(mesh.faces) / max(1, max_samples)))


def adaptive_mesh_surface_samples(mesh: FusedMesh, max_samples: int) -> list[dict]:
    stride = adaptive_proxy_face_stride(mesh, max_samples)
    samples: list[dict] = []
    for sample_index, face_index in enumerate(range(0, len(mesh.faces), stride)):
        face = mesh.faces[face_index]
        if not valid_triangle_indices(face, len(mesh.vertices)):
            continue
        vertices = [mesh.vertices[face[0]], mesh.vertices[face[1]], mesh.vertices[face[2]]]
        area = triangle_area(vertices[0], vertices[1], vertices[2])
        if area <= 1e-10:
            continue
        samples.append({
            "sampleIndex": sample_index,
            "faceIndex": face_index,
            "center": triangle_center(vertices[0], vertices[1], vertices[2]),
            "normal": triangle_normal(vertices[0], vertices[1], vertices[2]),
            "area": area * stride,
        })
    return samples


def keyframe_image_quality_stats(keyframes: list[dict]) -> dict[int, dict]:
    return {
        index: estimate_keyframe_image_quality(keyframe)
        for index, keyframe in enumerate(keyframes)
    }


def estimate_keyframe_image_quality(keyframe: dict, thumbnail_max: int = 112) -> dict:
    raw_base64 = keyframe.get("jpegBase64")
    if not raw_base64:
        return {
            "available": False,
            "sharpnessScore": safe_float(keyframe.get("sharpnessScore")) or 0.75,
            "exposureScore": 0.75,
            "meanLuminance": None,
            "overexposedRatio": None,
            "underexposedRatio": None,
        }

    try:
        image_bytes = base64.b64decode(raw_base64, validate=True)
        image = Image.open(BytesIO(image_bytes)).convert("L")
        image.thumbnail((thumbnail_max, thumbnail_max), Image.Resampling.BILINEAR)
        width, height = image.size
        values = list(image.getdata())
    except Exception:
        return {
            "available": False,
            "sharpnessScore": safe_float(keyframe.get("sharpnessScore")) or 0.65,
            "exposureScore": 0.55,
            "meanLuminance": None,
            "overexposedRatio": None,
            "underexposedRatio": None,
        }

    if not values or width < 3 or height < 3:
        return {
            "available": False,
            "sharpnessScore": 0.55,
            "exposureScore": 0.55,
            "meanLuminance": None,
            "overexposedRatio": None,
            "underexposedRatio": None,
        }

    mean_luminance = sum(values) / len(values)
    overexposed_ratio = sum(1 for value in values if value >= 248) / len(values)
    underexposed_ratio = sum(1 for value in values if value <= 6) / len(values)
    laplacian_sum = 0.0
    laplacian_count = 0
    for y in range(1, height - 1, 2):
        row = y * width
        for x in range(1, width - 1, 2):
            center = values[row + x]
            response = (
                4 * center
                - values[row + x - 1]
                - values[row + x + 1]
                - values[row - width + x]
                - values[row + width + x]
            )
            laplacian_sum += abs(response)
            laplacian_count += 1
    laplacian_mean = laplacian_sum / max(laplacian_count, 1)
    sharpness_score = clamp_float(laplacian_mean / 18.0, 0.35, 1.35)
    exposure_score = clamp_float(
        1.0
        - (abs(mean_luminance - 128.0) / 170.0)
        - overexposed_ratio * 0.75
        - underexposed_ratio * 0.9,
        0.15,
        1.0,
    )
    return {
        "available": True,
        "sharpnessScore": round(sharpness_score, 4),
        "exposureScore": round(exposure_score, 4),
        "meanLuminance": round(mean_luminance, 2),
        "overexposedRatio": round(overexposed_ratio, 4),
        "underexposedRatio": round(underexposed_ratio, 4),
    }


def adaptive_keyframe_record(
    keyframe: dict,
    *,
    index: int,
    samples: list[dict],
    image_quality: dict,
) -> dict:
    transform = keyframe.get("cameraTransform") or []
    intrinsics = keyframe.get("intrinsics") or []
    resolution = keyframe.get("imageResolution") or []
    validation = validate_keyframe_projection_metadata(keyframe)
    if not validation["valid"]:
        return {
            "usable": False,
            "index": index,
            "id": keyframe.get("id"),
            "rejectionReason": validation["reason"],
        }

    width, height = int(resolution[0]), int(resolution[1])
    fx = float(intrinsics[0])
    fy = float(intrinsics[4])
    world_to_camera = invert_rigid_transform(transform)
    camera_position = (float(transform[12]), float(transform[13]), float(transform[14]))
    pose_confidence = tracking_pose_confidence(keyframe)
    sharpness_score = float(image_quality.get("sharpnessScore") or safe_float(keyframe.get("sharpnessScore")) or 0.75)
    exposure_score = float(image_quality.get("exposureScore") or 0.75)
    sample_scores: dict[int, float] = {}
    visible_area = 0.0
    quality_sum = 0.0
    edge_reject_count = 0
    grazing_reject_count = 0
    invalid_projection_count = 0

    for sample in samples:
        projection = project_world_point_values(sample["center"], world_to_camera, intrinsics, width, height)
        if projection is None:
            invalid_projection_count += 1
            continue
        u, v, depth = projection
        edge_margin = min(u, v, width - u, height - v)
        edge_threshold = max(2.0, min(width, height) * TEXTURE_BLEND_EDGE_MARGIN_RATIO)
        if edge_margin < edge_threshold:
            edge_reject_count += 1
            continue

        view_vector = normalize(subtract(camera_position, sample["center"]))
        facing = abs(dot(sample["normal"], view_vector)) if sample["normal"] != (0.0, 0.0, 0.0) else 0.25
        if facing < TEXTURE_BLEND_MIN_FACING:
            grazing_reject_count += 1
            continue

        pixel_density = math.sqrt(max(fx * fy, 1e-6)) / max(depth, 0.2)
        resolution_score = clamp_float(pixel_density / 450.0, 0.2, 2.75)
        angle_score = clamp_float((facing - TEXTURE_BLEND_MIN_FACING) / (1.0 - TEXTURE_BLEND_MIN_FACING), 0.05, 1.0)
        edge_score = clamp_float(edge_margin / max(min(width, height) * 0.22, 1.0), 0.08, 1.0)
        area = float(sample["area"])
        quality = (
            area
            * resolution_score
            * angle_score
            * edge_score
            * clamp_float(sharpness_score, 0.25, 1.35)
            * clamp_float(exposure_score, 0.15, 1.0)
            * pose_confidence
        )
        if quality <= 0:
            continue
        sample_scores[int(sample["sampleIndex"])] = quality
        visible_area += area
        quality_sum += quality

    visible_sample_count = len(sample_scores)
    visible_ratio = visible_sample_count / max(len(samples), 1)
    if visible_ratio < ADAPTIVE_KEYFRAME_MIN_VISIBLE_SAMPLE_RATIO:
        return {
            "usable": False,
            "index": index,
            "id": keyframe.get("id"),
            "rejectionReason": "insufficient_projected_mesh_coverage",
            "visibleSampleCount": visible_sample_count,
            "visibleSampleRatio": round(visible_ratio, 5),
        }

    return {
        "usable": True,
        "index": index,
        "id": keyframe.get("id"),
        "timestamp": safe_float(keyframe.get("timestamp")),
        "cameraPosition": camera_position,
        "cameraForward": normalize((-transform[8], -transform[9], -transform[10])),
        "sampleScores": sample_scores,
        "visibleSampleCount": visible_sample_count,
        "visibleArea": visible_area,
        "qualitySum": quality_sum,
        "poseConfidence": pose_confidence,
        "sharpnessScore": round(sharpness_score, 4),
        "exposureScore": round(exposure_score, 4),
        "imageQuality": image_quality,
        "rejectCounts": {
            "invalidProjection": invalid_projection_count,
            "edge": edge_reject_count,
            "grazing": grazing_reject_count,
        },
    }


def validate_keyframe_projection_metadata(keyframe: dict) -> dict:
    transform = keyframe.get("cameraTransform") or []
    intrinsics = keyframe.get("intrinsics") or []
    resolution = keyframe.get("imageResolution") or []
    if len(transform) != 16 or not all(isinstance(value, (int, float)) and math.isfinite(float(value)) for value in transform):
        return {"valid": False, "reason": "invalid_camera_transform"}
    if len(intrinsics) != 9 or not all(isinstance(value, (int, float)) and math.isfinite(float(value)) for value in intrinsics):
        return {"valid": False, "reason": "invalid_camera_intrinsics"}
    if float(intrinsics[0]) <= 0 or float(intrinsics[4]) <= 0:
        return {"valid": False, "reason": "non_positive_focal_length"}
    if len(resolution) != 2:
        return {"valid": False, "reason": "missing_image_resolution"}
    width, height = int(resolution[0]), int(resolution[1])
    if width <= 0 or height <= 0:
        return {"valid": False, "reason": "invalid_image_resolution"}
    if not (0 <= float(intrinsics[6]) <= max(width, 1) * 1.25 and 0 <= float(intrinsics[7]) <= max(height, 1) * 1.25):
        return {"valid": False, "reason": "principal_point_outside_expected_bounds"}
    return {"valid": True, "reason": None}


def tracking_pose_confidence(keyframe: dict) -> float:
    state = str(keyframe.get("trackingState") or "normal").lower()
    reason = str(keyframe.get("trackingReason") or "").lower()
    if state == "normal":
        return 1.0
    if state == "limited":
        if "excessivemotion" in reason:
            return 0.35
        if "insufficientfeatures" in reason:
            return 0.55
        return 0.65
    if state == "notavailable":
        return 0.15
    return 0.8


def project_world_point_values(
    vertex: tuple[float, float, float],
    world_to_camera: list[float],
    intrinsics: list[float],
    width: int,
    height: int,
) -> tuple[float, float, float] | None:
    matrix = tuple(float(value) for value in world_to_camera[:16])
    x = matrix[0] * vertex[0] + matrix[4] * vertex[1] + matrix[8] * vertex[2] + matrix[12]
    y = matrix[1] * vertex[0] + matrix[5] * vertex[1] + matrix[9] * vertex[2] + matrix[13]
    z = matrix[2] * vertex[0] + matrix[6] * vertex[1] + matrix[10] * vertex[2] + matrix[14]
    depth = -z
    if depth <= 1e-5:
        return None
    fx = float(intrinsics[0])
    fy = float(intrinsics[4])
    cx = float(intrinsics[6])
    cy = float(intrinsics[7])
    u = fx * (x / depth) + cx
    v = cy - fy * (y / depth)
    if not (0 <= u < width and 0 <= v < height):
        return None
    return u, v, depth


def adaptive_keyframe_marginal_metrics(
    record: dict,
    selected_sample_quality: dict[int, float],
    selected_sample_observed: dict[int, bool],
    *,
    total_proxy_area: float,
    total_potential: float,
) -> dict:
    marginal_benefit = 0.0
    new_coverage_area = 0.0
    improved_sample_count = 0
    for sample_index, quality in record["sampleScores"].items():
        previous_quality = selected_sample_quality.get(sample_index, 0.0)
        if quality > previous_quality + ADAPTIVE_KEYFRAME_QUALITY_GAIN_EPSILON:
            marginal_benefit += quality - previous_quality
            improved_sample_count += 1
        if not selected_sample_observed.get(sample_index, False):
            new_coverage_area += quality

    return {
        "marginalBenefit": marginal_benefit,
        "marginalBenefitRatio": marginal_benefit / total_potential,
        "newCoverageRatio": new_coverage_area / total_proxy_area,
        "improvedSampleCount": improved_sample_count,
    }


def is_redundant_adaptive_pose(record: dict, selected_records: list[dict]) -> bool:
    position = record.get("cameraPosition") or (0.0, 0.0, 0.0)
    forward = record.get("cameraForward") or (0.0, 0.0, -1.0)
    for selected in selected_records:
        selected_position = selected.get("cameraPosition") or (0.0, 0.0, 0.0)
        selected_forward = selected.get("cameraForward") or (0.0, 0.0, -1.0)
        translation = length(subtract(position, selected_position))
        angle = math.degrees(math.acos(clamp_float(dot(forward, selected_forward), -1.0, 1.0)))
        if translation <= ADAPTIVE_KEYFRAME_POSE_DEDUPE_TRANSLATION_METERS and angle <= ADAPTIVE_KEYFRAME_POSE_DEDUPE_ANGLE_DEGREES:
            return True
    return False


def adaptive_keyframe_record_summary(record: dict, total_proxy_area: float) -> dict:
    return {
        "index": record.get("index"),
        "id": record.get("id"),
        "visibleSampleCount": record.get("visibleSampleCount"),
        "visibleCoverageRatio": round(float(record.get("visibleArea") or 0.0) / max(total_proxy_area, 1e-9), 5),
        "qualitySum": round(float(record.get("qualitySum") or 0.0), 5),
        "poseConfidence": record.get("poseConfidence"),
        "sharpnessScore": record.get("sharpnessScore"),
        "exposureScore": record.get("exposureScore"),
        "rejectCounts": record.get("rejectCounts"),
    }


def select_depth_frames_for_profile(
    depth_frames: list[dict],
    selected_keyframes: list[dict],
    profile: ProcessingProfile,
) -> tuple[list[dict], dict]:
    selected_keyframe_ids = [str(keyframe.get("id")) for keyframe in selected_keyframes if keyframe.get("id")]
    selected_keyframe_id_set = set(selected_keyframe_ids)
    paired = [
        frame for frame in depth_frames
        if frame.get("colorKeyframeId") and str(frame.get("colorKeyframeId")) in selected_keyframe_id_set
    ]
    if is_fast_rgbd_hero_patch_profile(profile):
        selected = best_depth_frame_per_selected_keyframe(selected_keyframes, paired)
        selected_ids = [item.get("id") for item in selected if item.get("id")]
        fallback_reason = None
        if not selected:
            selected = center_biased_items(depth_frames, limit=profile.max_depth_frames)
            selected_ids = [item.get("id") for item in selected if item.get("id")]
            fallback_reason = "no_depth_frames_matching_selected_keyframes"
        elif len(selected) < min(len(selected_keyframes), profile.max_depth_frames or len(selected_keyframes)):
            fallback_reason = "fewer_paired_depth_frames_than_selected_keyframes"
        return selected, {
            "strategy": "rgbd_hero_patch_candidate_pool" if fallback_reason is None else "rgbd_hero_patch_candidate_pool_fallback",
            "profile": profile.name,
            "originalDepthFrameCount": len(depth_frames),
            "pairedDepthFrameCount": len(paired),
            "geometryDepthSelection": "selected_keyframe_pairs" if fallback_reason != "no_depth_frames_matching_selected_keyframes" else "fallback_center_biased_depth_frames",
            "selectedDepthFrameCount": len(selected),
            "selectedDepthFrameIds": selected_ids,
            "selectedKeyframeIds": selected_keyframe_ids,
            "fallbackReason": fallback_reason,
        }

    candidates = depth_frames if profile.use_rgbd_geometry else (paired or depth_frames)
    selected = pose_diverse_items(
        candidates,
        limit=profile.max_depth_frames,
        transform_getter=lambda item: item.get("cameraTransform") or [],
        timestamp_getter=lambda item: item.get("timestamp"),
    )
    selected_ids = [item.get("id") for item in selected if item.get("id")]
    selected_from_all_depth = candidates is depth_frames
    return selected, {
        "strategy": (
            "depth_pose_diverse_backend_subset"
            if selected_from_all_depth and len(selected) < len(depth_frames)
            else "paired_pose_diverse_backend_subset"
            if len(selected) < len(depth_frames)
            else "all_uploaded_depth_frames"
        ),
        "profile": profile.name,
        "originalDepthFrameCount": len(depth_frames),
        "pairedDepthFrameCount": len(paired),
        "geometryDepthSelection": "all_depth_frames" if selected_from_all_depth else "selected_keyframe_pairs",
        "selectedDepthFrameCount": len(selected),
        "selectedDepthFrameIds": selected_ids,
    }


def select_rgbd_pairs_for_profile(
    paired_frames: list[tuple[dict, dict, Path, Path]],
    profile: ProcessingProfile,
) -> list[tuple[dict, dict, Path, Path]]:
    if is_fast_rgbd_hero_patch_profile(profile):
        selected_pairs, _stats = select_rgbd_loaded_pairs_for_fast_profile(
            paired_frames,
            limit=fast_rgbd_hero_patch_pair_limit(profile),
        )
        return selected_pairs

    return pose_diverse_items(
        paired_frames,
        limit=profile.max_rgbd_frames,
        transform_getter=lambda item: item[1].get("cameraTransform") or item[0].get("cameraTransform") or [],
        timestamp_getter=lambda item: item[1].get("timestamp") or item[0].get("timestamp"),
    )


def is_fast_rgbd_hero_patch_profile(profile: ProcessingProfile) -> bool:
    return (
        profile.name == "fast_onboarding"
        and profile.max_keyframes is not None
        and profile.max_depth_frames is not None
        and profile.max_rgbd_frames is not None
        and not profile.use_rgbd_geometry
        and profile.rgbd_hero_patch_texture
    )


def is_two_keyframe_rgbd_texture_profile(profile: ProcessingProfile) -> bool:
    return is_fast_rgbd_hero_patch_profile(profile)


def fast_rgbd_hero_patch_pair_limit(profile: ProcessingProfile) -> int:
    limits = [
        limit for limit in (profile.max_keyframes, profile.max_depth_frames, profile.max_rgbd_frames)
        if limit is not None and limit > 0
    ]
    return min([RGBD_HERO_PATCH_MAX_PATCHES, *limits]) if limits else RGBD_HERO_PATCH_MAX_PATCHES


def select_rgbd_payload_pairs_for_fast_profile(
    keyframes: list[dict],
    depth_frames: list[dict],
    *,
    limit: int | None = None,
) -> tuple[list[dict], dict]:
    pairs = build_payload_rgbd_pairs(keyframes, depth_frames)
    selected_pairs, selection_stats = select_rgbd_pair_records(
        pairs,
        limit=limit or RGBD_HERO_PATCH_MAX_PATCHES,
    )
    return selected_pairs, {
        "originalDepthFrameCount": len(depth_frames),
        "pairedDepthFrameCount": len(pairs),
        "selectedDepthFrameIds": [
            pair["depthFrame"].get("id")
            for pair in selected_pairs
            if pair["depthFrame"].get("id")
        ],
        **selection_stats,
    }


def select_rgbd_payload_candidate_pool_for_fast_profile(
    keyframes: list[dict],
    depth_frames: list[dict],
    *,
    limit: int | None = None,
) -> tuple[list[dict], dict]:
    pairs = build_payload_rgbd_pairs(keyframes, depth_frames)
    selected_pairs, selection_stats = select_rgbd_candidate_pool_pair_records(
        pairs,
        limit=limit or RGBD_HERO_PATCH_CANDIDATE_POOL_SIZE,
    )
    return selected_pairs, {
        "originalDepthFrameCount": len(depth_frames),
        "pairedDepthFrameCount": len(pairs),
        "selectedDepthFrameIds": [
            pair["depthFrame"].get("id")
            for pair in selected_pairs
            if pair["depthFrame"].get("id")
        ],
        **selection_stats,
    }


def select_two_rgbd_payload_pairs(keyframes: list[dict], depth_frames: list[dict]) -> tuple[list[dict], dict]:
    return select_rgbd_payload_pairs_for_fast_profile(keyframes, depth_frames, limit=2)


def build_payload_rgbd_pairs(keyframes: list[dict], depth_frames: list[dict]) -> list[dict]:
    keyframes_by_id = {
        str(keyframe.get("id")): (index, keyframe)
        for index, keyframe in enumerate(keyframes)
        if keyframe.get("id")
    }
    candidates_by_keyframe_id: dict[str, list[tuple[int, dict]]] = {}
    for depth_index, depth_frame in enumerate(depth_frames):
        color_keyframe_id = depth_frame.get("colorKeyframeId")
        if not color_keyframe_id:
            continue
        key = str(color_keyframe_id)
        if key in keyframes_by_id:
            candidates_by_keyframe_id.setdefault(key, []).append((depth_index, depth_frame))

    pairs: list[dict] = []
    for keyframe_id, (keyframe_index, keyframe) in keyframes_by_id.items():
        candidates = candidates_by_keyframe_id.get(keyframe_id) or []
        if not candidates:
            continue
        depth_index, depth_frame = min(
            candidates,
            key=lambda item: timestamp_delta(keyframe.get("timestamp"), item[1].get("timestamp")),
        )
        pairs.append({
            "keyframe": keyframe,
            "depthFrame": depth_frame,
            "keyframeIndex": keyframe_index,
            "depthFrameIndex": depth_index,
            "pose": pose_from_transform(
                keyframe.get("cameraTransform") or depth_frame.get("cameraTransform") or [],
                keyframe.get("timestamp") if keyframe.get("timestamp") is not None else depth_frame.get("timestamp"),
            ),
        })
    return sorted(pairs, key=lambda pair: int(pair["keyframeIndex"]))


def best_depth_frame_per_selected_keyframe(selected_keyframes: list[dict], paired_depth_frames: list[dict]) -> list[dict]:
    selected: list[dict] = []
    for keyframe in selected_keyframes:
        keyframe_id = str(keyframe.get("id")) if keyframe.get("id") else None
        if not keyframe_id:
            continue
        candidates = [
            frame for frame in paired_depth_frames
            if str(frame.get("colorKeyframeId")) == keyframe_id
        ]
        if not candidates:
            continue
        selected.append(min(
            candidates,
            key=lambda frame: timestamp_delta(keyframe.get("timestamp"), frame.get("timestamp")),
        ))
    return selected


def select_two_rgbd_loaded_pairs(
    paired_frames: list[tuple[dict, dict, Path, Path]],
) -> tuple[list[tuple[dict, dict, Path, Path]], dict]:
    return select_rgbd_loaded_pairs_for_fast_profile(paired_frames, limit=2)


def select_rgbd_loaded_pairs_for_fast_profile(
    paired_frames: list[tuple[dict, dict, Path, Path]],
    *,
    limit: int | None = None,
) -> tuple[list[tuple[dict, dict, Path, Path]], dict]:
    records = [
        {
            "pair": pair,
            "keyframe": pair[0],
            "depthFrame": pair[1],
            "keyframeIndex": index,
            "depthFrameIndex": index,
            "pose": pose_from_transform(
                pair[1].get("cameraTransform") or pair[0].get("cameraTransform") or [],
                pair[1].get("timestamp") if pair[1].get("timestamp") is not None else pair[0].get("timestamp"),
            ),
        }
        for index, pair in enumerate(paired_frames)
    ]
    selected_records, stats = select_rgbd_pair_records(records, limit=limit or RGBD_HERO_PATCH_MAX_PATCHES)
    return [record["pair"] for record in selected_records], stats


def select_two_rgbd_pair_records(pairs: list[dict]) -> tuple[list[dict], dict]:
    return select_rgbd_pair_records(pairs, limit=2)


def select_rgbd_pair_records(pairs: list[dict], *, limit: int | None = None) -> tuple[list[dict], dict]:
    limit = max(int(limit or RGBD_HERO_PATCH_MAX_PATCHES), 1)
    if not pairs:
        return [], {
            "fallbackReason": "no_valid_rgbd_pairs",
            "selectedPairCount": 0,
            "poseDelta": None,
        }
    if len(pairs) == 1 or limit <= 1:
        selected = [min(pairs, key=lambda pair: int(pair["keyframeIndex"]))]
        return selected, {
            "fallbackReason": "only_one_valid_rgbd_pair" if len(pairs) == 1 else None,
            "selectedPairCount": len(selected),
            "pairSelectionStrategy": "first_rgbd_hero_patch_pair",
            "selectedPairKeyframeIds": [pair["keyframe"].get("id") for pair in selected if pair["keyframe"].get("id")],
            "selectedPairDepthFrameIds": [pair["depthFrame"].get("id") for pair in selected if pair["depthFrame"].get("id")],
            "poseDelta": None,
        }

    primary = min(pairs, key=lambda pair: int(pair["keyframeIndex"]))
    selected = [primary]
    remaining = [pair for pair in pairs if pair is not primary]
    supplemental_selections = []
    while len(selected) < min(limit, len(pairs)) and remaining:
        target_delta = rgbd_hero_patch_supplement_target_for_index(len(selected))
        supplemental = max(
            remaining,
            key=lambda pair: timed_supplemental_rgbd_pair_score(
                pair,
                primary,
                selected=selected,
                target_delta=target_delta,
            ),
        )
        score = timed_supplemental_rgbd_pair_score(
            supplemental,
            primary,
            selected=selected,
            target_delta=target_delta,
        )
        remaining = [pair for pair in remaining if pair is not supplemental]
        selected.append(supplemental)
        supplemental_selections.append({
            "targetTimeDeltaSeconds": target_delta,
            "actualTimeDeltaSeconds": round(
                max(0.0, float(supplemental["pose"]["timestamp"]) - float(primary["pose"]["timestamp"])),
                4,
            ),
            "score": round(score, 6),
            "selectedKeyframeId": supplemental["keyframe"].get("id"),
            "selectedDepthFrameId": supplemental["depthFrame"].get("id"),
        })
    selected = sorted(selected, key=lambda pair: int(pair["keyframeIndex"]))
    pose_deltas = [
        pose_delta_summary(primary["pose"], pair["pose"])
        for pair in selected
        if pair is not primary
    ]
    return selected, {
        "fallbackReason": None,
        "selectedPairCount": len(selected),
        "pairSelectionStrategy": "first_pair_plus_timed_rgbd_hero_patch_supplements",
        "targetSupplementTimeDeltaSeconds": RGBD_HERO_PATCH_SUPPLEMENT_TARGET_TIME_DELTA_SECONDS,
        "targetSupplementTimeDeltasSeconds": list(RGBD_HERO_PATCH_SUPPLEMENT_TARGET_TIME_DELTAS_SECONDS),
        "minSupplementTimeDeltaSeconds": RGBD_HERO_PATCH_SUPPLEMENT_MIN_TIME_DELTA_SECONDS,
        "maxPreferredSupplementTimeDeltaSeconds": RGBD_HERO_PATCH_SUPPLEMENT_MAX_PREFERRED_TIME_DELTA_SECONDS,
        "selectedPairKeyframeIds": [pair["keyframe"].get("id") for pair in selected if pair["keyframe"].get("id")],
        "selectedPairDepthFrameIds": [pair["depthFrame"].get("id") for pair in selected if pair["depthFrame"].get("id")],
        "supplementalSelections": supplemental_selections,
        "poseDelta": pose_deltas[0] if pose_deltas else None,
        "poseDeltas": pose_deltas,
    }


def select_rgbd_candidate_pool_pair_records(pairs: list[dict], *, limit: int | None = None) -> tuple[list[dict], dict]:
    limit = max(int(limit or RGBD_HERO_PATCH_CANDIDATE_POOL_SIZE), 1)
    ordered = sorted(pairs, key=lambda pair: int(pair["keyframeIndex"]))
    if not ordered:
        return [], {
            "fallbackReason": "no_valid_rgbd_pairs",
            "selectedPairCount": 0,
            "candidatePoolLimit": limit,
            "pairSelectionStrategy": "rgbd_hero_patch_candidate_pool",
            "poseDelta": None,
        }
    if len(ordered) <= limit:
        return ordered, {
            "fallbackReason": "only_one_valid_rgbd_pair" if len(ordered) == 1 else None,
            "selectedPairCount": len(ordered),
            "candidatePoolLimit": limit,
            "pairSelectionStrategy": "all_valid_rgbd_pairs_as_candidate_pool",
            "selectedPairKeyframeIds": [pair["keyframe"].get("id") for pair in ordered if pair["keyframe"].get("id")],
            "selectedPairDepthFrameIds": [pair["depthFrame"].get("id") for pair in ordered if pair["depthFrame"].get("id")],
            "poseDelta": pose_delta_summary(ordered[0]["pose"], ordered[1]["pose"]) if len(ordered) > 1 else None,
        }

    primary = ordered[0]
    selected: list[dict] = [primary]
    selected_indexes = {id(primary)}
    window_selections = []
    per_window_target_count = max(2, math.ceil((limit - 1) / max(len(RGBD_HERO_PATCH_SUPPLEMENT_TARGET_TIME_DELTAS_SECONDS), 1)))

    for target_delta in RGBD_HERO_PATCH_SUPPLEMENT_TARGET_TIME_DELTAS_SECONDS:
        if len(selected) >= limit:
            break
        window_candidates = [
            pair for pair in ordered
            if id(pair) not in selected_indexes
            and abs(
                max(0.0, float(pair["pose"]["timestamp"]) - float(primary["pose"]["timestamp"]))
                - target_delta
            ) <= RGBD_HERO_PATCH_SUPPLEMENT_SELECTION_WINDOW_SECONDS
        ]
        ranked = sorted(
            window_candidates,
            key=lambda pair: (
                abs(max(0.0, float(pair["pose"]["timestamp"]) - float(primary["pose"]["timestamp"])) - target_delta),
                -pose_novelty_score(pair["pose"], [primary["pose"]]),
                int(pair["keyframeIndex"]),
            ),
        )
        selected_for_window = []
        for pair in ranked[:per_window_target_count]:
            if len(selected) >= limit:
                break
            selected.append(pair)
            selected_indexes.add(id(pair))
            selected_for_window.append(pair)
        window_selections.append({
            "targetTimeDeltaSeconds": target_delta,
            "windowSeconds": RGBD_HERO_PATCH_SUPPLEMENT_SELECTION_WINDOW_SECONDS,
            "candidateWindowCount": len(window_candidates),
            "selectedPairCount": len(selected_for_window),
            "selectedPairKeyframeIds": [
                pair["keyframe"].get("id") for pair in selected_for_window if pair["keyframe"].get("id")
            ],
        })

    while len(selected) < limit:
        remaining = [pair for pair in ordered if id(pair) not in selected_indexes]
        if not remaining:
            break
        next_pair = max(
            remaining,
            key=lambda pair: pose_novelty_score(pair["pose"], [item["pose"] for item in selected]),
        )
        selected.append(next_pair)
        selected_indexes.add(id(next_pair))

    selected = sorted(selected, key=lambda pair: int(pair["keyframeIndex"]))
    pose_deltas = [
        pose_delta_summary(primary["pose"], pair["pose"])
        for pair in selected
        if pair is not primary
    ]
    return selected, {
        "fallbackReason": None,
        "selectedPairCount": len(selected),
        "candidatePoolLimit": limit,
        "pairSelectionStrategy": "first_pair_plus_timed_rgbd_hero_patch_candidate_pool",
        "targetSupplementTimeDeltaSeconds": RGBD_HERO_PATCH_SUPPLEMENT_TARGET_TIME_DELTA_SECONDS,
        "targetSupplementTimeDeltasSeconds": list(RGBD_HERO_PATCH_SUPPLEMENT_TARGET_TIME_DELTAS_SECONDS),
        "supplementWindowSeconds": RGBD_HERO_PATCH_SUPPLEMENT_SELECTION_WINDOW_SECONDS,
        "selectedPairKeyframeIds": [pair["keyframe"].get("id") for pair in selected if pair["keyframe"].get("id")],
        "selectedPairDepthFrameIds": [pair["depthFrame"].get("id") for pair in selected if pair["depthFrame"].get("id")],
        "candidatePoolSelections": window_selections,
        "poseDelta": pose_deltas[0] if pose_deltas else None,
        "poseDeltas": pose_deltas,
    }


def timed_supplemental_rgbd_pair_score(
    candidate: dict,
    primary: dict,
    *,
    selected: list[dict] | None = None,
    target_delta: float | None = None,
) -> float:
    selected = selected or [primary]
    target_delta = target_delta or RGBD_HERO_PATCH_SUPPLEMENT_TARGET_TIME_DELTA_SECONDS
    time_delta = max(0.0, float(candidate["pose"]["timestamp"]) - float(primary["pose"]["timestamp"]))
    time_score = timed_supplement_score(time_delta, target_delta)
    diversity = pose_novelty_score(candidate["pose"], [pair["pose"] for pair in selected])
    diversity_score = min(diversity / 0.85, 1.0)
    capture_order_score = 1.0 / max(int(candidate["keyframeIndex"]) - int(primary["keyframeIndex"]), 1)
    return time_score * 2.0 + diversity_score * 0.5 + capture_order_score * 0.05


def modest_rgbd_pair_score(candidate: dict, primary: dict, pair_count: int, target_diversity: float) -> float:
    diversity = pose_novelty_score(candidate["pose"], [primary["pose"]])
    diversity_score = 1 - min(abs(diversity - target_diversity) / max(target_diversity, 1e-6), 1)
    center_position = float(candidate["keyframeIndex"]) / max(pair_count - 1, 1)
    center_score = 1 - abs(center_position - 0.5) * 0.5
    visible_baseline_bonus = min(diversity / max(target_diversity, 1e-6), 1) * 0.15
    return diversity_score + center_score + visible_baseline_bonus


def center_biased_items(items: list, limit: int | None) -> list:
    if limit is None or limit <= 0 or len(items) <= limit:
        return items
    if limit == 1:
        return [items[len(items) // 2]]
    center = (len(items) - 1) // 2
    indices = sorted({max(0, center - 1), min(len(items) - 1, center + 1)})[:limit]
    return [items[index] for index in indices]


def timestamp_delta(left: object, right: object) -> float:
    left_value = safe_float(left)
    right_value = safe_float(right)
    if left_value is None or right_value is None:
        return math.inf
    return abs(left_value - right_value)


def pose_delta_summary(left: dict, right: dict) -> dict:
    distance = length(subtract(left["position"], right["position"]))
    direction_dot = clamp_float(dot(left["forward"], right["forward"]), -1.0, 1.0)
    angle = math.acos(direction_dot)
    time_delta = abs(float(left["timestamp"]) - float(right["timestamp"]))
    return {
        "translationMeters": round(distance, 4),
        "angleDegrees": round(math.degrees(angle), 3),
        "timeDeltaSeconds": round(time_delta, 4),
    }


def pose_diverse_items(
    items: list,
    *,
    limit: int | None,
    transform_getter: Callable[[object], list],
    timestamp_getter: Callable[[object], object],
) -> list:
    if limit is None or limit <= 0 or len(items) <= limit:
        return items
    if limit == 1:
        return [items[len(items) // 2]]

    poses = [
        pose_from_transform(transform_getter(item), timestamp_getter(item))
        for item in items
    ]
    selected_indices: set[int] = {0, len(items) - 1}
    target_count = min(limit, len(items))

    while len(selected_indices) < target_count:
        selected_poses = [poses[index] for index in selected_indices]
        best_index = None
        best_score = -math.inf
        for index, pose in enumerate(poses):
            if index in selected_indices:
                continue
            score = pose_novelty_score(pose, selected_poses)
            if score > best_score:
                best_index = index
                best_score = score
        if best_index is None:
            break
        selected_indices.add(best_index)

    return [items[index] for index in sorted(selected_indices)]


def pose_from_transform(transform: list, timestamp: object) -> dict:
    if len(transform) >= 16:
        position = (float(transform[12]), float(transform[13]), float(transform[14]))
        forward = normalize((-float(transform[8]), -float(transform[9]), -float(transform[10])))
        if forward == (0.0, 0.0, 0.0):
            forward = (0.0, 0.0, -1.0)
    else:
        position = (0.0, 0.0, 0.0)
        forward = (0.0, 0.0, -1.0)
    try:
        resolved_timestamp = float(timestamp)
    except (TypeError, ValueError):
        resolved_timestamp = 0.0
    return {"position": position, "forward": forward, "timestamp": resolved_timestamp}


def safe_float(value: object) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def pose_novelty_score(candidate: dict, selected: list[dict]) -> float:
    if not selected:
        return math.inf
    scores = []
    candidate_position = candidate["position"]
    candidate_forward = candidate["forward"]
    for selected_pose in selected:
        distance = length(subtract(candidate_position, selected_pose["position"]))
        direction_dot = clamp_float(dot(candidate_forward, selected_pose["forward"]), -1.0, 1.0)
        angle = math.acos(direction_dot)
        time_delta = abs(float(candidate["timestamp"]) - float(selected_pose["timestamp"]))
        scores.append(distance / 0.45 + angle / 0.55 + min(time_delta / 8.0, 1.0) * 0.35)
    return min(scores)


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
    profile: ProcessingProfile | None = None,
) -> dict:
    profile = profile or PROCESSING_PROFILES["full_quality"]
    paired_frames = pair_rgbd_frames(keyframes, depth_frames, work_dir)
    if not paired_frames:
        raise RGBDFusionUnavailable("No RGB/depth frames could be paired for fallback depth mesh fusion.")

    selected_frames = select_rgbd_pairs_for_profile(
        paired_frames,
        ProcessingProfile(
            **{
                **profile.__dict__,
                "max_rgbd_frames": min(
                    RGBD_DEPTH_MESH_MAX_FRAMES,
                    profile.max_rgbd_frames or RGBD_DEPTH_MESH_MAX_FRAMES,
                ),
            }
        ),
    )
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
        "profile": processing_profile_stats(profile),
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
        "profile": processing_profile_stats(profile),
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
    *,
    absolute_tolerance: float = 0.08,
    relative_tolerance: float = 0.08,
) -> int:
    if any(index is None for index in indices) or not should_connect_depth_samples(
        depths,
        absolute_tolerance=absolute_tolerance,
        relative_tolerance=relative_tolerance,
    ):
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


def should_connect_depth_samples(
    depths: tuple[float, float, float],
    *,
    absolute_tolerance: float = 0.08,
    relative_tolerance: float = 0.08,
) -> bool:
    min_depth = min(depths)
    max_depth = max(depths)
    if min_depth <= 0:
        return False

    return (max_depth - min_depth) <= max(absolute_tolerance, min_depth * relative_tolerance)


def write_rgbd_tsdf_mesh(
    keyframes: list[dict],
    depth_frames: list[dict],
    work_dir: Path,
    output_obj_path: Path,
    output_json_path: Path,
    profile: ProcessingProfile | None = None,
) -> dict:
    profile = profile or PROCESSING_PROFILES["full_quality"]
    np, o3d = load_open3d_modules()
    paired_frames = pair_rgbd_frames(keyframes, depth_frames, work_dir)
    if not paired_frames:
        raise RGBDFusionUnavailable("No RGB/depth frames could be paired for TSDF fusion.")
    selected_frames = select_rgbd_pairs_for_profile(paired_frames, profile)

    volume = o3d.pipelines.integration.ScalableTSDFVolume(
        voxel_length=RGBD_VOXEL_LENGTH_METERS,
        sdf_trunc=RGBD_SDF_TRUNC_METERS,
        color_type=o3d.pipelines.integration.TSDFVolumeColorType.RGB8,
    )
    integrated_count = 0

    for keyframe, depth_frame, color_path, depth_path in selected_frames:
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
        "pairedFrameCount": len(paired_frames),
        "sampledDepthFrameCount": len(selected_frames),
        "integratedDepthFrameCount": integrated_count,
        "voxelLengthMeters": RGBD_VOXEL_LENGTH_METERS,
        "sdfTruncMeters": RGBD_SDF_TRUNC_METERS,
        "depthTruncMeters": RGBD_DEPTH_TRUNC_METERS,
        "postprocess": postprocess_stats,
        "profile": processing_profile_stats(profile),
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
        "sampledDepthFrameCount": len(selected_frames),
        "integratedDepthFrameCount": integrated_count,
        "voxelLengthMeters": RGBD_VOXEL_LENGTH_METERS,
        "sdfTruncMeters": RGBD_SDF_TRUNC_METERS,
        "depthTruncMeters": RGBD_DEPTH_TRUNC_METERS,
        "postprocess": postprocess_stats,
        "profile": processing_profile_stats(profile),
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
    anchor_stats: list[dict] = []

    for anchor in mesh_anchors:
        transform = anchor.get("transform") or []
        local_vertices = anchor.get("vertices") or []
        indices = anchor.get("triangleIndices") or []
        per_anchor = {
            "id": anchor.get("id"),
            "inputVertexCount": len(local_vertices),
            "inputIndexCount": len(indices),
            "inputFaceCount": len(indices) // 3,
            "transformValid": len(transform) == 16,
            "validWorldVertexCount": 0,
            "duplicateVertexCount": 0,
            "invalidVertexCount": 0,
            "emittedFaceCount": 0,
            "invalidFaceCount": 0,
            "duplicateFaceCount": 0,
        }
        if len(transform) != 16:
            invalid_vertex_count += len(local_vertices)
            invalid_face_count += len(indices) // 3
            per_anchor["invalidVertexCount"] = len(local_vertices)
            per_anchor["invalidFaceCount"] = len(indices) // 3
            anchor_stats.append(per_anchor)
            continue

        local_to_fused: list[int | None] = []
        for vertex in local_vertices:
            original_vertex_count += 1
            if len(vertex) != 3:
                invalid_vertex_count += 1
                per_anchor["invalidVertexCount"] += 1
                local_to_fused.append(None)
                continue

            world = transform_point(transform, vertex)
            if not all(math.isfinite(component) for component in world):
                invalid_vertex_count += 1
                per_anchor["invalidVertexCount"] += 1
                local_to_fused.append(None)
                continue

            key = quantized_vertex_key(world, quantization)
            existing = vertex_lookup.get(key)
            if existing is not None:
                duplicate_vertex_count += 1
                per_anchor["duplicateVertexCount"] += 1
                local_to_fused.append(existing)
                continue

            fused_index = len(vertices)
            vertex_lookup[key] = fused_index
            vertices.append(world)
            local_to_fused.append(fused_index)
            per_anchor["validWorldVertexCount"] += 1

        for index in range(0, len(indices) - 2, 3):
            original_face_count += 1
            try:
                raw_a = int(indices[index])
                raw_b = int(indices[index + 1])
                raw_c = int(indices[index + 2])
            except (TypeError, ValueError):
                invalid_face_count += 1
                per_anchor["invalidFaceCount"] += 1
                continue

            if min(raw_a, raw_b, raw_c) < 0 or max(raw_a, raw_b, raw_c) >= len(local_to_fused):
                invalid_face_count += 1
                per_anchor["invalidFaceCount"] += 1
                continue

            resolved = (local_to_fused[raw_a], local_to_fused[raw_b], local_to_fused[raw_c])
            if resolved[0] is None or resolved[1] is None or resolved[2] is None:
                invalid_face_count += 1
                per_anchor["invalidFaceCount"] += 1
                continue

            face = (int(resolved[0]), int(resolved[1]), int(resolved[2]))
            if len(set(face)) != 3 or triangle_area(vertices[face[0]], vertices[face[1]], vertices[face[2]]) <= 1e-10:
                invalid_face_count += 1
                per_anchor["invalidFaceCount"] += 1
                continue

            face_key = tuple(sorted(face))
            if face_key in seen_faces:
                duplicate_face_count += 1
                per_anchor["duplicateFaceCount"] += 1
                continue

            seen_faces.add(face_key)
            faces.append(face)
            per_anchor["emittedFaceCount"] += 1

        anchor_stats.append(per_anchor)

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
        "anchors": anchor_stats,
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


def build_mesh_integrity_report(mesh: FusedMesh) -> dict:
    bounds = mesh_bounds_stats(mesh.vertices)
    topology = mesh_topology_stats(mesh)
    connected = mesh_connected_component_stats(mesh)
    area = sum(triangle_area(mesh.vertices[a], mesh.vertices[b], mesh.vertices[c]) for a, b, c in mesh.faces)
    return {
        "geometrySource": mesh.stats.get("geometrySource", "unknown"),
        "vertexCount": len(mesh.vertices),
        "faceCount": len(mesh.faces),
        "primitiveType": "triangles",
        "coordinateSpace": "arkit_world_meters",
        "bounds": bounds,
        "surfaceAreaM2": round(area, 6),
        "topology": topology,
        "connectedComponents": connected,
        "sourceStats": {
            "anchorCount": mesh.stats.get("anchorCount", 0),
            "originalVertexCount": mesh.stats.get("originalVertexCount", 0),
            "originalFaceCount": mesh.stats.get("originalFaceCount", 0),
            "duplicateVertexCount": mesh.stats.get("duplicateVertexCount", 0),
            "invalidVertexCount": mesh.stats.get("invalidVertexCount", 0),
            "invalidFaceCount": mesh.stats.get("invalidFaceCount", 0),
            "duplicateFaceCount": mesh.stats.get("duplicateFaceCount", 0),
            "anchors": mesh.stats.get("anchors", []),
        },
        "validation": {
            "hasVertices": bool(mesh.vertices),
            "hasTriangleFaces": bool(mesh.faces),
            "allVerticesFinite": topology["nonFiniteVertexCount"] == 0,
            "allIndicesInRange": topology["outOfRangeIndexCount"] == 0,
            "nonDegenerateFaces": topology["degenerateFaceCount"] == 0,
            "glbPrimitiveMode": 4,
            "unassignedFacesOpaque": True,
        },
    }


def mesh_bounds_stats(vertices: list[tuple[float, float, float]]) -> dict:
    if not vertices:
        return {
            "min": None,
            "max": None,
            "dimensionsMeters": None,
            "diagonalMeters": 0,
        }

    min_x = min(vertex[0] for vertex in vertices)
    min_y = min(vertex[1] for vertex in vertices)
    min_z = min(vertex[2] for vertex in vertices)
    max_x = max(vertex[0] for vertex in vertices)
    max_y = max(vertex[1] for vertex in vertices)
    max_z = max(vertex[2] for vertex in vertices)
    dimensions = (max_x - min_x, max_y - min_y, max_z - min_z)
    return {
        "min": [round(min_x, 6), round(min_y, 6), round(min_z, 6)],
        "max": [round(max_x, 6), round(max_y, 6), round(max_z, 6)],
        "dimensionsMeters": [round(value, 6) for value in dimensions],
        "diagonalMeters": round(length(dimensions), 6),
    }


def mesh_topology_stats(mesh: FusedMesh) -> dict:
    non_finite_vertex_count = sum(
        1 for vertex in mesh.vertices
        if len(vertex) != 3 or not all(math.isfinite(component) for component in vertex)
    )
    out_of_range_index_count = 0
    duplicate_index_face_count = 0
    degenerate_face_count = 0
    unique_edges: set[tuple[int, int]] = set()
    edge_face_counts: dict[tuple[int, int], int] = {}
    vertex_count = len(mesh.vertices)

    for face in mesh.faces:
        if len(face) != 3:
            out_of_range_index_count += 1
            continue
        if min(face) < 0 or max(face) >= vertex_count:
            out_of_range_index_count += sum(1 for index in face if index < 0 or index >= vertex_count)
            continue
        if len(set(face)) != 3:
            duplicate_index_face_count += 1
            degenerate_face_count += 1
            continue
        if triangle_area(mesh.vertices[face[0]], mesh.vertices[face[1]], mesh.vertices[face[2]]) <= 1e-10:
            degenerate_face_count += 1
        for edge in ((face[0], face[1]), (face[1], face[2]), (face[2], face[0])):
            edge_key = tuple(sorted(edge))
            unique_edges.add(edge_key)
            edge_face_counts[edge_key] = edge_face_counts.get(edge_key, 0) + 1

    boundary_edge_count = sum(1 for count in edge_face_counts.values() if count == 1)
    non_manifold_edge_count = sum(1 for count in edge_face_counts.values() if count > 2)
    referenced_vertices = {index for face in mesh.faces for index in face if 0 <= index < vertex_count}
    return {
        "nonFiniteVertexCount": non_finite_vertex_count,
        "outOfRangeIndexCount": out_of_range_index_count,
        "duplicateIndexFaceCount": duplicate_index_face_count,
        "degenerateFaceCount": degenerate_face_count,
        "referencedVertexCount": len(referenced_vertices),
        "unreferencedVertexCount": vertex_count - len(referenced_vertices),
        "uniqueEdgeCount": len(unique_edges),
        "boundaryEdgeCount": boundary_edge_count,
        "nonManifoldEdgeCount": non_manifold_edge_count,
    }


def mesh_connected_component_stats(mesh: FusedMesh, max_components: int = 12) -> dict:
    if not mesh.faces:
        return {
            "componentCount": 0,
            "largestFaceCount": 0,
            "largestSurfaceAreaM2": 0,
            "components": [],
        }

    vertex_to_faces: dict[int, list[int]] = {}
    for face_index, face in enumerate(mesh.faces):
        for vertex_index in face:
            vertex_to_faces.setdefault(vertex_index, []).append(face_index)

    visited = [False] * len(mesh.faces)
    components: list[dict] = []
    for start_index in range(len(mesh.faces)):
        if visited[start_index]:
            continue
        queue: deque[int] = deque([start_index])
        visited[start_index] = True
        face_indices: list[int] = []
        vertex_indices: set[int] = set()
        surface_area = 0.0

        while queue:
            face_index = queue.popleft()
            face = mesh.faces[face_index]
            face_indices.append(face_index)
            vertex_indices.update(face)
            surface_area += triangle_area(mesh.vertices[face[0]], mesh.vertices[face[1]], mesh.vertices[face[2]])
            for vertex_index in face:
                for adjacent_face_index in vertex_to_faces.get(vertex_index, []):
                    if not visited[adjacent_face_index]:
                        visited[adjacent_face_index] = True
                        queue.append(adjacent_face_index)

        components.append({
            "faceCount": len(face_indices),
            "vertexCount": len(vertex_indices),
            "surfaceAreaM2": round(surface_area, 6),
        })

    components.sort(key=lambda item: (item["faceCount"], item["surfaceAreaM2"]), reverse=True)
    return {
        "componentCount": len(components),
        "largestFaceCount": components[0]["faceCount"] if components else 0,
        "largestSurfaceAreaM2": components[0]["surfaceAreaM2"] if components else 0,
        "components": components[:max_components],
        "truncatedComponentList": len(components) > max_components,
    }


def write_geometry_diagnostic_artifacts(mesh: FusedMesh, work_dir: Path) -> dict:
    integrity_report = build_mesh_integrity_report(mesh)
    artifacts = {
        "geometryOnlyGlb": write_mesh_glb(
            mesh,
            work_dir / "geometry_only.glb",
            name="geometry_only",
            material_color=(0.72, 0.72, 0.70, 1.0),
            double_sided=True,
        ),
        "geometryCulledGlb": write_mesh_glb(
            mesh,
            work_dir / "geometry_culled.glb",
            name="geometry_culled",
            material_color=(0.72, 0.72, 0.70, 1.0),
            double_sided=False,
        ),
    }
    integrity_report["diagnosticArtifacts"] = artifacts
    (work_dir / "mesh_integrity_report.json").write_text(
        json.dumps(integrity_report, indent=2),
        encoding="utf-8",
    )
    return {
        "meshIntegrityReport": {
            "format": "json",
            "path": "mesh_integrity_report.json",
            "available": True,
        },
        **artifacts,
    }


def make_checker_png_bytes(size: int = 512, squares: int = 16) -> bytes:
    image = Image.new("RGBA", (size, size), (238, 238, 232, 255))
    draw = ImageDraw.Draw(image)
    square_size = max(1, size // squares)
    colors = ((32, 32, 34, 255), (235, 235, 224, 255), (168, 22, 37, 255))
    for y in range(squares):
        for x in range(squares):
            color = colors[(x + y) % 2]
            if x == 0 or y == 0 or x == squares - 1 or y == squares - 1:
                color = colors[2]
            draw.rectangle(
                [
                    x * square_size,
                    y * square_size,
                    min(size, (x + 1) * square_size) - 1,
                    min(size, (y + 1) * square_size) - 1,
                ],
                fill=color,
            )
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def write_uv_checker_glb(mesh: FusedMesh, output_path: Path) -> dict:
    face_uvs = [((0.04, 0.04), (0.96, 0.04), (0.04, 0.96)) for _ in mesh.faces]
    return write_mesh_glb(
        mesh,
        output_path,
        name="uv_checker",
        face_uvs=face_uvs,
        texture_png_bytes=make_checker_png_bytes(),
        material_color=(1.0, 1.0, 1.0, 1.0),
        double_sided=True,
    )


def write_coverage_debug_glb(
    mesh: FusedMesh,
    output_path: Path,
    output_report_path: Path | None,
    face_statuses: list[str],
) -> dict:
    palette = {
        "projected": (58, 156, 91),
        "projected_solid": (62, 132, 214),
        "candidate": (84, 173, 201),
        "fallback_unobserved": (224, 67, 67),
        "fallback": (226, 164, 64),
        "unknown": (162, 162, 156),
    }
    normalized_statuses = [
        status if status in palette else "unknown"
        for status in (face_statuses[:len(mesh.faces)] + ["unknown"] * max(0, len(mesh.faces) - len(face_statuses)))
    ]
    face_colors = [palette[status] for status in normalized_statuses]
    report = build_coverage_debug_report(mesh, normalized_statuses)
    if output_report_path is not None:
        output_report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    glb_stats = write_mesh_glb(
        mesh,
        output_path,
        name="coverage_debug",
        face_colors=face_colors,
        material_color=(1.0, 1.0, 1.0, 1.0),
        double_sided=True,
    )
    glb_stats["coverageReport"] = report
    return glb_stats


def build_coverage_debug_report(mesh: FusedMesh, face_statuses: list[str]) -> dict:
    category_stats: dict[str, dict] = {}
    total_area = 0.0
    for face, status in zip(mesh.faces, face_statuses):
        area = triangle_area(mesh.vertices[face[0]], mesh.vertices[face[1]], mesh.vertices[face[2]])
        total_area += area
        bucket = category_stats.setdefault(status, {"faceCount": 0, "surfaceAreaM2": 0.0})
        bucket["faceCount"] += 1
        bucket["surfaceAreaM2"] += area

    for bucket in category_stats.values():
        bucket["surfaceAreaM2"] = round(bucket["surfaceAreaM2"], 6)
        bucket["faceRatio"] = round(bucket["faceCount"] / max(len(mesh.faces), 1), 6)
        bucket["surfaceAreaRatio"] = round(bucket["surfaceAreaM2"] / max(total_area, 1e-12), 6)

    projected_face_count = sum(
        stats["faceCount"]
        for status, stats in category_stats.items()
        if status in {"projected", "projected_solid", "candidate"}
    )
    return {
        "faceCount": len(mesh.faces),
        "surfaceAreaM2": round(total_area, 6),
        "projectedOrCandidateFaceCount": projected_face_count,
        "projectedOrCandidateFaceRatio": round(projected_face_count / max(len(mesh.faces), 1), 6),
        "categories": category_stats,
        "legend": {
            "projected": "Face received image-projected texels.",
            "projected_solid": "Face received a direct projected solid color.",
            "candidate": "Face has a coherent keyframe owner, but per-face raster stats were unavailable.",
            "fallback_unobserved": "Face was rasterized with opaque unobserved fallback color.",
            "fallback": "Face has fallback fill rather than image projection.",
            "unknown": "Face status was not resolved by the texture pass.",
        },
    }


def write_mesh_glb(
    mesh: FusedMesh,
    output_path: Path,
    *,
    name: str,
    material_color: tuple[float, float, float, float],
    double_sided: bool,
    face_uvs: FaceUVs | None = None,
    texture_png_bytes: bytes | None = None,
    face_colors: list[tuple[int, int, int]] | None = None,
) -> dict:
    if not mesh.vertices or not mesh.faces:
        return {
            "format": "glb",
            "path": output_path.name,
            "available": False,
            "reason": "Mesh has no vertices or triangle faces.",
        }
    if face_uvs is not None and len(face_uvs) != len(mesh.faces):
        return {
            "format": "glb",
            "path": output_path.name,
            "available": False,
            "reason": "UV coordinate count does not match face count.",
        }
    if face_colors is not None and len(face_colors) != len(mesh.faces):
        return {
            "format": "glb",
            "path": output_path.name,
            "available": False,
            "reason": "Face color count does not match face count.",
        }

    expanded = face_uvs is not None or face_colors is not None
    if expanded:
        vertices: list[tuple[float, float, float]] = []
        normals: list[tuple[float, float, float]] = []
        uvs: list[tuple[float, float]] = []
        colors: list[tuple[int, int, int, int]] = []
        indices: list[int] = []
        for face_index, face in enumerate(mesh.faces):
            face_vertices = [mesh.vertices[face[0]], mesh.vertices[face[1]], mesh.vertices[face[2]]]
            normal = triangle_normal(face_vertices[0], face_vertices[1], face_vertices[2])
            base_index = len(vertices)
            vertices.extend(face_vertices)
            normals.extend([normal] * 3)
            indices.extend([base_index, base_index + 1, base_index + 2])
            if face_uvs is not None:
                uvs.extend(face_uvs[face_index])
            if face_colors is not None:
                r, g, b = face_colors[face_index]
                colors.extend([(r, g, b, 255)] * 3)
    else:
        vertices = list(mesh.vertices)
        normals = compute_vertex_normals(mesh.vertices, mesh.faces)
        uvs = []
        colors = []
        indices = [index for face in mesh.faces for index in face]

    validation = validate_glb_mesh_inputs(vertices, indices, uvs if face_uvs is not None else None)
    if not validation["valid"]:
        return {
            "format": "glb",
            "path": output_path.name,
            "available": False,
            "reason": validation["reason"],
            "validation": validation,
        }

    binary = bytearray()
    buffer_views: list[dict] = []
    accessors: list[dict] = []

    def append_buffer_view(data: bytes, target: int | None = None) -> int:
        align_bytearray(binary, 4)
        offset = len(binary)
        binary.extend(data)
        view = {"buffer": 0, "byteOffset": offset, "byteLength": len(data)}
        if target is not None:
            view["target"] = target
        buffer_views.append(view)
        return len(buffer_views) - 1

    def append_accessor(
        data: bytes,
        *,
        component_type: int,
        accessor_type: str,
        count: int,
        target: int | None,
        minimum: list[float] | None = None,
        maximum: list[float] | None = None,
        normalized: bool = False,
    ) -> int:
        view_index = append_buffer_view(data, target)
        accessor = {
            "bufferView": view_index,
            "byteOffset": 0,
            "componentType": component_type,
            "count": count,
            "type": accessor_type,
        }
        if minimum is not None:
            accessor["min"] = minimum
        if maximum is not None:
            accessor["max"] = maximum
        if normalized:
            accessor["normalized"] = True
        accessors.append(accessor)
        return len(accessors) - 1

    position_bytes = b"".join(struct.pack("<3f", *vertex) for vertex in vertices)
    normal_bytes = b"".join(struct.pack("<3f", *normal) for normal in normals)
    index_bytes = b"".join(struct.pack("<I", index) for index in indices)
    position_min = [min(vertex[axis] for vertex in vertices) for axis in range(3)]
    position_max = [max(vertex[axis] for vertex in vertices) for axis in range(3)]

    position_accessor = append_accessor(
        position_bytes,
        component_type=5126,
        accessor_type="VEC3",
        count=len(vertices),
        target=34962,
        minimum=[round(value, 7) for value in position_min],
        maximum=[round(value, 7) for value in position_max],
    )
    normal_accessor = append_accessor(
        normal_bytes,
        component_type=5126,
        accessor_type="VEC3",
        count=len(normals),
        target=34962,
    )
    index_accessor = append_accessor(
        index_bytes,
        component_type=5125,
        accessor_type="SCALAR",
        count=len(indices),
        target=34963,
        minimum=[0],
        maximum=[max(indices)],
    )

    attributes = {
        "POSITION": position_accessor,
        "NORMAL": normal_accessor,
    }
    if face_uvs is not None:
        uv_bytes = b"".join(struct.pack("<2f", uv[0], uv[1]) for uv in uvs)
        attributes["TEXCOORD_0"] = append_accessor(
            uv_bytes,
            component_type=5126,
            accessor_type="VEC2",
            count=len(uvs),
            target=34962,
        )
    if face_colors is not None:
        color_bytes = b"".join(struct.pack("<4B", *color) for color in colors)
        attributes["COLOR_0"] = append_accessor(
            color_bytes,
            component_type=5121,
            accessor_type="VEC4",
            count=len(colors),
            target=34962,
            normalized=True,
        )

    material = {
        "name": f"{name}_opaque_material",
        "alphaMode": "OPAQUE",
        "doubleSided": double_sided,
        "pbrMetallicRoughness": {
            "baseColorFactor": [float(component) for component in material_color],
            "metallicFactor": 0.0,
            "roughnessFactor": 0.88,
        },
    }
    images = None
    textures = None
    samplers = None
    if texture_png_bytes is not None:
        image_view_index = append_buffer_view(texture_png_bytes)
        images = [{"bufferView": image_view_index, "mimeType": "image/png", "name": f"{name}_texture"}]
        samplers = [{"magFilter": 9729, "minFilter": 9987, "wrapS": 10497, "wrapT": 10497}]
        textures = [{"sampler": 0, "source": 0}]
        material["pbrMetallicRoughness"]["baseColorTexture"] = {"index": 0}

    gltf = {
        "asset": {"version": "2.0", "generator": "LidarAI diagnostic GLB exporter"},
        "scene": 0,
        "scenes": [{"nodes": [0]}],
        "nodes": [{"name": name, "mesh": 0}],
        "meshes": [{
            "name": name,
            "primitives": [{
                "attributes": attributes,
                "indices": index_accessor,
                "material": 0,
                "mode": 4,
            }],
        }],
        "materials": [material],
        "buffers": [{"byteLength": len(binary)}],
        "bufferViews": buffer_views,
        "accessors": accessors,
    }
    if images is not None:
        gltf["images"] = images
        gltf["samplers"] = samplers
        gltf["textures"] = textures

    write_glb_file(output_path, gltf, bytes(binary))
    return {
        "format": "glb",
        "path": output_path.name,
        "available": True,
        "primitiveMode": 4,
        "primitiveType": "triangles",
        "materialAlphaMode": "OPAQUE",
        "doubleSided": double_sided,
        "vertexCount": len(vertices),
        "sourceVertexCount": len(mesh.vertices),
        "faceCount": len(mesh.faces),
        "indexCount": len(indices),
        "expandedPerFace": expanded,
        "hasTexture": texture_png_bytes is not None,
        "hasVertexColors": face_colors is not None,
        "hasUVs": face_uvs is not None,
        "validation": validation,
    }


def write_textured_usdz(
    mesh: FusedMesh,
    output_path: Path,
    *,
    face_uvs: FaceUVs,
    texture_path: Path,
    name: str = "textured_mesh",
) -> dict:
    if not mesh.vertices or not mesh.faces:
        return {
            "format": "usdz",
            "path": output_path.name,
            "available": False,
            "reason": "Mesh has no vertices or triangle faces.",
        }
    if len(face_uvs) != len(mesh.faces):
        return {
            "format": "usdz",
            "path": output_path.name,
            "available": False,
            "reason": "UV coordinate count does not match face count.",
        }
    if not texture_path.exists():
        return {
            "format": "usdz",
            "path": output_path.name,
            "available": False,
            "reason": "Texture PNG is missing.",
        }

    flat_uvs = [uv for face_uv in face_uvs for uv in face_uv]
    validation = validate_glb_mesh_inputs(
        mesh.vertices,
        [index for face in mesh.faces for index in face],
        flat_uvs,
    )
    if not validation["valid"]:
        return {
            "format": "usdz",
            "path": output_path.name,
            "available": False,
            "reason": validation["reason"],
            "validation": validation,
        }

    usda_path = output_path.with_suffix(".usda.tmp")
    texture_arcname = texture_path.name
    root_name = usd_identifier(name)
    vertex_normals = compute_vertex_normals(mesh.vertices, mesh.faces)
    try:
        write_textured_usda(
            usda_path,
            mesh=mesh,
            face_uvs=face_uvs,
            vertex_normals=vertex_normals,
            root_name=root_name,
            mesh_name="Mesh",
            material_name="LidarAI_Textured_Material",
            texture_arcname=texture_arcname,
        )
        write_usdz_archive(output_path, [
            (f"{name}.usda", usda_path),
            (texture_arcname, texture_path),
        ])
    finally:
        try:
            usda_path.unlink()
        except FileNotFoundError:
            pass

    return {
        "format": "usdz",
        "path": output_path.name,
        "available": output_path.exists(),
        "primitiveType": "triangles",
        "doubleSided": True,
        "vertexCount": len(mesh.vertices),
        "faceCount": len(mesh.faces),
        "uvCoordinateCount": len(flat_uvs),
        "hasTexture": True,
        "texturePath": texture_arcname,
        "sizeBytes": output_path.stat().st_size if output_path.exists() else 0,
        "generator": "direct_usda_usdz_package",
        "validation": validation,
    }


def usd_identifier(value: str) -> str:
    cleaned = "".join(character if character.isalnum() or character == "_" else "_" for character in value)
    if not cleaned:
        return "Mesh"
    if cleaned[0].isdigit():
        cleaned = f"_{cleaned}"
    return cleaned


def write_textured_usda(
    output_path: Path,
    *,
    mesh: FusedMesh,
    face_uvs: FaceUVs,
    vertex_normals: list[tuple[float, float, float]],
    root_name: str,
    mesh_name: str,
    material_name: str,
    texture_arcname: str,
) -> None:
    with output_path.open("w", encoding="utf-8") as handle:
        handle.write("#usda 1.0\n")
        handle.write("(\n")
        handle.write(f"    defaultPrim = \"{root_name}\"\n")
        handle.write("    metersPerUnit = 1\n")
        handle.write("    upAxis = \"Y\"\n")
        handle.write(")\n\n")
        handle.write(f"def Xform \"{root_name}\"\n")
        handle.write("{\n")
        handle.write(f"    def Mesh \"{mesh_name}\" (\n")
        handle.write("        prepend apiSchemas = [\"MaterialBindingAPI\"]\n")
        handle.write("    )\n")
        handle.write("    {\n")
        handle.write("        uniform token subdivisionScheme = \"none\"\n")
        handle.write("        uniform bool doubleSided = 1\n")
        write_usd_tuple_array(
            handle,
            "point3f[] points",
            mesh.vertices,
            indent="        ",
            tuple_formatter=format_usd_vec3,
        )
        write_usd_scalar_array(
            handle,
            "int[] faceVertexCounts",
            (3 for _face in mesh.faces),
            indent="        ",
            value_formatter=str,
        )
        write_usd_scalar_array(
            handle,
            "int[] faceVertexIndices",
            (index for face in mesh.faces for index in face),
            indent="        ",
            value_formatter=str,
        )
        write_usd_tuple_array(
            handle,
            "normal3f[] normals",
            vertex_normals,
            indent="        ",
            tuple_formatter=format_usd_vec3,
            metadata=' (\n            interpolation = "vertex"\n        )',
        )
        write_usd_tuple_array(
            handle,
            "texCoord2f[] primvars:st",
            (uv for face_uv in face_uvs for uv in face_uv),
            indent="        ",
            tuple_formatter=format_usd_vec2,
            metadata=' (\n            interpolation = "faceVarying"\n        )',
        )
        handle.write(f"        rel material:binding = </{root_name}/Looks/{material_name}>\n")
        handle.write("    }\n\n")
        handle.write("    def Scope \"Looks\"\n")
        handle.write("    {\n")
        handle.write(f"        def Material \"{material_name}\"\n")
        handle.write("        {\n")
        handle.write(
            f"            token outputs:surface.connect = </{root_name}/Looks/{material_name}/PreviewSurface.outputs:surface>\n"
        )
        handle.write("            def Shader \"PreviewSurface\"\n")
        handle.write("            {\n")
        handle.write("                uniform token info:id = \"UsdPreviewSurface\"\n")
        handle.write(
            f"                color3f inputs:diffuseColor.connect = </{root_name}/Looks/{material_name}/DiffuseTexture.outputs:rgb>\n"
        )
        handle.write("                float inputs:roughness = 1\n")
        handle.write("                float inputs:metallic = 0\n")
        handle.write("                token outputs:surface\n")
        handle.write("            }\n")
        handle.write("            def Shader \"DiffuseTexture\"\n")
        handle.write("            {\n")
        handle.write("                uniform token info:id = \"UsdUVTexture\"\n")
        handle.write(f"                asset inputs:file = @{texture_arcname}@\n")
        handle.write("                token inputs:sourceColorSpace = \"sRGB\"\n")
        handle.write(
            f"                float2 inputs:st.connect = </{root_name}/Looks/{material_name}/Primvar_st.outputs:result>\n"
        )
        handle.write("                color3f outputs:rgb\n")
        handle.write("            }\n")
        handle.write("            def Shader \"Primvar_st\"\n")
        handle.write("            {\n")
        handle.write("                uniform token info:id = \"UsdPrimvarReader_float2\"\n")
        handle.write("                string inputs:varname = \"st\"\n")
        handle.write("                float2 outputs:result\n")
        handle.write("            }\n")
        handle.write("        }\n")
        handle.write("    }\n")
        handle.write("}\n")


def write_usd_scalar_array(
    handle,
    declaration: str,
    values,
    *,
    indent: str,
    value_formatter: Callable[[object], str],
    values_per_line: int = 24,
) -> None:
    handle.write(f"{indent}{declaration} = [\n")
    line_values: list[str] = []
    for value in values:
        line_values.append(value_formatter(value))
        if len(line_values) >= values_per_line:
            handle.write(f"{indent}    {', '.join(line_values)},\n")
            line_values = []
    if line_values:
        handle.write(f"{indent}    {', '.join(line_values)},\n")
    handle.write(f"{indent}]\n")


def write_usd_tuple_array(
    handle,
    declaration: str,
    values,
    *,
    indent: str,
    tuple_formatter: Callable[[object], str],
    metadata: str = "",
    values_per_line: int = 4,
) -> None:
    handle.write(f"{indent}{declaration} = [\n")
    line_values: list[str] = []
    for value in values:
        line_values.append(tuple_formatter(value))
        if len(line_values) >= values_per_line:
            handle.write(f"{indent}    {', '.join(line_values)},\n")
            line_values = []
    if line_values:
        handle.write(f"{indent}    {', '.join(line_values)},\n")
    handle.write(f"{indent}]{metadata}\n")


def format_usd_vec3(value: object) -> str:
    x, y, z = value
    return f"({float(x):.6f}, {float(y):.6f}, {float(z):.6f})"


def format_usd_vec2(value: object) -> str:
    u, v = value
    return f"({float(u):.8f}, {float(v):.8f})"


def write_usdz_archive(output_path: Path, files: list[tuple[str, Path]]) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output_path, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as archive:
        for arcname, source_path in files:
            info = zipfile.ZipInfo(arcname)
            info.compress_type = zipfile.ZIP_STORED
            info.extra = usdz_alignment_extra(
                current_offset=archive.fp.tell(),
                filename_length=len(arcname.encode("utf-8")),
            )
            with archive.open(info, "w") as destination:
                with source_path.open("rb") as source:
                    shutil.copyfileobj(source, destination, length=1024 * 1024)


def usdz_alignment_extra(current_offset: int, filename_length: int) -> bytes:
    local_header_length = 30 + filename_length
    padding_length = (64 - ((current_offset + local_header_length) % 64)) % 64
    if padding_length == 0:
        return b""
    if padding_length < 4:
        padding_length += 64
    payload_length = padding_length - 4
    return struct.pack("<HH", 0xCAFE, payload_length) + (b"\0" * payload_length)


def validate_glb_mesh_inputs(
    vertices: list[tuple[float, float, float]],
    indices: list[int],
    uvs: list[tuple[float, float]] | None,
) -> dict:
    if len(indices) % 3 != 0:
        return {"valid": False, "reason": "Index count is not divisible by three."}
    if not vertices or not indices:
        return {"valid": False, "reason": "Mesh is empty."}
    non_finite_vertex_count = sum(
        1 for vertex in vertices
        if len(vertex) != 3 or not all(math.isfinite(component) for component in vertex)
    )
    out_of_range_index_count = sum(1 for index in indices if index < 0 or index >= len(vertices))
    degenerate_face_count = 0
    for offset in range(0, len(indices), 3):
        face = (indices[offset], indices[offset + 1], indices[offset + 2])
        if len(set(face)) != 3:
            degenerate_face_count += 1
            continue
        if out_of_range_index_count == 0 and triangle_area(vertices[face[0]], vertices[face[1]], vertices[face[2]]) <= 1e-10:
            degenerate_face_count += 1
    uv_non_finite_count = 0
    uv_out_of_range_count = 0
    if uvs is not None:
        for u, v in uvs:
            if not math.isfinite(u) or not math.isfinite(v):
                uv_non_finite_count += 1
            elif not (0.0 <= u <= 1.0 and 0.0 <= v <= 1.0):
                uv_out_of_range_count += 1

    valid = (
        non_finite_vertex_count == 0
        and out_of_range_index_count == 0
        and degenerate_face_count == 0
        and uv_non_finite_count == 0
        and uv_out_of_range_count == 0
    )
    return {
        "valid": valid,
        "reason": None if valid else "GLB validation failed.",
        "nonFiniteVertexCount": non_finite_vertex_count,
        "outOfRangeIndexCount": out_of_range_index_count,
        "degenerateFaceCount": degenerate_face_count,
        "uvNonFiniteCount": uv_non_finite_count,
        "uvOutOfRangeCount": uv_out_of_range_count,
        "primitiveMode": 4,
        "materialAlphaMode": "OPAQUE",
    }


def align_bytearray(buffer: bytearray, alignment: int) -> None:
    padding = (-len(buffer)) % alignment
    if padding:
        buffer.extend(b"\x00" * padding)


def write_glb_file(output_path: Path, gltf: dict, binary: bytes) -> None:
    json_chunk = json.dumps(gltf, separators=(",", ":")).encode("utf-8")
    json_padding = (-len(json_chunk)) % 4
    if json_padding:
        json_chunk += b" " * json_padding
    bin_padding = (-len(binary)) % 4
    if bin_padding:
        binary += b"\x00" * bin_padding

    total_length = 12 + 8 + len(json_chunk) + 8 + len(binary)
    with output_path.open("wb") as output:
        output.write(struct.pack("<III", 0x46546C67, 2, total_length))
        output.write(struct.pack("<II", len(json_chunk), 0x4E4F534A))
        output.write(json_chunk)
        output.write(struct.pack("<II", len(binary), 0x004E4942))
        output.write(binary)


def mesh_geometry_preservation_stats(source: FusedMesh, fused: FusedMesh) -> dict:
    same_vertex_count = len(source.vertices) == len(fused.vertices)
    same_face_count = len(source.faces) == len(fused.faces)
    max_vertex_delta = None
    mean_vertex_delta = None
    if same_vertex_count:
        deltas = [
            length(subtract(source_vertex, fused_vertex))
            for source_vertex, fused_vertex in zip(source.vertices, fused.vertices)
        ]
        max_vertex_delta = max(deltas) if deltas else 0.0
        mean_vertex_delta = (sum(deltas) / len(deltas)) if deltas else 0.0

    face_mismatch_count = None
    if same_face_count:
        face_mismatch_count = sum(
            1 for source_face, fused_face in zip(source.faces, fused.faces)
            if source_face != fused_face
        )

    geometry_preserved = (
        same_vertex_count
        and same_face_count
        and (max_vertex_delta is not None and max_vertex_delta <= 1e-7)
        and face_mismatch_count == 0
    )
    return {
        "source": "arkit_fused_mesh",
        "fused": "fused_mesh",
        "sourceVertexCount": len(source.vertices),
        "sourceFaceCount": len(source.faces),
        "fusedVertexCount": len(fused.vertices),
        "fusedFaceCount": len(fused.faces),
        "sameVertexCount": same_vertex_count,
        "sameFaceCount": same_face_count,
        "maxVertexDeltaMeters": round(max_vertex_delta, 9) if max_vertex_delta is not None else None,
        "meanVertexDeltaMeters": round(mean_vertex_delta, 9) if mean_vertex_delta is not None else None,
        "faceMismatchCount": face_mismatch_count,
        "geometryPreserved": geometry_preserved,
    }


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


def load_projection_depth_frames(
    depth_frames: list[dict] | None,
    work_dir: Path | None,
) -> dict[str, ProjectionDepthFrame]:
    if not depth_frames or work_dir is None:
        return {}

    loaded: dict[str, ProjectionDepthFrame] = {}
    for depth_frame in depth_frames:
        resolution = depth_frame.get("depthResolution") or []
        transform = depth_frame.get("cameraTransform") or []
        intrinsics = depth_frame.get("intrinsics") or []
        depth_path = depth_frame.get("path")
        if len(resolution) != 2 or len(transform) != 16 or len(intrinsics) != 9 or not depth_path:
            continue

        width, height = int(resolution[0]), int(resolution[1])
        depth_file = work_dir / depth_path
        if width <= 0 or height <= 0 or not depth_file.exists():
            continue

        try:
            depth_values = read_float32_depth_values(depth_file, width, height)
            confidence_values = read_confidence_values(depth_frame, work_dir, width, height)
        except Exception as exc:
            logger.warning("Skipping depth frame for texture projection visibility: %s", exc)
            continue

        color_keyframe_id = str(depth_frame.get("colorKeyframeId")) if depth_frame.get("colorKeyframeId") else None
        frame = ProjectionDepthFrame(
            id=str(depth_frame.get("id")) if depth_frame.get("id") else None,
            color_keyframe_id=color_keyframe_id,
            width=width,
            height=height,
            world_to_camera=invert_rigid_transform(transform),
            intrinsics=intrinsics,
            depth_values=depth_values,
            confidence_values=confidence_values,
            timestamp=safe_float(depth_frame.get("timestamp")),
            path=str(depth_path),
            confidence_path=str(depth_frame.get("confidencePath")) if depth_frame.get("confidencePath") else None,
        )
        if color_keyframe_id:
            loaded[color_keyframe_id] = frame
        if frame.id:
            loaded.setdefault(frame.id, frame)

    return loaded


def load_projection_keyframes(
    keyframes: list[dict],
    keyframe_dir: Path,
    *,
    depth_frames: list[dict] | None = None,
    work_dir: Path | None = None,
) -> list[ProjectionKeyframe]:
    depth_by_keyframe_id = load_projection_depth_frames(depth_frames, work_dir)
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
            timestamp=safe_float(keyframe.get("timestamp")),
            captured_at=str(keyframe.get("capturedAt")) if keyframe.get("capturedAt") else None,
            color_correction=color_correction,
            depth_frame=depth_by_keyframe_id.get(str(keyframe.get("id"))) if keyframe.get("id") else None,
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
        center_bias = max(0.05, min(edge_margin / keyframe.center_bias_denominator, 1))
        depth_visibility = depth_visibility_for_world_point(vertex, keyframe)
        if depth_visibility.status == "occluded":
            continue
        distance_weight = 1 / max(depth, 0.2)
        weight = center_bias * distance_weight * depth_visibility.weight
        samples.append((weight, sample_image_nearest(keyframe, u, v)))

    if not samples:
        return None

    total_weight = sum(weight for weight, _ in samples)
    r = sum(weight * color[0] for weight, color in samples) / total_weight
    g = sum(weight * color[1] for weight, color in samples) / total_weight
    b = sum(weight * color[2] for weight, color in samples) / total_weight
    return (clamp_color(r), clamp_color(g), clamp_color(b))


def densify_lidar_surface_render_mesh(
    mesh: FusedMesh,
    *,
    max_edge_meters: float,
    max_face_count: int,
    max_iterations: int,
) -> tuple[FusedMesh, dict]:
    source_vertex_count = len(mesh.vertices)
    source_face_count = len(mesh.faces)
    source_edge_stats = mesh_edge_length_stats(mesh.vertices, mesh.faces)
    if (
        source_face_count == 0
        or source_face_count >= max_face_count
        or max_edge_meters <= 0
        or max_iterations <= 0
    ):
        stats = {
            "enabled": True,
            "algorithm": "lidar_surface_edge_subdivision",
            "used": False,
            "sourceVertexCount": source_vertex_count,
            "sourceFaceCount": source_face_count,
            "renderVertexCount": source_vertex_count,
            "renderFaceCount": source_face_count,
            "targetFaceCount": max_face_count,
            "targetMaxEdgeMeters": max_edge_meters,
            "iterations": 0,
            "splitFaceCount": 0,
            "midpointVertexCount": 0,
            "capReached": source_face_count >= max_face_count,
            "sourceEdgeLengthMeters": source_edge_stats,
            "renderEdgeLengthMeters": source_edge_stats,
            "reason": (
                "source render mesh is already at or above the densification face budget"
                if source_face_count >= max_face_count
                else "source render mesh has no faces to densify"
                if source_face_count == 0
                else "surface densification disabled by invalid limits"
            ),
        }
        return mesh, stats

    vertices = list(mesh.vertices)
    faces = list(mesh.faces)
    midpoint_cache: dict[tuple[int, int], int] = {}
    split_face_count = 0
    iterations = 0
    cap_reached = False

    for iteration in range(max_iterations):
        projected_face_count = len(faces)
        new_faces: list[tuple[int, int, int]] = []
        iteration_split_count = 0

        for face in faces:
            a, b, c = face
            if not valid_triangle_indices(face, len(vertices)):
                new_faces.append(face)
                continue

            edge_lengths = (
                length(subtract(vertices[b], vertices[a])),
                length(subtract(vertices[c], vertices[b])),
                length(subtract(vertices[a], vertices[c])),
            )
            longest_edge = max(edge_lengths)
            if longest_edge <= max_edge_meters:
                new_faces.append(face)
                continue

            if projected_face_count >= max_face_count:
                cap_reached = True
                new_faces.append(face)
                continue

            edge_index = edge_lengths.index(longest_edge)
            if edge_index == 0:
                midpoint = midpoint_vertex_index(vertices, midpoint_cache, a, b)
                new_faces.append((a, midpoint, c))
                new_faces.append((midpoint, b, c))
            elif edge_index == 1:
                midpoint = midpoint_vertex_index(vertices, midpoint_cache, b, c)
                new_faces.append((a, b, midpoint))
                new_faces.append((a, midpoint, c))
            else:
                midpoint = midpoint_vertex_index(vertices, midpoint_cache, c, a)
                new_faces.append((a, b, midpoint))
                new_faces.append((midpoint, b, c))
            projected_face_count += 1
            iteration_split_count += 1

        faces = new_faces
        if iteration_split_count == 0:
            break
        split_face_count += iteration_split_count
        iterations = iteration + 1

    render_edge_stats = mesh_edge_length_stats(vertices, faces)
    used = len(faces) > source_face_count
    stats = {
        "enabled": True,
        "algorithm": "lidar_surface_edge_subdivision",
        "used": used,
        "surfaceConstrained": True,
        "geometrySource": mesh.stats.get("geometrySource"),
        "sourceVertexCount": source_vertex_count,
        "sourceFaceCount": source_face_count,
        "renderVertexCount": len(vertices),
        "renderFaceCount": len(faces),
        "targetFaceCount": max_face_count,
        "targetMaxEdgeMeters": max_edge_meters,
        "iterations": iterations,
        "maxIterations": max_iterations,
        "splitFaceCount": split_face_count,
        "midpointVertexCount": len(vertices) - source_vertex_count,
        "capReached": cap_reached or len(faces) >= max_face_count,
        "faceIncreaseRatio": round(len(faces) / source_face_count, 4) if source_face_count else 0,
        "sourceEdgeLengthMeters": source_edge_stats,
        "renderEdgeLengthMeters": render_edge_stats,
        "reason": (
            "subdivided sparse LiDAR triangles for denser texture coverage"
            if used
            else "all LiDAR triangle edges already fit the densification threshold"
        ),
    }
    return FusedMesh(vertices=vertices, faces=faces, stats=mesh.stats), stats


def midpoint_vertex_index(
    vertices: list[tuple[float, float, float]],
    midpoint_cache: dict[tuple[int, int], int],
    first_index: int,
    second_index: int,
) -> int:
    key = (first_index, second_index) if first_index < second_index else (second_index, first_index)
    cached_index = midpoint_cache.get(key)
    if cached_index is not None:
        return cached_index

    first = vertices[first_index]
    second = vertices[second_index]
    midpoint = (
        (first[0] + second[0]) / 2,
        (first[1] + second[1]) / 2,
        (first[2] + second[2]) / 2,
    )
    vertices.append(midpoint)
    midpoint_index = len(vertices) - 1
    midpoint_cache[key] = midpoint_index
    return midpoint_index


def valid_triangle_indices(face: tuple[int, int, int], vertex_count: int) -> bool:
    return (
        0 <= face[0] < vertex_count
        and 0 <= face[1] < vertex_count
        and 0 <= face[2] < vertex_count
        and face[0] != face[1]
        and face[1] != face[2]
        and face[0] != face[2]
    )


def mesh_edge_length_stats(
    vertices: list[tuple[float, float, float]],
    faces: list[tuple[int, int, int]],
) -> dict:
    edge_keys: set[tuple[int, int]] = set()
    edge_lengths: list[float] = []
    invalid_edge_count = 0
    for face in faces:
        for first_index, second_index in (
            (face[0], face[1]),
            (face[1], face[2]),
            (face[2], face[0]),
        ):
            if not (0 <= first_index < len(vertices) and 0 <= second_index < len(vertices)):
                invalid_edge_count += 1
                continue
            if first_index == second_index:
                invalid_edge_count += 1
                continue
            key = (
                (first_index, second_index)
                if first_index < second_index
                else (second_index, first_index)
            )
            if key in edge_keys:
                continue
            edge_keys.add(key)
            edge_lengths.append(length(subtract(vertices[second_index], vertices[first_index])))

    if not edge_lengths:
        return {
            "edgeCount": 0,
            "invalidEdgeCount": invalid_edge_count,
            "min": 0,
            "mean": 0,
            "max": 0,
        }

    return {
        "edgeCount": len(edge_lengths),
        "invalidEdgeCount": invalid_edge_count,
        "min": round(min(edge_lengths), 5),
        "mean": round(sum(edge_lengths) / len(edge_lengths), 5),
        "max": round(max(edge_lengths), 5),
    }


def make_texture_render_mesh(mesh: FusedMesh, profile: ProcessingProfile | None = None) -> FusedMesh:
    profile = profile or current_full_quality_profile()
    source_face_count = len(mesh.faces)
    atlas_max_size = texture_atlas_max_size_for_mesh(mesh)
    preferred_target_face_count = (
        profile.texture_tsdf_render_target_faces
        if is_open3d_tsdf_mesh(mesh)
        else profile.texture_render_target_faces
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

    if profile.densify_texture_render_mesh and not is_open3d_tsdf_mesh(mesh):
        densified_mesh, densify_stats = densify_lidar_surface_render_mesh(
            mesh,
            max_edge_meters=FAST_ONBOARDING_TEXTURE_SURFACE_DENSIFY_MAX_EDGE_METERS,
            max_face_count=target_face_count,
            max_iterations=FAST_ONBOARDING_TEXTURE_SURFACE_DENSIFY_MAX_ITERATIONS,
        )
        if len(densified_mesh.faces) > source_face_count:
            render_stats = {
                **base_stats,
                **densify_stats,
                "used": True,
                "renderVertexCount": len(densified_mesh.vertices),
                "renderFaceCount": len(densified_mesh.faces),
                "geometryPreserved": False,
                "rawGeometryPreserved": bool(mesh.stats.get("geometryPreserved")),
                "smoothing": {
                    "enabled": False,
                    "scope": "surface subdivision preserves LiDAR triangle planes",
                },
            }
            return FusedMesh(
                vertices=densified_mesh.vertices,
                faces=densified_mesh.faces,
                stats={**mesh.stats, "textureRenderMesh": render_stats},
            )

    if profile.preserve_texture_render_mesh:
        render_stats = {
            **base_stats,
            "used": False,
            "algorithm": "none",
            "renderVertexCount": len(mesh.vertices),
            "renderFaceCount": source_face_count,
            "geometryPreserved": True,
            "rawGeometryPreserved": bool(mesh.stats.get("geometryPreserved")),
            "surfaceDensify": {
                "enabled": profile.densify_texture_render_mesh,
                "used": False,
                "reason": "no sparse LiDAR triangle subdivision was applied",
            },
            "smoothing": {
                "enabled": False,
                "scope": "disabled by profile",
            },
            "reason": f"{profile.name} preserves the ARKit/LiDAR mesh for texture projection.",
        }
        return FusedMesh(
            vertices=mesh.vertices,
            faces=mesh.faces,
            stats={**mesh.stats, "textureRenderMesh": render_stats},
        )

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
                    **simplify_stats.get("renderSmoothing", {"enabled": False}),
                    "scope": "photoreal render mesh only",
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
    render_smoothing_stats = smooth_open3d_tsdf_render_mesh(simplified, np)
    plane_stats = regularize_open3d_render_planes(simplified, np, o3d)
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
        "renderSmoothing": render_smoothing_stats,
        "planeRegularization": plane_stats,
    }
    return FusedMesh(vertices=vertices, faces=faces, stats=stats), stats


def smooth_open3d_tsdf_render_mesh(triangle_mesh, np) -> dict:
    if TEXTURE_TSDF_RENDER_EXTRA_SMOOTHING_ITERATIONS <= 0:
        return {"enabled": False, "reason": "disabled"}

    vertex_count = len(triangle_mesh.vertices)
    face_count = len(triangle_mesh.triangles)
    if vertex_count == 0 or face_count == 0:
        return {"enabled": False, "reason": "empty mesh", "vertexCount": vertex_count, "faceCount": face_count}

    before = np.asarray(triangle_mesh.vertices, dtype=np.float64).copy()
    smoothed = triangle_mesh.filter_smooth_taubin(
        number_of_iterations=TEXTURE_TSDF_RENDER_EXTRA_SMOOTHING_ITERATIONS
    )
    triangle_mesh.vertices = smoothed.vertices
    triangle_mesh.triangles = smoothed.triangles
    triangle_mesh.compute_vertex_normals()
    after = np.asarray(triangle_mesh.vertices, dtype=np.float64)
    if len(before) != len(after):
        return {
            "enabled": True,
            "algorithm": "open3d_taubin_render_mesh",
            "iterations": TEXTURE_TSDF_RENDER_EXTRA_SMOOTHING_ITERATIONS,
            "vertexCountChanged": True,
        }

    displacements = np.linalg.norm(after - before, axis=1)
    moved = displacements[displacements > 1e-5]
    return {
        "enabled": True,
        "algorithm": "open3d_taubin_render_mesh",
        "iterations": TEXTURE_TSDF_RENDER_EXTRA_SMOOTHING_ITERATIONS,
        "movedVertexCount": int(len(moved)),
        "meanDisplacementMeters": round(float(np.mean(displacements)) if len(displacements) else 0, 5),
        "maxDisplacementMeters": round(float(np.max(displacements)) if len(displacements) else 0, 5),
    }


def regularize_open3d_render_planes(triangle_mesh, np, o3d) -> dict:
    if not TEXTURE_RENDER_PLANE_REGULARIZATION_ENABLED:
        return {"enabled": False, "reason": "disabled"}

    vertex_count = len(triangle_mesh.vertices)
    face_count = len(triangle_mesh.triangles)
    if vertex_count < TEXTURE_RENDER_PLANE_MIN_VERTICES or face_count == 0:
        return {
            "enabled": False,
            "reason": "mesh too small for stable plane detection",
            "vertexCount": vertex_count,
            "faceCount": face_count,
        }

    triangle_mesh.compute_vertex_normals()
    vertices = np.asarray(triangle_mesh.vertices, dtype=np.float64)
    normals = np.asarray(triangle_mesh.vertex_normals, dtype=np.float64)
    min_plane_vertices = max(
        TEXTURE_RENDER_PLANE_MIN_VERTICES,
        int(vertex_count * TEXTURE_RENDER_PLANE_MIN_VERTEX_RATIO),
    )
    remaining_indices = np.arange(vertex_count)
    displacement_sum = np.zeros_like(vertices)
    displacement_count = np.zeros(vertex_count, dtype=np.int32)
    planes: list[dict] = []

    for plane_index in range(TEXTURE_RENDER_PLANE_MAX_PLANES):
        if len(remaining_indices) < min_plane_vertices:
            break

        point_cloud = o3d.geometry.PointCloud()
        point_cloud.points = o3d.utility.Vector3dVector(vertices[remaining_indices])
        try:
            plane_model, local_inliers = point_cloud.segment_plane(
                distance_threshold=TEXTURE_RENDER_PLANE_DISTANCE_THRESHOLD_METERS,
                ransac_n=3,
                num_iterations=700,
            )
        except RuntimeError:
            break

        if len(local_inliers) < min_plane_vertices:
            break

        global_inliers = remaining_indices[np.asarray(local_inliers, dtype=np.int64)]
        normal = np.asarray(plane_model[:3], dtype=np.float64)
        norm = float(np.linalg.norm(normal))
        if not math.isfinite(norm) or norm <= 1e-8:
            break

        normal = normal / norm
        plane_offset = float(plane_model[3]) / norm
        distances = vertices[global_inliers] @ normal + plane_offset
        normal_alignment = np.abs(normals[global_inliers] @ normal)
        selected_mask = (
            (np.abs(distances) <= TEXTURE_RENDER_PLANE_DISTANCE_THRESHOLD_METERS * 2.25)
            & (normal_alignment >= TEXTURE_RENDER_PLANE_NORMAL_ALIGNMENT)
        )
        selected_indices = global_inliers[selected_mask]
        if len(selected_indices) >= min_plane_vertices:
            selected_distances = vertices[selected_indices] @ normal + plane_offset
            displacements = -selected_distances[:, None] * normal[None, :] * TEXTURE_RENDER_PLANE_STRENGTH
            displacement_lengths = np.linalg.norm(displacements, axis=1)
            clamp_mask = displacement_lengths > TEXTURE_RENDER_PLANE_MAX_DISPLACEMENT_METERS
            if np.any(clamp_mask):
                displacements[clamp_mask] *= (
                    TEXTURE_RENDER_PLANE_MAX_DISPLACEMENT_METERS / displacement_lengths[clamp_mask]
                )[:, None]

            displacement_sum[selected_indices] += displacements
            displacement_count[selected_indices] += 1
            normal_abs = np.abs(normal)
            dominant_axis = ("x", "y", "z")[int(np.argmax(normal_abs))]
            planes.append({
                "planeIndex": plane_index,
                "inlierVertexCount": int(len(global_inliers)),
                "regularizedVertexCount": int(len(selected_indices)),
                "dominantAxis": dominant_axis,
                "normal": [round(float(component), 4) for component in normal],
                "offset": round(plane_offset, 6),
                "meanAbsDistanceMeters": round(float(np.mean(np.abs(selected_distances))), 5),
                "maxAbsDistanceMeters": round(float(np.max(np.abs(selected_distances))), 5),
            })

        remaining_mask = np.ones(len(remaining_indices), dtype=bool)
        remaining_mask[np.asarray(local_inliers, dtype=np.int64)] = False
        remaining_indices = remaining_indices[remaining_mask]

    moved_mask = displacement_count > 0
    moved_vertex_count = int(np.count_nonzero(moved_mask))
    if moved_vertex_count == 0:
        return {
            "enabled": True,
            "algorithm": "open3d_ransac_large_plane_vertex_projection",
            "planeCount": len(planes),
            "movedVertexCount": 0,
            "minPlaneVertexCount": int(min_plane_vertices),
            "planes": planes,
        }

    adjusted_vertices = vertices.copy()
    adjusted_vertices[moved_mask] += displacement_sum[moved_mask] / displacement_count[moved_mask, None]
    displacements = np.linalg.norm(adjusted_vertices[moved_mask] - vertices[moved_mask], axis=1)
    triangle_mesh.vertices = o3d.utility.Vector3dVector(adjusted_vertices)
    triangle_mesh.compute_vertex_normals()

    return {
        "enabled": True,
        "algorithm": "open3d_ransac_large_plane_vertex_projection",
        "planeCount": len(planes),
        "movedVertexCount": moved_vertex_count,
        "movedVertexRatio": round(moved_vertex_count / max(vertex_count, 1), 4),
        "minPlaneVertexCount": int(min_plane_vertices),
        "maxDisplacementMeters": round(float(np.max(displacements)), 5),
        "meanDisplacementMeters": round(float(np.mean(displacements)), 5),
        "strength": TEXTURE_RENDER_PLANE_STRENGTH,
        "distanceThresholdMeters": TEXTURE_RENDER_PLANE_DISTANCE_THRESHOLD_METERS,
        "planes": planes,
    }


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


def compute_vertex_normals(
    vertices: list[tuple[float, float, float]],
    faces: list[tuple[int, int, int]],
) -> list[tuple[float, float, float]]:
    accumulators = [(0.0, 0.0, 0.0) for _ in vertices]
    for face in faces:
        a, b, c = vertices[face[0]], vertices[face[1]], vertices[face[2]]
        normal = cross(subtract(b, a), subtract(c, a))
        for vertex_index in face:
            accumulators[vertex_index] = add(accumulators[vertex_index], normal)

    normals: list[tuple[float, float, float]] = []
    for normal in accumulators:
        normalized = normalize(normal)
        normals.append(normalized if normalized != (0.0, 0.0, 0.0) else (0.0, 1.0, 0.0))
    return normals


def obj_face_line(face: tuple[int, int, int], uv_start: int, include_normals: bool = True) -> str:
    if include_normals:
        return (
            f"f {face[0] + 1}/{uv_start}/{face[0] + 1} "
            f"{face[1] + 1}/{uv_start + 1}/{face[1] + 1} "
            f"{face[2] + 1}/{uv_start + 2}/{face[2] + 1}"
        )

    return f"f {face[0] + 1}/{uv_start} {face[1] + 1}/{uv_start + 1} {face[2] + 1}/{uv_start + 2}"


async def write_textured_obj(
    mesh: FusedMesh,
    keyframes: list[ProjectionKeyframe],
    output_obj_path: Path,
    output_mtl_path: Path,
    output_texture_path: Path,
    output_debug_path: Path | None = None,
    output_debug_preview_path: Path | None = None,
    output_projection_overlay_dir: Path | None = None,
    output_textured_usdz_path: Path | None = None,
    output_textured_glb_path: Path | None = None,
    output_uv_checker_glb_path: Path | None = None,
    output_coverage_debug_glb_path: Path | None = None,
    output_coverage_debug_report_path: Path | None = None,
    report_progress: Callable[[float, str], Awaitable[None]] | None = None,
    is_cancelled: CancellationCheck | None = None,
    profile: ProcessingProfile | None = None,
) -> dict:
    profile = profile or current_full_quality_profile()
    source_keyframes = list(keyframes)
    dense_single_view_enabled = profile.dense_single_view_texture and bool(source_keyframes)
    dense_single_view_stats: dict | None = None
    active_projection_mode = (
        "dense_single_view"
        if dense_single_view_enabled
        else profile.planar_chart_projection_mode
    )
    if dense_single_view_enabled:
        keyframes, dense_single_view_stats = select_dense_single_view_texture_keyframe(
            mesh,
            source_keyframes,
        )
    face_count = len(mesh.faces)
    atlas_max_size = texture_atlas_max_size_for_mesh(mesh)
    atlas_layout_spec = build_texture_atlas_layout(mesh, atlas_max_size=atlas_max_size)
    atlas_width = atlas_layout_spec.width
    atlas_height = atlas_layout_spec.height
    tile_size = atlas_layout_spec.tile_size
    columns = atlas_layout_spec.columns
    tile_padding = atlas_tile_padding(tile_size)
    dilation_pixels = texture_dilation_pixels(tile_size)
    texture = Image.new("RGB", (atlas_width, atlas_height), FALLBACK_COLOR)
    texture_mask = Image.new("L", (atlas_width, atlas_height), 0)
    texture_pixels = texture.load()
    mask_pixels = texture_mask.load()
    vt_lines: list[str] = []
    face_lines: list[str] = []
    face_uvs: FaceUVs = []
    textured_face_count = 0
    fallback_face_count = 0
    rasterized_pixel_count = 0
    projected_pixel_count = 0
    fallback_pixel_count = 0
    neighbor_filled_pixel_count = 0
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
    rejected_depth_edge_sample_count = 0
    rejected_occluded_sample_count = 0
    depth_tested_sample_count = 0
    missing_depth_sample_count = 0
    color_correction = texture_color_correction_for_keyframes(keyframes)
    uv_min_u = math.inf
    uv_min_v = math.inf
    uv_max_u = -math.inf
    uv_max_v = -math.inf
    uv_out_of_range_count = 0
    uv_non_finite_count = 0
    coverage_face_statuses = ["unknown"] * face_count
    visibility_stats = build_keyframe_mesh_visibility_masks(mesh, keyframes)
    face_owner_labels, coherent_label_stats = assign_coherent_face_keyframes(mesh, keyframes)
    source_image_atlas_layout = (
        build_source_image_atlas_layout(
            keyframes=keyframes,
            face_owner_labels=face_owner_labels,
            face_to_chart=atlas_layout_spec.face_to_chart,
            atlas_max_size=atlas_max_size,
            tile_start_y=atlas_layout_spec.tile_start_y,
        )
        if should_use_source_image_projection_atlas(
            profile=profile,
            active_projection_mode=active_projection_mode,
            keyframes=keyframes,
        )
        else None
    )
    if source_image_atlas_layout is not None:
        atlas_width = source_image_atlas_layout.width
        atlas_height = source_image_atlas_layout.height
        source_background_color = (
            TEXTURE_UNOBSERVED_COLOR
            if active_projection_mode in {"direct", "dense_single_view"}
            else FALLBACK_COLOR
        )
        texture = Image.new("RGB", (atlas_width, atlas_height), source_background_color)
        texture_mask = Image.new("L", (atlas_width, atlas_height), 0)
        texture_pixels = texture.load()
        mask_pixels = texture_mask.load()
        atlas_layout_spec.width = atlas_width
        atlas_layout_spec.height = atlas_height
        atlas_layout_spec.strategy = (
            "planar_chart_atlas_with_source_keyframe_projection"
            if atlas_layout_spec.planar_charts
            else "source_keyframe_projection_atlas"
        )
        atlas_layout_spec.stats = {
            **atlas_layout_spec.stats,
            "enabled": True,
            "reason": "source_image_projection_atlas",
            "sourceImageAtlas": source_image_atlas_layout.stats,
        }
        paste_source_image_atlas_tiles(
            texture,
            texture_mask,
            source_image_atlas_layout,
        )
        texture_pixels = texture.load()
        mask_pixels = texture_mask.load()
    serial_texture_budget_enabled = (
        atlas_layout_spec.planar_charts
        or profile.fallback_texture_face_limit is not None
        or active_projection_mode in {"direct", "dense_single_view"}
    )
    parallel_worker_count = 1 if serial_texture_budget_enabled else texture_parallel_worker_count(face_count)
    parallel_result = None
    if parallel_worker_count > 1:
        try:
            parallel_result = await rasterize_texture_atlas_parallel(
                mesh=mesh,
                keyframes=keyframes,
                atlas_width=atlas_width,
                atlas_height=atlas_height,
                tile_size=tile_size,
                columns=columns,
                dilation_pixels=dilation_pixels,
                worker_count=parallel_worker_count,
                report_progress=report_progress,
                is_cancelled=is_cancelled,
            )
        except Exception as exc:  # pragma: no cover - defensive fallback for production worker issues.
            logger.exception("Parallel texture atlas rendering failed; falling back to serial renderer: %s", exc)
            parallel_result = None

    if parallel_result is not None:
        texture = parallel_result["texture"]
        texture_pixels = texture.load()
        vt_lines = parallel_result["vt_lines"]
        face_lines = parallel_result["face_lines"]
        face_uvs = parallel_result["face_uvs"]
        textured_face_count = parallel_result["textured_face_count"]
        fallback_face_count = parallel_result["fallback_face_count"]
        rasterized_pixel_count = parallel_result["rasterized_pixel_count"]
        projected_pixel_count = parallel_result["projected_pixel_count"]
        fallback_pixel_count = parallel_result["fallback_pixel_count"]
        neighbor_filled_pixel_count = parallel_result.get("neighbor_filled_pixel_count", 0)
        dilated_pixel_count = parallel_result["dilated_pixel_count"]
        uv_vertex_sample_stats = parallel_result["uv_vertex_sample_stats"]
        uv_face_interior_sample_stats = parallel_result["uv_face_interior_sample_stats"]
        selected_keyframe_face_counts = parallel_result["selected_keyframe_face_counts"]
        keyframe_contribution_counts = parallel_result["keyframe_contribution_counts"]
        blended_pixel_count = parallel_result["blended_pixel_count"]
        single_sample_pixel_count = parallel_result["single_sample_pixel_count"]
        accepted_projection_sample_count = parallel_result["accepted_projection_sample_count"]
        rejected_overexposed_sample_count = parallel_result["rejected_overexposed_sample_count"]
        rejected_underexposed_sample_count = parallel_result["rejected_underexposed_sample_count"]
        rejected_edge_sample_count = parallel_result["rejected_edge_sample_count"]
        rejected_grazing_sample_count = parallel_result["rejected_grazing_sample_count"]
        rejected_invalid_projection_sample_count = parallel_result["rejected_invalid_projection_sample_count"]
        rejected_depth_edge_sample_count = parallel_result["rejected_depth_edge_sample_count"]
        rejected_occluded_sample_count = parallel_result["rejected_occluded_sample_count"]
        depth_tested_sample_count = parallel_result["depth_tested_sample_count"]
        missing_depth_sample_count = parallel_result["missing_depth_sample_count"]
        uv_min_u = parallel_result["uv_min_u"]
        uv_min_v = parallel_result["uv_min_v"]
        uv_max_u = parallel_result["uv_max_u"]
        uv_max_v = parallel_result["uv_max_v"]
        uv_out_of_range_count = parallel_result["uv_out_of_range_count"]
        uv_non_finite_count = parallel_result["uv_non_finite_count"]
        coverage_face_statuses = [
            "candidate" if label is not None else "fallback_unobserved"
            for label in face_owner_labels
        ]
    else:
        parallel_worker_count = 1

    fallback_high_quality_faces, fallback_budget_stats = fallback_texture_face_budget(
        mesh,
        atlas_layout_spec,
        profile.fallback_texture_face_limit if parallel_result is None else None,
    )
    fallback_total = face_count - len(atlas_layout_spec.face_to_chart)
    fallback_progress_interval = max(1, min(max(fallback_total, 1) // 50, 1_000))
    fallback_processed_count = 0
    fallback_projected_count = 0
    solid_projected_face_count = 0
    solid_fallback_face_count = 0
    if active_projection_mode in {"direct", "dense_single_view"}:
        solid_scene_color = TEXTURE_UNOBSERVED_COLOR
        solid_scene_color_projected = False
    else:
        solid_scene_color = FALLBACK_COLOR
        solid_scene_color_projected = False

    planar_chart_contexts: dict[int, dict] = {}
    planar_chart_texture_stats: list[dict] = []
    if parallel_result is None and atlas_layout_spec.planar_charts:
        if report_progress is not None:
            await report_progress(
                82.5,
                (
                    f"Texturing {len(atlas_layout_spec.planar_charts)} planar room charts "
                    f"({active_projection_mode}, stride {max(1, profile.planar_chart_raster_stride)})"
                ),
            )
            await asyncio.sleep(0)

        for chart_index, chart in enumerate(atlas_layout_spec.planar_charts):
            if is_cancelled is not None and is_cancelled():
                raise asyncio.CancelledError

            region_points = chart_region_points(chart)
            all_candidates = texture_projection_candidates_for_region(
                region_points,
                chart.normal,
                keyframes,
                relaxed=active_projection_mode == "dense_single_view",
            )
            owner_candidate = (
                all_candidates[0]
                if active_projection_mode in {"direct", "dense_single_view"} and all_candidates
                else None
            )
            candidates = [owner_candidate] if owner_candidate is not None else all_candidates
            if candidates:
                selected_key = candidates[0].keyframe_debug_id
                selected_keyframe_face_counts[selected_key] = (
                    selected_keyframe_face_counts.get(selected_key, 0) + len(chart.face_indices)
                )

            fallback_color: tuple[int, int, int] | None = None

            def resolve_chart_fallback_color() -> tuple[int, int, int]:
                nonlocal fallback_color
                if fallback_color is None:
                    if active_projection_mode == "dense_single_view":
                        fallback_color = (
                            average_dense_single_view_surface_color(region_points, chart.normal, keyframes)
                            or TEXTURE_UNOBSERVED_COLOR
                        )
                    elif active_projection_mode == "direct":
                        fallback_color = TEXTURE_UNOBSERVED_COLOR
                    else:
                        fallback_color = average_projected_color(region_points, keyframes) or FALLBACK_COLOR
                return fallback_color

            raster_stats = rasterize_planar_chart_texture(
                texture_pixels,
                mask_pixels,
                chart,
                candidates,
                resolve_chart_fallback_color,
                secondary_candidates=all_candidates[1:] if owner_candidate is not None else [],
                sample_stride=max(1, profile.planar_chart_raster_stride),
                projection_mode=active_projection_mode,
            )
            planar_chart_contexts[chart.chart_id] = {
                "regionPoints": region_points,
                "fallbackColor": fallback_color,
                "stats": raster_stats,
            }
            for key, value in raster_stats["keyframeContributionCounts"].items():
                keyframe_contribution_counts[key] = keyframe_contribution_counts.get(key, 0) + value
            rasterized_pixel_count += raster_stats["filledPixelCount"]
            projected_pixel_count += raster_stats["projectedPixelCount"]
            fallback_pixel_count += raster_stats["fallbackPixelCount"]
            neighbor_filled_pixel_count += raster_stats.get(
                "localFilledPixelCount",
                raster_stats.get("neighborFilledPixelCount", 0),
            )
            blended_pixel_count += raster_stats["blendedPixelCount"]
            single_sample_pixel_count += raster_stats["singleSamplePixelCount"]
            accepted_projection_sample_count += raster_stats["acceptedProjectionSampleCount"]
            rejected_overexposed_sample_count += raster_stats["rejectedOverexposedSampleCount"]
            rejected_underexposed_sample_count += raster_stats["rejectedUnderexposedSampleCount"]
            rejected_edge_sample_count += raster_stats["rejectedEdgeSampleCount"]
            rejected_grazing_sample_count += raster_stats["rejectedGrazingSampleCount"]
            rejected_invalid_projection_sample_count += raster_stats["rejectedInvalidProjectionSampleCount"]
            rejected_depth_edge_sample_count += raster_stats["rejectedDepthEdgeSampleCount"]
            rejected_occluded_sample_count += raster_stats["rejectedOccludedSampleCount"]
            depth_tested_sample_count += raster_stats["depthTestedSampleCount"]
            missing_depth_sample_count += raster_stats["missingDepthSampleCount"]
            if raster_stats["projectedPixelCount"] > 0:
                textured_face_count += len(chart.face_indices)
                chart_coverage_status = "projected"
            else:
                fallback_face_count += len(chart.face_indices)
                chart_coverage_status = "fallback_unobserved" if active_projection_mode == "direct" else "fallback"
            for chart_face_index in chart.face_indices:
                if 0 <= chart_face_index < len(coverage_face_statuses):
                    coverage_face_statuses[chart_face_index] = chart_coverage_status
            owner_angle_degrees = None
            owner_depth_error_meters = None
            owner_depth_status = None
            if owner_candidate is not None:
                center_point = region_points[0]
                owner_angle_degrees = round(
                    math.degrees(math.acos(clamp_float(abs(owner_candidate.facing), -1.0, 1.0))),
                    3,
                )
                owner_depth_visibility = depth_visibility_for_world_point(center_point, owner_candidate.keyframe)
                owner_depth_status = owner_depth_visibility.status
                if (
                    owner_depth_visibility.projected_depth is not None
                    and owner_depth_visibility.sampled_depth is not None
                ):
                    owner_depth_error_meters = round(
                        abs(owner_depth_visibility.sampled_depth - owner_depth_visibility.projected_depth),
                        4,
                    )
            planar_chart_texture_stats.append({
                **planar_chart_stats(chart),
                "candidateKeyframeCount": len(all_candidates),
                "rasterCandidateKeyframeCount": len(candidates),
                "ownerKeyframeId": owner_candidate.keyframe_debug_id if owner_candidate is not None else None,
                "ownerAngleDegrees": owner_angle_degrees,
                "ownerDepthErrorMeters": owner_depth_error_meters,
                "ownerDepthStatus": owner_depth_status,
                "sampleStride": max(1, profile.planar_chart_raster_stride),
                "projectionMode": active_projection_mode,
            })
            if report_progress is not None:
                await report_progress(
                    82.5 + ((chart_index + 1) / len(atlas_layout_spec.planar_charts)) * 0.5,
                    f"Textured wall chart {chart_index + 1} / {len(atlas_layout_spec.planar_charts)}",
                )
                await asyncio.sleep(0)

    for face_index, face in enumerate(mesh.faces) if parallel_result is None else []:
        if is_cancelled is not None and is_cancelled():
            raise asyncio.CancelledError

        chart = atlas_layout_spec.face_to_chart.get(face_index)
        face_vertices = [mesh.vertices[face[0]], mesh.vertices[face[1]], mesh.vertices[face[2]]]
        source_projection: dict | None = None
        source_candidates: list[TextureProjectionCandidate] = []
        tile: tuple[int, int] | None = None
        if chart is not None:
            atlas_triangle = [planar_chart_pixel_for_vertex(chart, vertex) for vertex in face_vertices]
        elif source_image_atlas_layout is not None:
            fallback_processed_count += 1
            source_candidates = texture_projection_candidates(
                face_vertices,
                keyframes,
                relaxed=active_projection_mode == "dense_single_view",
                face_index=face_index,
            )
            source_candidates = prioritize_owner_candidate(source_candidates, face_owner_labels[face_index])
            source_projection = source_image_atlas_face_projection(
                face_vertices,
                source_candidates,
                source_image_atlas_layout,
            )
            atlas_triangle = (
                source_projection["atlasPoints"]
                if source_projection is not None and source_projection.get("atlasPoints") is not None
                else source_image_atlas_fallback_triangle(source_image_atlas_layout)
            )
        else:
            tile = atlas_tile_for_layout(face_index, atlas_layout_spec)
            atlas_triangle = atlas_triangle_points(tile, tile_size)

        uv_start = len(vt_lines) + 1
        current_face_uvs: list[tuple[float, float]] = []
        for point in atlas_triangle:
            u = (point[0] + 0.5) / atlas_width
            v = 1 - ((point[1] + 0.5) / atlas_height)
            current_face_uvs.append((u, v))
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
        face_uvs.append((current_face_uvs[0], current_face_uvs[1], current_face_uvs[2]))

        if chart is not None:
            for point in atlas_triangle:
                uv_vertex_sample_stats.add(sample_texture_at_atlas_point(texture_pixels, atlas_width, atlas_height, point[0], point[1]))
            for point in atlas_interior_sample_points(atlas_triangle):
                uv_face_interior_sample_stats.add(sample_texture_at_atlas_point(texture_pixels, atlas_width, atlas_height, point[0], point[1]))
            face_lines.append(obj_face_line(face, uv_start))
            continue

        if source_image_atlas_layout is not None:
            if source_projection is not None:
                rejected_invalid_projection_sample_count += int(source_projection.get("rejectedInvalidProjectionSampleCount", 0))
                rejected_depth_edge_sample_count += int(source_projection.get("rejectedDepthEdgeSampleCount", 0))
                rejected_occluded_sample_count += int(source_projection.get("rejectedOccludedSampleCount", 0))
                depth_tested_sample_count += int(source_projection.get("depthTestedSampleCount", 0))
                missing_depth_sample_count += int(source_projection.get("missingDepthSampleCount", 0))

            if source_projection is not None and source_projection.get("atlasPoints") is not None:
                selected_key = str(source_projection["keyframe"])
                selected_keyframe_face_counts[selected_key] = selected_keyframe_face_counts.get(selected_key, 0) + 1
                keyframe_contribution_counts[selected_key] = keyframe_contribution_counts.get(selected_key, 0) + 1
                rasterized_pixel_count += 1
                projected_pixel_count += 1
                single_sample_pixel_count += 1
                accepted_projection_sample_count += 1
                textured_face_count += 1
                fallback_projected_count += 1
                coverage_face_statuses[face_index] = "projected_source_atlas"
            else:
                rasterized_pixel_count += 1
                fallback_pixel_count += 1
                fallback_face_count += 1
                coverage_face_statuses[face_index] = "fallback_unobserved"

            for point in atlas_triangle:
                uv_vertex_sample_stats.add(
                    sample_texture_at_atlas_point(texture_pixels, atlas_width, atlas_height, point[0], point[1])
                )
            for point in atlas_interior_sample_points(atlas_triangle):
                uv_face_interior_sample_stats.add(
                    sample_texture_at_atlas_point(texture_pixels, atlas_width, atlas_height, point[0], point[1])
                )

            face_lines.append(obj_face_line(face, uv_start))
            if report_progress is not None and (
                fallback_processed_count % fallback_progress_interval == 0
                or fallback_processed_count == fallback_total
            ):
                fraction = (fallback_processed_count / fallback_total) if fallback_total else 1
                coverage = int((fallback_projected_count / fallback_processed_count) * 100) if fallback_processed_count else 0
                await report_progress(
                    83 + fraction * 10,
                    (
                        f"Projecting source-atlas faces {fallback_processed_count} / {fallback_total} "
                        f"({coverage}% assigned)"
                    ),
                )
                await asyncio.sleep(0)
            continue

        fallback_processed_count += 1
        if tile is None:
            tile = atlas_tile_for_layout(face_index, atlas_layout_spec)
        if face_index in fallback_high_quality_faces:
            candidates = texture_projection_candidates(
                face_vertices,
                keyframes,
                relaxed=active_projection_mode == "dense_single_view",
                face_index=face_index,
            )
            candidates = prioritize_owner_candidate(candidates, face_owner_labels[face_index])
            if candidates:
                selected_key = candidates[0].keyframe_debug_id
                selected_keyframe_face_counts[selected_key] = selected_keyframe_face_counts.get(selected_key, 0) + 1
            fallback_color: tuple[int, int, int] | None = None

            def resolve_fallback_color() -> tuple[int, int, int]:
                nonlocal fallback_color
                if fallback_color is None:
                    face_normal = triangle_normal(face_vertices[0], face_vertices[1], face_vertices[2])
                    sample_points = [
                        face_vertices[0],
                        face_vertices[1],
                        face_vertices[2],
                        triangle_center(face_vertices[0], face_vertices[1], face_vertices[2]),
                    ]
                    if active_projection_mode == "dense_single_view":
                        fallback_color = (
                            average_dense_single_view_surface_color(
                                sample_points,
                                face_normal,
                                keyframes,
                            )
                            or TEXTURE_UNOBSERVED_COLOR
                        )
                    elif active_projection_mode == "direct":
                        fallback_color = TEXTURE_UNOBSERVED_COLOR
                    else:
                        fallback_color = average_projected_color(face_vertices, keyframes) or FALLBACK_COLOR
                return fallback_color

            raster_stats = rasterize_face_texture(
                texture_pixels,
                mask_pixels,
                atlas_triangle,
                face_vertices,
                candidates,
                resolve_fallback_color,
                projection_mode=active_projection_mode,
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
            rejected_depth_edge_sample_count += raster_stats["rejectedDepthEdgeSampleCount"]
            rejected_occluded_sample_count += raster_stats["rejectedOccludedSampleCount"]
            depth_tested_sample_count += raster_stats["depthTestedSampleCount"]
            missing_depth_sample_count += raster_stats["missingDepthSampleCount"]
            if raster_stats["projectedPixelCount"] > 0:
                textured_face_count += 1
                fallback_projected_count += 1
                coverage_face_statuses[face_index] = "projected"
            else:
                fallback_face_count += 1
                coverage_face_statuses[face_index] = "fallback_unobserved" if active_projection_mode == "direct" else "fallback"
        else:
            solid_color = solid_scene_color
            solid_is_projected = solid_scene_color_projected
            if active_projection_mode in {"direct", "dense_single_view"}:
                direct_sample = (
                    dense_single_view_surface_color(
                        triangle_center(face_vertices[0], face_vertices[1], face_vertices[2]),
                        triangle_normal(face_vertices[0], face_vertices[1], face_vertices[2]),
                        keyframes,
                    )
                    if active_projection_mode == "dense_single_view"
                    else direct_projected_surface_color(
                        triangle_center(face_vertices[0], face_vertices[1], face_vertices[2]),
                        triangle_normal(face_vertices[0], face_vertices[1], face_vertices[2]),
                        keyframes,
                    )
                )
                if direct_sample is not None:
                    solid_color = direct_sample[0]
                    solid_is_projected = True
            solid_stats = fill_solid_texture_tile(
                texture_pixels,
                mask_pixels,
                tile,
                tile_size,
                solid_color,
            )
            filled_pixels = solid_stats["filledPixelCount"]
            rasterized_pixel_count += filled_pixels
            if solid_is_projected:
                projected_pixel_count += filled_pixels
                single_sample_pixel_count += filled_pixels
                accepted_projection_sample_count += filled_pixels
                textured_face_count += 1
                fallback_projected_count += 1
                solid_projected_face_count += 1
                coverage_face_statuses[face_index] = "projected_solid"
            else:
                fallback_pixel_count += filled_pixels
                fallback_face_count += 1
                solid_fallback_face_count += 1
                coverage_face_statuses[face_index] = "fallback_unobserved" if active_projection_mode == "direct" else "fallback"

        for point in atlas_triangle:
            uv_vertex_sample_stats.add(sample_texture_at_atlas_point(texture_pixels, atlas_width, atlas_height, point[0], point[1]))
        for point in atlas_interior_sample_points(atlas_triangle):
            uv_face_interior_sample_stats.add(sample_texture_at_atlas_point(texture_pixels, atlas_width, atlas_height, point[0], point[1]))

        face_lines.append(obj_face_line(face, uv_start))
        if report_progress is not None and (
            fallback_processed_count % fallback_progress_interval == 0
            or fallback_processed_count == fallback_total
        ):
            fraction = (fallback_processed_count / fallback_total) if fallback_total else 1
            coverage = int((fallback_projected_count / fallback_processed_count) * 100) if fallback_processed_count else 0
            solid_count = solid_projected_face_count + solid_fallback_face_count
            await report_progress(
                83 + fraction * 10,
                (
                    f"Texturing fallback faces {fallback_processed_count} / {fallback_total} "
                    f"({coverage}% projected, {solid_count} solid shaded)"
                ),
            )
            await asyncio.sleep(0)

    if planar_chart_texture_stats:
        for chart_stats in planar_chart_texture_stats:
            context_stats = planar_chart_contexts.get(int(chart_stats["chartId"]), {}).get("stats", {})
            filled = int(context_stats.get("filledPixelCount", 0))
            projected = int(context_stats.get("projectedPixelCount", 0))
            local_filled = int(context_stats.get(
                "localFilledPixelCount",
                context_stats.get("neighborFilledPixelCount", 0),
            ))
            chart_stats["rasterizedPixelCount"] = filled
            chart_stats["projectedPixelCount"] = projected
            chart_stats["fallbackPixelCount"] = int(context_stats.get("fallbackPixelCount", 0))
            chart_stats["localFilledPixelCount"] = local_filled
            chart_stats["neighborFilledPixelCount"] = local_filled
            chart_stats["secondaryFilledPixelCount"] = int(context_stats.get("secondaryFilledPixelCount", 0))
            chart_stats["secondaryRegionCount"] = int(context_stats.get("secondaryRegionCount", 0))
            chart_stats["secondaryAcceptedRegionCount"] = int(context_stats.get("secondaryAcceptedRegionCount", 0))
            chart_stats["secondaryRejectedRegionCount"] = int(context_stats.get("secondaryRejectedRegionCount", 0))
            chart_stats["secondaryKeyframeIds"] = list(context_stats.get("secondaryKeyframeIds", []))
            chart_stats["unresolvedFallbackPixelCount"] = int(
                context_stats.get("unresolvedFallbackPixelCount", context_stats.get("fallbackPixelCount", 0))
            )
            chart_stats["maxFillRadius"] = int(context_stats.get("maxFillRadius", 0))
            chart_stats["projectedPixelRatio"] = round(projected / max(filled, 1), 4)
    atlas_layout_debug_stats = {
        **atlas_layout_spec.stats,
        "charts": planar_chart_texture_stats or atlas_layout_spec.stats.get("charts", []),
        "fallbackBudget": fallback_budget_stats,
    }
    if source_image_atlas_layout is not None:
        non_chart_face_count = fallback_total
        source_projected_face_count = fallback_projected_count
        source_fallback_face_count = max(0, non_chart_face_count - source_projected_face_count)
        atlas_layout_debug_stats["nonChartFaceCount"] = non_chart_face_count
        atlas_layout_debug_stats["sourceProjectedFaceCount"] = source_projected_face_count
        atlas_layout_debug_stats["sourceFallbackFaceCount"] = source_fallback_face_count
        atlas_layout_debug_stats["legacyPerFaceFallbackTileSize"] = atlas_layout_debug_stats.get("fallbackTileSize")
        atlas_layout_debug_stats["fallbackFaceCount"] = source_fallback_face_count
        atlas_layout_debug_stats["fallbackTileSize"] = None

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
        neighbor_filled_pixel_count=neighbor_filled_pixel_count,
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
        rejected_depth_edge_sample_count=rejected_depth_edge_sample_count,
        rejected_occluded_sample_count=rejected_occluded_sample_count,
        depth_tested_sample_count=depth_tested_sample_count,
        missing_depth_sample_count=missing_depth_sample_count,
        render_mesh_stats=mesh.stats.get("textureRenderMesh", {}),
        color_correction=color_correction,
        atlas_layout_stats=atlas_layout_debug_stats,
        uv_strategy=atlas_layout_spec.strategy,
        normal_coordinate_count=len(mesh.vertices),
    )
    texture_diagnostics["processing"] = {
        "textureWorkerCount": parallel_worker_count,
        "parallelEnabled": parallel_worker_count > 1,
        "planarChartRasterStride": max(1, profile.planar_chart_raster_stride),
        "planarChartProjectionMode": profile.planar_chart_projection_mode,
        "activeProjectionMode": active_projection_mode,
        "planarChartCount": len(atlas_layout_spec.planar_charts),
        "fallbackTextureFaceLimit": profile.fallback_texture_face_limit,
        "fallbackHighQualityFaceCount": fallback_budget_stats.get("fallbackHighQualityFaceCount", 0),
        "solidProjectedFaceCount": solid_projected_face_count,
        "solidFallbackFaceCount": solid_fallback_face_count,
        "solidSceneColor": list(solid_scene_color),
        "solidSceneColorProjected": solid_scene_color_projected,
        "sourceKeyframeCount": len(source_keyframes),
        "activeTextureKeyframeCount": len(keyframes),
        "denseSingleViewTexture": dense_single_view_enabled,
        "rgbdHeroPatchTexture": profile.rgbd_hero_patch_texture,
        "sourceImageProjectionAtlas": (
            source_image_atlas_layout.stats
            if source_image_atlas_layout is not None
            else {
                "enabled": False,
                "reason": (
                    "not_applicable_for_projection_mode"
                    if active_projection_mode not in {"direct", "dense_single_view"}
                    else "no_non_chart_owned_faces"
                ),
            }
        ),
        "cpuVisibility": {
            "enabled": bool(visibility_stats),
            "rasterMaxSize": TEXTURE_VISIBILITY_RASTER_MAX_SIZE,
            "keyframeCount": len(visibility_stats),
        },
        "coherentLabeling": coherent_label_stats,
    }
    texture_diagnostics["geometry"] = texture_geometry_debug(mesh)
    texture_diagnostics["keyframes"] = projection_keyframe_debug_summaries(source_keyframes)
    texture_diagnostics["visibility"] = visibility_stats
    texture_diagnostics["coherentLabeling"] = coherent_label_stats
    if dense_single_view_stats is not None:
        texture_diagnostics["denseSingleViewTexture"] = dense_single_view_stats
    texture_diagnostics["poseDelta"] = (
        projection_keyframe_pose_delta(source_keyframes[0], source_keyframes[1])
        if len(source_keyframes) >= 2
        else None
    )
    texture_diagnostics["perKeyframeProjection"] = per_keyframe_mesh_projection_stats(mesh, source_keyframes)
    texture_diagnostics["projection"]["faceRejectionReasons"] = {
        "overexposedSampleCount": rejected_overexposed_sample_count,
        "underexposedSampleCount": rejected_underexposed_sample_count,
        "edgeSampleCount": rejected_edge_sample_count,
        "grazingSampleCount": rejected_grazing_sample_count,
        "invalidProjectionSampleCount": rejected_invalid_projection_sample_count,
        "depthEdgeSampleCount": rejected_depth_edge_sample_count,
        "occludedSampleCount": rejected_occluded_sample_count,
    }
    texture_diagnostics["projection"]["depthVisibilityStats"] = {
        "depthTestedSampleCount": depth_tested_sample_count,
        "missingDepthSampleCount": missing_depth_sample_count,
        "depthEdgeSampleCount": rejected_depth_edge_sample_count,
        "occludedSampleCount": rejected_occluded_sample_count,
        "depthVisibilityDecisionCount": (
            depth_tested_sample_count
            + missing_depth_sample_count
            + rejected_depth_edge_sample_count
            + rejected_occluded_sample_count
        ),
    }
    if output_projection_overlay_dir is not None:
        texture_diagnostics["projectionOverlays"] = write_two_keyframe_projection_overlays(
            mesh,
            source_keyframes,
            output_projection_overlay_dir,
        )

    usdz_artifact = {
        "format": "usdz",
        "path": "textured_mesh.usdz",
        "available": False,
        "reason": "Textured USDZ export was not requested.",
    }
    glb_artifacts = {
        "texturedMeshGlb": {
            "format": "glb",
            "path": "textured_mesh.glb",
            "available": False,
            "reason": "Textured GLB export was not requested.",
        },
        "uvCheckerGlb": {
            "format": "glb",
            "path": "uv_checker.glb",
            "available": False,
            "reason": "UV checker GLB export was not requested.",
        },
        "coverageDebugGlb": {
            "format": "glb",
            "path": "coverage_debug.glb",
            "available": False,
            "reason": "Coverage debug GLB export was not requested.",
        },
        "coverageDebugReport": {
            "format": "json",
            "path": "coverage_debug_report.json",
            "available": False,
            "reason": "Coverage debug report export was not requested.",
        },
    }
    texture.save(output_texture_path)
    texture_png_bytes = output_texture_path.read_bytes()
    if output_textured_usdz_path is not None:
        usdz_artifact = write_textured_usdz(
            mesh,
            output_textured_usdz_path,
            face_uvs=face_uvs,
            texture_path=output_texture_path,
            name="textured_mesh",
        )
    if output_textured_glb_path is not None:
        glb_artifacts["texturedMeshGlb"] = write_mesh_glb(
            mesh,
            output_textured_glb_path,
            name="textured_mesh",
            face_uvs=face_uvs,
            texture_png_bytes=texture_png_bytes,
            material_color=(1.0, 1.0, 1.0, 1.0),
            double_sided=True,
        )
    if output_uv_checker_glb_path is not None:
        glb_artifacts["uvCheckerGlb"] = write_uv_checker_glb(mesh, output_uv_checker_glb_path)
    if output_coverage_debug_glb_path is not None:
        glb_artifacts["coverageDebugGlb"] = write_coverage_debug_glb(
            mesh,
            output_coverage_debug_glb_path,
            output_coverage_debug_report_path,
            coverage_face_statuses,
        )
        glb_artifacts["coverageDebugReport"] = {
            "format": "json",
            "path": output_coverage_debug_report_path.name if output_coverage_debug_report_path else None,
            "available": bool(output_coverage_debug_report_path and output_coverage_debug_report_path.exists()),
        }
    texture_diagnostics["usdzDiagnostics"] = usdz_artifact
    texture_diagnostics["glbDiagnostics"] = glb_artifacts
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
    vertex_normals = compute_vertex_normals(mesh.vertices, mesh.faces)
    for x, y, z in mesh.vertices:
        lines.append(f"v {x:.6f} {y:.6f} {z:.6f}")
    lines.extend(vt_lines)
    for x, y, z in vertex_normals:
        lines.append(f"vn {x:.6f} {y:.6f} {z:.6f}")
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
        "uvStrategy": atlas_layout_spec.strategy,
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
        "atlasLayout": atlas_layout_debug_stats,
        "textureWorkerCount": parallel_worker_count,
        "usdz": usdz_artifact,
        "glb": glb_artifacts["texturedMeshGlb"],
        "diagnosticGlbs": {
            "uvCheckerGlb": glb_artifacts["uvCheckerGlb"],
            "coverageDebugGlb": glb_artifacts["coverageDebugGlb"],
            "coverageDebugReport": glb_artifacts["coverageDebugReport"],
        },
        "diagnostics": texture_diagnostics,
    }


_TEXTURE_WORKER_VERTICES: list[tuple[float, float, float]] = []
_TEXTURE_WORKER_FACES: list[tuple[int, int, int]] = []
_TEXTURE_WORKER_KEYFRAMES: list[ProjectionKeyframe] = []
_TEXTURE_WORKER_TILE_SIZE = 0
_TEXTURE_WORKER_COLUMNS = 0
_TEXTURE_WORKER_DILATION_PIXELS = 0


async def rasterize_texture_atlas_parallel(
    *,
    mesh: FusedMesh,
    keyframes: list[ProjectionKeyframe],
    atlas_width: int,
    atlas_height: int,
    tile_size: int,
    columns: int,
    dilation_pixels: int,
    worker_count: int,
    report_progress: Callable[[float, str], Awaitable[None]] | None,
    is_cancelled: CancellationCheck | None,
) -> dict:
    face_count = len(mesh.faces)
    texture = Image.new("RGB", (atlas_width, atlas_height), FALLBACK_COLOR)
    vt_lines: list[str] = []
    face_lines: list[str] = []
    face_uvs: FaceUVs = []
    uv_vertex_sample_stats = ColorStatsAccumulator()
    uv_face_interior_sample_stats = ColorStatsAccumulator()
    selected_keyframe_face_counts: dict[str, int] = {}
    keyframe_contribution_counts: dict[str, int] = {}
    textured_face_count = 0
    fallback_face_count = 0
    rasterized_pixel_count = 0
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
    rejected_depth_edge_sample_count = 0
    rejected_occluded_sample_count = 0
    depth_tested_sample_count = 0
    missing_depth_sample_count = 0
    dilated_pixel_count = 0
    uv_min_u = math.inf
    uv_min_v = math.inf
    uv_max_u = -math.inf
    uv_max_v = -math.inf
    uv_out_of_range_count = 0
    uv_non_finite_count = 0

    for face_index, face in enumerate(mesh.faces):
        tile = atlas_tile(face_index, tile_size, columns)
        atlas_triangle = atlas_triangle_points(tile, tile_size)
        uv_start = face_index * 3 + 1
        current_face_uvs: list[tuple[float, float]] = []
        for point in atlas_triangle:
            u = (point[0] + 0.5) / atlas_width
            v = 1 - ((point[1] + 0.5) / atlas_height)
            current_face_uvs.append((u, v))
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
        face_uvs.append((current_face_uvs[0], current_face_uvs[1], current_face_uvs[2]))

        face_lines.append(obj_face_line(face, uv_start))

    if face_count == 0:
        return {
            "texture": texture,
            "vt_lines": vt_lines,
            "face_lines": face_lines,
            "face_uvs": face_uvs,
            "textured_face_count": textured_face_count,
            "fallback_face_count": fallback_face_count,
            "rasterized_pixel_count": rasterized_pixel_count,
            "projected_pixel_count": projected_pixel_count,
            "fallback_pixel_count": fallback_pixel_count,
            "dilated_pixel_count": dilated_pixel_count,
            "uv_vertex_sample_stats": uv_vertex_sample_stats,
            "uv_face_interior_sample_stats": uv_face_interior_sample_stats,
            "selected_keyframe_face_counts": selected_keyframe_face_counts,
            "keyframe_contribution_counts": keyframe_contribution_counts,
            "blended_pixel_count": blended_pixel_count,
            "single_sample_pixel_count": single_sample_pixel_count,
            "accepted_projection_sample_count": accepted_projection_sample_count,
            "rejected_overexposed_sample_count": rejected_overexposed_sample_count,
            "rejected_underexposed_sample_count": rejected_underexposed_sample_count,
            "rejected_edge_sample_count": rejected_edge_sample_count,
            "rejected_grazing_sample_count": rejected_grazing_sample_count,
            "rejected_invalid_projection_sample_count": rejected_invalid_projection_sample_count,
            "rejected_depth_edge_sample_count": rejected_depth_edge_sample_count,
            "rejected_occluded_sample_count": rejected_occluded_sample_count,
            "depth_tested_sample_count": depth_tested_sample_count,
            "missing_depth_sample_count": missing_depth_sample_count,
            "uv_min_u": uv_min_u,
            "uv_min_v": uv_min_v,
            "uv_max_u": uv_max_u,
            "uv_max_v": uv_max_v,
            "uv_out_of_range_count": uv_out_of_range_count,
            "uv_non_finite_count": uv_non_finite_count,
        }

    set_texture_worker_state(
        vertices=mesh.vertices,
        faces=mesh.faces,
        keyframes=keyframes,
        tile_size=tile_size,
        columns=columns,
        dilation_pixels=dilation_pixels,
    )
    processed_count = 0
    progress_interval = max(1, min(face_count // 50, 1_000))
    next_report_count = progress_interval
    chunks = tuple(texture_face_chunks(face_count, TEXTURE_PARALLEL_CHUNK_SIZE))
    logger.info(
        "Rendering texture atlas with %s worker processes over %s chunks",
        worker_count,
        len(chunks),
    )

    try:
        fork_context = multiprocessing.get_context("fork")
        with ProcessPoolExecutor(max_workers=worker_count, mp_context=fork_context) as executor:
            for chunk_results in executor.map(rasterize_texture_face_chunk_worker, chunks, chunksize=1):
                if is_cancelled is not None and is_cancelled():
                    raise asyncio.CancelledError

                for result in chunk_results:
                    tile = atlas_tile(result.face_index, tile_size, columns)
                    tile_image = Image.frombytes("RGB", (tile_size, tile_size), result.tile_bytes)
                    texture.paste(tile_image, tile)

                    if result.selected_keyframe:
                        selected_keyframe_face_counts[result.selected_keyframe] = (
                            selected_keyframe_face_counts.get(result.selected_keyframe, 0) + 1
                        )
                    for key in result.keyframe_contribution_keys:
                        keyframe_contribution_counts[key] = keyframe_contribution_counts.get(key, 0) + 1
                    for color in result.uv_vertex_sample_colors:
                        uv_vertex_sample_stats.add(color)
                    for color in result.uv_face_interior_sample_colors:
                        uv_face_interior_sample_stats.add(color)

                    rasterized_pixel_count += result.filled_pixel_count
                    projected_pixel_count += result.projected_pixel_count
                    fallback_pixel_count += result.fallback_pixel_count
                    blended_pixel_count += result.blended_pixel_count
                    single_sample_pixel_count += result.single_sample_pixel_count
                    accepted_projection_sample_count += result.accepted_projection_sample_count
                    rejected_overexposed_sample_count += result.rejected_overexposed_sample_count
                    rejected_underexposed_sample_count += result.rejected_underexposed_sample_count
                    rejected_edge_sample_count += result.rejected_edge_sample_count
                    rejected_grazing_sample_count += result.rejected_grazing_sample_count
                    rejected_invalid_projection_sample_count += result.rejected_invalid_projection_sample_count
                    rejected_depth_edge_sample_count += result.rejected_depth_edge_sample_count
                    rejected_occluded_sample_count += result.rejected_occluded_sample_count
                    depth_tested_sample_count += result.depth_tested_sample_count
                    missing_depth_sample_count += result.missing_depth_sample_count
                    dilated_pixel_count += result.dilated_pixel_count
                    if result.projected_pixel_count > 0:
                        textured_face_count += 1
                    else:
                        fallback_face_count += 1
                    processed_count += 1

                if report_progress is not None and (
                    processed_count >= next_report_count or processed_count == face_count
                ):
                    fraction = processed_count / face_count
                    coverage = int((textured_face_count / processed_count) * 100) if processed_count else 0
                    await report_progress(
                        83 + fraction * 10,
                        (
                            f"Texturing atlas faces {processed_count} / {face_count} "
                            f"({coverage}% projected, {worker_count} workers)"
                        ),
                    )
                    next_report_count = processed_count + progress_interval
                    await asyncio.sleep(0)
    finally:
        set_texture_worker_state(vertices=[], faces=[], keyframes=[], tile_size=0, columns=0, dilation_pixels=0)

    return {
        "texture": texture,
        "vt_lines": vt_lines,
        "face_lines": face_lines,
        "face_uvs": face_uvs,
        "textured_face_count": textured_face_count,
        "fallback_face_count": fallback_face_count,
        "rasterized_pixel_count": rasterized_pixel_count,
        "projected_pixel_count": projected_pixel_count,
        "fallback_pixel_count": fallback_pixel_count,
        "dilated_pixel_count": dilated_pixel_count,
        "uv_vertex_sample_stats": uv_vertex_sample_stats,
        "uv_face_interior_sample_stats": uv_face_interior_sample_stats,
        "selected_keyframe_face_counts": selected_keyframe_face_counts,
        "keyframe_contribution_counts": keyframe_contribution_counts,
        "blended_pixel_count": blended_pixel_count,
        "single_sample_pixel_count": single_sample_pixel_count,
        "accepted_projection_sample_count": accepted_projection_sample_count,
        "rejected_overexposed_sample_count": rejected_overexposed_sample_count,
        "rejected_underexposed_sample_count": rejected_underexposed_sample_count,
        "rejected_edge_sample_count": rejected_edge_sample_count,
        "rejected_grazing_sample_count": rejected_grazing_sample_count,
        "rejected_invalid_projection_sample_count": rejected_invalid_projection_sample_count,
        "rejected_depth_edge_sample_count": rejected_depth_edge_sample_count,
        "rejected_occluded_sample_count": rejected_occluded_sample_count,
        "depth_tested_sample_count": depth_tested_sample_count,
        "missing_depth_sample_count": missing_depth_sample_count,
        "uv_min_u": uv_min_u,
        "uv_min_v": uv_min_v,
        "uv_max_u": uv_max_u,
        "uv_max_v": uv_max_v,
        "uv_out_of_range_count": uv_out_of_range_count,
        "uv_non_finite_count": uv_non_finite_count,
    }


def texture_parallel_worker_count(face_count: int) -> int:
    setting = os.getenv("LIDARAI_TEXTURE_WORKERS", "auto").strip().lower()
    if setting in {"0", "1", "false", "off", "no", "serial"}:
        return 1
    if setting not in {"", "auto"}:
        try:
            return max(1, int(setting))
        except ValueError:
            logger.warning("Ignoring invalid LIDARAI_TEXTURE_WORKERS value: %s", setting)
            return 1
    if face_count < TEXTURE_PARALLEL_MIN_FACE_COUNT:
        return 1
    if sys.platform != "linux" or "fork" not in multiprocessing.get_all_start_methods():
        return 1

    cpu_count = os.cpu_count() or 1
    if cpu_count <= 1:
        return 1
    return min(cpu_count, TEXTURE_PARALLEL_MAX_AUTO_WORKERS)


def texture_face_chunks(face_count: int, chunk_size: int) -> list[tuple[int, ...]]:
    return [
        tuple(range(start, min(start + chunk_size, face_count)))
        for start in range(0, face_count, max(1, chunk_size))
    ]


def set_texture_worker_state(
    *,
    vertices: list[tuple[float, float, float]],
    faces: list[tuple[int, int, int]],
    keyframes: list[ProjectionKeyframe],
    tile_size: int,
    columns: int,
    dilation_pixels: int,
) -> None:
    global _TEXTURE_WORKER_VERTICES
    global _TEXTURE_WORKER_FACES
    global _TEXTURE_WORKER_KEYFRAMES
    global _TEXTURE_WORKER_TILE_SIZE
    global _TEXTURE_WORKER_COLUMNS
    global _TEXTURE_WORKER_DILATION_PIXELS
    _TEXTURE_WORKER_VERTICES = vertices
    _TEXTURE_WORKER_FACES = faces
    _TEXTURE_WORKER_KEYFRAMES = keyframes
    _TEXTURE_WORKER_TILE_SIZE = tile_size
    _TEXTURE_WORKER_COLUMNS = columns
    _TEXTURE_WORKER_DILATION_PIXELS = dilation_pixels


def rasterize_texture_face_chunk_worker(face_indices: tuple[int, ...]) -> list[TextureFaceResult]:
    return [rasterize_texture_face_for_worker(face_index) for face_index in face_indices]


def rasterize_texture_face_for_worker(face_index: int) -> TextureFaceResult:
    tile_size = _TEXTURE_WORKER_TILE_SIZE
    face = _TEXTURE_WORKER_FACES[face_index]
    vertices = _TEXTURE_WORKER_VERTICES
    keyframes = _TEXTURE_WORKER_KEYFRAMES
    face_vertices = [vertices[face[0]], vertices[face[1]], vertices[face[2]]]
    candidates = texture_projection_candidates(face_vertices, keyframes, face_index=face_index)
    selected_keyframe = candidates[0].keyframe_debug_id if candidates else None
    fallback_color: tuple[int, int, int] | None = None

    def resolve_fallback_color() -> tuple[int, int, int]:
        nonlocal fallback_color
        if fallback_color is None:
            fallback_color = average_projected_color(face_vertices, keyframes) or FALLBACK_COLOR
        return fallback_color

    tile_texture = Image.new("RGB", (tile_size, tile_size), FALLBACK_COLOR)
    tile_mask = Image.new("L", (tile_size, tile_size), 0)
    texture_pixels = tile_texture.load()
    mask_pixels = tile_mask.load()
    atlas_triangle = atlas_triangle_points((0, 0), tile_size)
    raster_stats = rasterize_face_texture(
        texture_pixels,
        mask_pixels,
        atlas_triangle,
        face_vertices,
        candidates,
        resolve_fallback_color,
    )
    dilated_pixel_count = dilate_texture_tile(
        texture_pixels,
        mask_pixels,
        (0, 0),
        tile_size,
        _TEXTURE_WORKER_DILATION_PIXELS,
    )
    uv_vertex_sample_colors = tuple(
        sample_texture_at_atlas_point(texture_pixels, tile_size, tile_size, point[0], point[1])
        for point in atlas_triangle
    )
    uv_face_interior_sample_colors = tuple(
        sample_texture_at_atlas_point(texture_pixels, tile_size, tile_size, point[0], point[1])
        for point in atlas_interior_sample_points(atlas_triangle)
    )
    keyframe_contribution_keys = tuple(
        key
        for key, count in raster_stats["keyframeContributionCounts"].items()
        for _ in range(count)
    )
    return TextureFaceResult(
        face_index=face_index,
        tile_bytes=tile_texture.tobytes(),
        selected_keyframe=selected_keyframe,
        filled_pixel_count=raster_stats["filledPixelCount"],
        projected_pixel_count=raster_stats["projectedPixelCount"],
        fallback_pixel_count=raster_stats["fallbackPixelCount"],
        blended_pixel_count=raster_stats["blendedPixelCount"],
        single_sample_pixel_count=raster_stats["singleSamplePixelCount"],
        accepted_projection_sample_count=raster_stats["acceptedProjectionSampleCount"],
        rejected_overexposed_sample_count=raster_stats["rejectedOverexposedSampleCount"],
        rejected_underexposed_sample_count=raster_stats["rejectedUnderexposedSampleCount"],
        rejected_edge_sample_count=raster_stats["rejectedEdgeSampleCount"],
        rejected_grazing_sample_count=raster_stats["rejectedGrazingSampleCount"],
        rejected_invalid_projection_sample_count=raster_stats["rejectedInvalidProjectionSampleCount"],
        rejected_depth_edge_sample_count=raster_stats["rejectedDepthEdgeSampleCount"],
        rejected_occluded_sample_count=raster_stats["rejectedOccludedSampleCount"],
        depth_tested_sample_count=raster_stats["depthTestedSampleCount"],
        missing_depth_sample_count=raster_stats["missingDepthSampleCount"],
        dilated_pixel_count=dilated_pixel_count,
        keyframe_contribution_keys=keyframe_contribution_keys,
        uv_vertex_sample_colors=uv_vertex_sample_colors,
        uv_face_interior_sample_colors=uv_face_interior_sample_colors,
    )


def atlas_layout(face_count: int, atlas_max_size: int = TEXTURE_ATLAS_MAX_SIZE) -> tuple[int, int, int, int]:
    if face_count <= 0:
        return 64, 64, 32, 1

    columns = max(1, math.ceil(math.sqrt(face_count)))
    tile_size = max(TEXTURE_TILE_MIN_SIZE, min(TEXTURE_TILE_MAX_SIZE, atlas_max_size // columns))
    rows = max(1, math.ceil(face_count / columns))
    return columns * tile_size, rows * tile_size, tile_size, columns


def build_texture_atlas_layout(mesh: FusedMesh, atlas_max_size: int) -> TextureAtlasLayout:
    face_count = len(mesh.faces)
    planar_charts = detect_planar_texture_charts(mesh, atlas_max_size=atlas_max_size)
    if not planar_charts:
        atlas_width, atlas_height, tile_size, columns = atlas_layout(face_count, atlas_max_size=atlas_max_size)
        return TextureAtlasLayout(
            width=atlas_width,
            height=atlas_height,
            tile_size=tile_size,
            columns=columns,
            tile_start_y=0,
            planar_charts=[],
            face_to_chart={},
            face_to_tile_index={face_index: face_index for face_index in range(face_count)},
            strategy="render_mesh_per_face_atlas_padded",
            stats={"enabled": False, "reason": "no planar charts selected"},
        )

    selected_charts = list(planar_charts)
    while selected_charts:
        packed_charts, chart_height = pack_planar_texture_charts(selected_charts, atlas_max_size)
        face_to_chart = {
            face_index: chart
            for chart in packed_charts
            for face_index in chart.face_indices
        }
        non_chart_face_indices = [
            face_index for face_index in range(face_count)
            if face_index not in face_to_chart
        ]
        tile_size, columns, tile_rows = fit_per_face_tiles_below_charts(
            non_chart_face_count=len(non_chart_face_indices),
            atlas_max_size=atlas_max_size,
            tile_start_y=chart_height,
        )
        if tile_size > 0:
            atlas_height = max(64, chart_height + tile_rows * tile_size)
            return TextureAtlasLayout(
                width=atlas_max_size,
                height=min(atlas_max_size, atlas_height),
                tile_size=tile_size,
                columns=columns,
                tile_start_y=chart_height,
                planar_charts=packed_charts,
                face_to_chart=face_to_chart,
                face_to_tile_index={face_index: index for index, face_index in enumerate(non_chart_face_indices)},
                strategy="planar_chart_atlas_with_per_face_fallback",
                stats={
                    "enabled": True,
                    "chartCount": len(packed_charts),
                    "chartedFaceCount": len(face_to_chart),
                    "fallbackFaceCount": len(non_chart_face_indices),
                    "tileStartY": chart_height,
                    "fallbackTileSize": tile_size,
                    "fallbackTileRows": tile_rows,
                    "atlasMaxSize": atlas_max_size,
                    "charts": [planar_chart_stats(chart) for chart in packed_charts],
                },
            )

        selected_charts = selected_charts[:-1]

    atlas_width, atlas_height, tile_size, columns = atlas_layout(face_count, atlas_max_size=atlas_max_size)
    return TextureAtlasLayout(
        width=atlas_width,
        height=atlas_height,
        tile_size=tile_size,
        columns=columns,
        tile_start_y=0,
        planar_charts=[],
        face_to_chart={},
        face_to_tile_index={face_index: face_index for face_index in range(face_count)},
        strategy="render_mesh_per_face_atlas_padded",
        stats={"enabled": False, "reason": "planar charts could not fit with fallback tiles"},
    )


def detect_planar_texture_charts(mesh: FusedMesh, atlas_max_size: int) -> list[PlanarTextureChart]:
    if not TEXTURE_PLANAR_CHARTS_ENABLED or not mesh.faces:
        return []

    vertices = mesh.vertices
    face_centers = [
        triangle_center(vertices[face[0]], vertices[face[1]], vertices[face[2]])
        for face in mesh.faces
    ]
    face_normals = [
        triangle_normal(vertices[face[0]], vertices[face[1]], vertices[face[2]])
        for face in mesh.faces
    ]
    face_areas = [
        triangle_area(vertices[face[0]], vertices[face[1]], vertices[face[2]])
        for face in mesh.faces
    ]

    render_stats = mesh.stats.get("textureRenderMesh", {}) if isinstance(mesh.stats, dict) else {}
    plane_stats = render_stats.get("planeRegularization", {}) if isinstance(render_stats, dict) else {}
    planes = plane_stats.get("planes") if isinstance(plane_stats, dict) else None
    if not isinstance(planes, list) or not planes:
        planes = infer_planar_texture_planes(face_centers, face_normals, face_areas)
    if not planes:
        return []

    assigned_faces: set[int] = set()
    charts: list[PlanarTextureChart] = []

    for source_plane_index, plane in enumerate(planes[:TEXTURE_PLANAR_CHART_MAX_COUNT * 2]):
        normal_values = plane.get("normal") if isinstance(plane, dict) else None
        if not isinstance(normal_values, list) or len(normal_values) != 3:
            continue

        normal = normalize((float(normal_values[0]), float(normal_values[1]), float(normal_values[2])))
        if normal == (0.0, 0.0, 0.0):
            continue

        offset_value = plane.get("offset") if isinstance(plane, dict) else None
        if offset_value is None:
            offset = estimate_plane_offset(face_centers, face_normals, normal)
        else:
            offset = float(offset_value)

        candidate_faces: list[int] = []
        for face_index, center in enumerate(face_centers):
            if face_index in assigned_faces:
                continue
            normal_alignment = abs(dot(face_normals[face_index], normal))
            if normal_alignment < TEXTURE_PLANAR_CHART_NORMAL_ALIGNMENT:
                continue
            distance = abs(dot(center, normal) + offset)
            if distance > TEXTURE_PLANAR_CHART_DISTANCE_METERS:
                continue
            candidate_faces.append(face_index)

        if len(candidate_faces) < TEXTURE_PLANAR_CHART_MIN_FACE_COUNT:
            continue

        axis_u, axis_v = plane_texture_axes(normal)
        components = connected_planar_face_components(candidate_faces, mesh.faces, face_areas)
        for component_faces, component_area in components:
            if (
                len(component_faces) < TEXTURE_PLANAR_CHART_MIN_FACE_COUNT
                or component_area < TEXTURE_PLANAR_CHART_MIN_AREA_M2
            ):
                continue

            chart = build_planar_texture_chart(
                mesh=mesh,
                face_indices=component_faces,
                normal=normal,
                offset=offset,
                axis_u=axis_u,
                axis_v=axis_v,
                atlas_max_size=atlas_max_size,
                source_plane_index=source_plane_index,
                chart_id=len(charts),
            )
            if chart is None:
                continue

            charts.append(chart)
            assigned_faces.update(component_faces)
            if len(charts) >= TEXTURE_PLANAR_CHART_MAX_COUNT:
                break

        if len(charts) >= TEXTURE_PLANAR_CHART_MAX_COUNT:
            break

    charts.sort(
        key=lambda chart: (
            len(chart.face_indices) * chart.width * chart.height,
            chart.max_u - chart.min_u,
            chart.max_v - chart.min_v,
        ),
        reverse=True,
    )
    for chart_id, chart in enumerate(charts):
        chart.chart_id = chart_id
    return charts


def connected_planar_face_components(
    face_indices: list[int],
    faces: list[tuple[int, int, int]],
    face_areas: list[float],
) -> list[tuple[list[int], float]]:
    if not face_indices:
        return []

    face_index_set = set(face_indices)
    vertex_to_faces: dict[int, list[int]] = {}
    for face_index in face_indices:
        for vertex_index in faces[face_index]:
            vertex_to_faces.setdefault(vertex_index, []).append(face_index)

    components: list[tuple[list[int], float]] = []
    unvisited = set(face_indices)
    while unvisited:
        start = unvisited.pop()
        stack = [start]
        component = [start]
        area = face_areas[start]

        while stack:
            face_index = stack.pop()
            for vertex_index in faces[face_index]:
                for neighbor_index in vertex_to_faces.get(vertex_index, []):
                    if neighbor_index not in unvisited or neighbor_index not in face_index_set:
                        continue
                    unvisited.remove(neighbor_index)
                    stack.append(neighbor_index)
                    component.append(neighbor_index)
                    area += face_areas[neighbor_index]

        components.append((component, area))

    components.sort(key=lambda item: item[1], reverse=True)
    return components


def build_planar_texture_chart(
    *,
    mesh: FusedMesh,
    face_indices: list[int],
    normal: tuple[float, float, float],
    offset: float,
    axis_u: tuple[float, float, float],
    axis_v: tuple[float, float, float],
    atlas_max_size: int,
    source_plane_index: int,
    chart_id: int,
) -> PlanarTextureChart | None:
    coord_us: list[float] = []
    coord_vs: list[float] = []
    for face_index in face_indices:
        face = mesh.faces[face_index]
        for vertex_index in face:
            vertex = mesh.vertices[vertex_index]
            coord_us.append(dot(vertex, axis_u))
            coord_vs.append(dot(vertex, axis_v))

    if not coord_us or not coord_vs:
        return None

    min_u = min(coord_us) - TEXTURE_PLANAR_CHART_PADDING_METERS
    max_u = max(coord_us) + TEXTURE_PLANAR_CHART_PADDING_METERS
    min_v = min(coord_vs) - TEXTURE_PLANAR_CHART_PADDING_METERS
    max_v = max(coord_vs) + TEXTURE_PLANAR_CHART_PADDING_METERS
    extent_u = max(max_u - min_u, 0.05)
    extent_v = max(max_v - min_v, 0.05)
    width = int(math.ceil(extent_u * TEXTURE_PLANAR_CHART_PIXELS_PER_METER))
    height = int(math.ceil(extent_v * TEXTURE_PLANAR_CHART_PIXELS_PER_METER))
    width = min(TEXTURE_PLANAR_CHART_MAX_SIZE, max(TEXTURE_PLANAR_CHART_MIN_SIZE, width))
    height = min(TEXTURE_PLANAR_CHART_MAX_SIZE, max(TEXTURE_PLANAR_CHART_MIN_SIZE, height))
    width = min(width, atlas_max_size)
    height = min(height, atlas_max_size)
    if width <= 0 or height <= 0:
        return None

    return PlanarTextureChart(
        chart_id=chart_id,
        face_indices=face_indices,
        normal=normal,
        plane_offset=offset,
        axis_u=axis_u,
        axis_v=axis_v,
        min_u=min_u,
        max_u=max_u,
        min_v=min_v,
        max_v=max_v,
        width=width,
        height=height,
        source_plane_index=source_plane_index,
    )


def infer_planar_texture_planes(
    face_centers: list[tuple[float, float, float]],
    face_normals: list[tuple[float, float, float]],
    face_areas: list[float],
) -> list[dict]:
    if not face_centers:
        return []

    min_face_count = TEXTURE_PLANAR_CHART_MIN_FACE_COUNT
    min_area = TEXTURE_PLANAR_CHART_MIN_AREA_M2
    seed_indices = sorted(range(len(face_centers)), key=lambda index: face_areas[index], reverse=True)
    assigned_faces: set[int] = set()
    planes: list[dict] = []
    max_seed_count = min(len(seed_indices), 128)

    for seed_index in seed_indices[:max_seed_count]:
        if len(face_centers) - len(assigned_faces) < min_face_count:
            break
        if seed_index in assigned_faces:
            continue

        seed_normal = face_normals[seed_index]
        if seed_normal == (0.0, 0.0, 0.0):
            continue

        seed_center = face_centers[seed_index]
        offset = -dot(seed_center, seed_normal)
        candidate_faces: list[int] = []
        area = 0.0

        for face_index, center in enumerate(face_centers):
            if face_index in assigned_faces:
                continue
            if abs(dot(face_normals[face_index], seed_normal)) < TEXTURE_PLANAR_CHART_NORMAL_ALIGNMENT:
                continue
            if abs(dot(center, seed_normal) + offset) > TEXTURE_PLANAR_CHART_DISTANCE_METERS:
                continue
            candidate_faces.append(face_index)
            area += face_areas[face_index]

        if len(candidate_faces) < min_face_count or area < min_area:
            continue

        distances = [dot(face_centers[index], seed_normal) for index in candidate_faces]
        offset = -median_float(distances)
        normal_abs = [abs(component) for component in seed_normal]
        dominant_axis = ("x", "y", "z")[normal_abs.index(max(normal_abs))]
        planes.append({
            "planeIndex": len(planes),
            "algorithm": "face_normal_offset_cluster",
            "dominantAxis": dominant_axis,
            "normal": [round(component, 4) for component in seed_normal],
            "offset": round(offset, 6),
            "faceCount": len(candidate_faces),
            "areaM2": round(area, 4),
        })
        assigned_faces.update(candidate_faces)

        if len(planes) >= TEXTURE_PLANAR_CHART_MAX_COUNT * 2:
            break

    return planes


def estimate_plane_offset(
    face_centers: list[tuple[float, float, float]],
    face_normals: list[tuple[float, float, float]],
    normal: tuple[float, float, float],
) -> float:
    distances = [
        dot(center, normal)
        for center, face_normal in zip(face_centers, face_normals)
        if abs(dot(face_normal, normal)) >= TEXTURE_PLANAR_CHART_NORMAL_ALIGNMENT
    ]
    if not distances:
        return 0.0
    return -median_float(distances)


def plane_texture_axes(normal: tuple[float, float, float]) -> tuple[tuple[float, float, float], tuple[float, float, float]]:
    reference = (0.0, 1.0, 0.0) if abs(normal[1]) < 0.85 else (1.0, 0.0, 0.0)
    axis_u = normalize(cross(reference, normal))
    if axis_u == (0.0, 0.0, 0.0):
        axis_u = (1.0, 0.0, 0.0)
    axis_v = normalize(cross(normal, axis_u))
    return axis_u, axis_v


def pack_planar_texture_charts(
    charts: list[PlanarTextureChart],
    atlas_max_size: int,
) -> tuple[list[PlanarTextureChart], int]:
    packed: list[PlanarTextureChart] = []
    x = 0
    y = 0
    row_height = 0
    height_cap = int(atlas_max_size * TEXTURE_PLANAR_CHART_ATLAS_HEIGHT_RATIO)
    for chart in charts:
        if chart.width > atlas_max_size or chart.height > height_cap:
            continue
        if x > 0 and x + chart.width > atlas_max_size:
            y += row_height
            x = 0
            row_height = 0
        if y + chart.height > height_cap:
            continue
        chart.x = x
        chart.y = y
        packed.append(chart)
        x += chart.width
        row_height = max(row_height, chart.height)

    chart_height = y + row_height if packed else 0
    return packed, chart_height


def fit_per_face_tiles_below_charts(
    non_chart_face_count: int,
    atlas_max_size: int,
    tile_start_y: int,
) -> tuple[int, int, int]:
    if non_chart_face_count <= 0:
        return TEXTURE_TILE_MIN_SIZE, 1, 0

    available_height = atlas_max_size - tile_start_y
    if available_height < TEXTURE_TILE_MIN_SIZE:
        return 0, 0, 0

    for tile_size in range(TEXTURE_TILE_MAX_SIZE, TEXTURE_TILE_MIN_SIZE - 1, -1):
        columns = max(1, atlas_max_size // tile_size)
        rows = math.ceil(non_chart_face_count / columns)
        if rows * tile_size <= available_height:
            return tile_size, columns, rows

    return 0, 0, 0


def planar_chart_stats(chart: PlanarTextureChart) -> dict:
    return {
        "chartId": chart.chart_id,
        "sourcePlaneIndex": chart.source_plane_index,
        "faceCount": len(chart.face_indices),
        "rect": {"x": chart.x, "y": chart.y, "width": chart.width, "height": chart.height},
        "normal": [round(component, 4) for component in chart.normal],
        "planeOffset": round(chart.plane_offset, 6),
        "extentMeters": [
            round(chart.max_u - chart.min_u, 4),
            round(chart.max_v - chart.min_v, 4),
        ],
    }


def atlas_tile(face_index: int, tile_size: int, columns: int) -> tuple[int, int]:
    column = face_index % columns
    row = face_index // columns
    return column * tile_size, row * tile_size


def atlas_tile_for_layout(face_index: int, layout: TextureAtlasLayout) -> tuple[int, int]:
    tile_index = layout.face_to_tile_index[face_index]
    column = tile_index % layout.columns
    row = tile_index // layout.columns
    return column * layout.tile_size, layout.tile_start_y + row * layout.tile_size


def planar_chart_point(
    chart: PlanarTextureChart,
    pixel_x: float,
    pixel_y: float,
) -> tuple[float, float, float]:
    u_ratio = 0.0 if chart.width <= 1 else clamp_float((pixel_x - chart.x) / max(chart.width - 1, 1), 0.0, 1.0)
    v_ratio = 0.0 if chart.height <= 1 else clamp_float((pixel_y - chart.y) / max(chart.height - 1, 1), 0.0, 1.0)
    coord_u = chart.min_u + u_ratio * (chart.max_u - chart.min_u)
    coord_v = chart.min_v + v_ratio * (chart.max_v - chart.min_v)
    return add(add(multiply(chart.axis_u, coord_u), multiply(chart.axis_v, coord_v)), multiply(chart.normal, -chart.plane_offset))


def planar_chart_pixel_for_vertex(
    chart: PlanarTextureChart,
    vertex: tuple[float, float, float],
) -> tuple[float, float]:
    coord_u = dot(vertex, chart.axis_u)
    coord_v = dot(vertex, chart.axis_v)
    u_ratio = (coord_u - chart.min_u) / max(chart.max_u - chart.min_u, 1e-6)
    v_ratio = (coord_v - chart.min_v) / max(chart.max_v - chart.min_v, 1e-6)
    return (
        chart.x + clamp_float(u_ratio, 0.0, 1.0) * max(chart.width - 1, 1),
        chart.y + clamp_float(v_ratio, 0.0, 1.0) * max(chart.height - 1, 1),
    )


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


def should_use_source_image_projection_atlas(
    *,
    profile: ProcessingProfile,
    active_projection_mode: str,
    keyframes: list[ProjectionKeyframe],
) -> bool:
    return (
        TEXTURE_SOURCE_IMAGE_ATLAS_ENABLED
        and bool(keyframes)
        and active_projection_mode in {"direct", "dense_single_view"}
        and profile.fallback_texture_face_limit is None
    )


def build_source_image_atlas_layout(
    *,
    keyframes: list[ProjectionKeyframe],
    face_owner_labels: list[str | None],
    face_to_chart: dict[int, PlanarTextureChart],
    atlas_max_size: int,
    tile_start_y: int,
) -> SourceImageAtlasLayout | None:
    owner_counts: dict[str, int] = {}
    for face_index, owner_label in enumerate(face_owner_labels):
        if owner_label is None or face_index in face_to_chart:
            continue
        owner_counts[owner_label] = owner_counts.get(owner_label, 0) + 1

    if not owner_counts:
        return None

    keyframe_order = {keyframe.debug_id: index for index, keyframe in enumerate(keyframes)}
    eligible_keyframes = [
        keyframe
        for keyframe in keyframes
        if owner_counts.get(keyframe.debug_id, 0) > 0
    ]
    eligible_keyframes.sort(
        key=lambda keyframe: (
            owner_counts.get(keyframe.debug_id, 0),
            -keyframe_order.get(keyframe.debug_id, 0),
        ),
        reverse=True,
    )

    tile_size, columns, rows, capacity = fit_source_image_tiles(
        source_keyframe_count=len(eligible_keyframes),
        atlas_max_size=atlas_max_size,
        tile_start_y=tile_start_y,
    )
    if tile_size <= 0 or columns <= 0 or rows <= 0 or capacity <= 0:
        return None

    included_keyframes = eligible_keyframes[:capacity]
    placements: dict[str, SourceImageAtlasPlacement] = {}
    for index, keyframe in enumerate(included_keyframes):
        column = index % columns
        row = index // columns
        tile_x = column * tile_size
        tile_y = tile_start_y + row * tile_size
        placement = build_source_image_atlas_placement(
            keyframe=keyframe,
            tile_x=tile_x,
            tile_y=tile_y,
            tile_size=tile_size,
            owner_face_count=owner_counts.get(keyframe.debug_id, 0),
        )
        placements[keyframe.debug_id] = placement

    atlas_height = max(64, tile_start_y + rows * tile_size)
    dropped_keyframes = eligible_keyframes[capacity:]
    return SourceImageAtlasLayout(
        width=atlas_max_size,
        height=min(atlas_max_size, atlas_height),
        tile_size=tile_size,
        columns=columns,
        rows=rows,
        tile_start_y=tile_start_y,
        placements=placements,
        stats={
            "enabled": True,
            "strategy": "source_keyframe_projection_atlas",
            "sourceKeyframeCount": len(keyframes),
            "eligibleKeyframeCount": len(eligible_keyframes),
            "packedKeyframeCount": len(included_keyframes),
            "droppedKeyframeCount": len(dropped_keyframes),
            "tileSize": tile_size,
            "tilePadding": TEXTURE_SOURCE_IMAGE_ATLAS_PADDING,
            "columns": columns,
            "rows": rows,
            "tileStartY": tile_start_y,
            "atlasMaxSize": atlas_max_size,
            "placements": [
                source_image_atlas_placement_stats(placement)
                for placement in placements.values()
            ],
            "droppedKeyframes": [
                {
                    "keyframe": keyframe.debug_id,
                    "ownerFaceCount": owner_counts.get(keyframe.debug_id, 0),
                }
                for keyframe in dropped_keyframes[:12]
            ],
        },
    )


def fit_source_image_tiles(
    *,
    source_keyframe_count: int,
    atlas_max_size: int,
    tile_start_y: int,
) -> tuple[int, int, int, int]:
    if source_keyframe_count <= 0:
        return 0, 0, 0, 0

    available_height = atlas_max_size - tile_start_y
    if available_height < TEXTURE_SOURCE_IMAGE_ATLAS_MIN_TILE_SIZE:
        return 0, 0, 0, 0

    max_tile_size = min(TEXTURE_SOURCE_IMAGE_ATLAS_MAX_TILE_SIZE, atlas_max_size)
    step = max(1, TEXTURE_SOURCE_IMAGE_ATLAS_TILE_STEP)
    for tile_size in range(max_tile_size, TEXTURE_SOURCE_IMAGE_ATLAS_MIN_TILE_SIZE - 1, -step):
        columns = max(1, atlas_max_size // tile_size)
        rows = math.ceil(source_keyframe_count / columns)
        if rows * tile_size <= available_height:
            return tile_size, columns, rows, source_keyframe_count

    tile_size = TEXTURE_SOURCE_IMAGE_ATLAS_MIN_TILE_SIZE
    columns = max(1, atlas_max_size // tile_size)
    rows = max(0, available_height // tile_size)
    capacity = min(source_keyframe_count, columns * rows)
    if capacity <= 0:
        return 0, 0, 0, 0
    return tile_size, columns, math.ceil(capacity / columns), capacity


def build_source_image_atlas_placement(
    *,
    keyframe: ProjectionKeyframe,
    tile_x: int,
    tile_y: int,
    tile_size: int,
    owner_face_count: int,
) -> SourceImageAtlasPlacement:
    padding = min(TEXTURE_SOURCE_IMAGE_ATLAS_PADDING, max(0, (tile_size - 1) // 2))
    content_size = max(1, tile_size - padding * 2)
    source_scale = min(content_size / max(keyframe.width, 1), content_size / max(keyframe.height, 1))
    image_width = max(1, min(content_size, int(round(keyframe.width * source_scale))))
    image_height = max(1, min(content_size, int(round(keyframe.height * source_scale))))
    image_x = tile_x + padding + max(0, (content_size - image_width) // 2)
    image_y = tile_y + padding + max(0, (content_size - image_height) // 2)
    return SourceImageAtlasPlacement(
        keyframe=keyframe,
        x=tile_x,
        y=tile_y,
        tile_size=tile_size,
        image_x=image_x,
        image_y=image_y,
        image_width=image_width,
        image_height=image_height,
        source_scale=source_scale,
        owner_face_count=owner_face_count,
    )


def source_image_atlas_placement_stats(placement: SourceImageAtlasPlacement) -> dict:
    return {
        "keyframe": placement.keyframe.debug_id,
        "ownerFaceCount": placement.owner_face_count,
        "tile": {
            "x": placement.x,
            "y": placement.y,
            "size": placement.tile_size,
        },
        "imageRect": {
            "x": placement.image_x,
            "y": placement.image_y,
            "width": placement.image_width,
            "height": placement.image_height,
        },
        "sourceImage": {
            "width": placement.keyframe.width,
            "height": placement.keyframe.height,
        },
        "sourceScale": round(placement.source_scale, 5),
    }


def paste_source_image_atlas_tiles(
    texture: Image.Image,
    texture_mask: Image.Image,
    source_atlas: SourceImageAtlasLayout,
) -> dict:
    filled_pixel_count = 0
    keyframe_contribution_counts: dict[str, int] = {}
    resample = getattr(getattr(Image, "Resampling", Image), "LANCZOS", Image.BICUBIC)

    for placement in source_atlas.placements.values():
        resized = placement.keyframe.image.resize(
            (placement.image_width, placement.image_height),
            resample=resample,
        )
        texture.paste(resized, (placement.image_x, placement.image_y))
        texture_mask.paste(
            Image.new("L", (placement.image_width, placement.image_height), 255),
            (placement.image_x, placement.image_y),
        )
        pixel_count = placement.image_width * placement.image_height
        filled_pixel_count += pixel_count
        keyframe_contribution_counts[placement.keyframe.debug_id] = pixel_count

    return {
        "filledPixelCount": filled_pixel_count,
        "projectedPixelCount": filled_pixel_count,
        "singleSamplePixelCount": filled_pixel_count,
        "acceptedProjectionSampleCount": filled_pixel_count,
        "keyframeContributionCounts": keyframe_contribution_counts,
    }


def source_image_atlas_point(
    placement: SourceImageAtlasPlacement,
    projection: tuple[float, float, float],
) -> tuple[float, float]:
    u, v, _depth = projection
    scale_x = (placement.image_width - 1) / max(placement.keyframe.width - 1, 1)
    scale_y = (placement.image_height - 1) / max(placement.keyframe.height - 1, 1)
    x = placement.image_x + clamp_float(u, 0.0, placement.keyframe.width - 1) * scale_x
    y = placement.image_y + clamp_float(v, 0.0, placement.keyframe.height - 1) * scale_y
    return x, y


def source_image_atlas_face_projection(
    face_vertices: list[tuple[float, float, float]],
    candidates: list[TextureProjectionCandidate],
    source_atlas: SourceImageAtlasLayout,
) -> dict:
    rejected_invalid_projection_count = 0
    rejected_depth_edge_count = 0
    rejected_occluded_count = 0
    depth_tested_count = 0
    missing_depth_count = 0
    center = triangle_center(face_vertices[0], face_vertices[1], face_vertices[2])

    for candidate in candidates:
        placement = source_atlas.placements.get(candidate.keyframe_debug_id)
        if placement is None:
            continue
        depth_visibility = depth_visibility_for_world_point(center, placement.keyframe)
        if depth_visibility.status == "occluded":
            rejected_occluded_count += 1
            continue
        if depth_visibility.status == "depth_edge":
            rejected_depth_edge_count += 1
            continue
        if depth_visibility.status == "visible":
            depth_tested_count += 1
        else:
            missing_depth_count += 1

        projections = [project_world_point(vertex, placement.keyframe) for vertex in face_vertices]
        if any(projection is None for projection in projections):
            rejected_invalid_projection_count += 1
            continue
        atlas_points = [
            source_image_atlas_point(placement, projection)
            for projection in projections
            if projection is not None
        ]
        if len(atlas_points) == 3:
            return {
                "atlasPoints": atlas_points,
                "keyframe": candidate.keyframe_debug_id,
                "rejectedInvalidProjectionSampleCount": rejected_invalid_projection_count,
                "rejectedDepthEdgeSampleCount": rejected_depth_edge_count,
                "rejectedOccludedSampleCount": rejected_occluded_count,
                "depthTestedSampleCount": depth_tested_count,
                "missingDepthSampleCount": missing_depth_count,
            }
    return {
        "atlasPoints": None,
        "keyframe": None,
        "rejectedInvalidProjectionSampleCount": rejected_invalid_projection_count,
        "rejectedDepthEdgeSampleCount": rejected_depth_edge_count,
        "rejectedOccludedSampleCount": rejected_occluded_count,
        "depthTestedSampleCount": depth_tested_count,
        "missingDepthSampleCount": missing_depth_count,
    }


def source_image_atlas_fallback_triangle(source_atlas: SourceImageAtlasLayout) -> list[tuple[float, float]]:
    placement = next(iter(source_atlas.placements.values()), None)
    if placement is None:
        return atlas_triangle_points((0, 0), max(TEXTURE_SOURCE_IMAGE_ATLAS_MIN_TILE_SIZE, 8))

    padding = max(1, min(TEXTURE_SOURCE_IMAGE_ATLAS_PADDING, placement.tile_size // 4))
    left = placement.x + 0.25
    top = placement.y + 0.25
    right = placement.x + max(1.0, padding - 0.25)
    bottom = placement.y + max(1.0, padding - 0.25)
    return [(left, bottom), (right, bottom), (left, top)]


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


def fallback_texture_face_budget(mesh: FusedMesh, layout: TextureAtlasLayout, limit: int | None) -> tuple[set[int], dict]:
    non_chart_face_indices = [
        face_index for face_index in range(len(mesh.faces))
        if face_index not in layout.face_to_chart
    ]
    if limit is None or limit <= 0 or len(non_chart_face_indices) <= limit:
        return set(non_chart_face_indices), {
            "fallbackTextureFaceLimit": limit,
            "fallbackHighQualityFaceCount": len(non_chart_face_indices),
            "fallbackSolidFaceCount": 0,
            "fallbackPrioritization": "all_non_chart_faces",
        }

    vertices = mesh.vertices
    ranked = sorted(
        non_chart_face_indices,
        key=lambda face_index: triangle_area(
            vertices[mesh.faces[face_index][0]],
            vertices[mesh.faces[face_index][1]],
            vertices[mesh.faces[face_index][2]],
        ),
        reverse=True,
    )
    high_quality = set(ranked[:limit])
    return high_quality, {
        "fallbackTextureFaceLimit": limit,
        "fallbackHighQualityFaceCount": len(high_quality),
        "fallbackSolidFaceCount": max(0, len(non_chart_face_indices) - len(high_quality)),
        "fallbackPrioritization": "largest_non_chart_triangles",
    }


def mesh_surface_sample_points(mesh: FusedMesh, max_points: int = 256) -> list[tuple[float, float, float]]:
    if not mesh.faces:
        return mesh.vertices[:max_points]

    step = max(1, math.ceil(len(mesh.faces) / max_points))
    points = [
        triangle_center(
            mesh.vertices[face[0]],
            mesh.vertices[face[1]],
            mesh.vertices[face[2]],
        )
        for face in mesh.faces[::step]
    ]
    return points[:max_points]


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
    neighbor_filled_pixel_count: int,
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
    rejected_depth_edge_sample_count: int,
    rejected_occluded_sample_count: int,
    depth_tested_sample_count: int,
    missing_depth_sample_count: int,
    render_mesh_stats: dict,
    color_correction: dict,
    atlas_layout_stats: dict,
    uv_strategy: str,
    normal_coordinate_count: int,
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
    total_projection_decisions = (
        accepted_projection_sample_count
        + rejected_overexposed_sample_count
        + rejected_underexposed_sample_count
        + rejected_edge_sample_count
        + rejected_grazing_sample_count
        + rejected_invalid_projection_sample_count
        + rejected_depth_edge_sample_count
        + rejected_occluded_sample_count
    )
    depth_visibility_decision_count = (
        depth_tested_sample_count
        + missing_depth_sample_count
        + rejected_depth_edge_sample_count
        + rejected_occluded_sample_count
    )
    atlas_charts = atlas_layout_stats.get("charts", []) if isinstance(atlas_layout_stats, dict) else []
    per_chart_owner_frames = [
        {
            "chartId": chart.get("chartId"),
            "ownerKeyframeId": chart.get("ownerKeyframeId"),
        }
        for chart in atlas_charts
        if isinstance(chart, dict) and chart.get("ownerKeyframeId") is not None
    ]
    owner_angles = [
        float(chart["ownerAngleDegrees"])
        for chart in atlas_charts
        if isinstance(chart, dict) and chart.get("ownerAngleDegrees") is not None
    ]
    owner_depth_errors = [
        float(chart["ownerDepthErrorMeters"])
        for chart in atlas_charts
        if isinstance(chart, dict) and chart.get("ownerDepthErrorMeters") is not None
    ]
    selected_texture_keyframes = [
        item["keyframe"]
        for item in top_keyframe_counts(selected_keyframe_face_counts, value_label="faceCount")
    ]
    diagnostics = {
        "version": "v2",
        "renderMesh": render_mesh_stats,
        "uvStrategy": uv_strategy,
        "atlasLayout": atlas_layout_stats,
        "summary": {
            "selectedTextureKeyframes": selected_texture_keyframes,
            "perChartOwnerFrames": per_chart_owner_frames,
            "rejectedOccludedSampleCount": rejected_occluded_sample_count,
            "rejectedDepthEdgeSampleCount": rejected_depth_edge_sample_count,
            "unobservedTexelRatio": round(fallback_raster_ratio, 4),
            "atlasChartCount": int(atlas_layout_stats.get("chartCount", 0)) if isinstance(atlas_layout_stats, dict) else 0,
            "planarChartCount": int(atlas_layout_stats.get("chartCount", 0)) if isinstance(atlas_layout_stats, dict) else 0,
            "textureCoverage": round(raster_projection_ratio, 4),
            "averageOwnerAngleDegrees": (
                round(sum(owner_angles) / len(owner_angles), 3)
                if owner_angles
                else None
            ),
            "averageOwnerDepthErrorMeters": (
                round(sum(owner_depth_errors) / len(owner_depth_errors), 4)
                if owner_depth_errors
                else None
            ),
        },
        "objSyntax": {
            "faceCount": face_count,
            "faceWithUVIndexCount": face_count,
            "faceWithoutUVIndexCount": 0,
            "uvCoordinateCount": uv_coordinate_count,
            "expectedUVCoordinateCount": face_count * 3,
            "normalCoordinateCount": normal_coordinate_count,
            "expectedNormalCoordinateCount": normal_coordinate_count,
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
            "unobservedColor": list(TEXTURE_UNOBSERVED_COLOR),
            "rasterizedPixelCount": rasterized_pixel_count,
            "projectedPixelCount": projected_pixel_count,
            "fallbackPixelCount": fallback_pixel_count,
            "localFilledPixelCount": neighbor_filled_pixel_count,
            "neighborFilledPixelCount": neighbor_filled_pixel_count,
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
            "rejectedDepthEdgeSampleCount": rejected_depth_edge_sample_count,
            "rejectedOccludedSampleCount": rejected_occluded_sample_count,
            "depthTestedSampleCount": depth_tested_sample_count,
            "missingDepthSampleCount": missing_depth_sample_count,
            "depthVisibilityDecisionCount": depth_visibility_decision_count,
            "depthTestedDecisionRatio": round(
                (depth_tested_sample_count / depth_visibility_decision_count)
                if depth_visibility_decision_count
                else 0,
                4,
            ),
            "occludedDecisionRatio": round(
                (rejected_occluded_sample_count / total_projection_decisions)
                if total_projection_decisions
                else 0,
                4,
            ),
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


def texture_geometry_debug(mesh: FusedMesh) -> dict:
    return {
        "geometrySource": mesh.stats.get("geometrySource"),
        "vertexCount": len(mesh.vertices),
        "faceCount": len(mesh.faces),
        "geometryPreserved": bool(mesh.stats.get("geometryPreserved")),
        "geometryPreservation": mesh.stats.get("geometryPreservation"),
        "renderMesh": mesh.stats.get("textureRenderMesh", {}),
    }


def projection_keyframe_debug_summaries(keyframes: list[ProjectionKeyframe]) -> list[dict]:
    summaries: list[dict] = []
    for index, keyframe in enumerate(keyframes):
        depth_frame = keyframe.depth_frame
        summaries.append({
            "index": index,
            "id": keyframe.id,
            "path": keyframe.path,
            "timestamp": keyframe.timestamp,
            "capturedAt": keyframe.captured_at,
            "imageSize": [keyframe.width, keyframe.height],
            "intrinsics": [round(float(value), 6) for value in keyframe.intrinsics[:9]],
            "cameraPosition": [round(value, 6) for value in keyframe.camera_position],
            "forwardVector": [round(value, 6) for value in camera_forward_from_world_to_camera(keyframe.world_to_camera_values)],
            "meshVisibility": keyframe.mesh_visibility_stats,
            "depthFrame": (
                {
                    "id": depth_frame.id,
                    "colorKeyframeId": depth_frame.color_keyframe_id,
                    "path": depth_frame.path,
                    "confidencePath": depth_frame.confidence_path,
                    "timestamp": depth_frame.timestamp,
                    "depthSize": [depth_frame.width, depth_frame.height],
                    "intrinsics": [round(float(value), 6) for value in depth_frame.intrinsics[:9]],
                }
                if depth_frame is not None
                else None
            ),
        })
    return summaries


def projection_keyframe_pose_delta(left: ProjectionKeyframe, right: ProjectionKeyframe) -> dict:
    direction_dot = clamp_float(
        dot(
            camera_forward_from_world_to_camera(left.world_to_camera_values),
            camera_forward_from_world_to_camera(right.world_to_camera_values),
        ),
        -1.0,
        1.0,
    )
    time_delta = None
    if left.timestamp is not None and right.timestamp is not None:
        time_delta = abs(left.timestamp - right.timestamp)
    return {
        "translationMeters": round(length(subtract(left.camera_position, right.camera_position)), 4),
        "angleDegrees": round(math.degrees(math.acos(direction_dot)), 3),
        "timeDeltaSeconds": round(time_delta, 4) if time_delta is not None else None,
    }


def camera_forward_from_world_to_camera(world_to_camera_values: tuple[float, ...] | list[float]) -> tuple[float, float, float]:
    if len(world_to_camera_values) < 16:
        return (0.0, 0.0, -1.0)
    camera_to_world = invert_rigid_transform([float(value) for value in world_to_camera_values[:16]])
    forward = normalize((-camera_to_world[8], -camera_to_world[9], -camera_to_world[10]))
    return forward if forward != (0.0, 0.0, 0.0) else (0.0, 0.0, -1.0)


def per_keyframe_mesh_projection_stats(
    mesh: FusedMesh,
    keyframes: list[ProjectionKeyframe],
    max_sampled_faces: int = 20_000,
) -> list[dict]:
    if not mesh.faces or not keyframes:
        return []

    stride = max(1, math.ceil(len(mesh.faces) / max_sampled_faces))
    sampled_faces = list(range(0, len(mesh.faces), stride))
    stats: list[dict] = []
    for keyframe in keyframes:
        in_bounds_count = 0
        out_of_bounds_count = 0
        occluded_count = 0
        depth_edge_count = 0
        depth_tested_count = 0
        missing_depth_count = 0
        unknown_depth_count = 0
        for face_index in sampled_faces:
            face = mesh.faces[face_index]
            center = triangle_center(mesh.vertices[face[0]], mesh.vertices[face[1]], mesh.vertices[face[2]])
            projection = project_world_point(center, keyframe)
            if projection is None:
                out_of_bounds_count += 1
                continue
            in_bounds_count += 1
            depth_visibility = depth_visibility_for_world_point(center, keyframe)
            if depth_visibility.status == "occluded":
                occluded_count += 1
            elif depth_visibility.status == "depth_edge":
                depth_edge_count += 1
            elif depth_visibility.status == "unknown":
                unknown_depth_count += 1
            if depth_visibility.projected_depth is not None:
                depth_tested_count += 1
            if depth_visibility.sampled_depth is None:
                missing_depth_count += 1

        sample_count = len(sampled_faces)
        stats.append({
            "keyframeId": keyframe.id,
            "sampledFaceCenterCount": sample_count,
            "inBoundsFaceCenterCount": in_bounds_count,
            "outOfBoundsFaceCenterCount": out_of_bounds_count,
            "inBoundsRatio": round(in_bounds_count / max(sample_count, 1), 4),
            "occludedFaceCenterCount": occluded_count,
            "depthEdgeFaceCenterCount": depth_edge_count,
            "unknownDepthFaceCenterCount": unknown_depth_count,
            "depthTestedFaceCenterCount": depth_tested_count,
            "missingDepthFaceCenterCount": missing_depth_count,
        })
    return stats


def write_two_keyframe_projection_overlays(
    mesh: FusedMesh,
    keyframes: list[ProjectionKeyframe],
    output_dir: Path,
    max_points_per_keyframe: int = 5_000,
) -> list[dict]:
    overlays: list[dict] = []
    if not mesh.faces:
        return overlays

    for keyframe_index, keyframe in enumerate(keyframes[:2]):
        overlay = keyframe.image.copy()
        draw = ImageDraw.Draw(overlay)
        stride = max(1, math.ceil(len(mesh.faces) / max_points_per_keyframe))
        projected_count = 0
        occluded_count = 0
        sampled_count = 0
        for face_index in range(0, len(mesh.faces), stride):
            sampled_count += 1
            face = mesh.faces[face_index]
            vertices = [mesh.vertices[face[0]], mesh.vertices[face[1]], mesh.vertices[face[2]]]
            center = triangle_center(vertices[0], vertices[1], vertices[2])
            projection = project_world_point(center, keyframe)
            if projection is None:
                continue
            u, v, _depth = projection
            visibility = depth_visibility_for_world_point(center, keyframe)
            if visibility.status == "occluded":
                occluded_count += 1
                color = (255, 196, 0)
            else:
                projected_count += 1
                color = (0, 220, 120)
            radius = 2
            draw.ellipse((u - radius, v - radius, u + radius, v + radius), outline=color, fill=color)

        filename = f"two_keyframe_projection_{keyframe_index}.png"
        output_path = output_dir / filename
        overlay.save(output_path)
        overlays.append({
            "keyframeId": keyframe.id,
            "path": filename,
            "sampledFaceCenterCount": sampled_count,
            "projectedFaceCenterCount": projected_count,
            "occludedFaceCenterCount": occluded_count,
            "projectionRatio": round(projected_count / max(sampled_count, 1), 4),
        })
    return overlays


def is_fallback_color(color: tuple[int, int, int], tolerance: int = 3) -> bool:
    return all(abs(int(color[index]) - FALLBACK_COLOR[index]) <= tolerance for index in range(3))


def keyframe_mesh_face_visible(keyframe: ProjectionKeyframe, face_index: int) -> bool:
    mask = keyframe.mesh_visibility_mask
    if mask is None:
        return True
    return 0 <= face_index < len(mask) and mask[face_index] != 0


def build_keyframe_mesh_visibility_masks(
    mesh: FusedMesh,
    keyframes: list[ProjectionKeyframe],
    *,
    raster_max_size: int = TEXTURE_VISIBILITY_RASTER_MAX_SIZE,
) -> list[dict]:
    if not mesh.faces or not keyframes:
        return []

    face_centers = [
        triangle_center(mesh.vertices[face[0]], mesh.vertices[face[1]], mesh.vertices[face[2]])
        if valid_triangle_indices(face, len(mesh.vertices))
        else (0.0, 0.0, 0.0)
        for face in mesh.faces
    ]
    face_normals = [
        triangle_normal(mesh.vertices[face[0]], mesh.vertices[face[1]], mesh.vertices[face[2]])
        if valid_triangle_indices(face, len(mesh.vertices))
        else (0.0, 0.0, 0.0)
        for face in mesh.faces
    ]
    summaries: list[dict] = []
    for keyframe in keyframes:
        scale = min(1.0, raster_max_size / max(keyframe.width, keyframe.height, 1))
        raster_width = max(1, int(math.ceil(keyframe.width * scale)))
        raster_height = max(1, int(math.ceil(keyframe.height * scale)))
        pixel_count = raster_width * raster_height
        depth_buffer = [math.inf] * pixel_count
        face_buffer = [-1] * pixel_count
        projected_count = 0
        facing_rejected_count = 0
        out_of_bounds_count = 0
        for face_index, center in enumerate(face_centers):
            projection = project_world_point(center, keyframe)
            if projection is None:
                out_of_bounds_count += 1
                continue
            normal = face_normals[face_index]
            if normal != (0.0, 0.0, 0.0):
                view_vector = normalize(subtract(keyframe.camera_position, center))
                if abs(dot(normal, view_vector)) < TEXTURE_BLEND_MIN_FACING:
                    facing_rejected_count += 1
                    continue
            u, v, depth = projection
            x = min(raster_width - 1, max(0, int(u * scale)))
            y = min(raster_height - 1, max(0, int(v * scale)))
            buffer_index = y * raster_width + x
            if depth < depth_buffer[buffer_index]:
                depth_buffer[buffer_index] = depth
                face_buffer[buffer_index] = face_index
            projected_count += 1

        visibility_mask = bytearray(len(mesh.faces))
        visible_count = 0
        for face_index, center in enumerate(face_centers):
            projection = project_world_point(center, keyframe)
            if projection is None:
                continue
            u, v, depth = projection
            x = min(raster_width - 1, max(0, int(u * scale)))
            y = min(raster_height - 1, max(0, int(v * scale)))
            buffer_index = y * raster_width + x
            nearest_face = face_buffer[buffer_index]
            nearest_depth = depth_buffer[buffer_index]
            if nearest_face == face_index or depth <= nearest_depth + TEXTURE_VISIBILITY_DEPTH_TOLERANCE_METERS:
                visibility_mask[face_index] = 1
                visible_count += 1

        keyframe.mesh_visibility_mask = visibility_mask
        keyframe.mesh_visibility_stats = {
            "keyframeId": keyframe.debug_id,
            "rasterSize": [raster_width, raster_height],
            "faceCount": len(mesh.faces),
            "projectedFaceCenterCount": projected_count,
            "visibleFaceCenterCount": visible_count,
            "outOfBoundsFaceCenterCount": out_of_bounds_count,
            "facingRejectedFaceCenterCount": facing_rejected_count,
            "visibleFaceCenterRatio": round(visible_count / max(len(mesh.faces), 1), 4),
            "depthToleranceMeters": TEXTURE_VISIBILITY_DEPTH_TOLERANCE_METERS,
            "algorithm": "cpu_reduced_resolution_face_center_z_buffer",
        }
        summaries.append(keyframe.mesh_visibility_stats)
    return summaries


def mesh_face_neighbors(faces: list[tuple[int, int, int]]) -> list[set[int]]:
    edge_to_faces: dict[tuple[int, int], list[int]] = {}
    for face_index, face in enumerate(faces):
        for edge in ((face[0], face[1]), (face[1], face[2]), (face[2], face[0])):
            edge_to_faces.setdefault(tuple(sorted(edge)), []).append(face_index)
    neighbors = [set() for _face in faces]
    for face_indices in edge_to_faces.values():
        if len(face_indices) < 2:
            continue
        for left in face_indices:
            for right in face_indices:
                if left != right:
                    neighbors[left].add(right)
    return neighbors


def assign_coherent_face_keyframes(
    mesh: FusedMesh,
    keyframes: list[ProjectionKeyframe],
) -> tuple[list[str | None], dict]:
    face_count = len(mesh.faces)
    if not face_count or not keyframes:
        return [None] * face_count, {
            "enabled": False,
            "reason": "empty_mesh_or_keyframes",
        }

    face_candidates: list[list[TextureProjectionCandidate]] = []
    labels: list[str | None] = []
    initial_label_counts: dict[str, int] = {}
    for face_index, face in enumerate(mesh.faces):
        face_vertices = [mesh.vertices[face[0]], mesh.vertices[face[1]], mesh.vertices[face[2]]]
        candidates = texture_projection_candidates(
            face_vertices,
            keyframes,
            max_candidates=TEXTURE_COHERENT_LABEL_MAX_CANDIDATES,
            face_index=face_index,
        )
        face_candidates.append(candidates)
        label = candidates[0].keyframe_debug_id if candidates else None
        labels.append(label)
        if label is not None:
            initial_label_counts[label] = initial_label_counts.get(label, 0) + 1

    neighbors = mesh_face_neighbors(mesh.faces)
    changed_total = 0
    for _iteration in range(TEXTURE_COHERENT_LABEL_ITERATIONS):
        changed = 0
        next_labels = list(labels)
        for face_index, candidates in enumerate(face_candidates):
            if not candidates:
                continue
            score_by_label = {candidate.keyframe_debug_id: candidate.score for candidate in candidates}
            current_label = labels[face_index]
            current_score = score_by_label.get(current_label or "", 0.0)
            best_label = current_label
            best_energy = -math.inf
            for label, data_score in score_by_label.items():
                neighbor_support = sum(1 for neighbor in neighbors[face_index] if labels[neighbor] == label)
                energy = data_score + TEXTURE_COHERENT_LABEL_SMOOTHNESS_WEIGHT * neighbor_support
                if energy > best_energy:
                    best_energy = energy
                    best_label = label
            if best_label != current_label:
                best_data_score = score_by_label.get(best_label or "", 0.0)
                if current_score <= 0 or best_data_score >= current_score * TEXTURE_COHERENT_LABEL_SWITCH_TOLERANCE:
                    next_labels[face_index] = best_label
                    changed += 1
        labels = next_labels
        changed_total += changed
        if changed == 0:
            break

    final_label_counts: dict[str, int] = {}
    for label in labels:
        if label is not None:
            final_label_counts[label] = final_label_counts.get(label, 0) + 1
    seam_edges = count_label_boundary_edges(mesh.faces, labels)
    return labels, {
        "enabled": True,
        "algorithm": "adjacency_smoothed_face_owner_labels",
        "faceCount": face_count,
        "candidateRetentionPerFace": TEXTURE_COHERENT_LABEL_MAX_CANDIDATES,
        "iterationLimit": TEXTURE_COHERENT_LABEL_ITERATIONS,
        "changedLabelCount": changed_total,
        "unlabeledFaceCount": sum(1 for label in labels if label is None),
        "initialLabelCounts": top_keyframe_counts(initial_label_counts, value_label="faceCount"),
        "finalLabelCounts": top_keyframe_counts(final_label_counts, value_label="faceCount"),
        "seamEdgeCount": seam_edges,
        "smoothnessWeight": TEXTURE_COHERENT_LABEL_SMOOTHNESS_WEIGHT,
        "switchTolerance": TEXTURE_COHERENT_LABEL_SWITCH_TOLERANCE,
    }


def count_label_boundary_edges(faces: list[tuple[int, int, int]], labels: list[str | None]) -> int:
    edge_to_faces: dict[tuple[int, int], list[int]] = {}
    for face_index, face in enumerate(faces):
        for edge in ((face[0], face[1]), (face[1], face[2]), (face[2], face[0])):
            edge_to_faces.setdefault(tuple(sorted(edge)), []).append(face_index)
    seam_count = 0
    for face_indices in edge_to_faces.values():
        if len(face_indices) != 2:
            continue
        left, right = face_indices
        if labels[left] != labels[right]:
            seam_count += 1
    return seam_count


def prioritize_owner_candidate(
    candidates: list[TextureProjectionCandidate],
    owner_keyframe_id: str | None,
) -> list[TextureProjectionCandidate]:
    if not owner_keyframe_id or not candidates:
        return candidates
    owner = [candidate for candidate in candidates if candidate.keyframe_debug_id == owner_keyframe_id]
    if not owner:
        return candidates
    return owner + [candidate for candidate in candidates if candidate.keyframe_debug_id != owner_keyframe_id]


def select_face_keyframe(
    face_vertices: list[tuple[float, float, float]],
    keyframes: list[ProjectionKeyframe],
) -> ProjectionKeyframe | None:
    candidates = texture_projection_candidates(face_vertices, keyframes, max_candidates=1)
    return candidates[0].keyframe if candidates else None


def select_dense_single_view_texture_keyframe(
    mesh: FusedMesh,
    keyframes: list[ProjectionKeyframe],
) -> tuple[list[ProjectionKeyframe], dict]:
    sampled_face_count = 0
    if not keyframes:
        return [], {
            "enabled": True,
            "strategy": "max_visible_projected_face_centers",
            "sourceKeyframeCount": 0,
            "activeKeyframeCount": 0,
            "selectedKeyframeId": None,
            "fallbackReason": "no usable keyframes",
            "sampledFaceCenterCount": 0,
            "candidates": [],
        }
    if not mesh.faces:
        keyframe = keyframes[0]
        return [keyframe], {
            "enabled": True,
            "strategy": "max_visible_projected_face_centers",
            "sourceKeyframeCount": len(keyframes),
            "activeKeyframeCount": 1,
            "selectedKeyframeId": keyframe.debug_id,
            "selectedKeyframeIndex": 0,
            "fallbackReason": "mesh has no faces",
            "sampledFaceCenterCount": 0,
            "candidates": [],
        }

    stride = max(1, math.ceil(len(mesh.faces) / DENSE_SINGLE_VIEW_HERO_MAX_FACE_SAMPLES))
    candidate_stats: list[dict] = [
        {
            "index": index,
            "id": keyframe.id,
            "debugId": keyframe.debug_id,
            "score": 0.0,
            "projectedFaceCenterCount": 0,
            "edgeRejectedFaceCenterCount": 0,
            "invalidProjectionFaceCenterCount": 0,
            "occludedFaceCenterCount": 0,
            "visibleDepthFaceCenterCount": 0,
            "unknownDepthFaceCenterCount": 0,
            "meanEdgeMarginPixels": 0.0,
            "meanFacing": 0.0,
        }
        for index, keyframe in enumerate(keyframes)
    ]
    edge_margin_sums = [0.0 for _keyframe in keyframes]
    facing_sums = [0.0 for _keyframe in keyframes]

    for face_index in range(0, len(mesh.faces), stride):
        sampled_face_count += 1
        face = mesh.faces[face_index]
        vertices = [mesh.vertices[face[0]], mesh.vertices[face[1]], mesh.vertices[face[2]]]
        center = triangle_center(vertices[0], vertices[1], vertices[2])
        normal = triangle_normal(vertices[0], vertices[1], vertices[2])
        for keyframe_index, keyframe in enumerate(keyframes):
            stats = candidate_stats[keyframe_index]
            projection = project_world_point(center, keyframe)
            if projection is None:
                stats["invalidProjectionFaceCenterCount"] += 1
                continue

            u, v, depth = projection
            edge_margin = min(u, v, keyframe.width - u, keyframe.height - v)
            if edge_margin < DENSE_SINGLE_VIEW_EDGE_MARGIN_PIXELS:
                stats["edgeRejectedFaceCenterCount"] += 1
                continue

            view_vector = normalize(subtract(keyframe.camera_position, center))
            facing = abs(dot(normal, view_vector)) if normal != (0.0, 0.0, 0.0) else 0.25
            depth_visibility = depth_visibility_for_world_point(center, keyframe)
            if depth_visibility.status == "occluded":
                stats["occludedFaceCenterCount"] += 1
                depth_weight = 0.7
            elif depth_visibility.status == "visible":
                stats["visibleDepthFaceCenterCount"] += 1
                depth_weight = depth_visibility.weight
            else:
                stats["unknownDepthFaceCenterCount"] += 1
                depth_weight = TEXTURE_DEPTH_UNKNOWN_SAMPLE_WEIGHT

            center_bias = max(0.05, min(edge_margin / keyframe.center_bias_denominator, 1))
            score = center_bias * max(facing, 0.12) * depth_weight / max(depth, 0.2)
            stats["score"] += score
            stats["projectedFaceCenterCount"] += 1
            edge_margin_sums[keyframe_index] += edge_margin
            facing_sums[keyframe_index] += facing

    for index, stats in enumerate(candidate_stats):
        projected_count = max(int(stats["projectedFaceCenterCount"]), 1)
        stats["sampledFaceCenterCount"] = sampled_face_count
        stats["projectionRatio"] = round(
            int(stats["projectedFaceCenterCount"]) / max(sampled_face_count, 1),
            4,
        )
        stats["score"] = round(float(stats["score"]), 6)
        stats["meanEdgeMarginPixels"] = round(edge_margin_sums[index] / projected_count, 3)
        stats["meanFacing"] = round(facing_sums[index] / projected_count, 4)

    best_index = max(
        range(len(keyframes)),
        key=lambda index: (
            float(candidate_stats[index]["score"]),
            int(candidate_stats[index]["projectedFaceCenterCount"]),
            -index,
        ),
    )
    selected_keyframe = keyframes[best_index]
    fallback_reason = None
    if not candidate_stats[best_index]["projectedFaceCenterCount"]:
        fallback_reason = "no projected sampled face centers; using first uploaded keyframe"
        best_index = 0
        selected_keyframe = keyframes[0]

    return [selected_keyframe], {
        "enabled": True,
        "strategy": "max_visible_projected_face_centers",
        "sourceKeyframeCount": len(keyframes),
        "activeKeyframeCount": 1,
        "selectedKeyframeId": selected_keyframe.debug_id,
        "selectedKeyframeIndex": best_index,
        "bakedKeyframeIds": [selected_keyframe.debug_id],
        "fallbackReason": fallback_reason,
        "sampledFaceCenterCount": sampled_face_count,
        "faceSampleStride": stride,
        "candidates": candidate_stats,
    }


def texture_projection_candidates(
    face_vertices: list[tuple[float, float, float]],
    keyframes: list[ProjectionKeyframe],
    max_candidates: int = TEXTURE_BLEND_MAX_FACE_CANDIDATES,
    relaxed: bool = False,
    face_index: int | None = None,
) -> list[TextureProjectionCandidate]:
    if not keyframes:
        return []

    center = triangle_center(face_vertices[0], face_vertices[1], face_vertices[2])
    normal = triangle_normal(face_vertices[0], face_vertices[1], face_vertices[2])
    candidates: list[TextureProjectionCandidate] = []
    for keyframe in keyframes:
        if face_index is not None and not keyframe_mesh_face_visible(keyframe, face_index):
            continue
        projection = project_world_point(center, keyframe)
        if projection is None:
            continue

        u, v, depth = projection
        visible_vertex_count = sum(1 for vertex in face_vertices if project_world_point(vertex, keyframe) is not None)
        if visible_vertex_count == 0:
            continue

        edge_margin = min(u, v, keyframe.width - u, keyframe.height - v)
        edge_threshold = DENSE_SINGLE_VIEW_EDGE_MARGIN_PIXELS if relaxed else keyframe.edge_margin_threshold
        if edge_margin < edge_threshold:
            continue

        center_bias = max(0.05, min(edge_margin / keyframe.center_bias_denominator, 1))
        view_vector = normalize(subtract(keyframe.camera_position, center))
        facing = abs(dot(normal, view_vector)) if normal != (0.0, 0.0, 0.0) else 0.25
        min_facing = DENSE_SINGLE_VIEW_MIN_FACING if relaxed else TEXTURE_BLEND_MIN_FACING
        if facing < min_facing:
            continue

        depth_visibility = depth_visibility_for_world_point(center, keyframe)
        depth_weight = 0.05 if depth_visibility.status in {"occluded", "depth_edge"} else depth_visibility.weight

        score = (
            center_bias
            * (visible_vertex_count / 3)
            * max(facing, 0.15)
            * depth_weight
            / max(depth, 0.2)
        )
        candidates.append(TextureProjectionCandidate(
            keyframe=keyframe,
            keyframe_debug_id=keyframe.debug_id,
            score=score,
            visible_vertex_count=visible_vertex_count,
            center_projection=projection,
            facing=facing,
            center_edge_margin=edge_margin,
        ))

    candidates.sort(key=lambda candidate: candidate.score, reverse=True)
    return candidates[:max_candidates]


def texture_projection_candidates_for_region(
    region_points: list[tuple[float, float, float]],
    normal: tuple[float, float, float],
    keyframes: list[ProjectionKeyframe],
    max_candidates: int = TEXTURE_BLEND_MAX_FACE_CANDIDATES,
    relaxed: bool = False,
) -> list[TextureProjectionCandidate]:
    if not keyframes or not region_points:
        return []

    center = (
        sum(point[0] for point in region_points) / len(region_points),
        sum(point[1] for point in region_points) / len(region_points),
        sum(point[2] for point in region_points) / len(region_points),
    )
    candidates: list[TextureProjectionCandidate] = []
    for keyframe in keyframes:
        projected_points = [
            projection
            for point in region_points
            if (projection := project_world_point(point, keyframe)) is not None
        ]
        if not projected_points:
            continue

        edge_margins = [
            min(u, v, keyframe.width - u, keyframe.height - v)
            for u, v, _depth in projected_points
        ]
        best_edge_margin = max(edge_margins)
        region_edge_threshold = max(
            1.0,
            (
                DENSE_SINGLE_VIEW_EDGE_MARGIN_PIXELS
                if relaxed
                else keyframe.edge_margin_threshold * TEXTURE_PLANAR_CHART_DIRECT_EDGE_MARGIN_SCALE
            ),
        )
        if best_edge_margin < region_edge_threshold:
            continue

        projection = project_world_point(center, keyframe) or projected_points[0]
        u, v, depth = projection
        edge_margin = median_float(edge_margins)
        center_bias = max(0.05, min(edge_margin / keyframe.center_bias_denominator, 1))
        view_vector = normalize(subtract(keyframe.camera_position, center))
        facing = abs(dot(normal, view_vector)) if normal != (0.0, 0.0, 0.0) else 0.25
        min_facing = DENSE_SINGLE_VIEW_MIN_FACING if relaxed else TEXTURE_BLEND_MIN_FACING
        if facing < min_facing:
            continue

        depth_visibility = (
            depth_visibility_for_world_point(center, keyframe)
            if project_world_point(center, keyframe) is not None
            else DepthVisibilityResult("unknown", TEXTURE_DEPTH_UNKNOWN_SAMPLE_WEIGHT, None, None, None)
        )
        depth_weight = 0.05 if depth_visibility.status in {"occluded", "depth_edge"} else depth_visibility.weight

        visible_point_count = len(projected_points)
        score = (
            center_bias
            * (visible_point_count / len(region_points))
            * max(facing, 0.15)
            * depth_weight
            / max(depth, 0.2)
        )
        candidates.append(TextureProjectionCandidate(
            keyframe=keyframe,
            keyframe_debug_id=keyframe.debug_id,
            score=score,
            visible_vertex_count=min(3, visible_point_count),
            center_projection=projection,
            facing=facing,
            center_edge_margin=edge_margin,
        ))

    candidates.sort(key=lambda candidate: candidate.score, reverse=True)
    return candidates[:max_candidates]


def chart_region_points(chart: PlanarTextureChart) -> list[tuple[float, float, float]]:
    return [
        planar_chart_point(chart, chart.x + chart.width * 0.5, chart.y + chart.height * 0.5),
        planar_chart_point(chart, chart.x, chart.y),
        planar_chart_point(chart, chart.x + chart.width - 1, chart.y),
        planar_chart_point(chart, chart.x, chart.y + chart.height - 1),
        planar_chart_point(chart, chart.x + chart.width - 1, chart.y + chart.height - 1),
        planar_chart_point(chart, chart.x + chart.width * 0.5, chart.y),
        planar_chart_point(chart, chart.x + chart.width * 0.5, chart.y + chart.height - 1),
        planar_chart_point(chart, chart.x, chart.y + chart.height * 0.5),
        planar_chart_point(chart, chart.x + chart.width - 1, chart.y + chart.height * 0.5),
    ]


def empty_texture_raster_stats() -> dict:
    return {
        "filledPixelCount": 0,
        "projectedPixelCount": 0,
        "fallbackPixelCount": 0,
        "localFilledPixelCount": 0,
        "neighborFilledPixelCount": 0,
        "secondaryFilledPixelCount": 0,
        "secondaryRegionCount": 0,
        "secondaryAcceptedRegionCount": 0,
        "secondaryRejectedRegionCount": 0,
        "secondaryKeyframeIds": [],
        "unresolvedFallbackPixelCount": 0,
        "maxFillRadius": 0,
        "blendedPixelCount": 0,
        "singleSamplePixelCount": 0,
        "acceptedProjectionSampleCount": 0,
        "rejectedOverexposedSampleCount": 0,
        "rejectedUnderexposedSampleCount": 0,
        "rejectedEdgeSampleCount": 0,
        "rejectedGrazingSampleCount": 0,
        "rejectedInvalidProjectionSampleCount": 0,
        "rejectedDepthEdgeSampleCount": 0,
        "rejectedOccludedSampleCount": 0,
        "depthTestedSampleCount": 0,
        "missingDepthSampleCount": 0,
        "keyframeContributionCounts": {},
    }


def merge_texture_raster_stats(total: dict, addition: dict) -> None:
    for key in (
        "filledPixelCount",
        "projectedPixelCount",
        "fallbackPixelCount",
        "localFilledPixelCount",
        "neighborFilledPixelCount",
        "secondaryFilledPixelCount",
        "secondaryRegionCount",
        "secondaryAcceptedRegionCount",
        "secondaryRejectedRegionCount",
        "unresolvedFallbackPixelCount",
        "blendedPixelCount",
        "singleSamplePixelCount",
        "acceptedProjectionSampleCount",
        "rejectedOverexposedSampleCount",
        "rejectedUnderexposedSampleCount",
        "rejectedEdgeSampleCount",
        "rejectedGrazingSampleCount",
        "rejectedInvalidProjectionSampleCount",
        "rejectedDepthEdgeSampleCount",
        "rejectedOccludedSampleCount",
        "depthTestedSampleCount",
        "missingDepthSampleCount",
    ):
        total[key] += addition.get(key, 0)

    for key, value in addition.get("keyframeContributionCounts", {}).items():
        counts = total.setdefault("keyframeContributionCounts", {})
        counts[key] = counts.get(key, 0) + value

    secondary_ids = total.setdefault("secondaryKeyframeIds", [])
    for key in addition.get("secondaryKeyframeIds", []):
        if key not in secondary_ids:
            secondary_ids.append(key)


def direct_projected_texture_sample(
    world_point: tuple[float, float, float],
    candidates: list[TextureProjectionCandidate],
) -> TextureBlendResult:
    rejected_overexposed_count = 0
    rejected_underexposed_count = 0
    rejected_edge_count = 0
    rejected_grazing_count = 0
    rejected_invalid_projection_count = 0
    rejected_depth_edge_count = 0
    rejected_occluded_count = 0
    depth_tested_count = 0
    missing_depth_count = 0

    for candidate in candidates[:TEXTURE_PLANAR_CHART_DIRECT_MAX_CANDIDATES]:
        if candidate.facing < TEXTURE_BLEND_MIN_FACING:
            rejected_grazing_count += 1
            continue

        keyframe = candidate.keyframe
        projection = project_world_point(world_point, keyframe)
        if projection is None:
            rejected_invalid_projection_count += 1
            continue

        u, v, _depth = projection
        edge_margin = min(u, v, keyframe.width - u, keyframe.height - v)
        edge_threshold = max(1.0, keyframe.edge_margin_threshold * TEXTURE_PLANAR_CHART_DIRECT_EDGE_MARGIN_SCALE)
        if edge_margin < edge_threshold:
            rejected_edge_count += 1
            continue

        depth_visibility = depth_visibility_for_world_point(world_point, keyframe)
        if depth_visibility.status == "occluded":
            rejected_occluded_count += 1
            continue
        if depth_visibility.status == "depth_edge":
            rejected_depth_edge_count += 1
            continue
        if depth_visibility.status == "visible":
            depth_tested_count += 1
        else:
            missing_depth_count += 1

        color = sample_image_bilinear(keyframe, u, v)
        luminance = rgb_luminance(color)
        detail_range = max(color) - min(color)
        if luminance > TEXTURE_REJECT_OVEREXPOSED_LUMINANCE and detail_range <= TEXTURE_REJECT_LOW_DETAIL_RANGE:
            rejected_overexposed_count += 1
            continue
        if luminance < TEXTURE_REJECT_UNDEREXPOSED_LUMINANCE:
            rejected_underexposed_count += 1
            continue

        return TextureBlendResult(
            color,
            1,
            (candidate.keyframe_debug_id,),
            rejected_overexposed_count,
            rejected_underexposed_count,
            rejected_edge_count,
            rejected_grazing_count,
            rejected_invalid_projection_count,
            rejected_depth_edge_count,
            rejected_occluded_count,
            depth_tested_count,
            missing_depth_count,
        )

    return TextureBlendResult(
        FALLBACK_COLOR,
        0,
        (),
        rejected_overexposed_count,
        rejected_underexposed_count,
        rejected_edge_count,
        rejected_grazing_count,
        rejected_invalid_projection_count,
        rejected_depth_edge_count,
        rejected_occluded_count,
        depth_tested_count,
        missing_depth_count,
    )


def dense_single_view_texture_sample(
    world_point: tuple[float, float, float],
    candidates: list[TextureProjectionCandidate],
) -> TextureBlendResult:
    rejected_edge_count = 0
    rejected_invalid_projection_count = 0
    rejected_depth_edge_count = 0
    rejected_occluded_count = 0
    depth_tested_count = 0
    missing_depth_count = 0

    for candidate in candidates[:1]:
        keyframe = candidate.keyframe
        projection = project_world_point(world_point, keyframe)
        if projection is None:
            rejected_invalid_projection_count += 1
            continue

        u, v, _depth = projection
        edge_margin = min(u, v, keyframe.width - u, keyframe.height - v)
        if edge_margin < DENSE_SINGLE_VIEW_EDGE_MARGIN_PIXELS:
            rejected_edge_count += 1
            continue

        depth_visibility = depth_visibility_for_world_point(world_point, keyframe)
        if depth_visibility.status == "occluded":
            rejected_occluded_count += 1
            continue
        if depth_visibility.status == "depth_edge":
            rejected_depth_edge_count += 1
            continue
        if depth_visibility.status == "visible":
            depth_tested_count += 1
        else:
            missing_depth_count += 1

        return TextureBlendResult(
            sample_image_bilinear(keyframe, u, v),
            1,
            (candidate.keyframe_debug_id,),
            0,
            0,
            rejected_edge_count,
            0,
            rejected_invalid_projection_count,
            rejected_depth_edge_count,
            rejected_occluded_count,
            depth_tested_count,
            missing_depth_count,
        )

    return TextureBlendResult(
        FALLBACK_COLOR,
        0,
        (),
        0,
        0,
        rejected_edge_count,
        0,
        rejected_invalid_projection_count,
        rejected_depth_edge_count,
        rejected_occluded_count,
        depth_tested_count,
        missing_depth_count,
    )


def direct_projected_color_for_point(
    world_point: tuple[float, float, float],
    keyframes: list[ProjectionKeyframe],
) -> tuple[int, int, int] | None:
    best_score = -math.inf
    best_color: tuple[int, int, int] | None = None
    for keyframe in keyframes:
        projection = project_world_point(world_point, keyframe)
        if projection is None:
            continue

        u, v, depth = projection
        edge_margin = min(u, v, keyframe.width - u, keyframe.height - v)
        edge_threshold = max(1.0, keyframe.edge_margin_threshold * TEXTURE_PLANAR_CHART_DIRECT_EDGE_MARGIN_SCALE)
        if edge_margin < edge_threshold:
            continue

        depth_visibility = depth_visibility_for_world_point(world_point, keyframe)
        if depth_visibility.status in {"occluded", "depth_edge"}:
            continue

        color = sample_image_bilinear(keyframe, u, v)
        luminance = rgb_luminance(color)
        detail_range = max(color) - min(color)
        if luminance > TEXTURE_REJECT_OVEREXPOSED_LUMINANCE and detail_range <= TEXTURE_REJECT_LOW_DETAIL_RANGE:
            continue
        if luminance < TEXTURE_REJECT_UNDEREXPOSED_LUMINANCE:
            continue

        center_bias = max(0.05, min(edge_margin / keyframe.center_bias_denominator, 1))
        score = center_bias * depth_visibility.weight / max(depth, 0.2)
        if score > best_score:
            best_score = score
            best_color = color

    return best_color


def direct_projected_surface_color(
    world_point: tuple[float, float, float],
    normal: tuple[float, float, float],
    keyframes: list[ProjectionKeyframe],
) -> tuple[tuple[int, int, int], str] | None:
    best_score = -math.inf
    best_color: tuple[int, int, int] | None = None
    best_key: str | None = None
    surface_normal = normalize(normal)
    for keyframe in keyframes:
        projection = project_world_point(world_point, keyframe)
        if projection is None:
            continue

        u, v, depth = projection
        edge_margin = min(u, v, keyframe.width - u, keyframe.height - v)
        edge_threshold = max(1.0, keyframe.edge_margin_threshold * TEXTURE_PLANAR_CHART_DIRECT_EDGE_MARGIN_SCALE)
        if edge_margin < edge_threshold:
            continue

        facing = 0.25
        if surface_normal != (0.0, 0.0, 0.0):
            view_vector = normalize(subtract(keyframe.camera_position, world_point))
            facing = abs(dot(surface_normal, view_vector))
            if facing < TEXTURE_BLEND_MIN_FACING:
                continue

        depth_visibility = depth_visibility_for_world_point(world_point, keyframe)
        if depth_visibility.status in {"occluded", "depth_edge"}:
            continue

        color = sample_image_bilinear(keyframe, u, v)
        luminance = rgb_luminance(color)
        detail_range = max(color) - min(color)
        if luminance > TEXTURE_REJECT_OVEREXPOSED_LUMINANCE and detail_range <= TEXTURE_REJECT_LOW_DETAIL_RANGE:
            continue
        if luminance < TEXTURE_REJECT_UNDEREXPOSED_LUMINANCE:
            continue

        center_bias = max(0.05, min(edge_margin / keyframe.center_bias_denominator, 1))
        score = center_bias * max(facing, 0.15) * depth_visibility.weight / max(depth, 0.2)
        if score > best_score:
            best_score = score
            best_color = color
            best_key = keyframe.debug_id

    if best_color is None or best_key is None:
        return None
    return best_color, best_key


def dense_single_view_surface_color(
    world_point: tuple[float, float, float],
    normal: tuple[float, float, float],
    keyframes: list[ProjectionKeyframe],
) -> tuple[tuple[int, int, int], str] | None:
    best_score = -math.inf
    best_color: tuple[int, int, int] | None = None
    best_key: str | None = None
    surface_normal = normalize(normal)
    for keyframe in keyframes:
        projection = project_world_point(world_point, keyframe)
        if projection is None:
            continue

        u, v, depth = projection
        edge_margin = min(u, v, keyframe.width - u, keyframe.height - v)
        if edge_margin < DENSE_SINGLE_VIEW_EDGE_MARGIN_PIXELS:
            continue

        facing = 0.25
        if surface_normal != (0.0, 0.0, 0.0):
            view_vector = normalize(subtract(keyframe.camera_position, world_point))
            facing = abs(dot(surface_normal, view_vector))
            if facing < DENSE_SINGLE_VIEW_MIN_FACING:
                continue

        depth_visibility = depth_visibility_for_world_point(world_point, keyframe)
        if depth_visibility.status in {"occluded", "depth_edge"}:
            continue

        center_bias = max(0.05, min(edge_margin / keyframe.center_bias_denominator, 1))
        score = center_bias * max(facing, 0.12) * depth_visibility.weight / max(depth, 0.2)
        if score > best_score:
            best_score = score
            best_color = sample_image_bilinear(keyframe, u, v)
            best_key = keyframe.debug_id

    if best_color is None or best_key is None:
        return None
    return best_color, best_key


def average_dense_single_view_surface_color(
    points: list[tuple[float, float, float]],
    normal: tuple[float, float, float],
    keyframes: list[ProjectionKeyframe],
) -> tuple[int, int, int] | None:
    colors = [
        sample[0] for point in points
        if (sample := dense_single_view_surface_color(point, normal, keyframes)) is not None
    ]
    if not colors:
        return None

    count = len(colors)
    return (
        clamp_color(sum(color[0] for color in colors) / count),
        clamp_color(sum(color[1] for color in colors) / count),
        clamp_color(sum(color[2] for color in colors) / count),
    )


def average_direct_projected_surface_color(
    points: list[tuple[float, float, float]],
    normal: tuple[float, float, float],
    keyframes: list[ProjectionKeyframe],
) -> tuple[int, int, int] | None:
    colors = [
        sample[0] for point in points
        if (sample := direct_projected_surface_color(point, normal, keyframes)) is not None
    ]
    if not colors:
        return None

    count = len(colors)
    return (
        clamp_color(sum(color[0] for color in colors) / count),
        clamp_color(sum(color[1] for color in colors) / count),
        clamp_color(sum(color[2] for color in colors) / count),
    )


def average_direct_projected_color(
    points: list[tuple[float, float, float]],
    keyframes: list[ProjectionKeyframe],
) -> tuple[int, int, int] | None:
    colors = [
        color for point in points
        if (color := direct_projected_color_for_point(point, keyframes)) is not None
    ]
    if not colors:
        return None

    count = len(colors)
    return (
        clamp_color(sum(color[0] for color in colors) / count),
        clamp_color(sum(color[1] for color in colors) / count),
        clamp_color(sum(color[2] for color in colors) / count),
    )


def rasterize_planar_chart_texture(
    texture_pixels: object,
    mask_pixels: object,
    chart: PlanarTextureChart,
    candidates: list[TextureProjectionCandidate],
    fallback_color: Callable[[], tuple[int, int, int]],
    secondary_candidates: list[TextureProjectionCandidate] | None = None,
    sample_stride: int = 1,
    projection_mode: str = "blend",
) -> dict:
    stats = empty_texture_raster_stats()
    stride = max(1, sample_stride)
    chart_right = chart.x + chart.width
    chart_bottom = chart.y + chart.height
    for y in range(chart.y, chart_bottom, stride):
        for x in range(chart.x, chart_right, stride):
            color: tuple[int, int, int] | None = None
            blend: TextureBlendResult | None = None
            accepted_count = 0
            if candidates:
                world_point = planar_chart_point(chart, x + 0.5, y + 0.5)
                if projection_mode == "dense_single_view":
                    blend = dense_single_view_texture_sample(world_point, candidates)
                elif projection_mode == "direct":
                    blend = direct_projected_texture_sample(world_point, candidates)
                else:
                    blend = blend_projected_texture_sample(world_point, candidates)
                accepted_count = blend.accepted_sample_count
                if accepted_count > 0:
                    color = blend.color
            else:
                accepted_count = 0

            if color is None:
                color = fallback_color()
            block_width = min(stride, chart_right - x)
            block_height = min(stride, chart_bottom - y)
            block_pixel_count = block_width * block_height
            mask_value = 255 if accepted_count > 0 else 0
            for block_y in range(y, y + block_height):
                for block_x in range(x, x + block_width):
                    texture_pixels[block_x, block_y] = color
                    mask_pixels[block_x, block_y] = mask_value
            stats["filledPixelCount"] += block_pixel_count
            if blend is not None:
                stats["rejectedOverexposedSampleCount"] += blend.rejected_overexposed_sample_count
                stats["rejectedUnderexposedSampleCount"] += blend.rejected_underexposed_sample_count
                stats["rejectedEdgeSampleCount"] += blend.rejected_edge_sample_count
                stats["rejectedGrazingSampleCount"] += blend.rejected_grazing_sample_count
                stats["rejectedInvalidProjectionSampleCount"] += blend.rejected_invalid_projection_sample_count
                stats["rejectedDepthEdgeSampleCount"] += blend.rejected_depth_edge_sample_count
                stats["rejectedOccludedSampleCount"] += blend.rejected_occluded_sample_count
                stats["depthTestedSampleCount"] += blend.depth_tested_sample_count
                stats["missingDepthSampleCount"] += blend.missing_depth_sample_count
            if accepted_count > 0 and blend is not None:
                stats["projectedPixelCount"] += block_pixel_count
                stats["acceptedProjectionSampleCount"] += accepted_count * block_pixel_count
                if accepted_count > 1:
                    stats["blendedPixelCount"] += block_pixel_count
                else:
                    stats["singleSamplePixelCount"] += block_pixel_count
                for key in blend.keyframe_contribution_keys:
                    counts = stats["keyframeContributionCounts"]
                    counts[key] = counts.get(key, 0) + block_pixel_count
            else:
                stats["fallbackPixelCount"] += block_pixel_count

    stats["unresolvedFallbackPixelCount"] = stats["fallbackPixelCount"]
    if (
        TEXTURE_PLANAR_CHART_SECONDARY_FILL_ENABLED
        and projection_mode == "direct"
        and secondary_candidates
        and stats["fallbackPixelCount"] >= TEXTURE_PLANAR_CHART_SECONDARY_MIN_REGION_PIXELS
    ):
        secondary_stats = fill_planar_chart_holes_from_secondary_keyframes(
            texture_pixels,
            mask_pixels,
            chart,
            secondary_candidates,
        )
        secondary_filled = int(secondary_stats["secondaryFilledPixelCount"])
        stats["secondaryFilledPixelCount"] += secondary_filled
        stats["secondaryRegionCount"] += int(secondary_stats["secondaryRegionCount"])
        stats["secondaryAcceptedRegionCount"] += int(secondary_stats["secondaryAcceptedRegionCount"])
        stats["secondaryRejectedRegionCount"] += int(secondary_stats["secondaryRejectedRegionCount"])
        stats["fallbackPixelCount"] = max(0, stats["fallbackPixelCount"] - secondary_filled)
        stats["unresolvedFallbackPixelCount"] = stats["fallbackPixelCount"]
        stats["projectedPixelCount"] += secondary_filled
        stats["acceptedProjectionSampleCount"] += secondary_filled
        stats["singleSamplePixelCount"] += secondary_filled
        for key in secondary_stats.get("secondaryKeyframeIds", []):
            if key not in stats["secondaryKeyframeIds"]:
                stats["secondaryKeyframeIds"].append(key)
        for key, value in secondary_stats.get("keyframeContributionCounts", {}).items():
            counts = stats["keyframeContributionCounts"]
            counts[key] = counts.get(key, 0) + value

    if (
        TEXTURE_PLANAR_CHART_NEIGHBOR_FILL_ENABLED
        and projection_mode == "direct"
        and stats["projectedPixelCount"] > 0
        and stats["fallbackPixelCount"] > 0
    ):
        fill_stats = fill_planar_chart_holes_from_neighbors(
            texture_pixels,
            mask_pixels,
            chart,
            fallback_color(),
            max_radius=TEXTURE_PLANAR_CHART_LOCAL_FILL_MAX_RADIUS_PIXELS,
        )
        local_filled = fill_stats["localFilledPixelCount"]
        stats["localFilledPixelCount"] += local_filled
        stats["neighborFilledPixelCount"] += local_filled
        stats["fallbackPixelCount"] = fill_stats["unresolvedFallbackPixelCount"]
        stats["unresolvedFallbackPixelCount"] = fill_stats["unresolvedFallbackPixelCount"]
        stats["maxFillRadius"] = fill_stats["maxFillRadius"]

    return stats


def fill_planar_chart_holes_from_secondary_keyframes(
    texture_pixels: object,
    mask_pixels: object,
    chart: PlanarTextureChart,
    secondary_candidates: list[TextureProjectionCandidate],
) -> dict:
    chart_right = chart.x + chart.width
    chart_bottom = chart.y + chart.height
    width = chart.width
    height = chart.height
    visited = bytearray(width * height)
    neighbor_offsets = ((-1, 0), (1, 0), (0, -1), (0, 1))
    secondary_filled_count = 0
    region_count = 0
    accepted_region_count = 0
    rejected_region_count = 0
    contribution_counts: dict[str, int] = {}
    secondary_keyframe_ids: list[str] = []

    def choose_candidate(component: array) -> TextureProjectionCandidate | None:
        sample_skip = max(1, len(component) // TEXTURE_PLANAR_CHART_SECONDARY_MAX_SAMPLE_POINTS)
        sampled_indices = range(0, len(component), sample_skip)
        sample_points = [component[index] for index in sampled_indices]
        if not sample_points:
            return None

        best_candidate: TextureProjectionCandidate | None = None
        best_coverage = 0.0
        for candidate in secondary_candidates[:TEXTURE_PLANAR_CHART_DIRECT_MAX_CANDIDATES]:
            accepted = 0
            for packed_index in sample_points:
                local_x = int(packed_index % width)
                local_y = int(packed_index // width)
                world_point = planar_chart_point(chart, chart.x + local_x + 0.5, chart.y + local_y + 0.5)
                blend = direct_projected_texture_sample(world_point, [candidate])
                if blend.accepted_sample_count > 0:
                    accepted += 1

            coverage = accepted / len(sample_points)
            if coverage > best_coverage:
                best_coverage = coverage
                best_candidate = candidate

        if best_coverage < TEXTURE_PLANAR_CHART_SECONDARY_MIN_COVERAGE_RATIO:
            return None
        return best_candidate

    for local_y in range(height):
        for local_x in range(width):
            start_index = local_y * width + local_x
            if visited[start_index] != 0:
                continue

            x = chart.x + local_x
            y = chart.y + local_y
            if mask_pixels[x, y] != 0:
                visited[start_index] = 1
                continue

            queue: deque[int] = deque([start_index])
            visited[start_index] = 1
            component = array("I")
            while queue:
                index = queue.popleft()
                component.append(index)
                component_local_x = index % width
                component_local_y = index // width
                for offset_x, offset_y in neighbor_offsets:
                    neighbor_local_x = component_local_x + offset_x
                    neighbor_local_y = component_local_y + offset_y
                    if not (0 <= neighbor_local_x < width and 0 <= neighbor_local_y < height):
                        continue

                    neighbor_index = neighbor_local_y * width + neighbor_local_x
                    if visited[neighbor_index] != 0:
                        continue

                    neighbor_x = chart.x + neighbor_local_x
                    neighbor_y = chart.y + neighbor_local_y
                    if mask_pixels[neighbor_x, neighbor_y] != 0:
                        visited[neighbor_index] = 1
                        continue

                    visited[neighbor_index] = 1
                    queue.append(neighbor_index)

            if len(component) < TEXTURE_PLANAR_CHART_SECONDARY_MIN_REGION_PIXELS:
                continue

            region_count += 1
            if region_count > TEXTURE_PLANAR_CHART_SECONDARY_MAX_REGIONS:
                rejected_region_count += 1
                continue

            candidate = choose_candidate(component)
            if candidate is None:
                rejected_region_count += 1
                continue

            region_filled_count = 0
            for packed_index in component:
                local_x = int(packed_index % width)
                local_y = int(packed_index // width)
                x = chart.x + local_x
                y = chart.y + local_y
                if mask_pixels[x, y] != 0:
                    continue

                world_point = planar_chart_point(chart, x + 0.5, y + 0.5)
                blend = direct_projected_texture_sample(world_point, [candidate])
                if blend.accepted_sample_count <= 0:
                    continue

                texture_pixels[x, y] = blend.color
                mask_pixels[x, y] = 255
                region_filled_count += 1

            if region_filled_count > 0:
                accepted_region_count += 1
                secondary_filled_count += region_filled_count
                contribution_counts[candidate.keyframe_debug_id] = (
                    contribution_counts.get(candidate.keyframe_debug_id, 0) + region_filled_count
                )
                if candidate.keyframe_debug_id not in secondary_keyframe_ids:
                    secondary_keyframe_ids.append(candidate.keyframe_debug_id)
            else:
                rejected_region_count += 1

    return {
        "secondaryFilledPixelCount": secondary_filled_count,
        "secondaryRegionCount": region_count,
        "secondaryAcceptedRegionCount": accepted_region_count,
        "secondaryRejectedRegionCount": rejected_region_count,
        "secondaryKeyframeIds": secondary_keyframe_ids,
        "keyframeContributionCounts": contribution_counts,
    }


def fill_planar_chart_holes_from_neighbors(
    texture_pixels: object,
    mask_pixels: object,
    chart: PlanarTextureChart,
    fallback_color: tuple[int, int, int],
    max_radius: int = TEXTURE_PLANAR_CHART_LOCAL_FILL_MAX_RADIUS_PIXELS,
) -> dict:
    chart_right = chart.x + chart.width
    chart_bottom = chart.y + chart.height
    max_radius = max(0, int(max_radius))
    local_filled_count = 0

    neighbor_offsets = (
        (-1, -1), (0, -1), (1, -1),
        (-1, 0), (1, 0),
        (-1, 1), (0, 1), (1, 1),
    )
    width = chart.width
    height = chart.height
    visited = bytearray(width * height)
    queue: deque[int] = deque()

    if max_radius > 0:
        for local_y, y in enumerate(range(chart.y, chart_bottom)):
            for local_x, x in enumerate(range(chart.x, chart_right)):
                if mask_pixels[x, y] != 255:
                    continue

                touches_hole = False
                for offset_x, offset_y in neighbor_offsets:
                    neighbor_x = x + offset_x
                    neighbor_y = y + offset_y
                    if not (chart.x <= neighbor_x < chart_right and chart.y <= neighbor_y < chart_bottom):
                        continue
                    if mask_pixels[neighbor_x, neighbor_y] == 0:
                        touches_hole = True
                        break

                if touches_hole:
                    index = local_y * width + local_x
                    visited[index] = 1
                    queue.append(index)

    while queue:
        index = queue.popleft()
        distance = visited[index] - 1
        if distance >= max_radius:
            continue

        local_x = index % width
        local_y = index // width
        x = chart.x + local_x
        y = chart.y + local_y
        next_distance = distance + 1

        for offset_x, offset_y in neighbor_offsets:
            neighbor_local_x = local_x + offset_x
            neighbor_local_y = local_y + offset_y
            if not (0 <= neighbor_local_x < width and 0 <= neighbor_local_y < height):
                continue

            neighbor_x = x + offset_x
            neighbor_y = y + offset_y
            if mask_pixels[neighbor_x, neighbor_y] != 0:
                continue

            neighbor_index = neighbor_local_y * width + neighbor_local_x
            if visited[neighbor_index] != 0:
                continue

            colors: list[tuple[int, int, int]] = []
            for sample_offset_x, sample_offset_y in neighbor_offsets:
                sample_local_x = neighbor_local_x + sample_offset_x
                sample_local_y = neighbor_local_y + sample_offset_y
                if not (0 <= sample_local_x < width and 0 <= sample_local_y < height):
                    continue

                sample_x = chart.x + sample_local_x
                sample_y = chart.y + sample_local_y
                if mask_pixels[sample_x, sample_y] not in (64, 255):
                    continue

                sample = texture_pixels[sample_x, sample_y]
                colors.append((int(sample[0]), int(sample[1]), int(sample[2])))

            if not colors:
                continue

            texture_pixels[neighbor_x, neighbor_y] = (
                clamp_color(sum(color[0] for color in colors) / len(colors)),
                clamp_color(sum(color[1] for color in colors) / len(colors)),
                clamp_color(sum(color[2] for color in colors) / len(colors)),
            )
            mask_pixels[neighbor_x, neighbor_y] = 64
            visited[neighbor_index] = next_distance + 1
            queue.append(neighbor_index)
            local_filled_count += 1

    unresolved_count = 0
    for y in range(chart.y, chart_bottom):
        for x in range(chart.x, chart_right):
            if mask_pixels[x, y] != 0:
                continue
            texture_pixels[x, y] = fallback_color
            mask_pixels[x, y] = 128
            unresolved_count += 1

    return {
        "localFilledPixelCount": local_filled_count,
        "neighborFilledPixelCount": local_filled_count,
        "unresolvedFallbackPixelCount": unresolved_count,
        "maxFillRadius": max_radius,
    }


def fill_solid_texture_tile(
    texture_pixels: object,
    mask_pixels: object,
    tile_origin: tuple[int, int],
    tile_size: int,
    color: tuple[int, int, int],
) -> dict:
    filled_pixel_count = 0
    left, top = tile_origin
    for y in range(top, top + tile_size):
        for x in range(left, left + tile_size):
            texture_pixels[x, y] = color
            mask_pixels[x, y] = 255
            filled_pixel_count += 1

    return {
        "filledPixelCount": filled_pixel_count,
    }


def rasterize_face_texture(
    texture_pixels: object,
    mask_pixels: object,
    atlas_triangle: list[tuple[float, float]],
    face_vertices: list[tuple[float, float, float]],
    candidates: list[TextureProjectionCandidate],
    fallback_color: Callable[[], tuple[int, int, int]],
    projection_mode: str = "blend",
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
    rejected_depth_edge_sample_count = 0
    rejected_occluded_sample_count = 0
    depth_tested_sample_count = 0
    missing_depth_sample_count = 0
    keyframe_contribution_counts: dict[str, int] = {}

    for y in range(min_y, max_y + 1):
        for x in range(min_x, max_x + 1):
            bary = barycentric((x + 0.5, y + 0.5), atlas_triangle[0], atlas_triangle[1], atlas_triangle[2])
            if bary is None or min(bary) < -1e-5:
                continue

            color: tuple[int, int, int] | None = None
            if candidates:
                world_point = interpolate_triangle(face_vertices, bary)
                if projection_mode == "dense_single_view":
                    blend = dense_single_view_texture_sample(world_point, candidates)
                elif projection_mode == "direct":
                    blend = direct_projected_texture_sample(world_point, candidates)
                else:
                    blend = blend_projected_texture_sample(world_point, candidates)
                accepted_count = blend.accepted_sample_count
                if accepted_count > 0:
                    color = blend.color
                    projected_pixel_count += 1
                    accepted_projection_sample_count += accepted_count
                    rejected_overexposed_sample_count += blend.rejected_overexposed_sample_count
                    rejected_underexposed_sample_count += blend.rejected_underexposed_sample_count
                    rejected_edge_sample_count += blend.rejected_edge_sample_count
                    rejected_grazing_sample_count += blend.rejected_grazing_sample_count
                    rejected_invalid_projection_sample_count += blend.rejected_invalid_projection_sample_count
                    rejected_depth_edge_sample_count += blend.rejected_depth_edge_sample_count
                    rejected_occluded_sample_count += blend.rejected_occluded_sample_count
                    depth_tested_sample_count += blend.depth_tested_sample_count
                    missing_depth_sample_count += blend.missing_depth_sample_count
                    if accepted_count > 1:
                        blended_pixel_count += 1
                    else:
                        single_sample_pixel_count += 1
                    for key in blend.keyframe_contribution_keys:
                        keyframe_contribution_counts[key] = keyframe_contribution_counts.get(key, 0) + 1
                else:
                    rejected_overexposed_sample_count += blend.rejected_overexposed_sample_count
                    rejected_underexposed_sample_count += blend.rejected_underexposed_sample_count
                    rejected_edge_sample_count += blend.rejected_edge_sample_count
                    rejected_grazing_sample_count += blend.rejected_grazing_sample_count
                    rejected_invalid_projection_sample_count += blend.rejected_invalid_projection_sample_count
                    rejected_depth_edge_sample_count += blend.rejected_depth_edge_sample_count
                    rejected_occluded_sample_count += blend.rejected_occluded_sample_count
                    depth_tested_sample_count += blend.depth_tested_sample_count
                    missing_depth_sample_count += blend.missing_depth_sample_count
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
        "rejectedDepthEdgeSampleCount": rejected_depth_edge_sample_count,
        "rejectedOccludedSampleCount": rejected_occluded_sample_count,
        "depthTestedSampleCount": depth_tested_sample_count,
        "missingDepthSampleCount": missing_depth_sample_count,
        "keyframeContributionCounts": keyframe_contribution_counts,
    }


def blend_projected_texture_sample(
    world_point: tuple[float, float, float],
    candidates: list[TextureProjectionCandidate],
) -> TextureBlendResult:
    samples: list[tuple[float, tuple[int, int, int], str]] = []
    rejected_overexposed_count = 0
    rejected_underexposed_count = 0
    rejected_edge_count = 0
    rejected_grazing_count = 0
    rejected_invalid_projection_count = 0
    rejected_depth_edge_count = 0
    rejected_occluded_count = 0
    depth_tested_count = 0
    missing_depth_count = 0

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
        if edge_margin < keyframe.edge_margin_threshold:
            rejected_edge_count += 1
            continue

        depth_visibility = depth_visibility_for_world_point(world_point, keyframe)
        if depth_visibility.status == "occluded":
            rejected_occluded_count += 1
            continue
        if depth_visibility.status == "depth_edge":
            rejected_depth_edge_count += 1
            continue
        if depth_visibility.status == "visible":
            depth_tested_count += 1
        else:
            missing_depth_count += 1

        color = sample_image_bilinear(keyframe, u, v)
        luminance = rgb_luminance(color)
        detail_range = max(color) - min(color)
        if luminance > TEXTURE_REJECT_OVEREXPOSED_LUMINANCE and detail_range <= TEXTURE_REJECT_LOW_DETAIL_RANGE:
            rejected_overexposed_count += 1
            continue
        if luminance < TEXTURE_REJECT_UNDEREXPOSED_LUMINANCE:
            rejected_underexposed_count += 1
            continue

        edge_weight = max(0.05, min(edge_margin / keyframe.blend_edge_denominator, 1))
        weight = max(candidate.score, 1e-6) * edge_weight * depth_visibility.weight / max(depth, 0.2)
        samples.append((weight, color, candidate.keyframe_debug_id))

    if not samples:
        return TextureBlendResult(
            FALLBACK_COLOR,
            0,
            (),
            rejected_overexposed_count,
            rejected_underexposed_count,
            rejected_edge_count,
            rejected_grazing_count,
            rejected_invalid_projection_count,
            rejected_depth_edge_count,
            rejected_occluded_count,
            depth_tested_count,
            missing_depth_count,
        )

    if len(samples) == 1:
        _weight, color, key = samples[0]
        return TextureBlendResult(
            color,
            1,
            (key,),
            rejected_overexposed_count,
            rejected_underexposed_count,
            rejected_edge_count,
            rejected_grazing_count,
            rejected_invalid_projection_count,
            rejected_depth_edge_count,
            rejected_occluded_count,
            depth_tested_count,
            missing_depth_count,
        )

    total_weight = sum(weight for weight, _color, _key in samples)
    if total_weight <= 1e-8:
        total_weight = float(len(samples))
        samples = [(1.0, color, key) for _weight, color, key in samples]

    linear_r = sum(weight * SRGB_TO_LINEAR_LOOKUP[color[0]] for weight, color, _key in samples) / total_weight
    linear_g = sum(weight * SRGB_TO_LINEAR_LOOKUP[color[1]] for weight, color, _key in samples) / total_weight
    linear_b = sum(weight * SRGB_TO_LINEAR_LOOKUP[color[2]] for weight, color, _key in samples) / total_weight

    return TextureBlendResult(
        (
            linear_to_srgb(linear_r),
            linear_to_srgb(linear_g),
            linear_to_srgb(linear_b),
        ),
        len(samples),
        tuple(key for _weight, _color, key in samples),
        rejected_overexposed_count,
        rejected_underexposed_count,
        rejected_edge_count,
        rejected_grazing_count,
        rejected_invalid_projection_count,
        rejected_depth_edge_count,
        rejected_occluded_count,
        depth_tested_count,
        missing_depth_count,
    )


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
    return keyframe.edge_margin_threshold


def keyframe_debug_id(keyframe: ProjectionKeyframe) -> str:
    return keyframe.debug_id


def project_world_point(
    vertex: tuple[float, float, float],
    keyframe: ProjectionKeyframe,
) -> tuple[float, float, float] | None:
    matrix = keyframe.world_to_camera_values
    vx = float(vertex[0])
    vy = float(vertex[1])
    vz = float(vertex[2])
    x = matrix[0] * vx + matrix[4] * vy + matrix[8] * vz + matrix[12]
    y = matrix[1] * vx + matrix[5] * vy + matrix[9] * vz + matrix[13]
    z = matrix[2] * vx + matrix[6] * vy + matrix[10] * vz + matrix[14]
    depth = -z
    if depth <= 0.05:
        return None

    u = keyframe.fx * x / depth + keyframe.cx
    v = keyframe.cy - keyframe.fy * y / depth
    if not (0 <= u < keyframe.width and 0 <= v < keyframe.height):
        return None

    return u, v, depth


def project_world_point_to_depth(
    vertex: tuple[float, float, float],
    depth_frame: ProjectionDepthFrame,
) -> tuple[float, float, float] | None:
    matrix = depth_frame.world_to_camera_values
    vx = float(vertex[0])
    vy = float(vertex[1])
    vz = float(vertex[2])
    x = matrix[0] * vx + matrix[4] * vy + matrix[8] * vz + matrix[12]
    y = matrix[1] * vx + matrix[5] * vy + matrix[9] * vz + matrix[13]
    z = matrix[2] * vx + matrix[6] * vy + matrix[10] * vz + matrix[14]
    depth = -z
    if depth <= 0.05:
        return None

    u = depth_frame.fx * x / depth + depth_frame.cx
    v = depth_frame.cy - depth_frame.fy * y / depth
    if not (0 <= u < depth_frame.width and 0 <= v < depth_frame.height):
        return None

    return u, v, depth


def depth_visibility_for_world_point(
    vertex: tuple[float, float, float],
    keyframe: ProjectionKeyframe,
) -> DepthVisibilityResult:
    depth_frame = keyframe.depth_frame
    if depth_frame is None:
        return DepthVisibilityResult("unknown", TEXTURE_DEPTH_UNKNOWN_SAMPLE_WEIGHT, None, None, None)

    projection = project_world_point_to_depth(vertex, depth_frame)
    if projection is None:
        return DepthVisibilityResult("unknown", TEXTURE_DEPTH_UNKNOWN_SAMPLE_WEIGHT, None, None, None)

    u, v, projected_depth = projection
    sampled = sample_depth_frame_visibility(depth_frame, u, v)
    if sampled is None:
        return DepthVisibilityResult("unknown", TEXTURE_DEPTH_UNKNOWN_SAMPLE_WEIGHT, projected_depth, None, None)

    sampled_depth, confidence, depth_range = sampled
    tolerance = max(
        TEXTURE_DEPTH_OCCLUSION_BASE_TOLERANCE_METERS,
        projected_depth * TEXTURE_DEPTH_OCCLUSION_RELATIVE_TOLERANCE,
    )
    if sampled_depth + tolerance < projected_depth:
        return DepthVisibilityResult("occluded", 0.0, projected_depth, sampled_depth, confidence)
    depth_edge_threshold = max(
        TEXTURE_DEPTH_EDGE_ABSOLUTE_METERS,
        projected_depth * TEXTURE_DEPTH_EDGE_RELATIVE,
    )
    if depth_range > depth_edge_threshold:
        return DepthVisibilityResult("depth_edge", 0.0, projected_depth, sampled_depth, confidence)

    depth_error = abs(sampled_depth - projected_depth)
    match_weight = clamp_float(
        1 - (depth_error / max(tolerance * 4, 1e-6)),
        TEXTURE_DEPTH_MISMATCH_MIN_WEIGHT,
        1.0,
    )
    confidence_weight = 1.0
    if confidence is not None:
        confidence_weight = clamp_float(0.72 + min(max(confidence, 0), 2) * 0.14, 0.72, 1.0)

    return DepthVisibilityResult(
        "visible",
        clamp_float(match_weight * confidence_weight, TEXTURE_DEPTH_MISMATCH_MIN_WEIGHT, 1.0),
        projected_depth,
        sampled_depth,
        confidence,
    )


def sample_depth_frame_visibility(
    depth_frame: ProjectionDepthFrame,
    u: float,
    v: float,
) -> tuple[float, int | None, float] | None:
    center_x = int(round(u))
    center_y = int(round(v))
    radius = TEXTURE_DEPTH_NEIGHBORHOOD_RADIUS
    values: list[float] = []
    confidence_values: list[int] = []

    for y in range(center_y - radius, center_y + radius + 1):
        if y < 0 or y >= depth_frame.height:
            continue
        for x in range(center_x - radius, center_x + radius + 1):
            if x < 0 or x >= depth_frame.width:
                continue
            index = y * depth_frame.width + x
            if depth_frame.confidence_values is not None:
                confidence = int(depth_frame.confidence_values[index])
                if confidence == 0:
                    continue
                confidence_values.append(confidence)
            depth = float(depth_frame.depth_values[index])
            if not math.isfinite(depth) or depth <= 0 or depth > RGBD_DEPTH_TRUNC_METERS:
                continue
            values.append(depth)

    if not values:
        return None

    confidence = max(confidence_values) if confidence_values else None
    return median_float(values), confidence, max(values) - min(values)


def sample_depth_frame(
    depth_frame: ProjectionDepthFrame,
    u: float,
    v: float,
) -> tuple[float, int | None] | None:
    center_x = int(round(u))
    center_y = int(round(v))
    radius = TEXTURE_DEPTH_NEIGHBORHOOD_RADIUS
    values: list[float] = []
    confidence_values: list[int] = []

    for y in range(center_y - radius, center_y + radius + 1):
        if y < 0 or y >= depth_frame.height:
            continue
        for x in range(center_x - radius, center_x + radius + 1):
            if x < 0 or x >= depth_frame.width:
                continue
            index = y * depth_frame.width + x
            if depth_frame.confidence_values is not None:
                confidence = int(depth_frame.confidence_values[index])
                if confidence == 0:
                    continue
                confidence_values.append(confidence)
            depth = float(depth_frame.depth_values[index])
            if not math.isfinite(depth) or depth <= 0 or depth > RGBD_DEPTH_TRUNC_METERS:
                continue
            values.append(depth)

    if not values:
        return None

    confidence = max(confidence_values) if confidence_values else None
    return median_float(values), confidence


def sample_image_nearest(keyframe: ProjectionKeyframe, u: float, v: float) -> tuple[int, int, int]:
    pixels = keyframe.pixels
    x = max(0, min(int(round(u)), keyframe.width - 1))
    y = max(0, min(int(round(v)), keyframe.height - 1))
    return pixels[x, y]


def sample_image_bilinear(keyframe: ProjectionKeyframe, u: float, v: float) -> tuple[int, int, int]:
    pixels = keyframe.pixels
    width = keyframe.width
    height = keyframe.height
    x0 = max(0, min(int(math.floor(u)), width - 1))
    y0 = max(0, min(int(math.floor(v)), height - 1))
    x1 = max(0, min(x0 + 1, width - 1))
    y1 = max(0, min(y0 + 1, height - 1))
    dx = u - x0
    dy = v - y0
    c00 = pixels[x0, y0]
    c10 = pixels[x1, y0]
    c01 = pixels[x0, y1]
    c11 = pixels[x1, y1]
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


SRGB_TO_LINEAR_LOOKUP = tuple(srgb_to_linear(value) for value in range(256))


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


def add(
    a: tuple[float, float, float],
    b: tuple[float, float, float],
) -> tuple[float, float, float]:
    return (a[0] + b[0], a[1] + b[1], a[2] + b[2])


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
