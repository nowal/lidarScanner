from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field


SCHEMA_VERSION = "v1"


class JobStage(str, Enum):
    queued = "queued"
    uploading = "uploading"
    preprocessing = "preprocessing"
    meshing = "meshing"
    texturing = "texturing"
    postprocessing = "postprocessing"
    complete = "complete"
    failed = "failed"
    cancelled = "cancelled"


class JobStatus(str, Enum):
    queued = "queued"
    running = "running"
    complete = "complete"
    failed = "failed"
    cancelled = "cancelled"


class CreateJobResponse(BaseModel):
    schemaVersion: str = SCHEMA_VERSION
    jobId: str
    createdAt: datetime


class UploadChunkResponse(BaseModel):
    schemaVersion: str = SCHEMA_VERSION
    jobId: str
    receivedBytes: int
    totalBytes: int
    complete: bool


class UploadStateResponse(BaseModel):
    schemaVersion: str = SCHEMA_VERSION
    jobId: str
    receivedBytes: int
    totalBytes: int


class ArtifactLinks(BaseModel):
    resultBundleUrl: Optional[str] = None
    previewMeshUrl: Optional[str] = None
    manifestUrl: Optional[str] = None
    rawFusedMeshUrl: Optional[str] = None
    arkitFusedMeshUrl: Optional[str] = None
    rgbdFusedMeshUrl: Optional[str] = None
    rgbdSingleFramePointsUrl: Optional[str] = None
    rgbdSingleFrameMeshUrl: Optional[str] = None
    rgbdSingleFrameOverlayUrl: Optional[str] = None
    rgbdSingleFrameDepthUrl: Optional[str] = None
    rgbdSingleFrameConfidenceUrl: Optional[str] = None
    rgbdSingleFrameDiagnosticsUrl: Optional[str] = None
    vertexColoredPlyUrl: Optional[str] = None
    texturedObjUrl: Optional[str] = None
    texturedMtlUrl: Optional[str] = None
    texturePngUrl: Optional[str] = None
    textureDebugJsonUrl: Optional[str] = None
    textureDebugPreviewUrl: Optional[str] = None
    stageTimingsUrl: Optional[str] = None
    usdzUrl: Optional[str] = None
    glbUrl: Optional[str] = None


class JobStatusResponse(BaseModel):
    schemaVersion: str = SCHEMA_VERSION
    jobId: str
    createdAt: datetime
    updatedAt: datetime
    status: JobStatus
    stage: JobStage
    progress: float = Field(ge=0, le=100)
    message: str = ""
    error: Optional[str] = None
    artifacts: ArtifactLinks = Field(default_factory=ArtifactLinks)


class FinalizeUploadRequest(BaseModel):
    totalBytes: int = Field(gt=0)
    filename: str = "scan_payload.json"


class EventMessage(BaseModel):
    schemaVersion: str = SCHEMA_VERSION
    type: str = "job_status"
    payload: JobStatusResponse


class HealthResponse(BaseModel):
    status: str
    time: datetime


class ScanPayloadEnvelope(BaseModel):
    schemaVersion: str
    createdAt: datetime
    processingProfile: Optional[str] = None
    scanPurpose: Optional[str] = None
    alignmentContext: Optional[dict[str, Any]] = None
    captureSelection: Optional[dict[str, Any]] = None
    meshAnchors: list[dict[str, Any]]
    roomJSONBase64: Optional[str] = None
    roomJSONBase64List: list[str] = Field(default_factory=list)
    structureJSONBase64: Optional[str] = None
    roomPlanSegments: list[dict[str, Any]] = Field(default_factory=list)
    images: list[dict[str, Any]] = Field(default_factory=list)
    depthFrames: Optional[list[dict[str, Any]]] = None



def now_utc() -> datetime:
    return datetime.now(timezone.utc)
