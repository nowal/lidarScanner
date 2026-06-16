from __future__ import annotations

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass, field
from typing import Optional
from pathlib import Path

from .config import settings
from .models import ArtifactLinks, JobStage, JobStatus, JobStatusResponse, now_utc
from .pipeline import DEFAULT_PIPELINE

logger = logging.getLogger("lidarai.store")


@dataclass
class JobRecord:
    job_id: str
    created_at: str
    updated_at: str
    status: JobStatus
    stage: JobStage
    progress: float
    message: str
    error: Optional[str] = None
    total_bytes: int = 0
    uploaded_bytes: int = 0
    artifact_path: Optional[str] = None
    cancelled: bool = False
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class JobStore:
    def __init__(self, base_dir: str):
        self.base_path = Path(base_dir)
        self.jobs_path = self.base_path / "jobs"
        self.jobs_path.mkdir(parents=True, exist_ok=True)
        self.jobs: dict[str, JobRecord] = {}
        self.subscribers: dict[str, list[asyncio.Queue[dict]]] = {}
        self._load_existing_jobs()

    def create_job(self) -> JobRecord:
        job_id = str(uuid.uuid4())
        now = now_utc().isoformat()
        record = JobRecord(
            job_id=job_id,
            created_at=now,
            updated_at=now,
            status=JobStatus.queued,
            stage=JobStage.queued,
            progress=0,
            message="Job queued",
        )
        self.jobs[job_id] = record
        self._job_dir(job_id).mkdir(parents=True, exist_ok=True)
        (self._job_dir(job_id) / "upload").mkdir(parents=True, exist_ok=True)
        (self._job_dir(job_id) / "work").mkdir(parents=True, exist_ok=True)
        self._persist(record)
        return record

    def get(self, job_id: str) -> Optional[JobRecord]:
        return self.jobs.get(job_id)

    def _job_dir(self, job_id: str) -> Path:
        return self.jobs_path / job_id

    def upload_path(self, job_id: str) -> Path:
        return self._job_dir(job_id) / "upload" / "scan_payload.json"

    def result_dir(self, job_id: str) -> Path:
        return self._job_dir(job_id) / "result"

    def status_path(self, job_id: str) -> Path:
        return self._job_dir(job_id) / "status.json"

    def record_path(self, job_id: str) -> Path:
        return self._job_dir(job_id) / "record.json"

    def to_response(self, record: JobRecord) -> JobStatusResponse:
        result_bundle_url = None
        preview_mesh_url = None
        manifest_url = None
        raw_fused_mesh_url = None
        arkit_fused_mesh_url = None
        rgbd_fused_mesh_url = None
        vertex_colored_ply_url = None
        textured_obj_url = None
        textured_mtl_url = None
        texture_png_url = None
        texture_debug_json_url = None
        texture_debug_preview_url = None
        usdz_url = None
        glb_url = None
        if record.artifact_path:
            result_base = f"{settings.api_prefix}/jobs/{record.job_id}/result"
            result_dir = self.result_dir(record.job_id)
            result_bundle_url = result_base
            preview_mesh_url = f"{result_base}/colored_mesh.ply"
            manifest_url = f"{result_base}/manifest.json"
            raw_fused_mesh_url = self._artifact_url_if_present(result_dir, result_base, "fused_mesh.obj")
            arkit_fused_mesh_url = self._artifact_url_if_present(result_dir, result_base, "arkit_fused_mesh.obj")
            rgbd_fused_mesh_url = self._artifact_url_if_present(result_dir, result_base, "rgbd_fused_mesh.obj")
            vertex_colored_ply_url = self._artifact_url_if_present(result_dir, result_base, "colored_mesh.ply")
            textured_obj_url = self._artifact_url_if_present(result_dir, result_base, "textured_mesh.obj")
            textured_mtl_url = self._artifact_url_if_present(result_dir, result_base, "textured_mesh.mtl")
            texture_png_url = self._artifact_url_if_present(result_dir, result_base, "textured_mesh_texture.png")
            texture_debug_json_url = self._artifact_url_if_present(result_dir, result_base, "texture_debug.json")
            texture_debug_preview_url = self._artifact_url_if_present(result_dir, result_base, "texture_debug_preview.png")
            usdz_url = self._artifact_url_if_present(result_dir, result_base, "textured_mesh.usdz")
            glb_url = self._artifact_url_if_present(result_dir, result_base, "textured_mesh.glb")
        return JobStatusResponse(
            jobId=record.job_id,
            createdAt=record.created_at,
            updatedAt=record.updated_at,
            status=record.status,
            stage=record.stage,
            progress=record.progress,
            message=record.message,
            error=record.error,
            artifacts=ArtifactLinks(
                resultBundleUrl=result_bundle_url,
                previewMeshUrl=preview_mesh_url,
                manifestUrl=manifest_url,
                rawFusedMeshUrl=raw_fused_mesh_url,
                arkitFusedMeshUrl=arkit_fused_mesh_url,
                rgbdFusedMeshUrl=rgbd_fused_mesh_url,
                vertexColoredPlyUrl=vertex_colored_ply_url,
                texturedObjUrl=textured_obj_url,
                texturedMtlUrl=textured_mtl_url,
                texturePngUrl=texture_png_url,
                textureDebugJsonUrl=texture_debug_json_url,
                textureDebugPreviewUrl=texture_debug_preview_url,
                usdzUrl=usdz_url,
                glbUrl=glb_url,
            ),
        )

    def _artifact_url_if_present(self, result_dir: Path, result_base: str, filename: str) -> Optional[str]:
        return f"{result_base}/{filename}" if (result_dir / filename).exists() else None

    async def publish(self, record: JobRecord) -> None:
        event = {"jobId": record.job_id, "status": self.to_response(record).model_dump(mode="json")}
        for queue in self.subscribers.get(record.job_id, []):
            await queue.put(event)

    async def update(
        self,
        record: JobRecord,
        *,
        status: Optional[JobStatus] = None,
        stage: Optional[JobStage] = None,
        progress: Optional[float] = None,
        message: Optional[str] = None,
        error: Optional[str] = None,
    ) -> None:
        if status is not None:
            record.status = status
        if stage is not None:
            record.stage = stage
        if progress is not None:
            record.progress = max(0, min(progress, 100))
        if message is not None:
            record.message = message
        record.error = error
        record.updated_at = now_utc().isoformat()
        self._persist(record)
        await self.publish(record)

    def _persist(self, record: JobRecord) -> None:
        status_json = self.to_response(record).model_dump(mode="json")
        self.status_path(record.job_id).write_text(json.dumps(status_json, indent=2), encoding="utf-8")
        record_json = {
            "jobId": record.job_id,
            "createdAt": record.created_at,
            "updatedAt": record.updated_at,
            "status": record.status.value,
            "stage": record.stage.value,
            "progress": record.progress,
            "message": record.message,
            "error": record.error,
            "totalBytes": record.total_bytes,
            "uploadedBytes": record.uploaded_bytes,
            "artifactPath": record.artifact_path,
            "cancelled": record.cancelled,
        }
        self.record_path(record.job_id).write_text(json.dumps(record_json, indent=2), encoding="utf-8")

    def _load_existing_jobs(self) -> None:
        for job_dir in sorted(self.jobs_path.iterdir()):
            if not job_dir.is_dir():
                continue
            record = self._load_record(job_dir)
            if record:
                self.jobs[record.job_id] = record

    def _load_record(self, job_dir: Path) -> Optional[JobRecord]:
        record_path = job_dir / "record.json"
        if record_path.exists():
            try:
                data = json.loads(record_path.read_text(encoding="utf-8"))
                return self._record_from_private_json(job_dir, data)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Could not load persisted job record", extra={"job_id": job_dir.name, "error": str(exc)})

        status_path = job_dir / "status.json"
        if not status_path.exists():
            return None

        try:
            status = JobStatusResponse.model_validate(json.loads(status_path.read_text(encoding="utf-8")))
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not load persisted job status", extra={"job_id": job_dir.name, "error": str(exc)})
            return None

        upload_path = job_dir / "upload" / "scan_payload.json"
        uploaded_bytes = upload_path.stat().st_size if upload_path.exists() else 0
        artifact_path = str(job_dir / "result") if status.status == JobStatus.complete and (job_dir / "result").exists() else None
        return JobRecord(
            job_id=status.jobId,
            created_at=status.createdAt.isoformat(),
            updated_at=status.updatedAt.isoformat(),
            status=status.status,
            stage=status.stage,
            progress=status.progress,
            message=status.message,
            error=status.error,
            total_bytes=uploaded_bytes,
            uploaded_bytes=uploaded_bytes,
            artifact_path=artifact_path,
        )

    def _record_from_private_json(self, job_dir: Path, data: dict) -> JobRecord:
        status = JobStatus(data["status"])
        stage = JobStage(data["stage"])
        message = data.get("message", "")
        error = data.get("error")

        if status == JobStatus.running:
            status = JobStatus.failed
            stage = JobStage.failed
            message = "Processing was interrupted by a service restart. Please retry the upload."
            error = error or "Service restarted while the job was running."

        artifact_path = data.get("artifactPath")
        result_dir = job_dir / "result"
        if status == JobStatus.complete and not artifact_path and result_dir.exists():
            artifact_path = str(result_dir)

        return JobRecord(
            job_id=data["jobId"],
            created_at=data["createdAt"],
            updated_at=data["updatedAt"],
            status=status,
            stage=stage,
            progress=float(data.get("progress", 0)),
            message=message,
            error=error,
            total_bytes=int(data.get("totalBytes", 0)),
            uploaded_bytes=int(data.get("uploadedBytes", 0)),
            artifact_path=artifact_path,
            cancelled=bool(data.get("cancelled", False)),
        )

    async def run_pipeline(self, record: JobRecord) -> None:
        async with record.lock:
            if record.cancelled:
                await self.update(
                    record,
                    status=JobStatus.cancelled,
                    stage=JobStage.cancelled,
                    progress=record.progress,
                    message="Cancelled before processing started",
                )
                return

            await self.update(record, status=JobStatus.running, stage=JobStage.preprocessing, progress=1, message="Processing started")
            job_dir = self._job_dir(record.job_id)

            try:
                for stage in DEFAULT_PIPELINE:
                    if record.cancelled:
                        raise asyncio.CancelledError

                    async def report(stage_name: JobStage, progress: float, message: str) -> None:
                        await self.update(record, status=JobStatus.running, stage=stage_name, progress=progress, message=message)

                    await stage.run(job_dir, report=report, is_cancelled=lambda: record.cancelled)

                record.artifact_path = str(self.result_dir(record.job_id))
                await self.update(record, status=JobStatus.complete, stage=JobStage.complete, progress=100, message="Processing complete")
            except asyncio.CancelledError:
                await self.update(record, status=JobStatus.cancelled, stage=JobStage.cancelled, message="Processing cancelled")
            except Exception as exc:  # noqa: BLE001
                logger.exception("Pipeline failed", extra={"job_id": record.job_id})
                await self.update(
                    record,
                    status=JobStatus.failed,
                    stage=JobStage.failed,
                    message="Processing failed",
                    error=str(exc),
                )


store = JobStore(settings.storage_dir)
