import asyncio
import base64
import json
import struct
from io import BytesIO

import pytest
from httpx import ASGITransport, AsyncClient
from PIL import Image

import app.pipeline as pipeline
from app.config import settings
from app.main import app
from app.models import JobStage, JobStatus
from app.store import JobStore


def auth_headers():
    if not settings.auth_token:
        return {}
    return {"Authorization": f"Bearer {settings.auth_token}"}


async def wait_for_complete(client: AsyncClient, job_id: str, headers: dict[str, str]) -> dict:
    for _ in range(40):
        status_resp = await client.get(f"/api/v1/jobs/{job_id}", headers=headers)
        body = status_resp.json()
        if body["stage"] == "complete" or body["status"] == "failed":
            return body
        await asyncio.sleep(0.1)
    return (await client.get(f"/api/v1/jobs/{job_id}", headers=headers)).json()


def encoded_test_jpeg() -> str:
    image = Image.new("RGB", (100, 100), (205, 90, 45))
    buffer = BytesIO()
    image.save(buffer, format="JPEG")
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def encoded_depth_values(values: list[float]) -> str:
    return base64.b64encode(struct.pack(f"<{len(values)}f", *values)).decode("ascii")


def make_grid_mesh(size: int, spacing: float = 0.01) -> pipeline.FusedMesh:
    vertices = [
        (x * spacing, y * spacing, -1.0)
        for y in range(size + 1)
        for x in range(size + 1)
    ]
    faces = []
    for y in range(size):
        for x in range(size):
            top_left = y * (size + 1) + x
            top_right = top_left + 1
            bottom_left = top_left + size + 1
            bottom_right = bottom_left + 1
            faces.append((top_left, bottom_left, top_right))
            faces.append((top_right, bottom_left, bottom_right))
    return pipeline.FusedMesh(vertices=vertices, faces=faces, stats={"geometrySource": "test_grid"})


def test_job_store_rehydrates_persisted_job_records(tmp_path):
    store = JobStore(str(tmp_path))
    record = store.create_job()
    record.status = JobStatus.running
    record.stage = JobStage.texturing
    record.progress = 82
    record.total_bytes = 1234
    record.uploaded_bytes = 1234
    store._persist(record)

    reloaded_store = JobStore(str(tmp_path))
    reloaded = reloaded_store.get(record.job_id)

    assert reloaded is not None
    assert reloaded.status == JobStatus.failed
    assert reloaded.stage == JobStage.failed
    assert reloaded.total_bytes == 1234
    assert reloaded.uploaded_bytes == 1234
    assert "restart" in reloaded.message


def test_arkit_depth_backprojection_roundtrips_through_keyframe_projection():
    intrinsics = [
        10, 0, 0,
        0, 10, 0,
        1, 1, 1,
    ]
    transform = [
        1, 0, 0, 0,
        0, 1, 0, 0,
        0, 0, 1, 0,
        0, 0, 0, 1,
    ]
    world = pipeline.backproject_depth_sample_to_world(
        source_x=1,
        source_y=0,
        depth=1.0,
        intrinsics=intrinsics,
        camera_transform=transform,
    )
    image = Image.new("RGB", (3, 3), (255, 255, 255))
    keyframe = pipeline.ProjectionKeyframe(
        image=image,
        width=3,
        height=3,
        world_to_camera=pipeline.invert_rigid_transform(transform),
        camera_position=(0, 0, 0),
        intrinsics=intrinsics,
        pixels=image.load(),
    )

    projection = pipeline.project_world_point(world, keyframe)
    assert projection is not None
    assert projection[0] == pytest.approx(1)
    assert projection[1] == pytest.approx(0)


@pytest.mark.asyncio
async def test_health_check_startup():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/health")
        assert response.status_code == 200
        assert response.json()["status"] == "ok"


@pytest.mark.asyncio
async def test_job_lifecycle():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        headers = auth_headers()
        create_resp = await client.post("/api/v1/jobs", headers=headers)
        assert create_resp.status_code == 200
        job_id = create_resp.json()["jobId"]

        payload = {
            "schemaVersion": "v1",
            "createdAt": "2026-05-18T12:00:00Z",
            "meshAnchors": [],
            "roomJSONBase64": None,
        }
        data = json.dumps(payload).encode("utf-8")

        up_resp = await client.post(
            f"/api/v1/jobs/{job_id}/upload",
            headers={**headers, "x-upload-offset": "0", "x-upload-total": str(len(data))},
            content=data,
        )
        assert up_resp.status_code == 200
        assert up_resp.json()["complete"] is True

        fin_resp = await client.post(
            f"/api/v1/jobs/{job_id}/finalize",
            headers=headers,
            json={"totalBytes": len(data), "filename": "scan_payload.json"},
        )
        assert fin_resp.status_code == 200

        status = await wait_for_complete(client, job_id, headers)
        assert status["status"] == "complete"
        assert status["artifacts"]["manifestUrl"] == f"/api/v1/jobs/{job_id}/result/manifest.json"
        assert status["artifacts"]["vertexColoredPlyUrl"] == f"/api/v1/jobs/{job_id}/result/colored_mesh.ply"
        assert status["artifacts"]["texturedObjUrl"] == f"/api/v1/jobs/{job_id}/result/textured_mesh.obj"
        assert status["artifacts"]["textureDebugJsonUrl"] == f"/api/v1/jobs/{job_id}/result/texture_debug.json"

        result_resp = await client.get(f"/api/v1/jobs/{job_id}/result", headers=headers)
        assert result_resp.status_code == 200
        manifest_resp = await client.get(f"/api/v1/jobs/{job_id}/result/manifest.json", headers=headers)
        assert manifest_resp.status_code == 200
        manifest = manifest_resp.json()
        assert manifest["artifacts"]["rawFusedMesh"]["path"] == "fused_mesh.obj"
        assert manifest["artifacts"]["vertexColoredPlyDebugPreview"]["path"] == "colored_mesh.ply"
        assert manifest["artifacts"]["texturedObj"]["objPath"] == "textured_mesh.obj"
        assert manifest["artifacts"]["textureDebug"]["path"] == "texture_debug.json"


@pytest.mark.asyncio
async def test_textured_obj_artifacts_are_exported():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        headers = auth_headers()
        create_resp = await client.post("/api/v1/jobs", headers=headers)
        assert create_resp.status_code == 200
        job_id = create_resp.json()["jobId"]

        payload = {
            "schemaVersion": "v1",
            "createdAt": "2026-05-18T12:00:00Z",
            "meshAnchors": [
                {
                    "id": "00000000-0000-0000-0000-000000000001",
                    "transform": [
                        1, 0, 0, 0,
                        0, 1, 0, 0,
                        0, 0, 1, 0,
                        0, 0, 0, 1,
                    ],
                    "vertices": [
                        [-0.2, -0.2, -1.0],
                        [0.2, -0.2, -1.0],
                        [-0.2, 0.2, -1.0],
                        [-0.2, -0.2, -1.0],
                    ],
                    "triangleIndices": [0, 1, 2, 0, 3, 1],
                }
            ],
            "roomJSONBase64": None,
            "images": [
                {
                    "id": "00000000-0000-0000-0000-000000000002",
                    "capturedAt": "2026-05-18T12:00:01Z",
                    "timestamp": 1.0,
                    "cameraTransform": [
                        1, 0, 0, 0,
                        0, 1, 0, 0,
                        0, 0, 1, 0,
                        0, 0, 0, 1,
                    ],
                    "intrinsics": [
                        100, 0, 0,
                        0, 100, 0,
                        50, 50, 1,
                    ],
                    "imageResolution": [100, 100],
                    "jpegBase64": encoded_test_jpeg(),
                }
            ],
        }
        data = json.dumps(payload).encode("utf-8")

        up_resp = await client.post(
            f"/api/v1/jobs/{job_id}/upload",
            headers={**headers, "x-upload-offset": "0", "x-upload-total": str(len(data))},
            content=data,
        )
        assert up_resp.status_code == 200

        fin_resp = await client.post(
            f"/api/v1/jobs/{job_id}/finalize",
            headers=headers,
            json={"totalBytes": len(data), "filename": "scan_payload.json"},
        )
        assert fin_resp.status_code == 200

        status = await wait_for_complete(client, job_id, headers)
        assert status["status"] == "complete"
        assert status["artifacts"]["texturedObjUrl"].endswith("/textured_mesh.obj")
        assert status["artifacts"]["texturePngUrl"].endswith("/textured_mesh_texture.png")
        assert status["artifacts"]["usdzUrl"] is None

        obj_resp = await client.get(f"/api/v1/jobs/{job_id}/result/textured_mesh.obj", headers=headers)
        assert obj_resp.status_code == 200
        assert "mtllib textured_mesh.mtl" in obj_resp.text
        assert "vt " in obj_resp.text

        mtl_resp = await client.get(f"/api/v1/jobs/{job_id}/result/textured_mesh.mtl", headers=headers)
        assert mtl_resp.status_code == 200
        assert "map_Kd textured_mesh_texture.png" in mtl_resp.text

        png_resp = await client.get(f"/api/v1/jobs/{job_id}/result/textured_mesh_texture.png", headers=headers)
        assert png_resp.status_code == 200
        assert png_resp.headers["content-type"].startswith("image/png")

        debug_resp = await client.get(f"/api/v1/jobs/{job_id}/result/texture_debug.json", headers=headers)
        assert debug_resp.status_code == 200
        debug = debug_resp.json()
        assert debug["version"] == "v2"
        assert debug["objSyntax"]["faceWithUVIndexCount"] == 1
        assert debug["uv"]["outOfRangeCoordinateCount"] == 0
        assert debug["textureAtlas"]["fallbackColor"] == list(pipeline.FALLBACK_COLOR)
        assert debug["textureAtlas"]["tilePadding"] >= 1
        assert debug["textureAtlas"]["dilatedPixelCount"] > 0
        assert debug["textureAtlas"]["sampledPixels"]["sampleCount"] > 0
        assert debug["uvFaceInteriorSamples"]["nonWhiteRatio"] > 0
        assert debug["colorCorrection"]["enabled"] is True
        assert "meanSaturation" in debug["uvFaceInteriorSamples"]

        preview_resp = await client.get(f"/api/v1/jobs/{job_id}/result/texture_debug_preview.png", headers=headers)
        assert preview_resp.status_code == 200
        assert preview_resp.headers["content-type"].startswith("image/png")

        manifest = (await client.get(f"/api/v1/jobs/{job_id}/result/manifest.json", headers=headers)).json()
        assert manifest["preferredPhotorealArtifact"] == "textured_obj"
        assert manifest["artifacts"]["texturedObj"]["stats"]["texturedFaceCount"] == 1
        assert manifest["artifacts"]["texturedObj"]["stats"]["uvStrategy"] == "render_mesh_per_face_atlas_padded"
        assert manifest["artifacts"]["texturedObj"]["stats"]["renderMesh"]["used"] is False
        assert manifest["artifacts"]["textureDebug"]["available"] is True
        assert manifest["artifacts"]["textureDebug"]["stats"]["objSyntax"]["faceWithUVIndexCount"] == 1
        assert manifest["artifacts"]["rawFusedMesh"]["stats"]["invalidFaceCount"] == 1


def test_texture_render_mesh_reduces_dense_mesh_for_larger_atlas_tiles(monkeypatch):
    monkeypatch.setattr(pipeline, "TEXTURE_RENDER_TARGET_FACE_COUNT", 200)
    mesh = make_grid_mesh(size=40)
    original_tile = pipeline.atlas_layout(len(mesh.faces))[2]

    render_mesh = pipeline.make_texture_render_mesh(mesh)
    render_stats = render_mesh.stats["textureRenderMesh"]
    render_tile = pipeline.atlas_layout(len(render_mesh.faces))[2]

    assert render_stats["used"] is True
    assert render_stats["sourceFaceCount"] == len(mesh.faces)
    assert render_stats["renderFaceCount"] == len(render_mesh.faces)
    assert len(render_mesh.faces) < len(mesh.faces)
    assert render_tile > original_tile
    assert render_stats["smoothing"]["enabled"] is True
    assert render_stats["smoothing"]["movedVertexCount"] > 0
    assert render_stats["smoothing"]["maxTotalDisplacementMeters"] == pipeline.TEXTURE_RENDER_SMOOTHING_MAX_TOTAL_DISPLACEMENT_METERS


def test_tsdf_texture_render_mesh_prefers_open3d_quadric_decimation(monkeypatch):
    try:
        pipeline.load_open3d_modules()
    except pipeline.RGBDFusionUnavailable as exc:
        pytest.skip(str(exc))

    monkeypatch.setattr(pipeline, "TEXTURE_TSDF_RENDER_TARGET_FACE_COUNT", 200)
    mesh = make_grid_mesh(size=25)
    mesh.stats["geometrySource"] = "rgbd_tsdf_open3d"

    render_mesh = pipeline.make_texture_render_mesh(mesh)
    render_stats = render_mesh.stats["textureRenderMesh"]

    assert render_stats["used"] is True
    assert render_stats["algorithm"] == "open3d_quadric_decimation"
    assert render_stats["atlasMaxSize"] == pipeline.TEXTURE_TSDF_ATLAS_MAX_SIZE
    assert render_stats["renderFaceCount"] <= 200
    assert render_stats["renderFaceCount"] < len(mesh.faces)
    assert render_stats["smoothing"]["enabled"] is False


def test_texture_render_smoothing_reduces_spiky_vertices():
    mesh = pipeline.FusedMesh(
        vertices=[
            (-1.0, -1.0, 0.0),
            (0.0, -1.0, 0.0),
            (1.0, -1.0, 0.0),
            (-1.0, 0.0, 0.0),
            (0.0, 0.0, 0.55),
            (1.0, 0.0, 0.0),
            (-1.0, 1.0, 0.0),
            (0.0, 1.0, 0.0),
            (1.0, 1.0, 0.0),
        ],
        faces=[
            (0, 1, 4),
            (0, 4, 3),
            (1, 2, 5),
            (1, 5, 4),
            (3, 4, 7),
            (3, 7, 6),
            (4, 5, 8),
            (4, 8, 7),
        ],
        stats={},
    )

    smoothed, stats = pipeline.smooth_texture_render_mesh(
        mesh,
        iterations=5,
        strength=0.5,
        boundary_strength=0.1,
        hard_edge_weight=0.2,
        normal_cosine_threshold=0.72,
        max_step_meters=0.12,
        max_total_displacement_meters=0.25,
    )

    assert stats["enabled"] is True
    assert stats["movedVertexCount"] > 0
    assert smoothed.vertices[4][2] < mesh.vertices[4][2]


def test_keyframe_color_correction_includes_bounded_exposure_scales():
    dark = Image.new("RGB", (24, 24), (48, 48, 48))
    bright = Image.new("RGB", (24, 24), (220, 220, 220))

    correction = pipeline.build_keyframe_color_correction([dark, bright])
    exposure = correction["perKeyframeExposure"]

    assert correction["enabled"] is True
    assert correction["algorithm"] == "gray_world_channel_balance_with_bounded_per_keyframe_exposure"
    assert len(exposure) == 2
    assert 1.0 < exposure[0]["luminanceScale"] <= 1.25
    assert 0.75 <= exposure[1]["luminanceScale"] < 1.0


@pytest.mark.asyncio
async def test_textured_obj_blends_multiple_valid_keyframes(tmp_path):
    transform = [
        1, 0, 0, 0,
        0, 1, 0, 0,
        0, 0, 1, 0,
        0, 0, 0, 1,
    ]
    intrinsics = [
        100, 0, 0,
        0, 100, 0,
        50, 50, 1,
    ]
    red = Image.new("RGB", (100, 100), (220, 30, 30))
    green = Image.new("RGB", (100, 100), (30, 220, 30))
    keyframes = [
        pipeline.ProjectionKeyframe(
            image=red,
            width=red.width,
            height=red.height,
            world_to_camera=pipeline.invert_rigid_transform(transform),
            camera_position=(0, 0, 0),
            intrinsics=intrinsics,
            pixels=red.load(),
            id="red",
        ),
        pipeline.ProjectionKeyframe(
            image=green,
            width=green.width,
            height=green.height,
            world_to_camera=pipeline.invert_rigid_transform(transform),
            camera_position=(0, 0, 0),
            intrinsics=intrinsics,
            pixels=green.load(),
            id="green",
        ),
    ]
    mesh = pipeline.FusedMesh(
        vertices=[
            (-0.2, -0.2, -1.0),
            (0.2, -0.2, -1.0),
            (-0.2, 0.2, -1.0),
        ],
        faces=[(0, 1, 2)],
        stats={"geometrySource": "test"},
    )

    stats = await pipeline.write_textured_obj(
        mesh=mesh,
        keyframes=keyframes,
        output_obj_path=tmp_path / "textured.obj",
        output_mtl_path=tmp_path / "textured.mtl",
        output_texture_path=tmp_path / "texture.png",
        output_debug_path=tmp_path / "debug.json",
        output_debug_preview_path=tmp_path / "preview.png",
    )
    projection = stats["diagnostics"]["projection"]

    assert projection["blendedPixelCount"] > 0
    assert projection["singleSamplePixelCount"] == 0
    assert projection["meanSamplesPerProjectedPixel"] == pytest.approx(2.0)
    assert {item["keyframe"] for item in projection["keyframeContributionCounts"]} == {"red", "green"}
    assert stats["projectionCoverage"] == 1.0


def test_open3d_tsdf_postprocess_removes_small_components_when_available():
    try:
        np, o3d = pipeline.load_open3d_modules()
    except pipeline.RGBDFusionUnavailable as exc:
        pytest.skip(str(exc))

    mesh = o3d.geometry.TriangleMesh()
    mesh.vertices = o3d.utility.Vector3dVector(np.array([
        [0.0, 0.0, 0.0],
        [2.0, 0.0, 0.0],
        [0.0, 2.0, 0.0],
        [2.0, 2.0, 0.0],
        [10.0, 10.0, 0.0],
        [10.01, 10.0, 0.0],
        [10.0, 10.01, 0.0],
    ], dtype=np.float64))
    mesh.triangles = o3d.utility.Vector3iVector(np.array([
        [0, 1, 2],
        [1, 3, 2],
        [4, 5, 6],
    ], dtype=np.int32))

    processed, stats = pipeline.postprocess_open3d_tsdf_mesh(mesh, o3d, np)

    assert stats["componentFiltering"]["enabled"] is True
    assert stats["componentFiltering"]["removedComponentCount"] == 1
    assert stats["componentFiltering"]["removedFaceCount"] == 1
    assert stats["smoothing"]["enabled"] is True
    assert len(processed.triangles) == 2


@pytest.mark.asyncio
async def test_depth_frames_are_decoded_and_rgbd_fallback_mesh_is_exported(monkeypatch):
    def unavailable_open3d():
        raise pipeline.RGBDFusionUnavailable("Open3D intentionally unavailable in test.")

    monkeypatch.setattr(pipeline, "load_open3d_modules", unavailable_open3d)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        headers = auth_headers()
        create_resp = await client.post("/api/v1/jobs", headers=headers)
        assert create_resp.status_code == 200
        job_id = create_resp.json()["jobId"]

        payload = {
            "schemaVersion": "v1",
            "createdAt": "2026-05-18T12:00:00Z",
            "meshAnchors": [
                {
                    "id": "00000000-0000-0000-0000-000000000011",
                    "transform": [
                        1, 0, 0, 0,
                        0, 1, 0, 0,
                        0, 0, 1, 0,
                        0, 0, 0, 1,
                    ],
                    "vertices": [
                        [-0.2, -0.2, -1.0],
                        [0.2, -0.2, -1.0],
                        [-0.2, 0.2, -1.0],
                    ],
                    "triangleIndices": [0, 1, 2],
                }
            ],
            "roomJSONBase64": None,
            "images": [
                {
                    "id": "00000000-0000-0000-0000-000000000012",
                    "capturedAt": "2026-05-18T12:00:01Z",
                    "timestamp": 1.0,
                    "cameraTransform": [
                        1, 0, 0, 0,
                        0, 1, 0, 0,
                        0, 0, 1, 0,
                        0, 0, 0, 1,
                    ],
                    "intrinsics": [
                        100, 0, 0,
                        0, 100, 0,
                        50, 50, 1,
                    ],
                    "imageResolution": [100, 100],
                    "jpegBase64": encoded_test_jpeg(),
                }
            ],
            "depthFrames": [
                {
                    "id": "00000000-0000-0000-0000-000000000013",
                    "colorKeyframeId": "00000000-0000-0000-0000-000000000012",
                    "capturedAt": "2026-05-18T12:00:01Z",
                    "timestamp": 1.0,
                    "cameraTransform": [
                        1, 0, 0, 0,
                        0, 1, 0, 0,
                        0, 0, 1, 0,
                        0, 0, 0, 1,
                    ],
                    "intrinsics": [
                        10, 0, 0,
                        0, 10, 0,
                        1, 1, 1,
                    ],
                    "depthResolution": [2, 2],
                    "depthFormat": "float32_little_endian_meters",
                    "depthBase64": encoded_depth_values([1.0, 1.0, 1.0, 1.0]),
                    "confidenceFormat": "uint8_arkit_confidence",
                    "confidenceBase64": base64.b64encode(bytes([2, 2, 1, 2])).decode("ascii"),
                    "metersPerUnit": 1,
                }
            ],
        }
        data = json.dumps(payload).encode("utf-8")

        await client.post(
            f"/api/v1/jobs/{job_id}/upload",
            headers={**headers, "x-upload-offset": "0", "x-upload-total": str(len(data))},
            content=data,
        )
        await client.post(
            f"/api/v1/jobs/{job_id}/finalize",
            headers=headers,
            json={"totalBytes": len(data), "filename": "scan_payload.json"},
        )

        status = await wait_for_complete(client, job_id, headers)
        assert status["status"] == "complete"
        assert status["artifacts"]["arkitFusedMeshUrl"].endswith("/arkit_fused_mesh.obj")
        assert status["artifacts"]["rgbdFusedMeshUrl"].endswith("/rgbd_fused_mesh.obj")

        depth_manifest = (await client.get(f"/api/v1/jobs/{job_id}/result/depth_frame_manifest.json", headers=headers)).json()
        assert len(depth_manifest) == 1
        assert depth_manifest[0]["depthResolution"] == [2, 2]

        rgbd_stats = (await client.get(f"/api/v1/jobs/{job_id}/result/rgbd_fusion_stats.json", headers=headers)).json()
        assert rgbd_stats["used"] is True
        assert rgbd_stats["geometrySource"] == "rgbd_keyframe_depth_mesh"
        assert rgbd_stats["tsdfUnavailableReason"] == "Open3D intentionally unavailable in test."

        rgbd_mesh = await client.get(f"/api/v1/jobs/{job_id}/result/rgbd_fused_mesh.obj", headers=headers)
        assert rgbd_mesh.status_code == 200
        assert "o fused_mesh" in rgbd_mesh.text

        manifest = (await client.get(f"/api/v1/jobs/{job_id}/result/manifest.json", headers=headers)).json()
        assert manifest["artifacts"]["rgbdFusedMesh"]["stats"]["used"] is True


@pytest.mark.asyncio
async def test_upload_resume():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        headers = auth_headers()
        job_id = (await client.post("/api/v1/jobs", headers=headers)).json()["jobId"]
        full = b"abcdefghijklmnopqrstuvwxyz"
        first = full[:10]
        second = full[10:]

        await client.post(
            f"/api/v1/jobs/{job_id}/upload",
            headers={**headers, "x-upload-offset": "0", "x-upload-total": str(len(full))},
            content=first,
        )
        state = await client.get(f"/api/v1/jobs/{job_id}/upload-state", headers=headers)
        assert state.json()["receivedBytes"] == 10

        await client.post(
            f"/api/v1/jobs/{job_id}/upload",
            headers={**headers, "x-upload-offset": "10", "x-upload-total": str(len(full))},
            content=second,
        )
        state = await client.get(f"/api/v1/jobs/{job_id}/upload-state", headers=headers)
        assert state.json()["receivedBytes"] == len(full)
