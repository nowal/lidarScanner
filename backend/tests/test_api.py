import asyncio
import base64
import json
import struct
from io import BytesIO

import pytest
from pydantic import ValidationError
from httpx import ASGITransport, AsyncClient
from PIL import Image

import app.pipeline as pipeline
from app.config import settings
from app.home_ai import HomeAIAttachment, HomeAIChatRequest, HomeAIConversationState, _responses_input
from app.home_context_builder import build_home_guide_model_context, context_payload_size_chars
from app.home_guide_prompt import assign_home_guide_prompt_variant
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


def test_home_ai_responses_input_includes_uploaded_attachments():
    request = HomeAIChatRequest(
        message="Can you use these attachments for paint ideas?",
        attachments=[
            HomeAIAttachment(
                kind="image",
                fileName="living-room.jpg",
                mimeType="image/jpeg",
                byteCount=12,
                dataBase64="aW1hZ2U=",
            ),
            HomeAIAttachment(
                kind="file",
                fileName="scope.pdf",
                mimeType="application/pdf",
                byteCount=10,
                dataBase64="cGRm",
            ),
        ],
    )

    user_content = _responses_input(
        request,
        image_limit=0,
        include_history=True,
        prompt_variant="control",
    )[2]["content"]

    context_payload = json.loads(user_content[0]["text"])
    assert context_payload["userAttachments"][0]["fileName"] == "living-room.jpg"
    assert context_payload["userAttachments"][0]["includedAsModelInput"] is True
    assert any(
        item["type"] == "input_image" and item["image_url"].startswith("data:image/jpeg;base64,")
        for item in user_content
    )
    assert any(
        item["type"] == "input_file"
        and item["filename"] == "scope.pdf"
        and item["file_data"].startswith("data:application/pdf;base64,")
        for item in user_content
    )


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


def make_centered_wall_grid(size: int, spacing: float = 0.04) -> pipeline.FusedMesh:
    half = size * spacing / 2
    vertices = [
        (x * spacing - half, y * spacing - half, -1.0)
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
    return pipeline.FusedMesh(vertices=vertices, faces=faces, stats={"geometrySource": "test_wall_grid"})


def make_disconnected_wall_grids(size: int, spacing: float = 0.04, gap: float = 1.0) -> pipeline.FusedMesh:
    vertices: list[tuple[float, float, float]] = []
    faces: list[tuple[int, int, int]] = []
    half = size * spacing / 2
    for offset_x in (-gap / 2, gap / 2):
        vertex_offset = len(vertices)
        vertices.extend([
            (x * spacing - half + offset_x, y * spacing - half, -1.0)
            for y in range(size + 1)
            for x in range(size + 1)
        ])
        for y in range(size):
            for x in range(size):
                top_left = vertex_offset + y * (size + 1) + x
                top_right = top_left + 1
                bottom_left = top_left + size + 1
                bottom_right = bottom_left + 1
                faces.append((top_left, bottom_left, top_right))
                faces.append((top_right, bottom_left, bottom_right))
    return pipeline.FusedMesh(vertices=vertices, faces=faces, stats={"geometrySource": "test_disconnected_wall_grids"})


def make_test_planar_chart(width: int, height: int) -> pipeline.PlanarTextureChart:
    return pipeline.PlanarTextureChart(
        chart_id=0,
        face_indices=[0],
        normal=(0, 0, 1),
        plane_offset=1,
        axis_u=(1, 0, 0),
        axis_v=(0, 1, 0),
        min_u=-1.0,
        max_u=1.0,
        min_v=-1.0,
        max_v=1.0,
        width=width,
        height=height,
        x=0,
        y=0,
    )


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


def test_job_store_loads_job_created_by_another_process(tmp_path):
    reader_store = JobStore(str(tmp_path))
    writer_store = JobStore(str(tmp_path))
    record = writer_store.create_job()

    loaded = reader_store.get(record.job_id)

    assert loaded is not None
    assert loaded.job_id == record.job_id
    assert loaded.status == JobStatus.queued


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
async def test_home_ai_chat_returns_quote_draft_without_sending(monkeypatch):
    monkeypatch.setattr("app.home_ai.settings.openai_api_key", "")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        headers = auth_headers()
        response = await client.post(
            "/api/v1/ai/home-chat",
            headers=headers,
            json={
                "message": "What would it cost to paint this room and can you help me get quotes?",
                "homeContext": {
                    "roomCount": 1,
                    "rooms": [
                        {
                            "name": "Living Room",
                            "floorAreaSquareMeters": 18.5,
                            "wallCount": 4,
                            "openingCount": 1,
                            "windowCount": 2,
                        }
                    ],
                    "totals": {"floorAreaSquareMeters": 18.5},
                    "selectedKeyframes": [
                        {
                            "id": "00000000-0000-0000-0000-000000000111",
                            "imageResolution": [100, 100],
                            "jpegBase64": encoded_test_jpeg(),
                        }
                    ],
                },
            },
        )

    assert response.status_code == 200
    body = response.json()
    assert body["usedFallback"] is True
    assert body["state"]["requiresExplicitApproval"] is True
    assert body["state"]["stage"] == "quote_ready"
    assert body["state"]["conversionReadiness"] == "high"
    assert body["state"]["ctaAllowed"] is True
    assert body["state"]["suggestedServiceType"] == "Painting"
    assert body["quoteDraft"]["serviceType"] == "Painting"
    assert body["cta"]["type"] == "quote_request"
    assert body["cta"]["serviceType"] == "Painting"
    assert body["cta"]["label"] == "Request a painting quote for this space"
    assert body["quoteDraft"]["estimatedRangeLow"] > 0
    assert body["visualFocus"] is None
    assert "nothing goes to a provider" in body["message"]["content"]


@pytest.mark.asyncio
async def test_home_ai_chat_can_explore_without_quote_draft(monkeypatch):
    monkeypatch.setattr("app.home_ai.settings.openai_api_key", "")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        headers = auth_headers()
        response = await client.post(
            "/api/v1/ai/home-chat",
            headers=headers,
            json={
                "message": "What painting ideas would look good in this room?",
                "homeContext": {
                    "roomCount": 1,
                    "rooms": [{"name": "Bedroom", "floorAreaSquareMeters": 11.0}],
                    "totals": {"floorAreaSquareMeters": 11.0},
                },
            },
        )

    assert response.status_code == 200
    body = response.json()
    assert body["state"]["intent"] == "design_advice"
    assert body["state"]["requiresExplicitApproval"] is False
    assert body["state"]["ctaAllowed"] is False
    assert body["quoteDraft"] is None
    assert body["cta"] is None
    assert "RoomPlan" not in body["message"]["content"]
    assert "keyframe" not in body["message"]["content"].lower()


@pytest.mark.asyncio
async def test_home_ai_chat_does_not_show_cta_on_first_generic_exploration(monkeypatch):
    monkeypatch.setattr("app.home_ai.settings.openai_api_key", "")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/v1/ai/home-chat",
            headers=auth_headers(),
            json={
                "message": "I am just looking for ideas for this space.",
                "homeContext": {
                    "roomCount": 1,
                    "rooms": [{"name": "Room 1", "floorAreaSquareMeters": 14.0}],
                },
            },
        )

    assert response.status_code == 200
    body = response.json()
    assert body["state"]["ctaAllowed"] is False
    assert body["quoteDraft"] is None
    assert body["cta"] is None


@pytest.mark.asyncio
async def test_home_ai_chat_handles_missing_context_gracefully(monkeypatch):
    monkeypatch.setattr("app.home_ai.settings.openai_api_key", "")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/v1/ai/home-chat",
            headers=auth_headers(),
            json={"message": "Hi, can you help me think about my home?"},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["state"]["confidence"] == "low"
    assert body["contextQuality"]["hasMeasurements"] is False
    assert body["contextQuality"]["recommendedDataImprovements"]
    assert body["quoteDraft"] is None


def test_home_ai_structured_state_rejects_invalid_stage():
    with pytest.raises(ValidationError):
        HomeAIConversationState.model_validate(
            {
                "intent": "exploring",
                "stage": "ready_but_not_real",
                "conversionReadiness": "low",
            }
        )


def test_home_guide_prompt_variant_assignment_is_stable():
    first = assign_home_guide_prompt_variant("user-123")
    second = assign_home_guide_prompt_variant("user-123")
    other = assign_home_guide_prompt_variant("user-456")

    assert first == second
    assert first in {"control", "more_direct", "more_design_led"}
    assert other in {"control", "more_direct", "more_design_led"}


@pytest.mark.asyncio
async def test_home_ai_analytics_events_created_for_cta_and_quote_sent(monkeypatch, tmp_path):
    monkeypatch.setattr("app.home_ai.settings.openai_api_key", "")
    monkeypatch.setattr("app.home_ai.settings.storage_dir", str(tmp_path))
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/v1/ai/home-chat",
            headers=auth_headers(),
            json={
                "message": "What would it cost to paint this room?",
                "homeContext": {
                    "roomCount": 1,
                    "rooms": [{"name": "Living Room", "floorAreaSquareMeters": 18.5}],
                    "totals": {"floorAreaSquareMeters": 18.5},
                },
            },
        )
        body = response.json()
        event_response = await client.post(
            "/api/v1/ai/home-events",
            headers=auth_headers(),
            json={
                "threadId": body["threadId"],
                "eventType": "quote_request_sent",
                "payload": {"serviceType": "Painting", "providerId": "provider-1"},
            },
        )

    assert response.status_code == 200
    assert event_response.status_code == 200
    events_path = tmp_path / "ai_threads" / body["threadId"] / "events.jsonl"
    events = [json.loads(line) for line in events_path.read_text(encoding="utf-8").splitlines()]
    event_types = [event["eventType"] for event in events]
    assert "cta_quote_shown" in event_types
    assert "quote_request_sent" in event_types


def test_home_context_builder_keeps_payload_compact_and_redacts_images():
    context = {
        "roomCount": 20,
        "rooms": [
            {
                "id": f"room-{index}",
                "name": f"Room {index}",
                "floorAreaSquareMeters": 10 + index,
                "objects": [
                    {"category": f"Object {object_index}", "widthMeters": 1.1}
                    for object_index in range(30)
                ],
            }
            for index in range(20)
        ],
        "selectedKeyframes": [
            {
                "id": "frame-1",
                "imageResolution": [100, 100],
                "jpegBase64": "x" * 50000,
            }
        ],
    }

    model_context = build_home_guide_model_context(context, included_image_ids={"frame-1"})
    payload = model_context.model_dump(mode="json", exclude_none=True)
    serialized = json.dumps(payload)

    assert len(payload["rooms"]) == 6
    assert "x" * 100 not in serialized
    assert context_payload_size_chars(model_context) < 12000


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
        assert status["artifacts"]["previewMeshUrl"] == f"/api/v1/jobs/{job_id}/result/fused_mesh.obj"
        assert status["artifacts"]["vertexColoredPlyUrl"] is None
        assert status["artifacts"]["texturedObjUrl"] is None
        assert status["artifacts"]["textureDebugJsonUrl"] is None

        result_resp = await client.get(f"/api/v1/jobs/{job_id}/result", headers=headers)
        assert result_resp.status_code == 200
        manifest_resp = await client.get(f"/api/v1/jobs/{job_id}/result/manifest.json", headers=headers)
        assert manifest_resp.status_code == 200
        manifest = manifest_resp.json()
        assert manifest["artifacts"]["rawFusedMesh"]["path"] == "fused_mesh.obj"
        assert manifest["artifacts"]["vertexColoredPlyDebugPreview"]["path"] == "colored_mesh.ply"
        assert manifest["artifacts"]["vertexColoredPlyDebugPreview"]["available"] is False
        assert manifest["artifacts"]["texturedObj"]["objPath"] == "textured_mesh.obj"
        assert manifest["artifacts"]["texturedObj"]["stats"]["available"] is False
        assert manifest["artifacts"]["textureDebug"]["path"] == "texture_debug.json"
        assert manifest["artifacts"]["textureDebug"]["available"] is False
        assert manifest["coordinateTransforms"]["convention"] == "column_major_4x4"
        assert manifest["coordinateTransforms"]["modelFromARKitWorld"] == [
            1.0, 0.0, 0.0, 0.0,
            0.0, 1.0, 0.0, 0.0,
            0.0, 0.0, 1.0, 0.0,
            0.0, 0.0, 0.0, 1.0,
        ]


@pytest.mark.asyncio
async def test_raw_mesh_artifacts_are_exported_when_texturing_is_disabled():
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
        assert status["artifacts"]["previewMeshUrl"].endswith("/fused_mesh.obj")
        assert status["artifacts"]["texturedObjUrl"] is None
        assert status["artifacts"]["texturePngUrl"] is None
        assert status["artifacts"]["usdzUrl"] is None

        obj_resp = await client.get(f"/api/v1/jobs/{job_id}/result/fused_mesh.obj", headers=headers)
        assert obj_resp.status_code == 200
        assert "v " in obj_resp.text
        assert "f " in obj_resp.text

        manifest = (await client.get(f"/api/v1/jobs/{job_id}/result/manifest.json", headers=headers)).json()
        assert manifest["preferredPhotorealArtifact"] == "vertex_colored_ply"
        assert manifest["artifacts"]["texturedObj"]["stats"]["available"] is False
        assert manifest["artifacts"]["textureDebug"]["available"] is False
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
    assert render_stats["smoothing"]["enabled"] is True
    assert render_stats["smoothing"]["scope"] == "photoreal render mesh only"


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


@pytest.mark.asyncio
async def test_textured_obj_uses_planar_chart_for_large_wall(monkeypatch, tmp_path):
    monkeypatch.setattr(pipeline, "TEXTURE_PLANAR_CHART_MIN_FACE_COUNT", 20)
    monkeypatch.setattr(pipeline, "TEXTURE_PLANAR_CHART_MIN_AREA_M2", 0.01)
    monkeypatch.setattr(pipeline, "TEXTURE_PLANAR_CHART_PIXELS_PER_METER", 72)
    monkeypatch.setattr(pipeline, "TEXTURE_PLANAR_CHART_MIN_SIZE", 64)
    monkeypatch.setattr(pipeline, "TEXTURE_PLANAR_CHART_MAX_SIZE", 192)

    transform = [
        1, 0, 0, 0,
        0, 1, 0, 0,
        0, 0, 1, 0,
        0, 0, 0, 1,
    ]
    intrinsics = [
        120, 0, 0,
        0, 120, 0,
        64, 64, 1,
    ]
    image = Image.new("RGB", (128, 128), (180, 140, 95))
    keyframes = [
        pipeline.ProjectionKeyframe(
            image=image,
            width=image.width,
            height=image.height,
            world_to_camera=pipeline.invert_rigid_transform(transform),
            camera_position=(0, 0, 0),
            intrinsics=intrinsics,
            pixels=image.load(),
            id="wall",
        )
    ]
    profile = pipeline.replace(
        pipeline.PROCESSING_PROFILES["fast_onboarding"],
        planar_chart_raster_stride=2,
    )

    stats = await pipeline.write_textured_obj(
        mesh=make_centered_wall_grid(size=14, spacing=0.035),
        keyframes=keyframes,
        output_obj_path=tmp_path / "textured.obj",
        output_mtl_path=tmp_path / "textured.mtl",
        output_texture_path=tmp_path / "texture.png",
        output_debug_path=tmp_path / "debug.json",
        profile=profile,
    )

    atlas = stats["atlasLayout"]
    chart = atlas["charts"][0]

    assert stats["uvStrategy"] == "planar_chart_atlas_with_per_face_fallback"
    assert atlas["enabled"] is True
    assert atlas["chartedFaceCount"] == stats["faceCount"]
    assert chart["sampleStride"] == 2
    assert chart["projectionMode"] == "direct"
    assert chart["rasterizedPixelCount"] > 0
    assert stats["diagnostics"]["textureAtlas"]["unobservedColor"] == list(pipeline.TEXTURE_UNOBSERVED_COLOR)
    assert stats["diagnostics"]["processing"]["planarChartCount"] == 1
    assert stats["diagnostics"]["processing"]["planarChartRasterStride"] == 2
    assert stats["diagnostics"]["processing"]["planarChartProjectionMode"] == "direct"
    assert stats["projectionCoverage"] == 1.0


def test_planar_texture_charts_split_disconnected_surfaces(monkeypatch):
    monkeypatch.setattr(pipeline, "TEXTURE_PLANAR_CHART_MIN_FACE_COUNT", 20)
    monkeypatch.setattr(pipeline, "TEXTURE_PLANAR_CHART_MIN_AREA_M2", 0.01)
    monkeypatch.setattr(pipeline, "TEXTURE_PLANAR_CHART_PIXELS_PER_METER", 72)
    monkeypatch.setattr(pipeline, "TEXTURE_PLANAR_CHART_MIN_SIZE", 64)
    monkeypatch.setattr(pipeline, "TEXTURE_PLANAR_CHART_MAX_SIZE", 256)

    charts = pipeline.detect_planar_texture_charts(
        make_disconnected_wall_grids(size=7, spacing=0.04, gap=1.0),
        atlas_max_size=1024,
    )

    assert len(charts) == 2
    assert all(len(chart.face_indices) == 98 for chart in charts)


@pytest.mark.asyncio
async def test_fast_planar_chart_uses_single_owner_keyframe(monkeypatch, tmp_path):
    monkeypatch.setattr(pipeline, "TEXTURE_PLANAR_CHART_MIN_FACE_COUNT", 20)
    monkeypatch.setattr(pipeline, "TEXTURE_PLANAR_CHART_MIN_AREA_M2", 0.01)
    monkeypatch.setattr(pipeline, "TEXTURE_PLANAR_CHART_PIXELS_PER_METER", 72)
    monkeypatch.setattr(pipeline, "TEXTURE_PLANAR_CHART_MIN_SIZE", 64)
    monkeypatch.setattr(pipeline, "TEXTURE_PLANAR_CHART_MAX_SIZE", 192)

    transform = [
        1, 0, 0, 0,
        0, 1, 0, 0,
        0, 0, 1, 0,
        0, 0, 0, 1,
    ]
    intrinsics = [
        120, 0, 0,
        0, 120, 0,
        64, 64, 1,
    ]
    red = Image.new("RGB", (128, 128), (210, 60, 60))
    green = Image.new("RGB", (128, 128), (60, 210, 60))
    keyframes = [
        pipeline.ProjectionKeyframe(
            image=red,
            width=red.width,
            height=red.height,
            world_to_camera=pipeline.invert_rigid_transform(transform),
            camera_position=(0, 0, 0),
            intrinsics=intrinsics,
            pixels=red.load(),
            id="red-owner",
        ),
        pipeline.ProjectionKeyframe(
            image=green,
            width=green.width,
            height=green.height,
            world_to_camera=pipeline.invert_rigid_transform(transform),
            camera_position=(0, 0, 0),
            intrinsics=intrinsics,
            pixels=green.load(),
            id="green-secondary",
        ),
    ]

    stats = await pipeline.write_textured_obj(
        mesh=make_centered_wall_grid(size=14, spacing=0.035),
        keyframes=keyframes,
        output_obj_path=tmp_path / "textured.obj",
        output_mtl_path=tmp_path / "textured.mtl",
        output_texture_path=tmp_path / "texture.png",
        output_debug_path=tmp_path / "debug.json",
        profile=pipeline.PROCESSING_PROFILES["fast_onboarding"],
    )

    chart = stats["atlasLayout"]["charts"][0]
    contributions = stats["diagnostics"]["projection"]["keyframeContributionCounts"]

    assert chart["candidateKeyframeCount"] == 2
    assert chart["rasterCandidateKeyframeCount"] == 1
    assert chart["ownerKeyframeId"] == "red-owner"
    assert {item["keyframe"] for item in contributions} == {"red-owner"}


def test_planar_chart_local_fill_repairs_small_holes():
    chart = make_test_planar_chart(width=7, height=5)
    texture = Image.new("RGB", (7, 5), pipeline.FALLBACK_COLOR)
    mask = Image.new("L", (7, 5), 0)
    texture_pixels = texture.load()
    mask_pixels = mask.load()
    trusted_color = (40, 120, 210)
    fallback_color = (90, 95, 100)

    for y in range(chart.height):
        for x in range(chart.width):
            texture_pixels[x, y] = trusted_color
            mask_pixels[x, y] = 255

    texture_pixels[3, 2] = fallback_color
    mask_pixels[3, 2] = 0

    stats = pipeline.fill_planar_chart_holes_from_neighbors(
        texture_pixels,
        mask_pixels,
        chart,
        fallback_color,
        max_radius=2,
    )

    assert stats["localFilledPixelCount"] == 1
    assert stats["unresolvedFallbackPixelCount"] == 0
    assert mask_pixels[3, 2] == 64
    assert texture_pixels[3, 2] == trusted_color


def test_planar_chart_local_fill_does_not_smear_across_large_holes():
    chart = make_test_planar_chart(width=48, height=9)
    texture = Image.new("RGB", (48, 9), pipeline.FALLBACK_COLOR)
    mask = Image.new("L", (48, 9), 0)
    texture_pixels = texture.load()
    mask_pixels = mask.load()
    fallback_color = pipeline.TEXTURE_UNOBSERVED_COLOR

    for y in range(chart.height):
        for x in range(22, 26):
            texture_pixels[x, y] = (180, 40 + y, 35)
            mask_pixels[x, y] = 255

    stats = pipeline.fill_planar_chart_holes_from_neighbors(
        texture_pixels,
        mask_pixels,
        chart,
        fallback_color,
        max_radius=3,
    )

    assert stats["localFilledPixelCount"] > 0
    assert stats["unresolvedFallbackPixelCount"] > 0
    assert mask_pixels[21, 4] == 64
    assert texture_pixels[21, 4] != fallback_color
    assert mask_pixels[0, 4] == 128
    assert texture_pixels[0, 4] == fallback_color
    assert mask_pixels[47, 4] == 128
    assert texture_pixels[47, 4] == fallback_color


def test_direct_planar_chart_fills_far_owner_projection_holes_with_chart_color():
    transform = [
        1, 0, 0, 0,
        0, 1, 0, 0,
        0, 0, 1, 0,
        0, 0, 0, 1,
    ]
    intrinsics = [
        12, 0, 0,
        0, 12, 0,
        12, 12, 1,
    ]
    image = Image.new("RGB", (24, 24), (190, 130, 80))
    keyframe = pipeline.ProjectionKeyframe(
        image=image,
        width=image.width,
        height=image.height,
        world_to_camera=pipeline.invert_rigid_transform(transform),
        camera_position=(0, 0, 0),
        intrinsics=intrinsics,
        pixels=image.load(),
        id="owner",
    )
    chart = pipeline.PlanarTextureChart(
        chart_id=0,
        face_indices=[0],
        normal=(0, 0, 1),
        plane_offset=1,
        axis_u=(1, 0, 0),
        axis_v=(0, 1, 0),
        min_u=-2.0,
        max_u=2.0,
        min_v=-0.5,
        max_v=0.5,
        width=32,
        height=12,
        x=0,
        y=0,
    )
    candidates = pipeline.texture_projection_candidates_for_region(
        pipeline.chart_region_points(chart),
        chart.normal,
        [keyframe],
    )
    texture = Image.new("RGB", (32, 12), pipeline.FALLBACK_COLOR)
    mask = Image.new("L", (32, 12), 0)

    stats = pipeline.rasterize_planar_chart_texture(
        texture.load(),
        mask.load(),
        chart,
        candidates,
        lambda: pipeline.average_direct_projected_surface_color(
            pipeline.chart_region_points(chart),
            chart.normal,
            [keyframe],
        ) or pipeline.TEXTURE_UNOBSERVED_COLOR,
        sample_stride=1,
        projection_mode="direct",
    )

    assert stats["projectedPixelCount"] > 0
    assert stats["localFilledPixelCount"] > 0
    assert stats["neighborFilledPixelCount"] == stats["localFilledPixelCount"]
    assert stats["unresolvedFallbackPixelCount"] > 0
    assert stats["fallbackPixelCount"] == stats["unresolvedFallbackPixelCount"]
    assert stats["maxFillRadius"] == pipeline.TEXTURE_PLANAR_CHART_LOCAL_FILL_MAX_RADIUS_PIXELS
    assert texture.load()[0, 6] == (190, 130, 80)


def test_direct_planar_chart_secondary_keyframe_fills_large_owned_hole():
    transform = [
        1, 0, 0, 0,
        0, 1, 0, 0,
        0, 0, 1, 0,
        0, 0, 0, 1,
    ]
    owner_intrinsics = [
        12, 0, 0,
        0, 12, 0,
        12, 12, 1,
    ]
    secondary_intrinsics = [
        12, 0, 0,
        0, 12, 0,
        24, 12, 1,
    ]
    owner_image = Image.new("RGB", (24, 24), (190, 130, 80))
    secondary_image = Image.new("RGB", (24, 24), (40, 180, 90))
    owner = pipeline.ProjectionKeyframe(
        image=owner_image,
        width=owner_image.width,
        height=owner_image.height,
        world_to_camera=pipeline.invert_rigid_transform(transform),
        camera_position=(0, 0, 0),
        intrinsics=owner_intrinsics,
        pixels=owner_image.load(),
        id="owner",
    )
    secondary = pipeline.ProjectionKeyframe(
        image=secondary_image,
        width=secondary_image.width,
        height=secondary_image.height,
        world_to_camera=pipeline.invert_rigid_transform(transform),
        camera_position=(0, 0, 0),
        intrinsics=secondary_intrinsics,
        pixels=secondary_image.load(),
        id="left-secondary",
    )
    chart = pipeline.PlanarTextureChart(
        chart_id=0,
        face_indices=[0],
        normal=(0, 0, 1),
        plane_offset=1,
        axis_u=(1, 0, 0),
        axis_v=(0, 1, 0),
        min_u=-2.5,
        max_u=2.5,
        min_v=-0.5,
        max_v=0.5,
        width=96,
        height=16,
        x=0,
        y=0,
    )
    owner_candidate = pipeline.TextureProjectionCandidate(
        keyframe=owner,
        keyframe_debug_id=owner.debug_id,
        score=1,
        visible_vertex_count=3,
        center_projection=(12, 12, 1),
        facing=1,
        center_edge_margin=12,
    )
    secondary_candidate = pipeline.TextureProjectionCandidate(
        keyframe=secondary,
        keyframe_debug_id=secondary.debug_id,
        score=1,
        visible_vertex_count=3,
        center_projection=(12, 12, 1),
        facing=1,
        center_edge_margin=12,
    )
    texture = Image.new("RGB", (96, 16), pipeline.FALLBACK_COLOR)
    mask = Image.new("L", (96, 16), 0)

    stats = pipeline.rasterize_planar_chart_texture(
        texture.load(),
        mask.load(),
        chart,
        [owner_candidate],
        lambda: pipeline.TEXTURE_UNOBSERVED_COLOR,
        secondary_candidates=[secondary_candidate],
        sample_stride=1,
        projection_mode="direct",
    )

    pixels = texture.load()
    assert stats["secondaryFilledPixelCount"] > 0
    assert stats["secondaryAcceptedRegionCount"] == 1
    assert stats["secondaryKeyframeIds"] == ["left-secondary"]
    assert pixels[12, 8] == (40, 180, 90)
    assert pixels[90, 8] == pipeline.TEXTURE_UNOBSERVED_COLOR


@pytest.mark.asyncio
async def test_fast_texture_profile_caps_expensive_fallback_faces(tmp_path):
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
    image = Image.new("RGB", (100, 100), (160, 115, 80))
    keyframes = [
        pipeline.ProjectionKeyframe(
            image=image,
            width=image.width,
            height=image.height,
            world_to_camera=pipeline.invert_rigid_transform(transform),
            camera_position=(0, 0, 0),
            intrinsics=intrinsics,
            pixels=image.load(),
            id="room",
        )
    ]
    mesh = pipeline.FusedMesh(
        vertices=[
            (-0.4, -0.4, -1.0),
            (-0.1, -0.4, -1.0),
            (-0.4, -0.1, -1.0),
            (0.1, -0.4, -1.05),
            (0.4, -0.4, -1.05),
            (0.1, -0.1, -1.0),
            (-0.4, 0.1, -0.9),
            (-0.1, 0.1, -1.05),
            (-0.4, 0.4, -1.0),
            (0.1, 0.1, -0.95),
            (0.4, 0.1, -1.0),
            (0.1, 0.4, -1.05),
        ],
        faces=[
            (0, 1, 2),
            (3, 4, 5),
            (6, 7, 8),
            (9, 10, 11),
        ],
        stats={"geometrySource": "test_fallback_budget"},
    )
    profile = pipeline.replace(
        pipeline.PROCESSING_PROFILES["fast_onboarding"],
        fallback_texture_face_limit=1,
    )

    stats = await pipeline.write_textured_obj(
        mesh=mesh,
        keyframes=keyframes,
        output_obj_path=tmp_path / "textured.obj",
        output_mtl_path=tmp_path / "textured.mtl",
        output_texture_path=tmp_path / "texture.png",
        output_debug_path=tmp_path / "debug.json",
        profile=profile,
    )

    processing = stats["diagnostics"]["processing"]
    budget = stats["atlasLayout"]["fallbackBudget"]

    assert stats["uvStrategy"] == "render_mesh_per_face_atlas_padded"
    assert processing["fallbackTextureFaceLimit"] == 1
    assert processing["fallbackHighQualityFaceCount"] == 1
    assert processing["solidProjectedFaceCount"] == 3
    assert processing["solidFallbackFaceCount"] == 0
    assert processing["solidSceneColorProjected"] is False
    assert budget["fallbackPrioritization"] == "largest_non_chart_triangles"
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
async def test_rgbd_one_keyframe_diagnostic_exports_alignment_artifacts():
    transform = [
        1, 0, 0, 0,
        0, 1, 0, 0,
        0, 0, 1, 0,
        0, 0, 0, 1,
    ]
    rgb_intrinsics = [
        100, 0, 0,
        0, 100, 0,
        50, 50, 1,
    ]
    depth_intrinsics = [
        4, 0, 0,
        0, 4, 0,
        2, 2, 1,
    ]

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        headers = auth_headers()
        create_resp = await client.post("/api/v1/jobs", headers=headers)
        assert create_resp.status_code == 200
        job_id = create_resp.json()["jobId"]

        payload = {
            "schemaVersion": "v1",
            "createdAt": "2026-05-18T12:00:00Z",
            "processingProfile": "rgbd_one_keyframe_diagnostic",
            "meshAnchors": [],
            "roomJSONBase64": None,
            "images": [
                {
                    "id": "00000000-0000-0000-0000-000000000301",
                    "capturedAt": "2026-05-18T12:00:00Z",
                    "timestamp": 0.0,
                    "cameraTransform": transform,
                    "intrinsics": rgb_intrinsics,
                    "imageResolution": [100, 100],
                    "jpegBase64": encoded_test_jpeg(),
                },
                {
                    "id": "00000000-0000-0000-0000-000000000302",
                    "capturedAt": "2026-05-18T12:00:02Z",
                    "timestamp": 2.0,
                    "cameraTransform": transform,
                    "intrinsics": rgb_intrinsics,
                    "imageResolution": [100, 100],
                    "jpegBase64": encoded_test_jpeg(),
                },
            ],
            "depthFrames": [
                {
                    "id": "00000000-0000-0000-0000-000000000401",
                    "colorKeyframeId": "00000000-0000-0000-0000-000000000301",
                    "capturedAt": "2026-05-18T12:00:00Z",
                    "timestamp": 0.0,
                    "cameraTransform": transform,
                    "intrinsics": depth_intrinsics,
                    "depthResolution": [4, 4],
                    "depthFormat": "float32_little_endian_meters",
                    "depthBase64": encoded_depth_values([0.0] * 16),
                    "confidenceFormat": "uint8_arkit_confidence",
                    "confidenceBase64": base64.b64encode(bytes([0] * 16)).decode("ascii"),
                    "metersPerUnit": 1,
                },
                {
                    "id": "00000000-0000-0000-0000-000000000402",
                    "colorKeyframeId": "00000000-0000-0000-0000-000000000302",
                    "capturedAt": "2026-05-18T12:00:02Z",
                    "timestamp": 2.0,
                    "cameraTransform": transform,
                    "intrinsics": depth_intrinsics,
                    "depthResolution": [4, 4],
                    "depthFormat": "float32_little_endian_meters",
                    "depthBase64": encoded_depth_values([1.0] * 16),
                    "confidenceFormat": "uint8_arkit_confidence",
                    "confidenceBase64": base64.b64encode(bytes([2] * 16)).decode("ascii"),
                    "metersPerUnit": 1,
                },
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
        assert status["artifacts"]["rgbdSingleFrameMeshUrl"].endswith("/rgbd_single_frame_mesh.obj")
        assert status["artifacts"]["rgbdSingleFrameOverlayUrl"].endswith("/rgbd_single_frame_overlay.png")
        assert status["artifacts"]["previewMeshUrl"].endswith("/rgbd_single_frame_mesh.obj")

        diagnostics = (await client.get(
            f"/api/v1/jobs/{job_id}/result/rgbd_single_frame_diagnostics.json",
            headers=headers,
        )).json()
        assert diagnostics["available"] is True
        assert diagnostics["selectedKeyframeId"] == "00000000-0000-0000-0000-000000000302"
        assert diagnostics["selectedDepthFrameId"] == "00000000-0000-0000-0000-000000000402"
        assert diagnostics["depth"]["validDepthRatio"] == 1
        assert diagnostics["confidence"]["histogram"]["2"] == 16
        assert diagnostics["artifacts"]["pointsPly"]["pointCount"] == 16
        assert diagnostics["artifacts"]["meshObj"]["faceCount"] > 0
        assert diagnostics["reprojection"]["inBoundsRatio"] == pytest.approx(1)
        assert diagnostics["reprojection"]["medianExpectedPixelError"] == pytest.approx(0)

        overlay = await client.get(f"/api/v1/jobs/{job_id}/result/rgbd_single_frame_overlay.png", headers=headers)
        assert overlay.status_code == 200
        assert overlay.headers["content-type"] == "image/png"

        points = await client.get(f"/api/v1/jobs/{job_id}/result/rgbd_single_frame_points.ply", headers=headers)
        assert points.status_code == 200
        assert points.text.startswith("ply\n")

        mesh = await client.get(f"/api/v1/jobs/{job_id}/result/rgbd_single_frame_mesh.obj", headers=headers)
        assert mesh.status_code == 200
        assert "o rgbd_single_frame_mesh" in mesh.text

        manifest = (await client.get(f"/api/v1/jobs/{job_id}/result/manifest.json", headers=headers)).json()
        assert manifest["processingProfile"]["name"] == "rgbd_one_keyframe_diagnostic"
        assert manifest["preferredPhotorealArtifact"] == "rgbd_single_frame_mesh"
        assert manifest["artifacts"]["rgbdSingleFrameDiagnostic"]["available"] is True

        rgbd_stats = (await client.get(f"/api/v1/jobs/{job_id}/result/rgbd_fusion_stats.json", headers=headers)).json()
        assert rgbd_stats["used"] is False
        assert rgbd_stats["geometrySource"] == "single_frame_rgbd_diagnostic"


@pytest.mark.asyncio
async def test_fast_onboarding_profile_preserves_dense_geometry_with_fewer_keyframes():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        headers = auth_headers()
        create_resp = await client.post("/api/v1/jobs", headers=headers)
        assert create_resp.status_code == 200
        job_id = create_resp.json()["jobId"]

        payload = {
            "schemaVersion": "v1",
            "createdAt": "2026-05-18T12:00:00Z",
            "processingProfile": "fast_onboarding",
            "meshAnchors": [
                {
                    "id": "00000000-0000-0000-0000-000000000021",
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
                    "id": f"00000000-0000-0000-0000-0000000001{index:02d}",
                    "capturedAt": "2026-05-18T12:00:01Z",
                    "timestamp": float(index),
                    "cameraTransform": [
                        1, 0, 0, 0,
                        0, 1, 0, 0,
                        0, 0, 1, 0,
                        index * 0.03, 0, 0, 1,
                    ],
                    "intrinsics": [
                        100, 0, 0,
                        0, 100, 0,
                        50, 50, 1,
                    ],
                    "imageResolution": [100, 100],
                    "jpegBase64": encoded_test_jpeg(),
                }
                for index in range(55)
            ],
            "depthFrames": [
                {
                    "id": f"00000000-0000-0000-0000-0000000002{index:02d}",
                    "colorKeyframeId": f"00000000-0000-0000-0000-0000000001{index:02d}",
                    "capturedAt": "2026-05-18T12:00:01Z",
                    "timestamp": float(index),
                    "cameraTransform": [
                        1, 0, 0, 0,
                        0, 1, 0, 0,
                        0, 0, 1, 0,
                        index * 0.03, 0, 0, 1,
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
                    "confidenceBase64": base64.b64encode(bytes([2, 2, 2, 2])).decode("ascii"),
                    "metersPerUnit": 1,
                }
                for index in range(55)
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
        assert status["artifacts"]["texturedObjUrl"] is None
        assert status["artifacts"]["vertexColoredPlyUrl"] is None
        assert status["artifacts"]["previewMeshUrl"].endswith("/rgbd_fused_mesh.obj")
        assert status["artifacts"]["textureDebugPreviewUrl"] is None
        assert status["artifacts"]["stageTimingsUrl"].endswith("/stage_timings.json")

        rgbd_stats = (await client.get(f"/api/v1/jobs/{job_id}/result/rgbd_fusion_stats.json", headers=headers)).json()
        assert rgbd_stats["used"] is True
        assert rgbd_stats["geometrySource"] in {"rgbd_tsdf_open3d", "rgbd_keyframe_depth_mesh"}
        assert rgbd_stats["profile"]["name"] == "fast_onboarding"
        assert rgbd_stats["profile"]["useRgbdGeometry"] is True
        assert rgbd_stats["profile"]["textureRenderTargetFaces"] == pipeline.FAST_ONBOARDING_TEXTURE_RENDER_TARGET_FACE_COUNT
        assert rgbd_stats["profile"]["textureTsdfRenderTargetFaces"] == pipeline.FAST_ONBOARDING_TEXTURE_TSDF_RENDER_TARGET_FACE_COUNT
        assert rgbd_stats["sampledDepthFrameCount"] == 36

        keyframe_selection = (await client.get(f"/api/v1/jobs/{job_id}/result/keyframe_selection.json", headers=headers)).json()
        assert keyframe_selection["originalKeyframeCount"] == 55
        assert keyframe_selection["selectedKeyframeCount"] == 18

        depth_selection = (await client.get(f"/api/v1/jobs/{job_id}/result/depth_frame_selection.json", headers=headers)).json()
        assert depth_selection["originalDepthFrameCount"] == 55
        assert depth_selection["geometryDepthSelection"] == "all_depth_frames"
        assert depth_selection["selectedDepthFrameCount"] == 48

        manifest = (await client.get(f"/api/v1/jobs/{job_id}/result/manifest.json", headers=headers)).json()
        assert manifest["processingProfile"]["name"] == "fast_onboarding"
        assert manifest["artifacts"]["rgbdFusedMesh"]["stats"]["used"] is True
        assert manifest["artifacts"]["vertexColoredPlyDebugPreview"]["available"] is False
        assert manifest["artifacts"]["texturedObj"]["stats"]["available"] is False
        assert manifest["artifacts"]["textureDebug"]["previewAvailable"] is False

        timings = (await client.get(f"/api/v1/jobs/{job_id}/result/stage_timings.json", headers=headers)).json()
        assert timings["jobId"] == job_id
        assert not any(item["stageClass"] == "TexturedMeshStage" for item in timings["timings"])


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
