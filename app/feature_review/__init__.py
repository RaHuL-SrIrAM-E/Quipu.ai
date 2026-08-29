"""Feature Review — the controlled product-review boundary between
Detecting's AI interpretation and engineering execution (Level 3.4).

Deliberately not an agent package: no ADK, no Gemini anywhere here. See
app/feature_review/service.py and docs/architecture/feature_review.md.
"""

from app.feature_review.service import (
    DetectionNotFoundError,
    FeatureReviewError,
    FeatureReviewService,
    InsufficientEvidenceError,
    InvalidDetectionTypeError,
    InvalidReviewTransitionError,
    ReviewNotFoundError,
    TicketCreationFailedError,
    UnauthorizedReviewerError,
)

__all__ = [
    "DetectionNotFoundError",
    "FeatureReviewError",
    "FeatureReviewService",
    "InsufficientEvidenceError",
    "InvalidDetectionTypeError",
    "InvalidReviewTransitionError",
    "ReviewNotFoundError",
    "TicketCreationFailedError",
    "UnauthorizedReviewerError",
]
