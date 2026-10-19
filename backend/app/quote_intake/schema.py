from typing import Literal
from uuid import UUID
from pydantic import Field
from .rubrics import StrictModel, Status


class Answer(StrictModel):
    field: str
    status: Status
    value: str | None = Field(description='A factual string only when status=answered. For every other status this MUST be null, including recommendations and uncertainty.')
    sourceTurnId: str
    evidence: str = Field(min_length=1, max_length=2000,
                         description='Exact verbatim substring of the cited homeowner turn, never a paraphrase.')


class WorkItem(StrictModel):
    id: str = Field(pattern=r'^[a-z][a-z0-9_]{0,39}$')
    rubricId: str
    service: str | None
    serviceName: str = Field(min_length=1, max_length=150)
    subtype: str | None = Field(description='Null unless this rubric has an answered decision named subtype; then copy its value.')
    assessment: bool
    answers: list[Answer] = Field(max_length=30, description='Every supported common and applicable service decision for this item. Omit unanswered fields; reuse evidence across equivalent fields.')
    providerReview: list[str] = Field(max_length=15)


class ZipAnswer(StrictModel):
    role: Literal['project', 'destination']
    raw: str = Field(min_length=1, max_length=100, description='Exact ZIP substring as spoken or typed; do not normalize it.')
    sourceTurnId: str


class Question(StrictModel):
    workItemId: str
    field: str


class RemovedItem(StrictModel):
    id: str
    sourceTurnId: str
    evidence: str = Field(min_length=1, max_length=1000)


class Result(StrictModel):
    displayText: str = Field(min_length=1, max_length=900)
    serviceCandidates: list[str] = Field(max_length=14, description='Only exact supportedCatalogLabels. For an unsupported trade use an empty list and preserve its name in serviceName.')
    workItems: list[WorkItem] = Field(max_length=8)
    removedWorkItems: list[RemovedItem] = Field(max_length=8)
    answerUpdates: list[Answer] = Field(max_length=15)  # shared logistics only
    zipUpdates: list[ZipAnswer] = Field(max_length=4)
    nextQuestion: list[Question] = Field(max_length=2, description='Only applicable decisions still unanswered after extracting the entire message. If any answer evidence supplies this intent, populate the equivalent field instead of asking again.')
    deferred: list[str] = Field(max_length=20)
    quoteBrief: str = Field(max_length=3000)
    action: Literal['ask', 'request_zip', 'ready', 'needs_provider_review']
    intent: Literal['request', 'pause', 'cancel', 'browse', 'resume']
    intentSourceTurnId: str
    intentEvidence: str = Field(min_length=1, max_length=1000)


class TurnRequest(StrictModel):
    conversationId: UUID = Field(strict=False)
    homeId: UUID = Field(strict=False)
    clientTurnId: UUID = Field(strict=False)
    expectedRevision: int = Field(ge=0)
    text: str = Field(min_length=1, max_length=4000)
    timeZone: str = Field(default='America/New_York', max_length=100)


class SubmitRequest(StrictModel):
    conversationId: UUID = Field(strict=False)
    homeId: UUID = Field(strict=False)
    expectedRevision: int = Field(ge=0)
    uploadRevision: UUID = Field(strict=False)


class ZipFact(ZipAnswer):
    normalized: str | None


class Handoff(StrictModel):
    originalRequest: str = Field(min_length=1, max_length=4000)
    workItems: list[WorkItem] = Field(min_length=1, max_length=8)
    sharedAnswers: list[Answer] = Field(max_length=15)
    zipAnswers: list[ZipFact] = Field(max_length=2)
    projectZip: str = Field(pattern=r'^[0-9]{5}(?:-[0-9]{4})?$')
    destinationZip: str | None
    promptVersion: str
    promptHash: str = Field(pattern=r'^[0-9a-f]{64}$')
    rubricVersion: str
    rubricHash: str = Field(pattern=r'^[0-9a-f]{64}$')
    deferred: list[str] = Field(max_length=20)
