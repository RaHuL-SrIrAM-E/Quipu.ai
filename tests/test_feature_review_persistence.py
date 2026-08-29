"""FeatureReviewRepository contract tests — in-memory implementation (used
for the normal suite) plus Firestore serialization round-trip checks that
don't require a live Firestore connection."""

from datetime import datetime, timedelta, timezone

import pytest

from app.domain import DecisionSource, FeatureReview, ReviewStatus, Ticket
from app.persistence.errors import DuplicateEntityError, EntityNotFoundError, VersionConflictError
from app.persistence.memory import InMemoryFeatureReviewRepository
from app.persistence.repositories.feature_review import FeatureReviewQuery, FeatureReviewRepository
from app.persistence.serialization import from_firestore_dict, to_firestore_dict

NOW = datetime.now(timezone.utc)


def make_review(**overrides) -> FeatureReview:
    defaults = dict(detection_id="det-1", status=ReviewStatus.PENDING, created_at=NOW)
    defaults.update(overrides)
    return FeatureReview(**defaults)


def test_in_memory_repository_satisfies_protocol():
    assert isinstance(InMemoryFeatureReviewRepository(), FeatureReviewRepository)


@pytest.mark.asyncio
async def test_create_and_get_roundtrip():
    repo = InMemoryFeatureReviewRepository()
    review = make_review()
    await repo.create(review)
    fetched = await repo.get(review.review_id)
    assert fetched == review


@pytest.mark.asyncio
async def test_get_missing_returns_none():
    repo = InMemoryFeatureReviewRepository()
    assert await repo.get("does-not-exist") is None


@pytest.mark.asyncio
async def test_create_duplicate_review_id_rejected():
    repo = InMemoryFeatureReviewRepository()
    review = make_review()
    await repo.create(review)
    with pytest.raises(DuplicateEntityError):
        await repo.create(review)


@pytest.mark.asyncio
async def test_create_returns_independent_copy():
    repo = InMemoryFeatureReviewRepository()
    review = make_review()
    created = await repo.create(review)
    created.status = ReviewStatus.APPROVED
    fetched = await repo.get(review.review_id)
    assert fetched.status == ReviewStatus.PENDING


@pytest.mark.asyncio
async def test_find_by_detection_id_returns_matching_review():
    repo = InMemoryFeatureReviewRepository()
    review = make_review(detection_id="det-42")
    await repo.create(review)
    found = await repo.find_by_detection_id("det-42")
    assert found is not None
    assert found.review_id == review.review_id


@pytest.mark.asyncio
async def test_find_by_detection_id_missing_returns_none():
    repo = InMemoryFeatureReviewRepository()
    assert await repo.find_by_detection_id("no-such-detection") is None


@pytest.mark.asyncio
async def test_update_if_version_transitions_status():
    repo = InMemoryFeatureReviewRepository()
    review = await repo.create(make_review())
    updated = review.model_copy(update={"status": ReviewStatus.APPROVED, "reviewer_id": "pm@company.com", "reviewer_type": DecisionSource.HUMAN})
    saved = await repo.update_if_version(review.review_id, review.version, updated)
    assert saved.status == ReviewStatus.APPROVED
    assert saved.version == review.version + 1


@pytest.mark.asyncio
async def test_update_if_version_missing_raises_not_found():
    repo = InMemoryFeatureReviewRepository()
    review = make_review()
    with pytest.raises(EntityNotFoundError):
        await repo.update_if_version(review.review_id, 1, review)


@pytest.mark.asyncio
async def test_update_if_version_stale_version_raises_conflict():
    repo = InMemoryFeatureReviewRepository()
    review = await repo.create(make_review())
    updated = review.model_copy(update={"status": ReviewStatus.APPROVED})
    await repo.update_if_version(review.review_id, review.version, updated)  # version now 2

    stale_update = review.model_copy(update={"status": ReviewStatus.REJECTED})
    with pytest.raises(VersionConflictError):
        await repo.update_if_version(review.review_id, review.version, stale_update)  # still claims version 1


@pytest.mark.asyncio
async def test_query_filters_by_status():
    repo = InMemoryFeatureReviewRepository()
    await repo.create(make_review(detection_id="det-1", status=ReviewStatus.PENDING))
    await repo.create(make_review(detection_id="det-2", status=ReviewStatus.APPROVED))
    results = await repo.query(FeatureReviewQuery(status=ReviewStatus.PENDING))
    assert len(results) == 1
    assert results[0].detection_id == "det-1"


@pytest.mark.asyncio
async def test_query_filters_by_time_range():
    repo = InMemoryFeatureReviewRepository()
    old = make_review(detection_id="det-1", created_at=NOW - timedelta(days=10))
    recent = make_review(detection_id="det-2", created_at=NOW)
    await repo.create(old)
    await repo.create(recent)
    results = await repo.query(FeatureReviewQuery(since=NOW - timedelta(days=1)))
    assert len(results) == 1
    assert results[0].detection_id == "det-2"


@pytest.mark.asyncio
async def test_query_orders_newest_first():
    repo = InMemoryFeatureReviewRepository()
    older = make_review(detection_id="det-1", created_at=NOW - timedelta(hours=1))
    newer = make_review(detection_id="det-2", created_at=NOW)
    await repo.create(older)
    await repo.create(newer)
    results = await repo.query(FeatureReviewQuery())
    assert results[0].detection_id == "det-2"


@pytest.mark.asyncio
async def test_query_respects_limit():
    repo = InMemoryFeatureReviewRepository()
    for i in range(5):
        await repo.create(make_review(detection_id=f"det-{i}"))
    results = await repo.query(FeatureReviewQuery(limit=2))
    assert len(results) == 2


def test_query_limit_bounds_enforced():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        FeatureReviewQuery(limit=0)
    with pytest.raises(ValidationError):
        FeatureReviewQuery(limit=10_000)


# ---- Firestore serialization (no live connection required) ------------------


def test_to_firestore_dict_normalizes_enums_and_nested_ticket():
    ticket = Ticket(title="X", description="Y", source_detection_id="det-1")
    review = make_review(status=ReviewStatus.APPROVED, reviewer_type=DecisionSource.HUMAN, ticket=ticket, ticket_id=ticket.ticket_id, reviewed_at=NOW)
    data = to_firestore_dict(review)
    assert data["status"] == "approved"
    assert data["reviewer_type"] == "human"
    assert isinstance(data["ticket"], dict)
    assert data["ticket"]["source_detection_id"] == "det-1"


def test_from_firestore_dict_roundtrips():
    # created_at is explicitly tz-aware here — Ticket (Level 1.1) still
    # defaults to naive datetime.utcnow(), and to_firestore_dict's boundary
    # coercion of that naive default would otherwise make this roundtrip
    # comparison fail for a reason unrelated to what this test checks.
    ticket = Ticket(title="X", description="Y", source_detection_id="det-1", created_at=NOW)
    review = make_review(status=ReviewStatus.APPROVED, reviewer_id="pm@company.com", reviewer_type=DecisionSource.HUMAN, reviewed_at=NOW, ticket=ticket, ticket_id=ticket.ticket_id)
    data = to_firestore_dict(review)
    restored = from_firestore_dict(FeatureReview, data)
    assert restored == review


def test_domain_feature_review_module_has_no_google_imports():
    from pathlib import Path

    domain_src = Path("app/domain/feature_review.py").read_text()
    assert "google.cloud" not in domain_src
    assert "firestore" not in domain_src.lower()

    service_src = Path("app/feature_review/service.py").read_text()
    assert "google.cloud" not in service_src
    assert "import firestore" not in service_src.lower()
