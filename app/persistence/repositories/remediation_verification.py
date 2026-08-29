"""RemediationVerificationRepository — persistence for
RemediationVerification, independent of WorkflowState's own artifact/
execution storage (a verification record references a workflow_id but
isn't stored inside it — same top-level-collection rationale as
Signal/DetectionResult/ResolutionResult/FeatureReview).

Mirrors FeatureReviewRepository's optimistic-concurrency shape
(create/update_if_version, VersionConflictError) rather than Signal/
Detection/Resolution's simple upsert: a verification record has a real
IN_PROGRESS -> COMPLETED transition (see
app.domain.remediation_verification.VerificationStatus) that must be
version-checked, not blindly overwritten — the same reason FeatureReview
needed this shape (Level 3.4 §18) applies here (concurrent verification
attempts for the same deployment, §12/§15 of this task).

find_by_idempotency_key is the idempotency lookup (§12): at most one
RemediationVerification should ever exist per (resolution_id,
deployment_artifact_id, revision, verification_window) — see
app.domain.remediation_verification.compute_verification_key and
app.verification.service.
"""

from datetime import datetime
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, Field

from app.domain import RemediationVerification, VerificationOutcome, VerificationStatus


class RemediationVerificationQuery(BaseModel):
    """Filter dimensions a future incident dashboard is expected to need —
    same shape/spirit as DetectionQuery/ResolutionQuery/FeatureReviewQuery."""

    outcome: VerificationOutcome | None = None
    status: VerificationStatus | None = None
    since: datetime | None = None
    until: datetime | None = None
    limit: int = Field(default=50, gt=0, le=500)


@runtime_checkable
class RemediationVerificationRepository(Protocol):
    async def create(self, verification: RemediationVerification) -> RemediationVerification:
        """Raises DuplicateEntityError if verification_id already exists —
        the create()-level race guard for two concurrent
        verify_remediation() calls computing the same deterministic
        verification_id (see compute_verification_key)."""
        ...

    async def get(self, verification_id: str) -> RemediationVerification | None: ...

    async def find_by_resolution(self, resolution_id: str) -> list[RemediationVerification]:
        """Every verification attempt ever made for a given resolution
        (there can legitimately be more than one — a new deployment for
        the same resolution gets a new deployment_artifact_id/revision and
        therefore a new idempotency key)."""
        ...

    async def find_by_idempotency_key(self, idempotency_key: str) -> RemediationVerification | None:
        """The idempotency lookup (§12): at most one RemediationVerification
        per (resolution_id, deployment_artifact_id, revision, window)."""
        ...

    async def update_if_version(
        self, verification_id: str, expected_version: int, updated_verification: RemediationVerification
    ) -> RemediationVerification:
        """Raises EntityNotFoundError if missing, VersionConflictError if
        the stored version != expected_version. The only way a
        verification's status/outcome transitions from IN_PROGRESS to
        COMPLETED."""
        ...

    async def query(self, query: RemediationVerificationQuery) -> list[RemediationVerification]: ...
