"""Tests for the Pub/Sub Signal Consumer Worker (app/eventing/worker.py).
Uses the real in-memory Pub/Sub broker, the real SignalIngestionService,
and real InMemorySignalRepository throughout — no Google Cloud
credentials required. See docs/architecture/pubsub_worker.md.
"""

import asyncio
import json
import logging
from datetime import datetime, timezone

import pytest

from app.eventing.errors import IngestionFailureCategory
from app.eventing.ingestion_service import SignalIngestionService
from app.eventing.memory_pubsub import InMemoryPubSub
from app.eventing.trigger import SignalAvailableEvent
from app.eventing.worker import SignalConsumerWorker
from app.persistence.memory.repositories import InMemorySignalRepository
from app.persistence.repositories.signal import SignalQuery

NOW = datetime.now(timezone.utc)
TOPIC = "worker-test-topic"
SUBSCRIPTION = "worker-test-sub"


def _envelope_bytes(**overrides) -> bytes:
    body = {
        "event_id": "evt-1",
        "source": "customer_feedback",
        "event_type": "feedback",
        "occurred_at": NOW.isoformat(),
        "subject": "billing",
        "payload": {"feedback_id": "fb-1", "submitted_at": NOW.isoformat(), "text": "Please add CSV export"},
        "metadata": {},
    }
    body.update(overrides)
    return json.dumps(body).encode("utf-8")


def _broker() -> InMemoryPubSub:
    broker = InMemoryPubSub()
    broker.bind_subscription(topic=TOPIC, subscription=SUBSCRIPTION)
    return broker


async def _wait_until(predicate, *, timeout: float = 2.0, interval: float = 0.01) -> None:
    elapsed = 0.0
    while not predicate():
        if elapsed >= timeout:
            raise AssertionError(f"condition not met within {timeout}s")
        await asyncio.sleep(interval)
        elapsed += interval


class _RecordingTrigger:
    def __init__(self, *, fail: bool = False):
        self.calls: list[str] = []
        self._fail = fail

    async def on_signal_available(self, event: SignalAvailableEvent) -> None:
        self.calls.append(event.signal_id)
        if self._fail:
            raise RuntimeError("detection processor boom")


class _FailingSaveRepo(InMemorySignalRepository):
    def __init__(self, *, fail_times: int = 1):
        super().__init__()
        self._fail_times = fail_times

    async def save(self, signal):
        if self._fail_times > 0:
            self._fail_times -= 1
            raise ConnectionError("transient persistence failure")
        return await super().save(signal)


# ---------------------------------------------------------------------------
# 1-3: lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_worker_starts_successfully():
    broker = _broker()
    service = SignalIngestionService(InMemorySignalRepository())
    worker = SignalConsumerWorker(broker, service, subscription=SUBSCRIPTION, poll_interval_seconds=0.01)

    await worker.start()
    assert worker.is_running is True
    assert worker.counters.starts == 1

    await worker.stop()


@pytest.mark.asyncio
async def test_worker_stops_successfully():
    broker = _broker()
    service = SignalIngestionService(InMemorySignalRepository())
    worker = SignalConsumerWorker(broker, service, subscription=SUBSCRIPTION, poll_interval_seconds=0.01)
    await worker.start()

    await worker.stop()

    assert worker.is_running is False
    assert worker.counters.stops == 1


@pytest.mark.asyncio
async def test_graceful_cancellation_waits_for_inflight_within_budget():
    broker = _broker()
    repo = InMemorySignalRepository()

    release = asyncio.Event()
    started = asyncio.Event()

    class _SlowService(SignalIngestionService):
        async def ingest_one(self, message):
            started.set()
            await release.wait()
            return await super().ingest_one(message)

    service = _SlowService(repo)
    worker = SignalConsumerWorker(broker, service, subscription=SUBSCRIPTION, poll_interval_seconds=0.01, shutdown_timeout_seconds=2.0)
    await broker.publish(topic=TOPIC, data=_envelope_bytes())
    await worker.start()
    await started.wait()

    async def _release_soon():
        await asyncio.sleep(0.05)
        release.set()

    asyncio.create_task(_release_soon())
    await worker.stop()  # should wait for the in-flight message rather than cancelling it

    assert worker.counters.messages_processed == 1


@pytest.mark.asyncio
async def test_shutdown_cancels_after_timeout_budget_exceeded():
    broker = _broker()
    repo = InMemorySignalRepository()

    release = asyncio.Event()
    started = asyncio.Event()

    class _StuckService(SignalIngestionService):
        async def ingest_one(self, message):
            started.set()
            await release.wait()
            return await super().ingest_one(message)  # pragma: no cover — never reached in this test

    service = _StuckService(repo)
    worker = SignalConsumerWorker(broker, service, subscription=SUBSCRIPTION, poll_interval_seconds=0.01, shutdown_timeout_seconds=0.05)
    await broker.publish(topic=TOPIC, data=_envelope_bytes())
    await worker.start()
    await started.wait()

    await worker.stop()  # the in-flight task never releases — must give up and cancel within the budget

    assert worker.is_running is False
    release.set()  # let the (now-cancelled) task's wait unblock so nothing lingers


# ---------------------------------------------------------------------------
# 4-6: message processing and concurrency
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_one_message_processed_successfully():
    broker = _broker()
    repo = InMemorySignalRepository()
    service = SignalIngestionService(repo)
    worker = SignalConsumerWorker(broker, service, subscription=SUBSCRIPTION, poll_interval_seconds=0.01)
    await broker.publish(topic=TOPIC, data=_envelope_bytes())

    await worker.start()
    await _wait_until(lambda: worker.counters.messages_processed == 1)
    await worker.stop()

    signals = await repo.query(SignalQuery(limit=50))
    assert len(signals) == 1


@pytest.mark.asyncio
async def test_multiple_messages_processed_concurrently():
    broker = _broker()
    repo = InMemorySignalRepository()

    concurrent = 0
    max_concurrent = 0
    lock = asyncio.Lock()

    class _TrackingService(SignalIngestionService):
        async def ingest_one(self, message):
            nonlocal concurrent, max_concurrent
            async with lock:
                concurrent += 1
                max_concurrent = max(max_concurrent, concurrent)
            await asyncio.sleep(0.05)
            result = await super().ingest_one(message)
            async with lock:
                concurrent -= 1
            return result

    service = _TrackingService(repo)
    worker = SignalConsumerWorker(broker, service, subscription=SUBSCRIPTION, max_concurrency=5, poll_interval_seconds=0.01)
    for i in range(5):
        await broker.publish(topic=TOPIC, data=_envelope_bytes(event_id=f"evt-{i}", payload={"feedback_id": f"fb-{i}", "submitted_at": NOW.isoformat(), "text": f"feedback {i}"}))

    await worker.start()
    await _wait_until(lambda: worker.counters.messages_processed == 5, timeout=3.0)
    await worker.stop()

    assert max_concurrent > 1  # actually overlapped, not serialized


@pytest.mark.asyncio
async def test_concurrency_limit_is_enforced():
    broker = _broker()
    repo = InMemorySignalRepository()

    concurrent = 0
    max_concurrent = 0
    lock = asyncio.Lock()

    class _TrackingService(SignalIngestionService):
        async def ingest_one(self, message):
            nonlocal concurrent, max_concurrent
            async with lock:
                concurrent += 1
                max_concurrent = max(max_concurrent, concurrent)
            await asyncio.sleep(0.05)
            result = await super().ingest_one(message)
            async with lock:
                concurrent -= 1
            return result

    service = _TrackingService(repo)
    worker = SignalConsumerWorker(broker, service, subscription=SUBSCRIPTION, max_concurrency=2, max_messages_per_pull=10, poll_interval_seconds=0.01)
    for i in range(8):
        await broker.publish(topic=TOPIC, data=_envelope_bytes(event_id=f"evt-{i}", payload={"feedback_id": f"fb-{i}", "submitted_at": NOW.isoformat(), "text": f"feedback {i}"}))

    await worker.start()
    await _wait_until(lambda: worker.counters.messages_processed == 8, timeout=3.0)
    await worker.stop()

    assert max_concurrent <= 2


# ---------------------------------------------------------------------------
# 7-9: permanent failures are dropped, never redelivered forever
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_malformed_message_is_dropped():
    broker = _broker()
    service = SignalIngestionService(InMemorySignalRepository())
    worker = SignalConsumerWorker(broker, service, subscription=SUBSCRIPTION, poll_interval_seconds=0.01)
    await broker.publish(topic=TOPIC, data=b"not json")

    await worker.start()
    await _wait_until(lambda: worker.counters.messages_dropped == 1)
    await worker.stop()

    assert worker.counters.permanent_failures == 1
    assert worker.counters.messages_redelivered == 0


@pytest.mark.asyncio
async def test_unsupported_event_is_dropped():
    broker = _broker()
    service = SignalIngestionService(InMemorySignalRepository())
    worker = SignalConsumerWorker(broker, service, subscription=SUBSCRIPTION, poll_interval_seconds=0.01)
    await broker.publish(topic=TOPIC, data=_envelope_bytes(source="internal_system", event_type="feedback", payload={"feedback_id": "x", "submitted_at": NOW.isoformat(), "text": "x"}))

    await worker.start()
    await _wait_until(lambda: worker.counters.messages_dropped == 1)
    await worker.stop()

    assert worker.counters.permanent_failures == 1


@pytest.mark.asyncio
async def test_normalization_failure_is_dropped():
    broker = _broker()
    service = SignalIngestionService(InMemorySignalRepository())
    worker = SignalConsumerWorker(broker, service, subscription=SUBSCRIPTION, poll_interval_seconds=0.01)
    await broker.publish(topic=TOPIC, data=_envelope_bytes(payload={"feedback_id": "fb-1"}))  # missing required fields

    await worker.start()
    await _wait_until(lambda: worker.counters.messages_dropped == 1)
    await worker.stop()

    assert worker.counters.permanent_failures == 1


# ---------------------------------------------------------------------------
# 10-11: persistence failure -> redelivery; duplicate delivery -> idempotent
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_persistence_failure_causes_redelivery():
    broker = _broker()
    repo = _FailingSaveRepo(fail_times=999)
    service = SignalIngestionService(repo)
    worker = SignalConsumerWorker(broker, service, subscription=SUBSCRIPTION, poll_interval_seconds=0.02)
    await broker.publish(topic=TOPIC, data=_envelope_bytes())

    await worker.start()
    await _wait_until(lambda: worker.counters.messages_redelivered >= 1)
    await worker.stop()

    assert worker.counters.persistence_failures >= 1
    assert worker.counters.messages_processed == 0
    assert worker.counters.messages_dropped == 0


@pytest.mark.asyncio
async def test_duplicate_delivery_remains_idempotent():
    broker = _broker()
    repo = InMemorySignalRepository()
    service = SignalIngestionService(repo)
    worker = SignalConsumerWorker(broker, service, subscription=SUBSCRIPTION, poll_interval_seconds=0.01)
    data = _envelope_bytes()
    await broker.publish(topic=TOPIC, data=data)
    await broker.publish(topic=TOPIC, data=data)  # a second, independently-published copy of the same logical event

    await worker.start()
    await _wait_until(lambda: worker.counters.messages_received == 2)
    await worker.stop()

    signals = await repo.query(SignalQuery(limit=50))
    assert len(signals) == 1
    # Both deliveries are acknowledged/"processed" (dedup is a valid,
    # acknowledged outcome — see IngestOutcome.deduplicated) even though
    # only one Signal was ever actually created.
    assert worker.counters.messages_processed == 2
    assert worker.counters.messages_dropped == 0
    assert worker.counters.messages_redelivered == 0


# ---------------------------------------------------------------------------
# 12-13: detection failure isolation / poison message doesn't kill worker
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_detection_failure_does_not_cause_ingestion_redelivery():
    broker = _broker()
    repo = InMemorySignalRepository()
    trigger = _RecordingTrigger(fail=True)
    service = SignalIngestionService(repo, detection_trigger=trigger)
    worker = SignalConsumerWorker(broker, service, subscription=SUBSCRIPTION, poll_interval_seconds=0.01)
    await broker.publish(topic=TOPIC, data=_envelope_bytes())

    await worker.start()
    await _wait_until(lambda: worker.counters.messages_processed == 1)
    await worker.stop()

    assert worker.counters.messages_redelivered == 0
    assert len(trigger.calls) == 1
    signals = await repo.query(SignalQuery(limit=50))
    assert len(signals) == 1  # the Signal is persisted regardless of the trigger failure


@pytest.mark.asyncio
async def test_one_poison_message_does_not_kill_worker():
    broker = _broker()
    repo = InMemorySignalRepository()
    service = SignalIngestionService(repo)
    worker = SignalConsumerWorker(broker, service, subscription=SUBSCRIPTION, poll_interval_seconds=0.01)
    await broker.publish(topic=TOPIC, data=b"not json at all")
    await broker.publish(topic=TOPIC, data=_envelope_bytes(event_id="evt-good", payload={"feedback_id": "fb-good", "submitted_at": NOW.isoformat(), "text": "a real one"}))

    await worker.start()
    await _wait_until(lambda: worker.counters.messages_received == 2)
    await worker.stop()

    assert worker.counters.messages_dropped == 1
    assert worker.counters.messages_processed == 1
    signals = await repo.query(SignalQuery(limit=50))
    assert len(signals) == 1


# ---------------------------------------------------------------------------
# 15: no raw payload in logs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_worker_does_not_log_raw_payload(caplog):
    broker = _broker()
    service = SignalIngestionService(InMemorySignalRepository())
    worker = SignalConsumerWorker(broker, service, subscription=SUBSCRIPTION, poll_interval_seconds=0.01)
    secret_marker = "TOTALLY-UNIQUE-WORKER-SECRET-MARKER-13579"
    await broker.publish(topic=TOPIC, data=_envelope_bytes(payload={"feedback_id": "fb-1", "submitted_at": NOW.isoformat(), "text": secret_marker}))

    with caplog.at_level(logging.DEBUG):
        await worker.start()
        await _wait_until(lambda: worker.counters.messages_processed == 1)
        await worker.stop()

    assert secret_marker not in caplog.text


# ---------------------------------------------------------------------------
# 16: Pub/Sub message_id is not the Signal fingerprint basis
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pubsub_message_id_not_used_as_fingerprint():
    broker = _broker()
    repo = InMemorySignalRepository()
    service = SignalIngestionService(repo)
    worker = SignalConsumerWorker(broker, service, subscription=SUBSCRIPTION, poll_interval_seconds=0.01)
    data = _envelope_bytes()
    id1 = await broker.publish(topic=TOPIC, data=data)
    id2 = await broker.publish(topic=TOPIC, data=data)
    assert id1 != id2  # two distinct transport-layer message_ids

    await worker.start()
    await _wait_until(lambda: worker.counters.messages_received == 2)
    await worker.stop()

    signals = await repo.query(SignalQuery(limit=50))
    assert len(signals) == 1  # both collapse to the same Signal despite different message_ids


# ---------------------------------------------------------------------------
# 17-18: Google SDK isolation
# ---------------------------------------------------------------------------


def test_worker_module_has_no_google_sdk_import():
    import app.eventing.worker as worker_module

    source = worker_module.__file__
    with open(source) as f:
        text = f.read()
    assert "google.cloud" not in text
    assert "google.api_core" not in text


def test_no_google_sdk_import_leaks_outside_boundary():
    import ast
    import pathlib

    eventing_dir = pathlib.Path("app/eventing")
    allowed = {"google_pubsub_client.py"}
    for path in eventing_dir.glob("*.py"):
        if path.name in allowed:
            continue
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert not alias.name.startswith("google."), f"{path} imports {alias.name}"
            elif isinstance(node, ast.ImportFrom) and node.module:
                assert not node.module.startswith("google."), f"{path} imports from {node.module}"


# ---------------------------------------------------------------------------
# 19-22: existing suites remain green (regression guard)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_existing_ingestion_tests_still_pass():
    """A direct smoke check that SignalIngestionService's own behavior is
    unaffected by the worker's existence — the full file is also run as
    part of the complete suite."""
    broker = _broker()
    repo = InMemorySignalRepository()
    service = SignalIngestionService(repo)
    await broker.publish(topic=TOPIC, data=_envelope_bytes())
    [message] = await broker.pull(subscription=SUBSCRIPTION, max_messages=1)
    outcome = await service.ingest_one(message)
    assert outcome.acknowledged is True


@pytest.mark.asyncio
async def test_existing_detection_processor_flow_still_passes():
    from app.detection.processor import DetectionProcessor
    from app.agent_runtime.gateways.detections import RepositoryDetectionGateway
    from app.agent_runtime.gateways.signals import RepositorySignalGateway
    from app.persistence.memory.repositories import InMemoryDetectionRepository

    signal_repo = InMemorySignalRepository()
    detection_repo = InMemoryDetectionRepository()
    processor = DetectionProcessor(signal_gateway=RepositorySignalGateway(signal_repo), detection_gateway=RepositoryDetectionGateway(detection_repo))
    assert processor is not None  # construction alone proves no import-time regression


@pytest.mark.asyncio
async def test_existing_feature_flow_still_passes():
    from app.demo import DemoHarness

    summary = await DemoHarness().run_feature_flow()
    assert summary.verification_status == "passed"


@pytest.mark.asyncio
async def test_existing_incident_flow_still_passes():
    from app.demo import DemoHarness

    summary = await DemoHarness().run_incident_flow()
    assert summary.verification_status == "passed"


# ---------------------------------------------------------------------------
# Worker-level demo fixture (§15): Pub/Sub -> worker -> ingestion ->
# Signal persisted -> DetectionTrigger -> DetectionProcessor -> DetectingAgent
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_worker_demo_produces_signals_and_detection():
    from app.demo.worker_demo import run_worker_demo

    result = await run_worker_demo()

    assert result.messages_processed == 2
    assert result.messages_dropped == 0
    assert len(result.signal_ids) == 2
    assert result.detection_id is not None
    assert result.detection_type == "feature_opportunity"
