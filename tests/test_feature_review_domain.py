"""Domain-level tests for FeatureReview (Level 3.4, extended Level 3.5). No
persistence, no service — just the model."""

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from app.domain import DecisionSource, FeatureReview, ReviewStatus, Ticket


def make_review(**overrides) -> FeatureReview:
    defaults = dict(detection_id="det-1", status=ReviewStatus.PENDING)
    defaults.update(overrides)
    return FeatureReview(**defaults)


def test_valid_pending_review():
    review = make_review()
    assert review.status == ReviewStatus.PENDING
    assert review.reviewer_id is None
    assert review.ticket is None


def test_valid_approved_review():
    ticket = Ticket(title="[Feature Opportunity] X", description="Y", source_detection_id="det-1")
    review = make_review(
        status=ReviewStatus.APPROVED,
        reviewer_id="pm@company.com",
        reviewer_type=DecisionSource.HUMAN,
        reviewed_at=datetime.now(timezone.utc),
        ticket=ticket,
        ticket_id=ticket.ticket_id,
    )
    assert review.status == ReviewStatus.APPROVED
    assert review.ticket.ticket_id == review.ticket_id


def test_valid_rejected_review():
    review = make_review(
        status=ReviewStatus.REJECTED, reviewer_id="pm@company.com", reviewer_type=DecisionSource.HUMAN, review_comment="not now", reviewed_at=datetime.now(timezone.utc)
    )
    assert review.status == ReviewStatus.REJECTED
    assert review.review_comment == "not now"


def test_invalid_status_rejected():
    with pytest.raises(ValidationError):
        make_review(status="in_progress")


def test_invalid_reviewer_type_rejected():
    with pytest.raises(ValidationError):
        make_review(reviewer_type="not_a_real_source")


def test_reviewer_type_reuses_decision_source_enum():
    review = make_review(reviewer_type=DecisionSource.HUMAN)
    assert isinstance(review.reviewer_type, DecisionSource)


def test_empty_detection_id_rejected():
    with pytest.raises(ValidationError):
        make_review(detection_id="")


def test_naive_reviewed_at_rejected():
    with pytest.raises(ValidationError):
        make_review(reviewed_at=datetime.now())  # noqa: DTZ005


def test_naive_created_at_rejected():
    with pytest.raises(ValidationError):
        make_review(created_at=datetime.now())  # noqa: DTZ005


def test_created_at_defaults_to_aware_utc():
    review = make_review()
    assert review.created_at.tzinfo is not None


def test_reviewed_at_none_by_default():
    review = make_review()
    assert review.reviewed_at is None


def test_ticket_association_preserved():
    ticket = Ticket(title="X", description="Y", source_detection_id="det-1")
    review = make_review(ticket=ticket, ticket_id=ticket.ticket_id)
    assert review.ticket_id == ticket.ticket_id
    assert review.ticket.source_detection_id == "det-1"


def test_ticket_provenance_field_on_ticket_model():
    """Level 3.4's additive Ticket field."""
    ticket = Ticket(title="X", description="Y", source_detection_id="det-42")
    assert ticket.source_detection_id == "det-42"


def test_ticket_source_detection_id_defaults_none():
    """Existing SDLC-created tickets (with no feature-review origin) are
    unaffected by the additive field."""
    ticket = Ticket(title="X", description="Y")
    assert ticket.source_detection_id is None


def test_version_defaults_to_one():
    review = make_review()
    assert review.version == 1


def test_version_must_be_at_least_one():
    with pytest.raises(ValidationError):
        make_review(version=0)


def test_review_distinct_from_detection_and_signal_and_ticket():
    from app.domain import DetectionResult, Signal

    assert FeatureReview is not DetectionResult
    assert FeatureReview is not Signal
    assert FeatureReview is not Ticket
    assert "signal_type" not in FeatureReview.model_fields
    assert "detection_type" not in FeatureReview.model_fields


# ---- Level 3.5: workflow_id (Feature -> SDLC integration) --------------------


def test_workflow_id_defaults_none():
    review = make_review()
    assert review.workflow_id is None


def test_workflow_id_can_be_set():
    review = make_review(workflow_id="wf-123")
    assert review.workflow_id == "wf-123"
