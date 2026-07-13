from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

from typing import Optional

from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from sse_starlette.sse import EventSourceResponse

from .config import settings
from .logging_setup import configure_logging
from .models import (
    CreateJobResponse,
    EventMessage,
    FinalizeUploadRequest,
    HealthResponse,
    JobStage,
    JobStatus,
    UploadChunkResponse,
    UploadStateResponse,
    now_utc,
)
from .home_ai import (
    HomeAIChatRequest,
    HomeAIChatResponse,
    HomeAIEventRequest,
    HomeAIEventResponse,
    generate_home_ai_response,
    record_home_ai_event,
)
from .store import store

configure_logging(settings.storage_dir)
logger = logging.getLogger("lidarai.api")

app = FastAPI(title="LidarAI Processor", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[origin.strip() for origin in settings.cors_origins.split(",")],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def require_token(authorization: Optional[str] = Header(default=None)) -> None:
    if not settings.auth_token:
        return
    expected = f"Bearer {settings.auth_token}"
    if authorization != expected:
        raise HTTPException(status_code=401, detail="Invalid auth token")


@app.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    return HealthResponse(status="ok", time=now_utc())


@app.post(
    f"{settings.api_prefix}/ai/home-chat",
    response_model=HomeAIChatResponse,
    dependencies=[Depends(require_token)],
)
async def home_ai_chat(request_body: HomeAIChatRequest) -> HomeAIChatResponse:
    return await generate_home_ai_response(request_body)


@app.post(
    f"{settings.api_prefix}/ai/home-events",
    response_model=HomeAIEventResponse,
    dependencies=[Depends(require_token)],
)
async def home_ai_event(request_body: HomeAIEventRequest) -> HomeAIEventResponse:
    return await record_home_ai_event(request_body)


@app.post(f"{settings.api_prefix}/jobs", response_model=CreateJobResponse, dependencies=[Depends(require_token)])
async def create_job() -> CreateJobResponse:
    record = store.create_job()
    await store.publish(record)
    logger.info("Created job", extra={"job_id": record.job_id})
    return CreateJobResponse(jobId=record.job_id, createdAt=record.created_at)


@app.get(f"{settings.api_prefix}/jobs/{{job_id}}", dependencies=[Depends(require_token)])
async def get_job(job_id: str) -> JSONResponse:
    record = store.get(job_id)
    if not record:
        raise HTTPException(status_code=404, detail="Job not found")
    return JSONResponse(store.to_response(record).model_dump(mode="json"))


@app.get(f"{settings.api_prefix}/jobs/{{job_id}}/upload-state", response_model=UploadStateResponse, dependencies=[Depends(require_token)])
async def upload_state(job_id: str) -> UploadStateResponse:
    record = store.get(job_id)
    if not record:
        raise HTTPException(status_code=404, detail="Job not found")
    return UploadStateResponse(jobId=job_id, receivedBytes=record.uploaded_bytes, totalBytes=record.total_bytes)


@app.post(f"{settings.api_prefix}/jobs/{{job_id}}/upload", response_model=UploadChunkResponse, dependencies=[Depends(require_token)])
async def upload_chunk(
    job_id: str,
    request: Request,
    x_upload_offset: int = Header(default=0),
    x_upload_total: int = Header(default=0),
) -> UploadChunkResponse:
    record = store.get(job_id)
    if not record:
        raise HTTPException(status_code=404, detail="Job not found")

    body = await request.body()
    payload_path = store.upload_path(job_id)
    payload_path.parent.mkdir(parents=True, exist_ok=True)

    if x_upload_offset < 0:
        raise HTTPException(status_code=400, detail="Invalid offset")

    if x_upload_total > 0:
        record.total_bytes = x_upload_total

    mode = "r+b" if payload_path.exists() else "wb"
    with open(payload_path, mode) as f:
        f.seek(x_upload_offset)
        f.write(body)

    record.uploaded_bytes = max(record.uploaded_bytes, x_upload_offset + len(body))
    await store.update(
        record,
        status=JobStatus.queued,
        stage=JobStage.uploading,
        progress=(record.uploaded_bytes / record.total_bytes * 100) if record.total_bytes else 0,
        message=f"Uploaded {record.uploaded_bytes} / {record.total_bytes} bytes",
    )

    is_complete = record.total_bytes > 0 and record.uploaded_bytes >= record.total_bytes
    return UploadChunkResponse(
        jobId=job_id,
        receivedBytes=record.uploaded_bytes,
        totalBytes=record.total_bytes,
        complete=is_complete,
    )


@app.post(f"{settings.api_prefix}/jobs/{{job_id}}/finalize", dependencies=[Depends(require_token)])
async def finalize_upload(job_id: str, request_body: FinalizeUploadRequest, background: BackgroundTasks) -> JSONResponse:
    record = store.get(job_id)
    if not record:
        raise HTTPException(status_code=404, detail="Job not found")

    payload_path = store.upload_path(job_id)
    if not payload_path.exists():
        raise HTTPException(status_code=400, detail="No upload found")

    file_size = payload_path.stat().st_size
    if file_size != request_body.totalBytes:
        raise HTTPException(
            status_code=400,
            detail=f"Upload incomplete. expected={request_body.totalBytes} actual={file_size}",
        )

    record.uploaded_bytes = file_size
    record.total_bytes = request_body.totalBytes
    await store.update(record, status=JobStatus.queued, stage=JobStage.queued, progress=0, message="Upload finalized")
    background.add_task(store.run_pipeline, record)
    return JSONResponse(store.to_response(record).model_dump(mode="json"))


@app.post(f"{settings.api_prefix}/jobs/{{job_id}}/cancel", dependencies=[Depends(require_token)])
async def cancel_job(job_id: str) -> JSONResponse:
    record = store.get(job_id)
    if not record:
        raise HTTPException(status_code=404, detail="Job not found")
    record.cancelled = True
    await store.update(record, status=JobStatus.cancelled, stage=JobStage.cancelled, message="Cancellation requested")
    return JSONResponse(store.to_response(record).model_dump(mode="json"))


@app.get(f"{settings.api_prefix}/jobs/{{job_id}}/result", dependencies=[Depends(require_token)])
async def download_result(job_id: str) -> Response:
    return await download_result_file(job_id, "reconstructed_scene.json")


@app.get(f"{settings.api_prefix}/jobs/{{job_id}}/result/{{filename}}", dependencies=[Depends(require_token)])
async def download_named_result(job_id: str, filename: str) -> Response:
    return await download_result_file(job_id, filename)


async def download_result_file(job_id: str, filename: str) -> Response:
    record = store.get(job_id)
    if not record:
        raise HTTPException(status_code=404, detail="Job not found")
    if record.status != JobStatus.complete:
        raise HTTPException(status_code=409, detail="Result not ready")

    allowed_files = {
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
        "texture_debug.json",
        "texture_debug_preview.png",
        "two_keyframe_projection_0.png",
        "two_keyframe_projection_1.png",
        "stage_timings.json",
        "keyframe_selection.json",
        "depth_frame_selection.json",
        "processing_profile.json",
        "arkit_mesh_stats.json",
        "depth_frame_manifest.json",
        "rgbd_fusion_stats.json",
        "textured_mesh.usdz",
        "textured_mesh.glb",
        "keyframe_manifest.json",
    }
    if filename not in allowed_files:
        raise HTTPException(status_code=404, detail="Result file missing")

    result_file = store.result_dir(job_id) / filename
    if not result_file.exists():
        raise HTTPException(status_code=404, detail="Result file missing")

    media_type = media_type_for_result(filename)
    return FileResponse(result_file, filename=f"{job_id}-{filename}", media_type=media_type)


def media_type_for_result(filename: str) -> str:
    if filename.endswith(".json"):
        return "application/json"
    if filename.endswith(".png"):
        return "image/png"
    if filename.endswith(".obj"):
        return "model/obj"
    if filename.endswith(".ply"):
        return "model/ply"
    if filename.endswith(".mtl"):
        return "text/plain"
    if filename.endswith(".usdz"):
        return "model/vnd.usdz+zip"
    if filename.endswith(".glb"):
        return "model/gltf-binary"
    return "application/octet-stream"


@app.get(f"{settings.api_prefix}/jobs/{{job_id}}/events", dependencies=[Depends(require_token)])
async def stream_events(job_id: str, request: Request) -> EventSourceResponse:
    record = store.get(job_id)
    if not record:
        raise HTTPException(status_code=404, detail="Job not found")

    queue: asyncio.Queue[dict] = asyncio.Queue()
    store.subscribers.setdefault(job_id, []).append(queue)

    async def event_generator():
        try:
            initial = EventMessage(payload=store.to_response(record))
            yield {"event": "job_status", "data": initial.model_dump_json()}
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15)
                    msg = EventMessage(payload=event["status"])
                    yield {"event": "job_status", "data": msg.model_dump_json()}
                except asyncio.TimeoutError:
                    yield {"event": "ping", "data": json.dumps({"ok": True})}
        finally:
            subscribers = store.subscribers.get(job_id, [])
            if queue in subscribers:
                subscribers.remove(queue)

    return EventSourceResponse(event_generator())
