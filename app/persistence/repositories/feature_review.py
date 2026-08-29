"""FeatureReviewRepository — persistence for FeatureReview, independent of
WorkflowState (a feature opportunity may exist long before any workflow
does). Mirrors WorkflowRepository's optimistic-concurrency shape
(create/update_if_version, VersionConflictError) rather than
SignalRepository/DetectionRepository/ResolutionRepository's simple upsert
— reviews are the one entity in this family whose transitions must be
atomic against concurrent writers (§18 of Level 3.4's task: two reviewers
racing to approve the same review must not silently clobber each other).
find_by_detection_id is the idempotency lookup: at most one FeatureReview
should ever exist per detection_id — see app.feature_review.service.
"""

from datetime import datetime
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, Field

from app.domain import FeatureReview, ReviewStatus


class FeatureReviewQuery(BaseModel):
    """Filter dimensions a future review-queue UI/API is expected to need
    — same shape/spirit as SignalQuery/DetectionQuery/ResolutionQuery."""

    status: ReviewStatus | None = None
    since: datetime | None = None
    until: datetime | None = None
    limit: int = Field(default=50, gt=0, le=500)


@runtime_checkable
class FeatureReviewRepository(Protocol):
    async def create(self, review: FeatureReview) -> FeatureReview:
        """Raises DuplicateEntityError if review_id already exists."""
        ...

    async def get(self, review_id: str) -> FeatureReview | None: ...

    async def find_by_detection_id(self, detection_id: str) -> FeatureReview | None:
        """The idempotency lookup (Level 3.4 §17): at most one FeatureReview
        per detection_id. app.feature_review.service checks this before
        create() so re-running detection/review creation over the same
        DetectionResult returns the existing review rather than a
        duplicate."""
        ...

    async def update_if_version(self, review_id: str, expected_version: int, updated_review: FeatureReview) -> FeatureReview:
        """Raises EntityNotFoundError if missing, VersionConflictError if
        the stored version != expected_version. On success, the stored
        version is expected_version + 1. The only way a review's status
        transitions — never a plain unconditional update()."""
        ...

    async def query(self, query: FeatureReviewQuery) -> list[FeatureReview]: ...
