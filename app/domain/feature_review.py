"""FeatureReview — the controlled product-review boundary between
Detecting's AI interpretation and engineering execution. Framework-
independent (no Google SDK imports, no Jira client imports here — those
live in app/feature_review/service.py).

This is NOT another interpretation layer and NOT an agent — it is business
workflow/state management. Detecting already produced the interpretation
(`DetectionResult`, type `FEATURE_OPPORTUNITY`); FeatureReview only tracks
the separate, human decision about what to do with it:

    DetectionResult (FEATURE_OPPORTUNITY)  = "AI detected an opportunity"
    FeatureReview                          = "a human decided whether to
                                               act on it"                (this module)
    Ticket                                 = "the resulting engineering
                                               request, once approved"    (app.domain.ticket)

FeatureReview never mutates the DetectionResult it references — it only
stores `detection_id`. See app/feature_review/service.py for the state
machine (PENDING -> APPROVED / PENDING -> REJECTED, both terminal) and
concurrency semantics.
"""

import uuid
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field, field_validator

from app.domain.enums import DecisionSource, ReviewStatus
from app.domain.ticket import Ticket


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _require_aware(value: datetime | None) -> datetime | None:
    if value is not None and value.tzinfo is None:
        raise ValueError("timestamp must be timezone-aware (UTC)")
    return value


def _require_aware_not_none(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("timestamp must be timezone-aware (UTC)")
    return value


class FeatureReview(BaseModel):
    review_id: str = Field(default_factory=lambda: str(uuid.uuid4()))

    # Lineage to the upstream DetectionResult — never rewritten, never
    # embedded in full (see module docstring: FeatureReview references
    # Detecting's evidence by id only, it does not copy it).
    detection_id: str

    status: ReviewStatus = ReviewStatus.PENDING

    # Populated only once a human actually acts (approve/reject) — see
    # app.feature_review.service. reviewer_type reuses the existing
    # DecisionSource enum rather than inventing a redundant actor-identity
    # model; it must be HUMAN for approve()/reject() to succeed — an agent
    # (e.g. DetectingAgent itself) can never be the reviewer.
    reviewer_id: str | None = None
    reviewer_type: DecisionSource | None = None
    review_comment: str | None = None
    reviewed_at: datetime | None = None

    # Populated only on approval — see app.feature_review.service. `ticket`
    # is embedded directly (not a second TicketRepository — see
    # docs/architecture/feature_review.md "Ticket persistence" for why),
    # `ticket_id` is a denormalized top-level field for quick audit access
    # without unpacking the embedded object.
    ticket_id: str | None = None
    ticket: Ticket | None = None

    created_at: datetime = Field(default_factory=_utc_now)
    metadata: dict[str, Any] = Field(default_factory=dict)

    # Optimistic concurrency (same pattern as WorkflowState, Level 1.4):
    # every update must state which version it read, so two reviewers
    # racing to approve/reject the same review can't silently clobber each
    # other. See app.persistence.repositories.feature_review.
    version: int = Field(default=1, ge=1)

    _validate_reviewed_at = field_validator("reviewed_at")(_require_aware)
    _validate_created_at = field_validator("created_at")(_require_aware_not_none)

    @field_validator("detection_id")
    @classmethod
    def _not_empty(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("must not be empty")
        return value.strip()
