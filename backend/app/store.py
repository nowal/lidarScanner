from __future__ import annotations

import asyncio
import json
import logging
import shutil
import time
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
        record = self.jobs.get(job_id)
        if record and record.lock.locked():
            return record
        if record and record.status in {JobStatus.complete, JobStatus.failed, JobStatus.cancelled}:
            return record

        persisted = self._load_record(self._job_dir(job_id), mark_interrupted_running=False)
        if persisted:
            if record:
                persisted.lock = record.lock
            self.jobs[job_id] = persisted
            return persisted

        return record

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
        mesh_integrity_report_url = None
        geometry_only_glb_url = None
        geometry_culled_glb_url = None
        rgbd_fused_mesh_url = None
        rgbd_single_frame_points_url = None
        rgbd_single_frame_mesh_url = None
        rgbd_single_frame_overlay_url = None
        rgbd_single_frame_depth_url = None
        rgbd_single_frame_confidence_url = None
        rgbd_single_frame_diagnostics_url = None
        vertex_colored_ply_url = None
        textured_obj_url = None
        textured_mtl_url = None
        texture_png_url = None
        texture_debug_json_url = None
        texture_debug_preview_url = None
        uv_checker_glb_url = None
        coverage_debug_glb_url = None
        coverage_debug_report_url = None
        stage_timings_url = None
        usdz_url = None
        glb_url = None
        if record.artifact_path:
            result_base = f"{settings.api_prefix}/jobs/{record.job_id}/result"
            result_dir = self.result_dir(record.job_id)
            result_bundle_url = result_base
            manifest_url = f"{result_base}/manifest.json"
            raw_fused_mesh_url = self._artifact_url_if_present(result_dir, result_base, "fused_mesh.obj")
            arkit_fused_mesh_url = self._artifact_url_if_present(result_dir, result_base, "arkit_fused_mesh.obj")
            mesh_integrity_report_url = self._artifact_url_if_present(result_dir, result_base, "mesh_integrity_report.json")
            geometry_only_glb_url = self._artifact_url_if_present(result_dir, result_base, "geometry_only.glb")
            geometry_culled_glb_url = self._artifact_url_if_present(result_dir, result_base, "geometry_culled.glb")
            rgbd_fused_mesh_url = self._artifact_url_if_present(result_dir, result_base, "rgbd_fused_mesh.obj")
            rgbd_single_frame_points_url = self._artifact_url_if_present(result_dir, result_base, "rgbd_single_frame_points.ply")
            rgbd_single_frame_mesh_url = self._artifact_url_if_present(result_dir, result_base, "rgbd_single_frame_mesh.obj")
            rgbd_single_frame_overlay_url = self._artifact_url_if_present(result_dir, result_base, "rgbd_single_frame_overlay.png")
            rgbd_single_frame_depth_url = self._artifact_url_if_present(result_dir, result_base, "rgbd_single_frame_depth.png")
            rgbd_single_frame_confidence_url = self._artifact_url_if_present(result_dir, result_base, "rgbd_single_frame_confidence.png")
            rgbd_single_frame_diagnostics_url = self._artifact_url_if_present(result_dir, result_base, "rgbd_single_frame_diagnostics.json")
            vertex_colored_ply_url = self._artifact_url_if_present(result_dir, result_base, "colored_mesh.ply")
            preview_mesh_url = vertex_colored_ply_url or rgbd_fused_mesh_url or rgbd_single_frame_mesh_url or raw_fused_mesh_url
            textured_obj_url = self._artifact_url_if_present(result_dir, result_base, "textured_mesh.obj")
            textured_mtl_url = self._artifact_url_if_present(result_dir, result_base, "textured_mesh.mtl")
            texture_png_url = self._artifact_url_if_present(result_dir, result_base, "textured_mesh_texture.png")
            texture_debug_json_url = self._artifact_url_if_present(result_dir, result_base, "texture_debug.json")
            texture_debug_preview_url = self._artifact_url_if_present(result_dir, result_base, "texture_debug_preview.png")
            uv_checker_glb_url = self._artifact_url_if_present(result_dir, result_base, "uv_checker.glb")
            coverage_debug_glb_url = self._artifact_url_if_present(result_dir, result_base, "coverage_debug.glb")
            coverage_debug_report_url = self._artifact_url_if_present(result_dir, result_base, "coverage_debug_report.json")
            stage_timings_url = self._artifact_url_if_present(result_dir, result_base, "stage_timings.json")
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
                meshIntegrityReportUrl=mesh_integrity_report_url,
                geometryOnlyGlbUrl=geometry_only_glb_url,
                geometryCulledGlbUrl=geometry_culled_glb_url,
                rgbdFusedMeshUrl=rgbd_fused_mesh_url,
                rgbdSingleFramePointsUrl=rgbd_single_frame_points_url,
                rgbdSingleFrameMeshUrl=rgbd_single_frame_mesh_url,
                rgbdSingleFrameOverlayUrl=rgbd_single_frame_overlay_url,
                rgbdSingleFrameDepthUrl=rgbd_single_frame_depth_url,
                rgbdSingleFrameConfidenceUrl=rgbd_single_frame_confidence_url,
                rgbdSingleFrameDiagnosticsUrl=rgbd_single_frame_diagnostics_url,
                vertexColoredPlyUrl=vertex_colored_ply_url,
                texturedObjUrl=textured_obj_url,
                texturedMtlUrl=textured_mtl_url,
                texturePngUrl=texture_png_url,
                textureDebugJsonUrl=texture_debug_json_url,
                textureDebugPreviewUrl=texture_debug_preview_url,
                uvCheckerGlbUrl=uv_checker_glb_url,
                coverageDebugGlbUrl=coverage_debug_glb_url,
                coverageDebugReportUrl=coverage_debug_report_url,
                stageTimingsUrl=stage_timings_url,
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
        logger.info(
            "Job update status=%s stage=%s progress=%.1f message=%s",
            record.status.value,
            record.stage.value,
            record.progress,
            record.message,
            extra={"job_id": record.job_id},
        )
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
            record = self._load_record(job_dir, mark_interrupted_running=True)
            if record:
                self.jobs[record.job_id] = record

    def _load_record(self, job_dir: Path, *, mark_interrupted_running: bool) -> Optional[JobRecord]:
        record_path = job_dir / "record.json"
        if record_path.exists():
            try:
                data = json.loads(record_path.read_text(encoding="utf-8"))
                return self._record_from_private_json(
                    job_dir,
                    data,
                    mark_interrupted_running=mark_interrupted_running,
                )
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

    def _record_from_private_json(
        self,
        job_dir: Path,
        data: dict,
        *,
        mark_interrupted_running: bool,
    ) -> JobRecord:
        status = JobStatus(data["status"])
        stage = JobStage(data["stage"])
        message = data.get("message", "")
        error = data.get("error")

        if mark_interrupted_running and status == JobStatus.running:
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
            timings_path = job_dir / "work" / "stage_timings.json"
            stage_timings: list[dict] = []

            try:
                for stage in DEFAULT_PIPELINE:
                    if record.cancelled:
                        raise asyncio.CancelledError

                    async def report(stage_name: JobStage, progress: float, message: str) -> None:
                        await self.update(record, status=JobStatus.running, stage=stage_name, progress=progress, message=message)

                    started = time.perf_counter()
                    await stage.run(job_dir, report=report, is_cancelled=lambda: record.cancelled)
                    elapsed = time.perf_counter() - started
                    stage_timings.append({
                        "stage": stage.name.value,
                        "stageClass": stage.__class__.__name__,
                        "elapsedSeconds": round(elapsed, 3),
                    })
                    timings_path.write_text(json.dumps({
                        "jobId": record.job_id,
                        "timings": stage_timings,
                        "totalElapsedSeconds": round(sum(item["elapsedSeconds"] for item in stage_timings), 3),
                    }, indent=2), encoding="utf-8")

                record.artifact_path = str(self.result_dir(record.job_id))
                final_timings = self.result_dir(record.job_id) / "stage_timings.json"
                if timings_path.exists() and final_timings.parent.exists():
                    shutil.copyfile(timings_path, final_timings)
                await self.update(record, status=JobStatus.complete, stage=JobStage.complete, progress=100, message="Processing complete")
            except asyncio.CancelledError:
                await self.update(record, status=JobStatus.cancelled, stage=JobStage.cancelled, message="Processing cancelled")
            except Exception as exc:  # noqa: BLE001
                if stage_timings:
                    timings_path.write_text(json.dumps({
                        "jobId": record.job_id,
                        "timings": stage_timings,
                        "totalElapsedSeconds": round(sum(item["elapsedSeconds"] for item in stage_timings), 3),
                        "failed": True,
                    }, indent=2), encoding="utf-8")
                logger.exception("Pipeline failed", extra={"job_id": record.job_id})
                await self.update(
                    record,
                    status=JobStatus.failed,
                    stage=JobStage.failed,
                    message="Processing failed",
                    error=str(exc),
                )


store = JobStore(settings.storage_dir)
