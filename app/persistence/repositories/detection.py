"""DetectionRepository — persistence for DetectionResult, independent of
WorkflowState. Same rationale as SignalRepository (app.persistence.
repositories.signal): a DetectionResult isn't scoped to a workflow — it's
Detecting's interpretation of Signals that themselves exist before any
workflow does — so this has no workflow_id parameter anywhere.
"""

from datetime import datetime
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, Field

from app.domain import DetectionDomain, DetectionResult, DetectionType


class DetectionQuery(BaseModel):
    """Filter dimensions a future Incident Resolution / Feature review flow
    is expected to need — same shape/spirit as SignalQuery. All fields are
    optional equality/range filters, ANDed together; `limit` bounds the
    result size."""

    detection_type: DetectionType | None = None
    domain: DetectionDomain | None = None
    service_name: str | None = None
    environment: str | None = None
    since: datetime | None = None
    until: datetime | None = None
    limit: int = Field(default=50, gt=0, le=500)


@runtime_checkable
class DetectionRepository(Protocol):
    async def save(self, detection: DetectionResult) -> DetectionResult:
        """Create-or-replace by detection_id — same upsert pattern as
        ArtifactRepository.save/SignalRepository.save."""
        ...

    async def get(self, detection_id: str) -> DetectionResult | None: ...

    async def find_by_fingerprint(self, fingerprint: str) -> DetectionResult | None:
        """The deduplication boundary (Level 3.2 §21): DetectingAgent checks
        this before save() to avoid an uncontrolled duplicate DetectionResult
        for evidence it has already interpreted. See
        app.domain.detection.compute_detection_fingerprint."""
        ...

    async def query(self, query: DetectionQuery) -> list[DetectionResult]: ...
