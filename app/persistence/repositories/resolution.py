"""ResolutionRepository — persistence for ResolutionResult, independent of
WorkflowState. Same rationale as DetectionRepository/SignalRepository: a
ResolutionResult isn't scoped to a workflow — it's Incident Resolution's
diagnosis of a DetectionResult, itself not workflow-scoped either.
"""

from datetime import datetime
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, Field

from app.domain import RemediationRisk, RemediationStrategy, ResolutionResult


class ResolutionQuery(BaseModel):
    """Filter dimensions a future orchestration-reaction flow is expected
    to need — same shape/spirit as SignalQuery/DetectionQuery."""

    detection_id: str | None = None
    remediation_strategy: RemediationStrategy | None = None
    risk: RemediationRisk | None = None
    since: datetime | None = None
    until: datetime | None = None
    limit: int = Field(default=50, gt=0, le=500)


@runtime_checkable
class ResolutionRepository(Protocol):
    async def save(self, resolution: ResolutionResult) -> ResolutionResult:
        """Create-or-replace by resolution_id — same upsert pattern as
        ArtifactRepository.save/SignalRepository.save/DetectionRepository.save."""
        ...

    async def get(self, resolution_id: str) -> ResolutionResult | None: ...

    async def find_by_fingerprint(self, fingerprint: str) -> ResolutionResult | None:
        """The deduplication boundary (Level 3.3 §27): IncidentResolutionAgent
        checks this before save() to avoid an uncontrolled duplicate
        ResolutionResult for a DetectionResult it has already diagnosed. See
        app.domain.resolution.compute_resolution_fingerprint."""
        ...

    async def query(self, query: ResolutionQuery) -> list[ResolutionResult]: ...
