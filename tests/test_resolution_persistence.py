"""ResolutionRepository contract tests — in-memory implementation (used for
the normal suite) plus Firestore serialization round-trip checks that don't
require a live Firestore connection."""

from datetime import datetime, timedelta, timezone

import pytest

from app.domain import RemediationRisk, RemediationStrategy, ResolutionResult, SignalSeverity, compute_resolution_fingerprint
from app.persistence.memory import InMemoryResolutionRepository
from app.persistence.repositories.resolution import ResolutionQuery, ResolutionRepository
from app.persistence.serialization import from_firestore_dict, to_firestore_dict

NOW = datetime.now(timezone.utc)


def make_resolution(**overrides) -> ResolutionResult:
    detection_id = overrides.pop("detection_id", "det-1")
    strategy = overrides.get("remediation_strategy", RemediationStrategy.CODE_FIX)
    defaults = dict(
        detection_id=detection_id,
        diagnosis_summary="Application defect.",
        probable_root_cause="NPE in handler",
        root_cause_confidence=0.85,
        remediation_strategy=strategy,
        remediation_rationale="Errors correlate with recent deployment.",
        expected_outcome="Errors resolve.",
        verification_strategy="Monitor error rate.",
        risk=RemediationRisk.LOW,
        severity=SignalSeverity.CRITICAL,
        target_agent="codegen_agent",
        resolved_at=NOW,
        fingerprint=compute_resolution_fingerprint(detection_id=detection_id, remediation_strategy=strategy, subject="quipu-api"),
    )
    defaults.update(overrides)
    return ResolutionResult(**defaults)


def test_in_memory_repository_satisfies_protocol():
    assert isinstance(InMemoryResolutionRepository(), ResolutionRepository)


@pytest.mark.asyncio
async def test_save_and_get_roundtrip():
    repo = InMemoryResolutionRepository()
    resolution = make_resolution()
    await repo.save(resolution)
    fetched = await repo.get(resolution.resolution_id)
    assert fetched == resolution


@pytest.mark.asyncio
async def test_get_missing_returns_none():
    repo = InMemoryResolutionRepository()
    assert await repo.get("does-not-exist") is None


@pytest.mark.asyncio
async def test_save_returns_independent_copy():
    repo = InMemoryResolutionRepository()
    resolution = make_resolution()
    saved = await repo.save(resolution)
    saved.diagnosis_summary = "mutated locally"
    fetched = await repo.get(resolution.resolution_id)
    assert fetched.diagnosis_summary == "Application defect."


@pytest.mark.asyncio
async def test_find_by_fingerprint_returns_matching_resolution():
    repo = InMemoryResolutionRepository()
    resolution = make_resolution()
    await repo.save(resolution)
    found = await repo.find_by_fingerprint(resolution.fingerprint)
    assert found is not None
    assert found.resolution_id == resolution.resolution_id


@pytest.mark.asyncio
async def test_find_by_fingerprint_missing_returns_none():
    repo = InMemoryResolutionRepository()
    assert await repo.find_by_fingerprint("no-such-fingerprint") is None


@pytest.mark.asyncio
async def test_repeated_resolution_same_detection_and_strategy_has_same_fingerprint():
    repo = InMemoryResolutionRepository()
    first = make_resolution()
    duplicate = make_resolution()  # fresh resolution_id, same fingerprint
    assert first.fingerprint == duplicate.fingerprint
    await repo.save(first)
    existing = await repo.find_by_fingerprint(first.fingerprint)
    assert existing.resolution_id == first.resolution_id


@pytest.mark.asyncio
async def test_query_filters_by_detection_id():
    repo = InMemoryResolutionRepository()
    await repo.save(make_resolution(detection_id="det-1"))
    await repo.save(make_resolution(detection_id="det-2"))
    results = await repo.query(ResolutionQuery(detection_id="det-1"))
    assert len(results) == 1
    assert results[0].detection_id == "det-1"


@pytest.mark.asyncio
async def test_query_filters_by_remediation_strategy():
    repo = InMemoryResolutionRepository()
    await repo.save(make_resolution(detection_id="det-1", remediation_strategy=RemediationStrategy.CODE_FIX))
    await repo.save(make_resolution(detection_id="det-2", remediation_strategy=RemediationStrategy.ESCALATE, target_agent=None))
    results = await repo.query(ResolutionQuery(remediation_strategy=RemediationStrategy.ESCALATE))
    assert len(results) == 1
    assert results[0].remediation_strategy == RemediationStrategy.ESCALATE


@pytest.mark.asyncio
async def test_query_filters_by_risk():
    repo = InMemoryResolutionRepository()
    await repo.save(make_resolution(detection_id="det-1", risk=RemediationRisk.HIGH))
    await repo.save(make_resolution(detection_id="det-2", risk=RemediationRisk.LOW))
    results = await repo.query(ResolutionQuery(risk=RemediationRisk.HIGH))
    assert len(results) == 1


@pytest.mark.asyncio
async def test_query_filters_by_time_range():
    repo = InMemoryResolutionRepository()
    old = make_resolution(detection_id="det-1", resolved_at=NOW - timedelta(days=10))
    recent = make_resolution(detection_id="det-2", resolved_at=NOW)
    await repo.save(old)
    await repo.save(recent)
    results = await repo.query(ResolutionQuery(since=NOW - timedelta(days=1)))
    assert len(results) == 1
    assert results[0].resolution_id == recent.resolution_id


@pytest.mark.asyncio
async def test_query_orders_newest_first():
    repo = InMemoryResolutionRepository()
    older = make_resolution(detection_id="det-1", resolved_at=NOW - timedelta(hours=1))
    newer = make_resolution(detection_id="det-2", resolved_at=NOW)
    await repo.save(older)
    await repo.save(newer)
    results = await repo.query(ResolutionQuery())
    assert results[0].resolution_id == newer.resolution_id


@pytest.mark.asyncio
async def test_query_respects_limit():
    repo = InMemoryResolutionRepository()
    for i in range(5):
        await repo.save(make_resolution(detection_id=f"det-{i}"))
    results = await repo.query(ResolutionQuery(limit=2))
    assert len(results) == 2


def test_query_limit_bounds_enforced():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        ResolutionQuery(limit=0)
    with pytest.raises(ValidationError):
        ResolutionQuery(limit=10_000)


# ---- Firestore serialization (no live connection required) ------------------


def test_to_firestore_dict_normalizes_enums_to_values():
    resolution = make_resolution()
    data = to_firestore_dict(resolution)
    assert data["remediation_strategy"] == "code_fix"
    assert data["risk"] == "low"
    assert data["severity"] == "critical"


def test_to_firestore_dict_datetime_is_timezone_aware():
    resolution = make_resolution()
    data = to_firestore_dict(resolution)
    assert data["resolved_at"].tzinfo is not None


def test_from_firestore_dict_roundtrips():
    resolution = make_resolution(knowledge_references=["doc-1"], root_cause_candidates=["cause A"])
    data = to_firestore_dict(resolution)
    restored = from_firestore_dict(ResolutionResult, data)
    assert restored == resolution


def test_domain_resolution_module_has_no_google_imports():
    from pathlib import Path

    domain_src = Path("app/domain/resolution.py").read_text()
    assert "google.cloud" not in domain_src
    assert "firestore" not in domain_src.lower()

    gateway_src = Path("app/agent_runtime/gateways/resolutions.py").read_text()
    assert "google.cloud" not in gateway_src
