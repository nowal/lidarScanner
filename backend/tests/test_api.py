import asyncio
import base64
import json
import struct
import zipfile
from array import array
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


def read_glb_json(path) -> dict:
    data = path.read_bytes()
    magic, version, _ = struct.unpack_from("<III", data, 0)
    assert magic == 0x46546C67
    assert version == 2
    json_length, json_type = struct.unpack_from("<II", data, 12)
    assert json_type == 0x4E4F534A
    return json.loads(data[20:20 + json_length].decode("utf-8"))


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


def make_test_projection_keyframe(index: int, timestamp: float | None = None) -> pipeline.ProjectionKeyframe:
    image = Image.new("RGB", (8, 8), (80 + index, 90, 100))
    return pipeline.ProjectionKeyframe(
        image=image,
        width=image.width,
        height=image.height,
        world_to_camera=[
            1, 0, 0, 0,
            0, 1, 0, 0,
            0, 0, 1, 0,
            0, 0, 0, 1,
        ],
        camera_position=(0, 0, 0),
        intrinsics=[
            8, 0, 0,
            0, 8, 0,
            4, 4, 1,
        ],
        pixels=image.load(),
        id=f"kf-{index}",
        timestamp=float(index) if timestamp is None else timestamp,
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


def test_diagnostic_glb_exports_triangle_opaque_meshes(tmp_path):
    mesh = make_centered_wall_grid(size=1)

    geometry_stats = pipeline.write_mesh_glb(
        mesh,
        tmp_path / "geometry_only.glb",
        name="geometry_only",
        material_color=(0.7, 0.7, 0.7, 1.0),
        double_sided=True,
    )
    uv_stats = pipeline.write_uv_checker_glb(mesh, tmp_path / "uv_checker.glb")
    coverage_stats = pipeline.write_coverage_debug_glb(
        mesh,
        tmp_path / "coverage_debug.glb",
        tmp_path / "coverage_debug_report.json",
        ["projected"] * len(mesh.faces),
    )

    for filename, stats in [
        ("geometry_only.glb", geometry_stats),
        ("uv_checker.glb", uv_stats),
        ("coverage_debug.glb", coverage_stats),
    ]:
        assert stats["available"] is True
        gltf = read_glb_json(tmp_path / filename)
        primitive = gltf["meshes"][0]["primitives"][0]
        assert primitive["mode"] == 4
        assert gltf["materials"][0]["alphaMode"] == "OPAQUE"
        assert gltf["accessors"][primitive["indices"]]["count"] == len(mesh.faces) * 3

    uv_gltf = read_glb_json(tmp_path / "uv_checker.glb")
    uv_primitive = uv_gltf["meshes"][0]["primitives"][0]
    assert "TEXCOORD_0" in uv_primitive["attributes"]
    assert uv_gltf["images"][0]["mimeType"] == "image/png"
    assert uv_stats["validation"]["uvOutOfRangeCount"] == 0

    coverage_gltf = read_glb_json(tmp_path / "coverage_debug.glb")
    coverage_primitive = coverage_gltf["meshes"][0]["primitives"][0]
    assert "COLOR_0" in coverage_primitive["attributes"]
    coverage_report = json.loads((tmp_path / "coverage_debug_report.json").read_text(encoding="utf-8"))
    assert coverage_report["categories"]["projected"]["faceCount"] == len(mesh.faces)


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
            "processingProfile": "full_quality",
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
        assert status["artifacts"]["previewMeshUrl"].endswith("/colored_mesh.ply")
        assert status["artifacts"]["texturedObjUrl"].endswith("/textured_mesh.obj")
        assert status["artifacts"]["glbUrl"].endswith("/textured_mesh.glb")
        assert status["artifacts"]["geometryOnlyGlbUrl"].endswith("/geometry_only.glb")
        assert status["artifacts"]["geometryCulledGlbUrl"].endswith("/geometry_culled.glb")
        assert status["artifacts"]["uvCheckerGlbUrl"].endswith("/uv_checker.glb")
        assert status["artifacts"]["coverageDebugGlbUrl"].endswith("/coverage_debug.glb")
        assert status["artifacts"]["texturePngUrl"].endswith("/textured_mesh_texture.png")
        assert status["artifacts"]["usdzUrl"].endswith("/textured_mesh.usdz")

        obj_resp = await client.get(f"/api/v1/jobs/{job_id}/result/fused_mesh.obj", headers=headers)
        assert obj_resp.status_code == 200
        assert "v " in obj_resp.text
        assert "f " in obj_resp.text

        manifest = (await client.get(f"/api/v1/jobs/{job_id}/result/manifest.json", headers=headers)).json()
        assert manifest["preferredPhotorealArtifact"] == "usdz"
        assert manifest["artifacts"]["texturedObj"]["stats"]["projectionCoverage"] > 0
        assert manifest["artifacts"]["usdz"]["available"] is True
        assert manifest["artifacts"]["usdz"]["path"] == "textured_mesh.usdz"
        assert manifest["artifacts"]["glb"]["available"] is True
        assert manifest["artifacts"]["glb"]["path"] == "textured_mesh.glb"
        assert manifest["artifacts"]["geometryOnlyGlb"]["available"] is True
        assert manifest["artifacts"]["geometryOnlyGlb"]["stats"]["primitiveMode"] == 4
        assert manifest["artifacts"]["geometryCulledGlb"]["stats"]["doubleSided"] is False
        assert manifest["artifacts"]["uvCheckerGlb"]["available"] is True
        assert manifest["artifacts"]["coverageDebugGlb"]["available"] is True
        assert manifest["artifacts"]["meshIntegrityReport"]["available"] is True
        assert manifest["artifacts"]["textureDebug"]["available"] is True
        assert manifest["artifacts"]["rawFusedMesh"]["stats"]["invalidFaceCount"] == 1

        usdz_resp = await client.get(f"/api/v1/jobs/{job_id}/result/textured_mesh.usdz", headers=headers)
        assert usdz_resp.status_code == 200
        assert usdz_resp.headers["content-type"] == "model/vnd.usdz+zip"
        with zipfile.ZipFile(BytesIO(usdz_resp.content)) as archive:
            assert archive.namelist() == ["textured_mesh.usda", "textured_mesh_texture.png"]
            assert all(info.compress_type == zipfile.ZIP_STORED for info in archive.infolist())
            assert b"UsdPreviewSurface" in archive.read("textured_mesh.usda")


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


def test_fast_texture_render_mesh_densifies_sparse_lidar_surface():
    mesh = pipeline.FusedMesh(
        vertices=[
            (-0.2, -0.2, -1.0),
            (0.2, -0.2, -1.0),
            (-0.2, 0.2, -1.0),
        ],
        faces=[(0, 1, 2)],
        stats={"geometrySource": "arkit_mesh_anchor_fusion", "geometryPreserved": True},
    )

    render_mesh = pipeline.make_texture_render_mesh(
        mesh,
        profile=pipeline.PROCESSING_PROFILES["fast_onboarding"],
    )
    render_stats = render_mesh.stats["textureRenderMesh"]

    assert len(render_mesh.faces) > len(mesh.faces)
    assert len(render_mesh.vertices) > len(mesh.vertices)
    assert render_stats["used"] is True
    assert render_stats["algorithm"] == "lidar_surface_edge_subdivision"
    assert render_stats["surfaceConstrained"] is True
    assert render_stats["geometrySource"] == "arkit_mesh_anchor_fusion"
    assert render_stats["geometryPreserved"] is False
    assert render_stats["rawGeometryPreserved"] is True
    assert render_stats["splitFaceCount"] > 0
    assert render_stats["sourceEdgeLengthMeters"]["max"] > render_stats["renderEdgeLengthMeters"]["max"]
    assert render_stats["renderFaceCount"] == len(render_mesh.faces)
    assert render_stats["renderFaceCount"] <= pipeline.FAST_ONBOARDING_TEXTURE_RENDER_TARGET_FACE_COUNT


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
        profile=pipeline.replace(
            pipeline.PROCESSING_PROFILES["full_quality"],
            planar_chart_projection_mode="blend",
        ),
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
    assert stats["diagnostics"]["processing"]["activeProjectionMode"] == "direct"
    assert stats["diagnostics"]["processing"]["denseSingleViewTexture"] is False
    assert stats["diagnostics"]["summary"]["selectedTextureKeyframes"] == ["wall"]
    assert stats["diagnostics"]["summary"]["perChartOwnerFrames"][0]["ownerKeyframeId"] == "wall"
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
    assert chart["projectionMode"] == "direct"
    assert [item["id"] for item in stats["diagnostics"]["keyframes"]] == ["red-owner", "green-secondary"]
    assert stats["diagnostics"]["processing"]["sourceKeyframeCount"] == 2
    assert stats["diagnostics"]["processing"]["activeTextureKeyframeCount"] == 2
    assert stats["diagnostics"]["processing"]["denseSingleViewTexture"] is False
    assert stats["diagnostics"]["summary"]["perChartOwnerFrames"][0]["ownerKeyframeId"] == "red-owner"
    assert {item["keyframe"] for item in contributions} == {"red-owner"}


@pytest.mark.asyncio
async def test_blend_fallback_budget_uses_projected_solid_color_for_remaining_faces(tmp_path):
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
    red = Image.new("RGB", (128, 128), (210, 70, 70))
    green = Image.new("RGB", (128, 128), (70, 210, 70))
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
    mesh = make_centered_wall_grid(size=3, spacing=0.08)
    profile = pipeline.replace(
        pipeline.PROCESSING_PROFILES["fast_onboarding"],
        planar_chart_projection_mode="blend",
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

    fallback_budget = stats["atlasLayout"]["fallbackBudget"]
    processing = stats["diagnostics"]["processing"]
    projection = stats["diagnostics"]["projection"]

    assert fallback_budget["fallbackHighQualityFaceCount"] == 1
    assert fallback_budget["fallbackSolidFaceCount"] == len(mesh.faces) - 1
    assert processing["solidProjectedFaceCount"] == len(mesh.faces) - 1
    assert processing["solidFallbackFaceCount"] == 0
    assert stats["projectionCoverage"] == 1.0
    assert projection["blendedPixelCount"] > 0
    assert {item["keyframe"] for item in projection["keyframeContributionCounts"]} == {"red", "green"}


@pytest.mark.asyncio
async def test_fast_direct_projection_rejects_occluded_samples_and_stays_neutral(tmp_path):
    transform = [
        1, 0, 0, 0,
        0, 1, 0, 0,
        0, 0, 1, 0,
        0, 0, 0, 1,
    ]
    image_intrinsics = [
        100, 0, 0,
        0, 100, 0,
        50, 50, 1,
    ]
    depth_intrinsics = [
        4, 0, 0,
        0, 4, 0,
        2, 2, 1,
    ]
    image = Image.new("RGB", (100, 100), (250, 250, 250))
    depth_frame = pipeline.ProjectionDepthFrame(
        id="white-depth",
        color_keyframe_id="white-keyframe",
        width=4,
        height=4,
        world_to_camera=pipeline.invert_rigid_transform(transform),
        intrinsics=depth_intrinsics,
        depth_values=array("f", [0.1] * 16),
        confidence_values=bytes([2] * 16),
    )
    keyframes = [
        pipeline.ProjectionKeyframe(
            image=image,
            width=image.width,
            height=image.height,
            world_to_camera=pipeline.invert_rigid_transform(transform),
            camera_position=(0, 0, 0),
            intrinsics=image_intrinsics,
            pixels=image.load(),
            id="white-keyframe",
            depth_frame=depth_frame,
        )
    ]
    mesh = pipeline.FusedMesh(
        vertices=[
            (-0.2, -0.2, -1.0),
            (0.2, -0.2, -1.0),
            (-0.2, 0.2, -1.0),
        ],
        faces=[(0, 1, 2)],
        stats={"geometrySource": "white_occluded_test"},
    )

    stats = await pipeline.write_textured_obj(
        mesh=mesh,
        keyframes=keyframes,
        output_obj_path=tmp_path / "textured.obj",
        output_mtl_path=tmp_path / "textured.mtl",
        output_texture_path=tmp_path / "texture.png",
        output_debug_path=tmp_path / "debug.json",
        profile=pipeline.PROCESSING_PROFILES["fast_onboarding"],
    )

    projection = stats["diagnostics"]["projection"]
    texture = Image.open(tmp_path / "texture.png")
    pixels = texture.load()

    assert stats["projectionCoverage"] == 0.0
    assert projection["singleSamplePixelCount"] == 0
    assert projection["rejectedOccludedSampleCount"] > 0
    assert projection["depthVisibilityDecisionCount"] >= projection["rejectedOccludedSampleCount"]
    assert stats["diagnostics"]["summary"]["rejectedOccludedSampleCount"] == projection["rejectedOccludedSampleCount"]
    assert stats["diagnostics"]["summary"]["unobservedTexelRatio"] > 0
    assert pixels[stats["tilePadding"] + 1, stats["tilePadding"] + 1] == pipeline.TEXTURE_UNOBSERVED_COLOR


def test_project_vertex_color_ignores_zero_weight_depth_edge_samples():
    transform = [
        1, 0, 0, 0,
        0, 1, 0, 0,
        0, 0, 1, 0,
        0, 0, 0, 1,
    ]
    image_intrinsics = [
        100, 0, 0,
        0, 100, 0,
        50, 50, 1,
    ]
    depth_intrinsics = [
        4, 0, 0,
        0, 4, 0,
        2, 2, 1,
    ]
    image = Image.new("RGB", (100, 100), (180, 120, 80))
    depth_values = array("f", [
        1.0, 1.0, 1.0, 1.0,
        1.0, 2.0, 2.0, 2.0,
        1.0, 2.0, 1.0, 2.0,
        1.0, 2.0, 2.0, 2.0,
    ])
    depth_frame = pipeline.ProjectionDepthFrame(
        id="edge-depth",
        color_keyframe_id="edge-keyframe",
        width=4,
        height=4,
        world_to_camera=pipeline.invert_rigid_transform(transform),
        intrinsics=depth_intrinsics,
        depth_values=depth_values,
        confidence_values=bytes([2] * 16),
    )
    keyframe = pipeline.ProjectionKeyframe(
        image=image,
        width=image.width,
        height=image.height,
        world_to_camera=pipeline.invert_rigid_transform(transform),
        camera_position=(0, 0, 0),
        intrinsics=image_intrinsics,
        pixels=image.load(),
        id="edge-keyframe",
        depth_frame=depth_frame,
    )

    assert pipeline.depth_visibility_for_world_point((0.0, 0.0, -1.0), keyframe).status == "depth_edge"
    assert pipeline.project_vertex_color((0.0, 0.0, -1.0), [keyframe]) is None


def test_rgbd_hero_patch_depth_preparation_accepts_low_confidence_and_fills_holes():
    depth_values = array("f", [
        1.0, 1.0, 1.0,
        1.0, 0.0, 1.0,
        1.0, 1.0, 1.0,
    ])
    confidence_values = bytes([
        2, 0, 2,
        0, 0, 0,
        2, 0, 2,
    ])

    prepared = pipeline.prepare_rgbd_hero_patch_depth_grid(
        depth_values=depth_values,
        confidence_values=confidence_values,
        width=3,
        height=3,
    )
    stats = prepared["stats"]

    assert stats["originalValidDepthCount"] == 8
    assert stats["acceptedLowConfidenceDepthCount"] == 4
    assert stats["filledDepthHoleCount"] == 1
    assert stats["remainingInvalidDepthCount"] == 0
    assert stats["finalValidDepthRatio"] == 1.0
    assert prepared["depthValues"][4] == pytest.approx(1.0)


def make_onboarding_pair_fixture(
    tmp_path,
    *,
    index: int,
    timestamp: float,
    tx: float,
    width: int = 32,
    height: int = 24,
):
    (tmp_path / "keyframes").mkdir(exist_ok=True)
    image_path = tmp_path / "keyframes" / f"keyframe_{index}.jpg"
    Image.new("RGB", (width, height), (120 + index * 20, 105, 90)).save(image_path)
    depth_path = tmp_path / f"depth_{index}.bin"
    depth_path.write_bytes(array("f", [1.0] * (width * height)).tobytes())
    confidence_path = tmp_path / f"confidence_{index}.bin"
    confidence_path.write_bytes(bytes([2] * (width * height)))
    transform = [
        1, 0, 0, 0,
        0, 1, 0, 0,
        0, 0, 1, 0,
        tx, 0, 0, 1,
    ]
    intrinsics = [
        32, 0, 0,
        0, 32, 0,
        width / 2, height / 2, 1,
    ]
    keyframe = {
        "id": f"kf-{index}",
        "path": image_path.name,
        "timestamp": timestamp,
        "cameraTransform": transform,
        "intrinsics": intrinsics,
        "imageResolution": [width, height],
        "trackingState": "normal",
    }
    depth_frame = {
        "id": f"depth-{index}",
        "path": depth_path.name,
        "confidencePath": confidence_path.name,
        "timestamp": timestamp,
        "colorKeyframeId": keyframe["id"],
        "cameraTransform": transform,
        "intrinsics": intrinsics,
        "depthResolution": [width, height],
        "depthFormat": "float32",
        "confidenceFormat": "uint8",
    }
    return keyframe, depth_frame


def test_two_keyframe_rgbd_onboarding_pair_selection_adds_blendable_full_capture_supplements(tmp_path):
    keyframes = []
    depth_frames = []
    for index, timestamp, tx in [
        (0, 0.0, 0.0),
        (1, 2.4, 0.05),
        (2, 4.2, 2.0),
        (3, 7.0, 0.45),
    ]:
        keyframe, depth_frame = make_onboarding_pair_fixture(
            tmp_path,
            index=index,
            timestamp=timestamp,
            tx=tx,
        )
        keyframes.append(keyframe)
        depth_frames.append(depth_frame)

    selected, stats = pipeline.select_multi_keyframe_rgbd_onboarding_pairs(
        pipeline.pair_rgbd_frames(keyframes, depth_frames, tmp_path),
        work_dir=tmp_path,
    )

    selected_keyframe_ids = [pair[0]["id"] for pair in selected]
    assert stats["available"] is True
    assert stats["strategy"] == "best_multi_rgbd_keyframes_full_capture_overlap_tsdf"
    assert stats["selectedFrameCount"] >= 3
    assert "kf-0" in selected_keyframe_ids
    assert "kf-1" in selected_keyframe_ids
    assert "kf-3" in selected_keyframe_ids
    assert "kf-2" not in selected_keyframe_ids
    assert stats["selectedPair"]["blendable"] is True
    assert stats["selectedPair"]["timeDeltaSeconds"] == pytest.approx(2.4)
    assert stats["selectedPair"]["overlap"]["bidirectionalAgreementRatio"] >= 0.5
    assert stats["pairWindowSeconds"] == pipeline.RGBD_ONBOARDING_PAIR_WINDOW_SECONDS
    assert stats["candidatePoolCount"] <= stats["dynamicCandidatePoolLimit"]
    assert stats["selectedCoverageBinCount"] > 0
    assert stats["selectedCoverageRatio"] > 0
    assert any(
        frame["keyframeId"] == "kf-3" and frame["selectionRole"] == "supplemental"
        for frame in stats["selectedFrames"]
    )
    assert any(
        frame["candidate"]["keyframeId"] == "kf-3" and frame["newCoverageBinCount"] > 0
        for frame in stats["supplementalFrames"]
    )


def test_two_keyframe_rgbd_onboarding_pair_selection_requires_blendable_anchor_pair(tmp_path):
    keyframes = []
    depth_frames = []
    for index, timestamp, tx in [
        (0, 0.0, 0.0),
        (1, 6.0, 2.0),
    ]:
        keyframe, depth_frame = make_onboarding_pair_fixture(
            tmp_path,
            index=index,
            timestamp=timestamp,
            tx=tx,
        )
        keyframes.append(keyframe)
        depth_frames.append(depth_frame)

    selected, stats = pipeline.select_multi_keyframe_rgbd_onboarding_pairs(
        pipeline.pair_rgbd_frames(keyframes, depth_frames, tmp_path),
        work_dir=tmp_path,
    )

    assert selected == []
    assert stats["available"] is False
    assert stats["eligibleCandidateCount"] == 2
    assert stats["pairCandidateCount"] == 1
    assert stats["blendablePairCount"] == 0
    assert stats["reason"] == "No candidate pool pair had enough overlapping depth agreement."


def test_rgbd_pair_selection_prefers_first_pair_plus_timed_supplements():
    def make_pair(index: int, timestamp: float) -> dict:
        return {
            "keyframe": {"id": f"kf-{index}"},
            "depthFrame": {"id": f"depth-{index}"},
            "keyframeIndex": index,
            "depthFrameIndex": index,
            "pose": {
                "position": (index * 0.01, 0.0, 0.0),
                "forward": (0.0, 0.0, -1.0),
                "timestamp": timestamp,
            },
        }

    selected, stats = pipeline.select_rgbd_pair_records([
        make_pair(0, 0.0),
        make_pair(1, 0.8),
        make_pair(2, 2.5),
        make_pair(3, 4.5),
        make_pair(4, 6.0),
    ], limit=3)

    assert [pair["keyframe"]["id"] for pair in selected] == ["kf-0", "kf-2", "kf-3"]
    assert stats["pairSelectionStrategy"] == "first_pair_plus_timed_rgbd_hero_patch_supplements"
    assert stats["poseDelta"]["timeDeltaSeconds"] == pytest.approx(2.5)
    assert [item["timeDeltaSeconds"] for item in stats["poseDeltas"]] == pytest.approx([2.5, 4.5])
    assert [item["actualTimeDeltaSeconds"] for item in stats["supplementalSelections"]] == pytest.approx([2.5, 4.5])


def test_rgbd_candidate_pool_keeps_alternates_around_timed_windows():
    def make_pair(index: int, timestamp: float) -> dict:
        return {
            "keyframe": {"id": f"kf-{index}"},
            "depthFrame": {"id": f"depth-{index}"},
            "keyframeIndex": index,
            "depthFrameIndex": index,
            "pose": {
                "position": (index * 0.01, 0.0, 0.0),
                "forward": (0.0, 0.0, -1.0),
                "timestamp": timestamp,
            },
        }

    selected, stats = pipeline.select_rgbd_candidate_pool_pair_records([
        make_pair(0, 0.0),
        make_pair(1, 1.0),
        make_pair(2, 2.2),
        make_pair(3, 2.5),
        make_pair(4, 2.9),
        make_pair(5, 4.2),
        make_pair(6, 4.5),
        make_pair(7, 4.9),
        make_pair(8, 7.0),
    ], limit=8)

    assert [pair["keyframe"]["id"] for pair in selected] == [
        "kf-0",
        "kf-2",
        "kf-3",
        "kf-4",
        "kf-5",
        "kf-6",
        "kf-7",
        "kf-8",
    ]
    assert stats["pairSelectionStrategy"] == "first_pair_plus_timed_rgbd_hero_patch_candidate_pool"
    assert stats["candidatePoolLimit"] == 8
    assert [item["selectedPairCount"] for item in stats["candidatePoolSelections"]] == [3, 3]


def test_rgbd_hero_patch_candidate_selection_prefers_quality_within_timed_windows():
    def make_candidate(index: int, timestamp: float, sharpness: float, score: float | None = None) -> dict:
        return {
            "index": index,
            "keyframeId": f"kf-{index}",
            "depthFrameId": f"depth-{index}",
            "sourceTimestamp": timestamp,
            "timestampDeltaSeconds": 0.0,
            "validDepthRatio": 1.0,
            "highConfidenceRatio": 1.0,
            "rgbSharpnessScore": sharpness,
            "score": score if score is not None else 9.0 + min(sharpness / 28.0, 1.0),
        }

    selected, stats = pipeline.select_rgbd_hero_patch_candidates([
        make_candidate(0, 0.0, 12.0),
        make_candidate(1, 1.0, 12.0, score=50.0),
        make_candidate(2, 2.5, 2.0),
        make_candidate(3, 2.7, 32.0),
        make_candidate(4, 4.5, 2.0),
        make_candidate(5, 4.8, 32.0),
    ])

    assert [candidate["keyframeId"] for candidate in selected] == ["kf-0", "kf-3", "kf-5"]
    assert stats["strategy"] == "quality_aware_primary_plus_timed_supplemental_rgbd_hero_patches"
    assert stats["candidatePoolCount"] == 6
    assert [item["actualTimeDeltaSeconds"] for item in stats["supplementalSelections"]] == pytest.approx([2.7, 4.8])


def test_rgbd_hero_patch_ownership_culls_supplemental_faces_when_primary_owns():
    primary_patch = make_rgbd_hero_patch_test_patch("primary", bytes([2] * 16))
    secondary_patch = make_rgbd_hero_patch_test_patch("secondary", bytes([2] * 16))

    combined = pipeline.combine_rgbd_hero_patch_meshes([primary_patch, secondary_patch])
    cull_stats = combined["mesh"].stats["ownershipCull"]

    assert len(combined["mesh"].faces) == 2
    assert combined["patches"][0]["keptFaceCount"] == 2
    assert combined["patches"][1]["keptFaceCount"] == 0
    assert combined["patches"][1]["culledFaceCount"] == 2
    assert cull_stats["patches"][1]["primaryOwnedDepthAgreementCount"] == 2
    assert cull_stats["totalCulledFaceCount"] == 2


def test_rgbd_hero_patch_ownership_replaces_low_confidence_primary_faces():
    primary_patch = make_rgbd_hero_patch_test_patch("primary", bytes([0] * 16))
    secondary_patch = make_rgbd_hero_patch_test_patch("secondary", bytes([2] * 16))

    combined = pipeline.combine_rgbd_hero_patch_meshes([primary_patch, secondary_patch])
    cull_stats = combined["mesh"].stats["ownershipCull"]

    assert len(combined["mesh"].faces) == 2
    assert combined["patches"][0]["keptFaceCount"] == 0
    assert combined["patches"][0]["culledFaceCount"] == 2
    assert combined["patches"][0]["ownershipCull"]["replacedBySupplementalCount"] == 2
    assert combined["patches"][1]["keptFaceCount"] == 2
    assert cull_stats["patches"][1]["primaryMissingOrLowConfidenceCount"] == 2


def test_rgbd_hero_patch_ownership_replaces_stretched_primary_depth_edges():
    edge_depth_values = array("f", [
        1.0, 1.0, 1.0, 1.0,
        1.0, 1.0, 1.6, 1.6,
        1.0, 1.0, 1.6, 1.6,
        1.0, 1.0, 1.6, 1.6,
    ])
    primary_patch = make_rgbd_hero_patch_test_patch(
        "primary",
        bytes([2] * 16),
        depth_values=edge_depth_values,
    )
    secondary_patch = make_rgbd_hero_patch_test_patch("secondary", bytes([2] * 16))

    combined = pipeline.combine_rgbd_hero_patch_meshes([primary_patch, secondary_patch])
    cull_stats = combined["mesh"].stats["ownershipCull"]

    assert len(combined["mesh"].faces) == 2
    assert combined["patches"][0]["keptFaceCount"] == 0
    assert combined["patches"][0]["ownershipCull"]["replacedBySupplementalCount"] == 2
    assert combined["patches"][1]["keptFaceCount"] == 2
    assert cull_stats["patches"][1]["primaryStretchedDepthEdgeCount"] == 2


def test_single_keyframe_rgbd_onboarding_mesh_downsamples_preview_budget():
    width, height = 160, 120
    transform = [
        1, 0, 0, 0,
        0, 1, 0, 0,
        0, 0, 1, 0,
        0, 0, 0, 1,
    ]
    intrinsics = [
        120, 0, 0,
        0, 120, 0,
        width / 2, height / 2, 1,
    ]
    rgb_image = Image.new("RGB", (width, height), (180, 110, 70))
    projection_keyframe = pipeline.ProjectionKeyframe(
        image=rgb_image,
        width=width,
        height=height,
        world_to_camera=transform,
        camera_position=(0.0, 0.0, 0.0),
        intrinsics=intrinsics,
        pixels=rgb_image.load(),
        id="onboarding-keyframe",
    )

    result = pipeline.build_single_keyframe_rgbd_onboarding_mesh(
        keyframe={
            "id": "onboarding-keyframe",
            "cameraTransform": transform,
            "intrinsics": intrinsics,
            "imageResolution": [width, height],
        },
        depth_frame={
            "id": "onboarding-depth",
            "cameraTransform": transform,
            "intrinsics": intrinsics,
            "depthResolution": [width, height],
        },
        projection_keyframe=projection_keyframe,
        depth_values=array("f", [1.0] * (width * height)),
        width=width,
        height=height,
    )

    full_resolution_face_budget = (width - 1) * (height - 1) * 2
    stats = result["stats"]
    assert stats["targetSamples"] == pipeline.RGBD_ONBOARDING_TARGET_SAMPLES
    assert stats["sampleStep"] == 2
    assert stats["faceCount"] <= 10_000
    assert stats["faceCount"] < full_resolution_face_budget
    assert len(result["mesh"].faces) == stats["acceptedFaceCount"]
    assert result["mesh"].stats["targetSamples"] == pipeline.RGBD_ONBOARDING_TARGET_SAMPLES


def test_onboarding_lidar_support_candidates_are_bounded_and_nearest():
    support_index = {
        "cellSizePixels": 10,
        "maxCandidatesPerLookup": 3,
        "buckets": {
            (0, 0): [
                {"vertexIndex": 0, "u": 8.0, "v": 8.0, "world": (0, 0, 0), "depth": 1.0},
                {"vertexIndex": 1, "u": 5.0, "v": 5.0, "world": (0, 0, 0), "depth": 1.0},
                {"vertexIndex": 2, "u": 9.0, "v": 9.0, "world": (0, 0, 0), "depth": 1.0},
                {"vertexIndex": 3, "u": 2.0, "v": 2.0, "world": (0, 0, 0), "depth": 1.0},
            ],
        },
    }

    candidates = pipeline.nearby_lidar_support_candidates(support_index, 8.5, 8.5)

    assert [candidate["vertexIndex"] for candidate in candidates] == [0, 2, 1]


def test_single_keyframe_rgbd_onboarding_prunes_lidar_inconsistent_region(tmp_path):
    transform = [
        1, 0, 0, 0,
        0, 1, 0, 0,
        0, 0, 1, 0,
        0, 0, 0, 1,
    ]
    intrinsics = [
        4, 0, 0,
        0, 4, 0,
        2, 2, 1,
    ]
    keyframe_dir = tmp_path / "keyframes"
    depth_dir = tmp_path / "depth_frames"
    keyframe_dir.mkdir()
    depth_dir.mkdir()
    Image.new("RGB", (5, 5), (180, 110, 70)).save(keyframe_dir / "keyframe_001.jpg")

    depth_values = []
    for y in range(5):
        for x in range(5):
            depth_values.append(1.6 if x >= 3 and y >= 3 else 1.0)
    (depth_dir / "depth_001.f32").write_bytes(struct.pack(f"<{len(depth_values)}f", *depth_values))
    (depth_dir / "depth_001.confidence.u8").write_bytes(bytes([2] * 25))

    keyframes = [{
        "id": "onboarding-keyframe",
        "timestamp": 0.0,
        "cameraTransform": transform,
        "intrinsics": intrinsics,
        "imageResolution": [5, 5],
        "path": "keyframes/keyframe_001.jpg",
    }]
    depth_frames = [{
        "id": "onboarding-depth",
        "colorKeyframeId": "onboarding-keyframe",
        "timestamp": 0.0,
        "cameraTransform": transform,
        "intrinsics": intrinsics,
        "depthResolution": [5, 5],
        "depthFormat": "float32_little_endian_meters",
        "path": "depth_frames/depth_001.f32",
        "confidenceFormat": "uint8_arkit_confidence",
        "confidencePath": "depth_frames/depth_001.confidence.u8",
        "metersPerUnit": 1,
    }]

    lidar_vertices = [
        pipeline.backproject_depth_sample_to_world(x, y, 1.0, intrinsics, transform)
        for y in range(5)
        for x in range(5)
    ]
    lidar_faces = []
    for y in range(4):
        for x in range(4):
            top_left = y * 5 + x
            top_right = top_left + 1
            bottom_left = top_left + 5
            bottom_right = bottom_left + 1
            lidar_faces.append((top_left, bottom_left, top_right))
            lidar_faces.append((top_right, bottom_left, bottom_right))
    arkit_mesh = pipeline.FusedMesh(
        vertices=lidar_vertices,
        faces=lidar_faces,
        stats={"geometrySource": "synthetic_lidar_support_plane"},
    )

    stats = pipeline.write_single_keyframe_rgbd_onboarding_mesh(
        keyframes=keyframes,
        depth_frames=depth_frames,
        work_dir=tmp_path,
        arkit_mesh=arkit_mesh,
        output_obj_path=tmp_path / "rgbd_onboarding_mesh.obj",
        output_mtl_path=tmp_path / "rgbd_onboarding_mesh.mtl",
        output_texture_path=tmp_path / "rgbd_onboarding_texture.png",
        output_debug_path=tmp_path / "rgbd_onboarding_diagnostics.json",
        output_overlay_path=tmp_path / "rgbd_onboarding_overlay.png",
        output_usdz_path=None,
        profile=pipeline.PROCESSING_PROFILES["fast_onboarding"],
    )

    assert stats["available"] is True
    assert stats["selectedKeyframeId"] == "onboarding-keyframe"
    assert stats["selectedDepthFrameId"] == "onboarding-depth"
    assert stats["rawFaceCount"] > stats["prunedFaceCount"]
    assert stats["pruning"]["guardrailTriggered"] is False
    assert stats["pruning"]["applied"] is True
    assert stats["pruning"]["pruneReasonCounts"]["lidar_depth_disagreement_prune"] > 0
    assert (tmp_path / "rgbd_onboarding_mesh.obj").read_text(encoding="utf-8").count("\nf ") == stats["prunedFaceCount"]
    assert "map_Kd rgbd_onboarding_texture.png" in (tmp_path / "rgbd_onboarding_mesh.mtl").read_text(encoding="utf-8")


def test_single_keyframe_rgbd_onboarding_lidar_pruning_guardrail_keeps_rgbd_mesh(tmp_path):
    transform = [
        1, 0, 0, 0,
        0, 1, 0, 0,
        0, 0, 1, 0,
        0, 0, 0, 1,
    ]
    intrinsics = [
        4, 0, 0,
        0, 4, 0,
        2, 2, 1,
    ]
    keyframe_dir = tmp_path / "keyframes"
    depth_dir = tmp_path / "depth_frames"
    keyframe_dir.mkdir()
    depth_dir.mkdir()
    Image.new("RGB", (5, 5), (180, 110, 70)).save(keyframe_dir / "keyframe_001.jpg")

    depth_values = [2.0] * 25
    (depth_dir / "depth_001.f32").write_bytes(struct.pack(f"<{len(depth_values)}f", *depth_values))
    (depth_dir / "depth_001.confidence.u8").write_bytes(bytes([2] * 25))

    keyframes = [{
        "id": "onboarding-keyframe",
        "timestamp": 0.0,
        "cameraTransform": transform,
        "intrinsics": intrinsics,
        "imageResolution": [5, 5],
        "path": "keyframes/keyframe_001.jpg",
    }]
    depth_frames = [{
        "id": "onboarding-depth",
        "colorKeyframeId": "onboarding-keyframe",
        "timestamp": 0.0,
        "cameraTransform": transform,
        "intrinsics": intrinsics,
        "depthResolution": [5, 5],
        "depthFormat": "float32_little_endian_meters",
        "path": "depth_frames/depth_001.f32",
        "confidenceFormat": "uint8_arkit_confidence",
        "confidencePath": "depth_frames/depth_001.confidence.u8",
        "metersPerUnit": 1,
    }]

    lidar_vertices = [
        pipeline.backproject_depth_sample_to_world(x, y, 1.0, intrinsics, transform)
        for y in range(5)
        for x in range(5)
    ]
    lidar_faces = []
    for y in range(4):
        for x in range(4):
            top_left = y * 5 + x
            top_right = top_left + 1
            bottom_left = top_left + 5
            bottom_right = bottom_left + 1
            lidar_faces.append((top_left, bottom_left, top_right))
            lidar_faces.append((top_right, bottom_left, bottom_right))
    arkit_mesh = pipeline.FusedMesh(
        vertices=lidar_vertices,
        faces=lidar_faces,
        stats={"geometrySource": "synthetic_lidar_wrong_depth_plane"},
    )

    stats = pipeline.write_single_keyframe_rgbd_onboarding_mesh(
        keyframes=keyframes,
        depth_frames=depth_frames,
        work_dir=tmp_path,
        arkit_mesh=arkit_mesh,
        output_obj_path=tmp_path / "rgbd_onboarding_mesh.obj",
        output_mtl_path=tmp_path / "rgbd_onboarding_mesh.mtl",
        output_texture_path=tmp_path / "rgbd_onboarding_texture.png",
        output_debug_path=tmp_path / "rgbd_onboarding_diagnostics.json",
        output_overlay_path=tmp_path / "rgbd_onboarding_overlay.png",
        output_usdz_path=None,
        profile=pipeline.PROCESSING_PROFILES["fast_onboarding"],
    )

    pruning = stats["pruning"]
    assert stats["available"] is True
    assert stats["rawFaceCount"] == stats["prunedFaceCount"]
    assert pruning["guardrailTriggered"] is True
    assert pruning["applied"] is False
    assert pruning["wouldPruneFaceRatio"] > pipeline.RGBD_ONBOARDING_LIDAR_MAX_HARD_PRUNE_RATIO
    assert pruning["advisoryPruneReasonCounts"]["lidar_depth_disagreement_prune"] > 0
    assert pruning["pruneReasonCounts"]["lidar_prune_guardrail_keep"] == stats["rawFaceCount"]
    assert (tmp_path / "rgbd_onboarding_mesh.obj").read_text(encoding="utf-8").count("\nf ") == stats["prunedFaceCount"]


def make_rgbd_hero_patch_test_patch(
    name: str,
    confidence_values: bytes,
    *,
    depth_values: array | None = None,
) -> dict:
    vertices = [
        (-0.2, -0.2, -1.0),
        (0.2, -0.2, -1.0),
        (-0.2, 0.2, -1.0),
        (0.2, 0.2, -1.0),
    ]
    faces = [(0, 1, 2), (1, 3, 2)]
    mesh = pipeline.FusedMesh(vertices=vertices, faces=faces, stats={"geometrySource": "test_patch"})
    return {
        "patchIndex": 0 if name == "primary" else 1,
        "candidate": {
            "index": 0 if name == "primary" else 1,
            "sourceTimestamp": 0.0 if name == "primary" else 2.5,
            "timestampDeltaSeconds": 0.0,
            "colorKeyframeIdMatched": True,
            "validDepthRatio": 1.0,
            "highConfidenceRatio": 1.0,
            "rgbSharpnessScore": 10.0,
        },
        "keyframe": {"id": f"{name}-keyframe"},
        "depthFrame": {
            "id": f"{name}-depth",
            "colorKeyframeId": f"{name}-keyframe",
            "depthResolution": [4, 4],
        },
        "rgbImage": Image.new("RGB", (8, 8), (80, 120, 160)),
        "ownershipDepthFrame": pipeline.ProjectionDepthFrame(
            id=f"{name}-depth",
            color_keyframe_id=f"{name}-keyframe",
            width=4,
            height=4,
            world_to_camera=[
                1, 0, 0, 0,
                0, 1, 0, 0,
                0, 0, 1, 0,
                0, 0, 0, 1,
            ],
            intrinsics=[
                4, 0, 0,
                0, 4, 0,
                2, 2, 1,
            ],
            depth_values=depth_values or array("f", [1.0] * 16),
            confidence_values=confidence_values,
        ),
        "preparedDepthStats": {
            "finalValidDepthCount": 16,
            "remainingInvalidDepthCount": 0,
            "totalPixelCount": 16,
            "confidenceThreshold": "accept_all_nonzero_depth_values",
        },
        "mesh": mesh,
        "uvCoordinates": [
            (0.25, 0.75),
            (0.75, 0.75),
            (0.25, 0.25),
            (0.75, 0.25),
        ],
        "meshStats": {
            "vertexCount": len(vertices),
            "faceCount": len(faces),
            "acceptedFaceCount": len(faces),
            "rejectedFaceCount": 0,
            "invalidDepthSampleCount": 0,
            "outOfBoundsColorSampleCount": 0,
        },
    }


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


def test_fallback_texture_color_smoothing_blends_adjacent_face_tiles():
    mesh = make_grid_mesh(1)
    texture = Image.new("RGB", (8, 4), pipeline.FALLBACK_COLOR)
    pixels = texture.load()
    for y in range(4):
        for x in range(4):
            pixels[x, y] = (80, 80, 80)
        for x in range(4, 8):
            pixels[x, y] = (200, 200, 200)
    layout = pipeline.TextureAtlasLayout(
        width=8,
        height=4,
        tile_size=4,
        columns=2,
        tile_start_y=0,
        planar_charts=[],
        face_to_chart={},
        face_to_tile_index={0: 0, 1: 1},
        strategy="test_per_face_tiles",
        stats={},
    )

    stats = pipeline.smooth_fallback_texture_face_colors(
        texture,
        mesh,
        layout,
        ["fallback", "projected"],
        enabled=True,
    )

    assert stats["enabled"] is True
    assert stats["smoothedFaceCount"] == 1
    assert pixels[1, 1][0] > 80
    assert pixels[1, 1][0] < 200
    assert pixels[5, 1] == (200, 200, 200)


def test_fallback_texture_color_smoothing_does_not_spread_between_fallback_tiles():
    mesh = make_grid_mesh(1)
    texture = Image.new("RGB", (8, 4), pipeline.FALLBACK_COLOR)
    pixels = texture.load()
    for y in range(4):
        for x in range(4):
            pixels[x, y] = (80, 80, 80)
        for x in range(4, 8):
            pixels[x, y] = (200, 200, 200)
    layout = pipeline.TextureAtlasLayout(
        width=8,
        height=4,
        tile_size=4,
        columns=2,
        tile_start_y=0,
        planar_charts=[],
        face_to_chart={},
        face_to_tile_index={0: 0, 1: 1},
        strategy="test_per_face_tiles",
        stats={},
    )

    stats = pipeline.smooth_fallback_texture_face_colors(
        texture,
        mesh,
        layout,
        ["fallback", "fallback"],
        enabled=True,
    )

    assert stats["enabled"] is True
    assert stats["smoothedFaceCount"] == 0
    assert pixels[1, 1] == (80, 80, 80)
    assert pixels[5, 1] == (200, 200, 200)


def test_onboarding_planar_chart_stride_scales_with_chart_size():
    profile = pipeline.PROCESSING_PROFILES["fast_onboarding"]

    assert pipeline.planar_chart_raster_stride_for_profile(make_test_planar_chart(256, 256), profile) == 2
    assert pipeline.planar_chart_raster_stride_for_profile(make_test_planar_chart(800, 600), profile) == 3
    assert pipeline.planar_chart_raster_stride_for_profile(make_test_planar_chart(1200, 900), profile) == 4
    assert (
        pipeline.planar_chart_raster_stride_for_profile(
            make_test_planar_chart(1200, 900),
            pipeline.PROCESSING_PROFILES["full_quality"],
        )
        == 1
    )


def test_onboarding_blend_candidate_limit_is_lower_than_full_quality():
    candidates = list(range(8))
    profile = pipeline.PROCESSING_PROFILES["fast_onboarding"]

    assert pipeline.texture_blend_candidate_limit_for_profile(profile, "blend") == 4
    assert pipeline.texture_blend_candidate_limit_for_profile(profile, "direct") == 6
    assert pipeline.limit_texture_projection_candidates_for_profile(candidates, profile, "blend") == [0, 1, 2, 3]


def test_onboarding_texture_keyframe_selection_keeps_quality_and_temporal_spread():
    keyframes = [make_test_projection_keyframe(index) for index in range(8)]
    records = [
        {
            "validDepthRatio": 0.7,
            "centralCoverageRatio": 0.2,
            "highConfidenceRatio": 0.5,
            "depthEdgeChaosRatio": 0.4,
            "rgbSharpnessScore": 6,
            "sourceTimestamp": float(index),
        }
        for index in range(8)
    ]
    records[4].update({
        "validDepthRatio": 1.0,
        "centralCoverageRatio": 0.9,
        "highConfidenceRatio": 1.0,
        "depthEdgeChaosRatio": 0.05,
        "rgbSharpnessScore": 18,
    })

    selected, stats = pipeline.select_onboarding_texture_keyframes(keyframes, records, limit=4)

    selected_ids = [keyframe.debug_id for keyframe in selected]
    assert stats["enabled"] is True
    assert stats["selectedKeyframeCount"] == 4
    assert "kf-0" in selected_ids
    assert "kf-7" in selected_ids
    assert "kf-4" in selected_ids


def test_direct_planar_chart_leaves_far_owner_projection_holes_neutral():
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
        lambda: pipeline.TEXTURE_UNOBSERVED_COLOR,
        sample_stride=1,
        projection_mode="direct",
    )

    assert stats["projectedPixelCount"] > 0
    assert stats["localFilledPixelCount"] > 0
    assert stats["neighborFilledPixelCount"] == stats["localFilledPixelCount"]
    assert stats["unresolvedFallbackPixelCount"] > 0
    assert stats["fallbackPixelCount"] == stats["unresolvedFallbackPixelCount"]
    assert stats["maxFillRadius"] == pipeline.TEXTURE_PLANAR_CHART_LOCAL_FILL_MAX_RADIUS_PIXELS
    assert texture.load()[0, 6] == pipeline.TEXTURE_UNOBSERVED_COLOR


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
async def test_depth_frames_are_decoded_and_rgbd_geometry_is_not_used_for_full_quality(monkeypatch):
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
            "processingProfile": "full_quality",
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
        assert status["artifacts"]["rgbdFusedMeshUrl"] is None

        depth_manifest = (await client.get(f"/api/v1/jobs/{job_id}/result/depth_frame_manifest.json", headers=headers)).json()
        assert len(depth_manifest) == 1
        assert depth_manifest[0]["depthResolution"] == [2, 2]

        rgbd_stats = (await client.get(f"/api/v1/jobs/{job_id}/result/rgbd_fusion_stats.json", headers=headers)).json()
        assert rgbd_stats["used"] is False
        assert rgbd_stats["geometrySource"] == "arkit_mesh_anchor_fusion"
        assert rgbd_stats["profile"]["useRgbdGeometry"] is False

        manifest = (await client.get(f"/api/v1/jobs/{job_id}/result/manifest.json", headers=headers)).json()
        assert manifest["artifacts"]["rgbdFusedMesh"]["stats"]["used"] is False


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
async def test_fast_onboarding_profile_prefers_single_keyframe_rgbd_onboarding_mesh():
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
                        index * 0.005, 0, 0, 1,
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
                        index * 0.005, 0, 0, 1,
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
        assert status["artifacts"]["texturedObjUrl"].endswith("/rgbd_onboarding_mesh.obj")
        assert status["artifacts"]["texturedMtlUrl"].endswith("/rgbd_onboarding_mesh.mtl")
        assert status["artifacts"]["texturePngUrl"].endswith("/rgbd_onboarding_texture.png")
        assert status["artifacts"]["usdzUrl"] is None
        assert status["artifacts"]["vertexColoredPlyUrl"] is None
        assert status["artifacts"]["previewMeshUrl"].endswith("/rgbd_onboarding_mesh.obj")
        assert status["artifacts"]["rgbdOnboardingMeshUrl"].endswith("/rgbd_onboarding_mesh.obj")
        assert status["artifacts"]["rgbdOnboardingMtlUrl"].endswith("/rgbd_onboarding_mesh.mtl")
        assert status["artifacts"]["rgbdOnboardingTextureUrl"].endswith("/rgbd_onboarding_texture.png")
        assert status["artifacts"]["rgbdOnboardingDiagnosticsUrl"].endswith("/rgbd_onboarding_diagnostics.json")
        assert status["artifacts"]["rgbdFusedMeshUrl"] is None
        assert status["artifacts"]["textureDebugPreviewUrl"] is None
        assert status["artifacts"]["stageTimingsUrl"].endswith("/stage_timings.json")

        rgbd_stats = (await client.get(f"/api/v1/jobs/{job_id}/result/rgbd_fusion_stats.json", headers=headers)).json()
        assert rgbd_stats["used"] is False
        assert rgbd_stats["geometrySource"] == "rgbd_onboarding_stage"
        assert rgbd_stats["geometryPreserved"] is True
        assert rgbd_stats["profile"]["name"] == "fast_onboarding"
        assert rgbd_stats["profile"]["maxKeyframes"] is None
        assert rgbd_stats["profile"]["maxDepthFrames"] is None
        assert rgbd_stats["profile"]["maxRgbdFrames"] == 0
        assert rgbd_stats["profile"]["useRgbdGeometry"] is False
        assert rgbd_stats["profile"]["preserveTextureRenderMesh"] is True
        assert rgbd_stats["profile"]["densifyTextureRenderMesh"] is True
        assert rgbd_stats["profile"]["denseSingleViewTexture"] is False
        assert rgbd_stats["profile"]["rgbdHeroPatchTexture"] is False
        assert rgbd_stats["profile"]["rgbdOnboardingMesh"] is True
        assert rgbd_stats["profile"]["textureRenderTargetFaces"] == pipeline.FAST_ONBOARDING_TEXTURE_RENDER_TARGET_FACE_COUNT
        assert rgbd_stats["profile"]["textureTsdfRenderTargetFaces"] == pipeline.FAST_ONBOARDING_TEXTURE_TSDF_RENDER_TARGET_FACE_COUNT
        assert rgbd_stats["depthFrameCount"] == 55
        assert rgbd_stats["keyframeCount"] == 55

        keyframe_selection = (await client.get(f"/api/v1/jobs/{job_id}/result/keyframe_selection.json", headers=headers)).json()
        assert keyframe_selection["originalKeyframeCount"] == 55
        assert keyframe_selection["selectedKeyframeCount"] == 55
        assert keyframe_selection["strategy"] == "all_uploaded_keyframes_coverage_first"
        assert len(keyframe_selection["selectedKeyframeIds"]) == 55

        depth_selection = (await client.get(f"/api/v1/jobs/{job_id}/result/depth_frame_selection.json", headers=headers)).json()
        assert depth_selection["originalDepthFrameCount"] == 55
        assert depth_selection["geometryDepthSelection"] == "selected_keyframe_pairs"
        assert depth_selection["selectedDepthFrameCount"] == 55
        assert set(depth_selection["selectedDepthFrameIds"])

        depth_manifest = (await client.get(f"/api/v1/jobs/{job_id}/result/depth_frame_manifest.json", headers=headers)).json()
        assert len(depth_manifest) == 55
        assert {frame["colorKeyframeId"] for frame in depth_manifest}.issubset(set(keyframe_selection["selectedKeyframeIds"]))

        fused_mesh = await client.get(f"/api/v1/jobs/{job_id}/result/fused_mesh.obj", headers=headers)
        arkit_mesh = await client.get(f"/api/v1/jobs/{job_id}/result/arkit_fused_mesh.obj", headers=headers)
        assert fused_mesh.text == arkit_mesh.text

        manifest = (await client.get(f"/api/v1/jobs/{job_id}/result/manifest.json", headers=headers)).json()
        assert manifest["processingProfile"]["name"] == "fast_onboarding"
        assert manifest["preferredPhotorealArtifact"] == "rgbd_onboarding_mesh"
        assert manifest["preferredPreview"] == "rgbd_onboarding_mesh.obj"
        assert manifest["artifacts"]["rgbdOnboardingMesh"]["available"] is True
        assert manifest["artifacts"]["rgbdOnboardingMesh"]["preferred"] is True
        onboarding_stats = manifest["artifacts"]["rgbdOnboardingMesh"]["stats"]
        assert onboarding_stats["selectedKeyframeId"] in keyframe_selection["selectedKeyframeIds"][:4]
        assert onboarding_stats["selectedDepthFrameId"] in depth_selection["selectedDepthFrameIds"][:4]
        assert onboarding_stats["secondsFromCaptureStart"] <= pipeline.RGBD_ONBOARDING_WINDOW_SECONDS
        assert onboarding_stats["rawVertexCount"] > 0
        assert onboarding_stats["rawFaceCount"] > 0
        assert onboarding_stats["prunedVertexCount"] > 0
        assert onboarding_stats["prunedFaceCount"] > 0
        assert onboarding_stats["pruning"]["pruneReasonCounts"]
        assert onboarding_stats["becamePreferredPreview"] is True
        assert manifest["artifacts"]["rgbdFusedMesh"]["available"] is False
        assert manifest["artifacts"]["rgbdFusedMesh"]["stats"]["used"] is False
        assert manifest["artifacts"]["rawFusedMesh"]["stats"]["geometryPreserved"] is True
        assert manifest["artifacts"]["vertexColoredPlyDebugPreview"]["available"] is False
        assert manifest["artifacts"]["texturedObj"]["stats"]["available"] is False
        assert manifest["artifacts"]["textureDebug"]["previewAvailable"] is False
        assert manifest["artifacts"]["usdz"]["available"] is False

        onboarding_obj = await client.get(f"/api/v1/jobs/{job_id}/result/rgbd_onboarding_mesh.obj", headers=headers)
        onboarding_mtl = await client.get(f"/api/v1/jobs/{job_id}/result/rgbd_onboarding_mesh.mtl", headers=headers)
        onboarding_texture = await client.get(f"/api/v1/jobs/{job_id}/result/rgbd_onboarding_texture.png", headers=headers)
        onboarding_diagnostics = (await client.get(
            f"/api/v1/jobs/{job_id}/result/rgbd_onboarding_diagnostics.json",
            headers=headers,
        )).json()
        assert "o rgbd_onboarding_mesh" in onboarding_obj.text
        assert "mtllib rgbd_onboarding_mesh.mtl" in onboarding_obj.text
        assert "\nvt " in onboarding_obj.text
        assert "\nf " in onboarding_obj.text
        assert "map_Kd rgbd_onboarding_texture.png" in onboarding_mtl.text
        assert onboarding_texture.headers["content-type"] == "image/png"
        assert onboarding_diagnostics["selectedKeyframeId"] == onboarding_stats["selectedKeyframeId"]
        assert onboarding_diagnostics["selectedDepthFrameId"] == onboarding_stats["selectedDepthFrameId"]
        assert onboarding_diagnostics["pruning"]["rawFaceCount"] == onboarding_stats["rawFaceCount"]
        assert onboarding_diagnostics["pruning"]["finalFaceCount"] == onboarding_stats["prunedFaceCount"]

        timings = (await client.get(f"/api/v1/jobs/{job_id}/result/stage_timings.json", headers=headers)).json()
        assert timings["jobId"] == job_id
        assert any(item["stageClass"] == "TexturedMeshStage" for item in timings["timings"])


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
