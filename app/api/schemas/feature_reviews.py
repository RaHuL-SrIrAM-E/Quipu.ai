"""FeatureReview-facing response schemas, plus the request bodies for the
approve/reject commands. Approval/rejection identity
(`reviewer_id`/`reviewer_type`) is never taken from these request bodies
alone — `reviewer_type` is always fixed server-side to DecisionSource.HUMAN
by the route handler (see app/api/routes/feature_reviews.py) and
`reviewer_id` is cross-checked against the caller's authenticated identity
(app/api/auth.py) — this schema exists only to accept an optional
`review_comment`, never a privilege claim.
"""

from datetime import datetime

from pydantic import BaseModel, Field

from app.domain import FeatureReview


class FeatureReviewSummary(BaseModel):
    review_id: str
    detection_id: str
    status: str
    reviewer_id: str | None
    reviewer_type: str | None
    review_comment: str | None
    reviewed_at: datetime | None
    ticket_id: str | None
    ticket_title: str | None
    workflow_id: str | None
    created_at: datetime

    @classmethod
    def from_domain(cls, review: FeatureReview) -> "FeatureReviewSummary":
        return cls(
            review_id=review.review_id,
            detection_id=review.detection_id,
            status=review.status.value,
            reviewer_id=review.reviewer_id,
            reviewer_type=review.reviewer_type.value if review.reviewer_type else None,
            review_comment=review.review_comment,
            reviewed_at=review.reviewed_at,
            ticket_id=review.ticket_id,
            ticket_title=review.ticket.title if review.ticket else None,
            workflow_id=review.workflow_id,
            created_at=review.created_at,
        )


class ReviewDecisionRequest(BaseModel):
    """Body for POST /feature-reviews/{review_id}/approve|reject. No
    reviewer_type field exists here on purpose — see module docstring."""

    review_comment: str | None = Field(default=None, max_length=2000)
