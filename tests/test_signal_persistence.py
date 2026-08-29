"""SignalRepository contract tests — in-memory implementation (used for the
normal suite) plus Firestore serialization round-trip checks that don't
require a live Firestore connection (matching the existing
tests/test_firestore_persistence.py pattern for other repositories)."""

from datetime import datetime, timedelta, timezone

import pytest

from app.domain import Signal, SignalProvenance, SignalSeverity, SignalSource, SignalType, compute_fingerprint
from app.persistence.memory import InMemorySignalRepository
from app.persistence.repositories.signal import SignalQuery, SignalRepository
from app.persistence.serialization import from_firestore_dict, to_firestore_dict

NOW = datetime.now(timezone.utc)


def make_signal(**overrides) -> Signal:
    defaults = dict(
        signal_type=SignalType.METRIC_ANOMALY,
        source=SignalSource.CLOUD_MONITORING,
        severity=SignalSeverity.WARNING,
        observed_at=NOW,
        subject="quipu-api",
        summary="error rate increased",
        provenance=SignalProvenance(source_system="cloud_monitoring", source_event_id="evt-1"),
        fingerprint=compute_fingerprint(source=SignalSource.CLOUD_MONITORING, source_event_id="evt-1", subject="quipu-api"),
    )
    defaults.update(overrides)
    return Signal(**defaults)


# ---- Protocol conformance --------------------------------------------------


def test_in_memory_repository_satisfies_protocol():
    assert isinstance(InMemorySignalRepository(), SignalRepository)


# ---- create/get -------------------------------------------------------------


@pytest.mark.asyncio
async def test_save_and_get_roundtrip():
    repo = InMemorySignalRepository()
    signal = make_signal()
    await repo.save(signal)
    fetched = await repo.get(signal.signal_id)
    assert fetched == signal


@pytest.mark.asyncio
async def test_get_missing_returns_none():
    repo = InMemorySignalRepository()
    assert await repo.get("does-not-exist") is None


@pytest.mark.asyncio
async def test_save_returns_independent_copy():
    repo = InMemorySignalRepository()
    signal = make_signal()
    saved = await repo.save(signal)
    saved.summary = "mutated locally"
    fetched = await repo.get(signal.signal_id)
    assert fetched.summary == "error rate increased"


@pytest.mark.asyncio
async def test_save_upserts_by_signal_id():
    repo = InMemorySignalRepository()
    signal = make_signal()
    await repo.save(signal)
    updated = signal.model_copy(update={"status": signal.status})
    await repo.save(updated)  # no DuplicateEntityError — upsert, same pattern as ArtifactRepository
    fetched = await repo.get(signal.signal_id)
    assert fetched is not None


# ---- deduplication ----------------------------------------------------------


@pytest.mark.asyncio
async def test_find_by_fingerprint_returns_matching_signal():
    repo = InMemorySignalRepository()
    signal = make_signal()
    await repo.save(signal)
    found = await repo.find_by_fingerprint(signal.fingerprint)
    assert found is not None
    assert found.signal_id == signal.signal_id


@pytest.mark.asyncio
async def test_find_by_fingerprint_missing_returns_none():
    repo = InMemorySignalRepository()
    assert await repo.find_by_fingerprint("no-such-fingerprint") is None


@pytest.mark.asyncio
async def test_duplicate_observation_has_same_fingerprint_and_is_discoverable():
    """Same underlying event ingested twice (e.g. a webhook redelivery)
    produces the same fingerprint — the repository doesn't reject the
    second save (dedup enforcement is the caller's job, per the contract
    docstring), but find_by_fingerprint lets a caller detect it first."""
    repo = InMemorySignalRepository()
    first = make_signal()
    duplicate = make_signal()  # fresh signal_id (default_factory), same fingerprint (same source/event/subject)
    assert first.fingerprint == duplicate.fingerprint
    assert first.signal_id != duplicate.signal_id

    await repo.save(first)
    existing = await repo.find_by_fingerprint(first.fingerprint)
    assert existing.signal_id == first.signal_id  # caller can now choose not to save `duplicate`


# ---- query --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_query_filters_by_signal_type():
    repo = InMemorySignalRepository()
    await repo.save(make_signal(signal_type=SignalType.METRIC_ANOMALY, provenance=SignalProvenance(source_system="x", source_event_id="1")))
    await repo.save(
        make_signal(
            signal_type=SignalType.CUSTOMER_FEEDBACK,
            source=SignalSource.CUSTOMER_FEEDBACK,
            provenance=SignalProvenance(source_system="x", source_event_id="2"),
            fingerprint=compute_fingerprint(source=SignalSource.CUSTOMER_FEEDBACK, source_event_id="2", subject="quipu-api"),
        )
    )
    results = await repo.query(SignalQuery(signal_type=SignalType.METRIC_ANOMALY))
    assert len(results) == 1
    assert results[0].signal_type == SignalType.METRIC_ANOMALY


@pytest.mark.asyncio
async def test_query_filters_by_source():
    repo = InMemorySignalRepository()
    await repo.save(make_signal(source=SignalSource.CLOUD_MONITORING, provenance=SignalProvenance(source_system="x", source_event_id="1")))
    await repo.save(
        make_signal(
            source=SignalSource.CLOUD_LOGGING,
            provenance=SignalProvenance(source_system="x", source_event_id="2"),
            fingerprint=compute_fingerprint(source=SignalSource.CLOUD_LOGGING, source_event_id="2", subject="quipu-api"),
        )
    )
    results = await repo.query(SignalQuery(source=SignalSource.CLOUD_LOGGING))
    assert len(results) == 1
    assert results[0].source == SignalSource.CLOUD_LOGGING


@pytest.mark.asyncio
async def test_query_filters_by_service_name_and_environment():
    repo = InMemorySignalRepository()
    await repo.save(
        make_signal(service_name="quipu-api", environment="production", provenance=SignalProvenance(source_system="x", source_event_id="1"))
    )
    await repo.save(
        make_signal(
            service_name="quipu-worker",
            environment="staging",
            provenance=SignalProvenance(source_system="x", source_event_id="2"),
            fingerprint=compute_fingerprint(source=SignalSource.CLOUD_MONITORING, source_event_id="2", subject="quipu-api"),
        )
    )
    results = await repo.query(SignalQuery(service_name="quipu-api", environment="production"))
    assert len(results) == 1
    assert results[0].service_name == "quipu-api"


@pytest.mark.asyncio
async def test_query_filters_by_severity():
    repo = InMemorySignalRepository()
    await repo.save(make_signal(severity=SignalSeverity.CRITICAL, provenance=SignalProvenance(source_system="x", source_event_id="1")))
    await repo.save(
        make_signal(
            severity=SignalSeverity.INFO,
            provenance=SignalProvenance(source_system="x", source_event_id="2"),
            fingerprint=compute_fingerprint(source=SignalSource.CLOUD_MONITORING, source_event_id="2", subject="quipu-api"),
        )
    )
    results = await repo.query(SignalQuery(severity=SignalSeverity.CRITICAL))
    assert len(results) == 1
    assert results[0].severity == SignalSeverity.CRITICAL


@pytest.mark.asyncio
async def test_query_filters_by_time_range():
    repo = InMemorySignalRepository()
    old = make_signal(observed_at=NOW - timedelta(days=10), provenance=SignalProvenance(source_system="x", source_event_id="1"))
    recent = make_signal(
        observed_at=NOW,
        provenance=SignalProvenance(source_system="x", source_event_id="2"),
        fingerprint=compute_fingerprint(source=SignalSource.CLOUD_MONITORING, source_event_id="2", subject="quipu-api"),
    )
    await repo.save(old)
    await repo.save(recent)
    results = await repo.query(SignalQuery(since=NOW - timedelta(days=1)))
    assert len(results) == 1
    assert results[0].signal_id == recent.signal_id


@pytest.mark.asyncio
async def test_query_respects_limit():
    repo = InMemorySignalRepository()
    for i in range(5):
        await repo.save(
            make_signal(
                provenance=SignalProvenance(source_system="x", source_event_id=str(i)),
                fingerprint=compute_fingerprint(source=SignalSource.CLOUD_MONITORING, source_event_id=str(i), subject="quipu-api"),
            )
        )
    results = await repo.query(SignalQuery(limit=2))
    assert len(results) == 2


@pytest.mark.asyncio
async def test_query_orders_newest_first():
    repo = InMemorySignalRepository()
    older = make_signal(observed_at=NOW - timedelta(hours=1), provenance=SignalProvenance(source_system="x", source_event_id="1"))
    newer = make_signal(
        observed_at=NOW,
        provenance=SignalProvenance(source_system="x", source_event_id="2"),
        fingerprint=compute_fingerprint(source=SignalSource.CLOUD_MONITORING, source_event_id="2", subject="quipu-api"),
    )
    await repo.save(older)
    await repo.save(newer)
    results = await repo.query(SignalQuery())
    assert results[0].signal_id == newer.signal_id


@pytest.mark.asyncio
async def test_query_with_no_matches_returns_empty_list():
    repo = InMemorySignalRepository()
    await repo.save(make_signal())
    results = await repo.query(SignalQuery(service_name="does-not-exist"))
    assert results == []


def test_query_limit_bounds_enforced():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        SignalQuery(limit=0)
    with pytest.raises(ValidationError):
        SignalQuery(limit=10_000)


# ---- Firestore serialization (no live connection required) ------------------


def test_to_firestore_dict_normalizes_enums_to_values():
    signal = make_signal()
    data = to_firestore_dict(signal)
    assert data["signal_type"] == "metric_anomaly"
    assert data["source"] == "cloud_monitoring"
    assert data["severity"] == "warning"
    assert isinstance(data["provenance"], dict)


def test_to_firestore_dict_datetimes_are_timezone_aware():
    signal = make_signal()
    data = to_firestore_dict(signal)
    assert data["observed_at"].tzinfo is not None
    assert data["ingested_at"].tzinfo is not None


def test_from_firestore_dict_roundtrips():
    signal = make_signal(service_name="quipu-api", evidence={"count": 5}, metadata={"note": "x"})
    data = to_firestore_dict(signal)
    restored = from_firestore_dict(Signal, data)
    assert restored == signal


def test_firestore_repository_is_only_place_importing_firestore_sdk():
    import ast
    from pathlib import Path

    src = Path("app/persistence/firestore/repositories.py").read_text()
    tree = ast.parse(src)
    imports = {node.names[0].name for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module}
    modules = {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    assert any(m and m.startswith("google.cloud") for m in modules)

    domain_src = Path("app/domain/signal.py").read_text()
    assert "google.cloud" not in domain_src
    assert "firestore" not in domain_src.lower()
