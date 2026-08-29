"""DetectionRepository contract tests — in-memory implementation (used for
the normal suite) plus Firestore serialization round-trip checks that don't
require a live Firestore connection (matching test_signal_persistence.py's
pattern)."""

from datetime import datetime, timedelta, timezone

import pytest

from app.domain import DetectionDomain, DetectionResult, DetectionType, SignalSeverity, compute_detection_fingerprint
from app.persistence.memory import InMemoryDetectionRepository
from app.persistence.repositories.detection import DetectionQuery, DetectionRepository
from app.persistence.serialization import from_firestore_dict, to_firestore_dict

NOW = datetime.now(timezone.utc)


def make_detection(**overrides) -> DetectionResult:
    signal_ids = overrides.pop("_signal_ids", ["sig-1", "sig-2"])
    defaults = dict(
        detection_type=DetectionType.INCIDENT,
        domain=DetectionDomain.OPERATIONAL,
        title="Probable incident",
        summary="Error rate spiked",
        rationale="Errors and latency rose together.",
        confidence=0.9,
        severity=SignalSeverity.CRITICAL,
        subject="quipu-api",
        supporting_signal_ids=signal_ids,
        observation_window_minutes=15,
        detected_at=NOW,
        fingerprint=compute_detection_fingerprint(detection_type=DetectionType.INCIDENT, subject="quipu-api", supporting_signal_ids=signal_ids, window_minutes=15),
    )
    defaults.update(overrides)
    return DetectionResult(**defaults)


def test_in_memory_repository_satisfies_protocol():
    assert isinstance(InMemoryDetectionRepository(), DetectionRepository)


@pytest.mark.asyncio
async def test_save_and_get_roundtrip():
    repo = InMemoryDetectionRepository()
    detection = make_detection()
    await repo.save(detection)
    fetched = await repo.get(detection.detection_id)
    assert fetched == detection


@pytest.mark.asyncio
async def test_get_missing_returns_none():
    repo = InMemoryDetectionRepository()
    assert await repo.get("does-not-exist") is None


@pytest.mark.asyncio
async def test_save_returns_independent_copy():
    repo = InMemoryDetectionRepository()
    detection = make_detection()
    saved = await repo.save(detection)
    saved.summary = "mutated locally"
    fetched = await repo.get(detection.detection_id)
    assert fetched.summary == "Error rate spiked"


@pytest.mark.asyncio
async def test_find_by_fingerprint_returns_matching_detection():
    repo = InMemoryDetectionRepository()
    detection = make_detection()
    await repo.save(detection)
    found = await repo.find_by_fingerprint(detection.fingerprint)
    assert found is not None
    assert found.detection_id == detection.detection_id


@pytest.mark.asyncio
async def test_find_by_fingerprint_missing_returns_none():
    repo = InMemoryDetectionRepository()
    assert await repo.find_by_fingerprint("no-such-fingerprint") is None


@pytest.mark.asyncio
async def test_repeated_detection_same_evidence_has_same_fingerprint():
    repo = InMemoryDetectionRepository()
    first = make_detection()
    duplicate = make_detection()  # fresh detection_id, same fingerprint (same type/subject/signals/window)
    assert first.fingerprint == duplicate.fingerprint
    await repo.save(first)
    existing = await repo.find_by_fingerprint(first.fingerprint)
    assert existing.detection_id == first.detection_id


@pytest.mark.asyncio
async def test_query_filters_by_detection_type():
    repo = InMemoryDetectionRepository()
    await repo.save(make_detection(detection_type=DetectionType.INCIDENT))
    await repo.save(
        make_detection(
            detection_type=DetectionType.FEATURE_OPPORTUNITY,
            domain=DetectionDomain.PRODUCT,
            severity=None,
            _signal_ids=["sig-3"],
            fingerprint=compute_detection_fingerprint(detection_type=DetectionType.FEATURE_OPPORTUNITY, subject="quipu-api", supporting_signal_ids=["sig-3"], window_minutes=15),
        )
    )
    results = await repo.query(DetectionQuery(detection_type=DetectionType.INCIDENT))
    assert len(results) == 1
    assert results[0].detection_type == DetectionType.INCIDENT


@pytest.mark.asyncio
async def test_query_filters_by_domain():
    repo = InMemoryDetectionRepository()
    await repo.save(make_detection(domain=DetectionDomain.OPERATIONAL))
    await repo.save(
        make_detection(
            domain=DetectionDomain.PRODUCT,
            _signal_ids=["sig-9"],
            fingerprint=compute_detection_fingerprint(detection_type=DetectionType.INCIDENT, subject="quipu-api", supporting_signal_ids=["sig-9"], window_minutes=15),
        )
    )
    results = await repo.query(DetectionQuery(domain=DetectionDomain.PRODUCT))
    assert len(results) == 1
    assert results[0].domain == DetectionDomain.PRODUCT


@pytest.mark.asyncio
async def test_query_filters_by_service_name_and_environment():
    repo = InMemoryDetectionRepository()
    await repo.save(make_detection(service_name="quipu-api", environment="production"))
    await repo.save(
        make_detection(
            service_name="quipu-worker",
            environment="staging",
            _signal_ids=["sig-x"],
            fingerprint=compute_detection_fingerprint(detection_type=DetectionType.INCIDENT, subject="quipu-api", supporting_signal_ids=["sig-x"], window_minutes=15),
        )
    )
    results = await repo.query(DetectionQuery(service_name="quipu-api", environment="production"))
    assert len(results) == 1


@pytest.mark.asyncio
async def test_query_filters_by_time_range():
    repo = InMemoryDetectionRepository()
    old = make_detection(detected_at=NOW - timedelta(days=10))
    recent = make_detection(
        detected_at=NOW,
        _signal_ids=["sig-recent"],
        fingerprint=compute_detection_fingerprint(detection_type=DetectionType.INCIDENT, subject="quipu-api", supporting_signal_ids=["sig-recent"], window_minutes=15),
    )
    await repo.save(old)
    await repo.save(recent)
    results = await repo.query(DetectionQuery(since=NOW - timedelta(days=1)))
    assert len(results) == 1
    assert results[0].detection_id == recent.detection_id


@pytest.mark.asyncio
async def test_query_orders_newest_first():
    repo = InMemoryDetectionRepository()
    older = make_detection(detected_at=NOW - timedelta(hours=1))
    newer = make_detection(
        detected_at=NOW,
        _signal_ids=["sig-newer"],
        fingerprint=compute_detection_fingerprint(detection_type=DetectionType.INCIDENT, subject="quipu-api", supporting_signal_ids=["sig-newer"], window_minutes=15),
    )
    await repo.save(older)
    await repo.save(newer)
    results = await repo.query(DetectionQuery())
    assert results[0].detection_id == newer.detection_id


@pytest.mark.asyncio
async def test_query_respects_limit():
    repo = InMemoryDetectionRepository()
    for i in range(5):
        await repo.save(
            make_detection(
                _signal_ids=[f"sig-{i}"],
                fingerprint=compute_detection_fingerprint(detection_type=DetectionType.INCIDENT, subject="quipu-api", supporting_signal_ids=[f"sig-{i}"], window_minutes=15),
            )
        )
    results = await repo.query(DetectionQuery(limit=2))
    assert len(results) == 2


def test_query_limit_bounds_enforced():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        DetectionQuery(limit=0)
    with pytest.raises(ValidationError):
        DetectionQuery(limit=10_000)


# ---- Firestore serialization (no live connection required) ------------------


def test_to_firestore_dict_normalizes_enums_to_values():
    detection = make_detection()
    data = to_firestore_dict(detection)
    assert data["detection_type"] == "incident"
    assert data["domain"] == "operational"
    assert data["severity"] == "critical"


def test_to_firestore_dict_datetime_is_timezone_aware():
    detection = make_detection()
    data = to_firestore_dict(detection)
    assert data["detected_at"].tzinfo is not None


def test_from_firestore_dict_roundtrips():
    detection = make_detection(knowledge_references=["doc-1"])
    data = to_firestore_dict(detection)
    restored = from_firestore_dict(DetectionResult, data)
    assert restored == detection


def test_firestore_repository_module_is_only_place_importing_firestore_sdk_for_detection():
    from pathlib import Path

    domain_src = Path("app/domain/detection.py").read_text()
    assert "google.cloud" not in domain_src
    assert "firestore" not in domain_src.lower()

    gateway_src = Path("app/agent_runtime/gateways/detections.py").read_text()
    assert "google.cloud" not in gateway_src
