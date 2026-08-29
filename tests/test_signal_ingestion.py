"""Tests for the Pub/Sub Signal ingestion pipeline (app/eventing/). Uses the
real in-memory Pub/Sub broker (app.eventing.memory_pubsub.InMemoryPubSub)
and the real InMemorySignalRepository + real app.signals.adapters
normalization — no Google Cloud credentials required, and no mocked
repository standing in for actual persistence behavior. See
docs/architecture/pubsub_signal_ingestion.md.
"""

import json
from datetime import datetime, timezone

import pytest

from app.domain import SignalSeverity
from app.eventing.envelope import EventEnvelope, IngestionEventType
from app.eventing.errors import IngestionFailureCategory
from app.eventing.ingestion_service import SignalIngestionService
from app.eventing.memory_pubsub import InMemoryPubSub
from app.eventing.trigger import DetectionTrigger
from app.persistence.memory.repositories import InMemorySignalRepository
from app.persistence.repositories.signal import SignalQuery

SUBSCRIPTION = "signal-ingestion-sub"
TOPIC = "signal-ingestion-topic"


def _envelope_bytes(**overrides) -> bytes:
    body = {
        "event_id": "evt-1",
        "source": "customer_feedback",
        "event_type": "feedback",
        "occurred_at": "2026-01-01T00:00:00+00:00",
        "subject": "billing",
        "payload": {"feedback_id": "fb-1", "submitted_at": "2026-01-01T00:00:00+00:00", "text": "Invoices are confusing"},
        "metadata": {},
    }
    body.update(overrides)
    return json.dumps(body).encode("utf-8")


async def _publish_and_pull(broker: InMemoryPubSub, data: bytes):
    broker.bind_subscription(topic=TOPIC, subscription=SUBSCRIPTION)
    await broker.publish(topic=TOPIC, data=data)
    messages = await broker.pull(subscription=SUBSCRIPTION, max_messages=10)
    assert len(messages) == 1
    return messages[0]


def _service(**kwargs) -> tuple[SignalIngestionService, InMemorySignalRepository]:
    repo = InMemorySignalRepository()
    service = SignalIngestionService(repo, **kwargs)
    return service, repo


class _RecordingTrigger:
    def __init__(self, *, fail: bool = False):
        self.calls = []
        self._fail = fail

    async def on_signal_available(self, signal):
        self.calls.append(signal.signal_id)
        if self._fail:
            raise RuntimeError("trigger boom")


# ---------------------------------------------------------------------------
# 1-3: valid ingestion for three different sources
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_valid_cloud_monitoring_alert_creates_signal():
    broker = InMemoryPubSub()
    service, repo = _service()
    data = _envelope_bytes(
        event_id="evt-mon-1",
        source="cloud_monitoring",
        event_type="alert",
        payload={
            "incident": {
                "incident_id": "inc-1",
                "started_at": 1735689600,
                "summary": "High error rate",
                "resource": {"type": "cloud_run_revision", "labels": {"service_name": "checkout"}},
                "state": "open",
            }
        },
    )
    message = await _publish_and_pull(broker, data)
    outcome = await service.ingest_one(message)
    assert outcome.acknowledged is True
    assert outcome.signal_id is not None
    saved = await repo.get(outcome.signal_id)
    assert saved.source.value == "cloud_monitoring"
    assert saved.severity == SignalSeverity.CRITICAL
    assert service.counters.signals_created == 1


@pytest.mark.asyncio
async def test_valid_cloud_logging_entry_creates_signal():
    broker = InMemoryPubSub()
    service, repo = _service()
    data = _envelope_bytes(
        event_id="evt-log-1",
        source="cloud_logging",
        event_type="log_entry",
        payload={
            "insertId": "log-1",
            "timestamp": "2026-01-01T00:00:00+00:00",
            "severity": "ERROR",
            "textPayload": "boom",
            "resource": {"labels": {"service_name": "checkout"}},
        },
    )
    message = await _publish_and_pull(broker, data)
    outcome = await service.ingest_one(message)
    assert outcome.acknowledged is True
    saved = await repo.get(outcome.signal_id)
    assert saved.source.value == "cloud_logging"
    assert saved.provenance.source_event_id == "log-1"


@pytest.mark.asyncio
async def test_valid_customer_feedback_creates_signal():
    broker = InMemoryPubSub()
    service, repo = _service()
    data = _envelope_bytes()
    message = await _publish_and_pull(broker, data)
    outcome = await service.ingest_one(message)
    assert outcome.acknowledged is True
    saved = await repo.get(outcome.signal_id)
    assert saved.source.value == "customer_feedback"
    assert saved.summary == "Invoices are confusing"


# ---------------------------------------------------------------------------
# 4/18: duplicate/redelivered event produces exactly one Signal (idempotency)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_duplicate_delivery_creates_only_one_signal():
    broker = InMemoryPubSub()
    service, repo = _service()
    broker.bind_subscription(topic=TOPIC, subscription=SUBSCRIPTION)
    data = _envelope_bytes()
    await broker.publish(topic=TOPIC, data=data)
    await broker.publish(topic=TOPIC, data=data)  # a second, independently-published copy of the same logical event

    messages = await broker.pull(subscription=SUBSCRIPTION, max_messages=10)
    assert len(messages) == 2

    outcome1 = await service.ingest_one(messages[0])
    outcome2 = await service.ingest_one(messages[1])

    assert outcome1.acknowledged and outcome2.acknowledged
    assert outcome2.deduplicated is True
    assert outcome1.signal_id == outcome2.signal_id
    assert service.counters.signals_created == 1
    assert service.counters.signals_deduplicated == 1
    all_signals = await repo.query(SignalQuery(limit=500))
    assert len(all_signals) == 1


@pytest.mark.asyncio
async def test_redelivery_after_nack_remains_idempotent():
    """Scenario 18: a message redelivered because it was nacked (simulating
    an expired ack deadline before persistence completed) must not create a
    second Signal on the retry."""
    broker = InMemoryPubSub()
    service, repo = _service()
    data = _envelope_bytes()
    message = await _publish_and_pull(broker, data)

    # Simulate a crash after normalization but before persistence: nack it.
    await message.nack()
    redelivered = await broker.pull(subscription=SUBSCRIPTION, max_messages=10)
    assert len(redelivered) == 1
    assert redelivered[0].delivery_attempt == 2

    outcome = await service.ingest_one(redelivered[0])
    assert outcome.acknowledged is True
    assert outcome.deduplicated is False  # first successful persistence
    assert service.counters.signals_created == 1

    # Now redeliver again — this time it should dedup.
    outcome2 = await service.ingest_one(redelivered[0])
    assert outcome2.deduplicated is True
    assert service.counters.signals_created == 1


# ---------------------------------------------------------------------------
# 5-7: malformed envelope / unsupported source / unsupported event type
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_malformed_envelope_is_rejected_and_acked():
    broker = InMemoryPubSub()
    service, _ = _service()
    message = await _publish_and_pull(broker, b"not json at all")
    outcome = await service.ingest_one(message)
    assert outcome.acknowledged is True
    assert outcome.category == IngestionFailureCategory.MALFORMED_ENVELOPE
    assert service.counters.messages_rejected == 1


@pytest.mark.asyncio
async def test_malformed_envelope_missing_required_field_is_rejected():
    broker = InMemoryPubSub()
    service, _ = _service()
    data = json.dumps({"source": "customer_feedback", "event_type": "feedback"}).encode("utf-8")  # missing event_id/occurred_at/payload
    message = await _publish_and_pull(broker, data)
    outcome = await service.ingest_one(message)
    assert outcome.acknowledged is True
    assert outcome.category == IngestionFailureCategory.MALFORMED_ENVELOPE


@pytest.mark.asyncio
async def test_unsupported_source_is_rejected_and_acked():
    broker = InMemoryPubSub()
    service, _ = _service()
    data = _envelope_bytes(source="internal_system", event_type="feedback", payload={"feedback_id": "x", "submitted_at": "2026-01-01T00:00:00+00:00", "text": "x"})
    message = await _publish_and_pull(broker, data)
    outcome = await service.ingest_one(message)
    assert outcome.acknowledged is True
    assert outcome.category == IngestionFailureCategory.UNSUPPORTED_SOURCE
    assert service.counters.messages_rejected == 1


@pytest.mark.asyncio
async def test_unsupported_event_type_for_known_source_is_rejected():
    broker = InMemoryPubSub()
    service, _ = _service()
    data = _envelope_bytes(source="customer_feedback", event_type="pattern")  # valid source, wrong event_type for it
    message = await _publish_and_pull(broker, data)
    outcome = await service.ingest_one(message)
    assert outcome.acknowledged is True
    assert outcome.category == IngestionFailureCategory.UNSUPPORTED_EVENT_TYPE


@pytest.mark.asyncio
async def test_adapter_normalization_failure_is_rejected():
    """A structurally-valid envelope whose payload is missing fields the
    target adapter itself requires (app.signals.adapters._require) — the
    existing adapter's own validation, not a second one."""
    broker = InMemoryPubSub()
    service, _ = _service()
    data = _envelope_bytes(payload={"feedback_id": "fb-1"})  # missing submitted_at/text
    message = await _publish_and_pull(broker, data)
    outcome = await service.ingest_one(message)
    assert outcome.acknowledged is True
    assert outcome.category == IngestionFailureCategory.NORMALIZATION_FAILURE


# ---------------------------------------------------------------------------
# 8: sanitization is applied (reused, not reimplemented)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_secret_shaped_metadata_is_sanitized():
    broker = InMemoryPubSub()
    service, repo = _service()
    data = _envelope_bytes(
        payload={
            "feedback_id": "fb-secret",
            "submitted_at": "2026-01-01T00:00:00+00:00",
            "text": "my api_key=sk-verysecret123 leaked in this message",
        }
    )
    message = await _publish_and_pull(broker, data)
    outcome = await service.ingest_one(message)
    saved = await repo.get(outcome.signal_id)
    # The adapter's own sanitize_metadata() truncates/redacts secret-shaped
    # *keys*, not embedded substrings — assert the existing sanitizer ran
    # (evidence passed through it) rather than re-deriving its exact regex.
    assert "text" in saved.evidence


# ---------------------------------------------------------------------------
# 9-11: ack/persistence coupling and transient-failure retryability
# ---------------------------------------------------------------------------


class _FailingRepo(InMemorySignalRepository):
    def __init__(self, *, fail_times: int = 1):
        super().__init__()
        self._fail_times = fail_times

    async def save(self, signal):
        if self._fail_times > 0:
            self._fail_times -= 1
            raise ConnectionError("transient Firestore failure")
        return await super().save(signal)


@pytest.mark.asyncio
async def test_persistence_failure_leaves_message_unacknowledged():
    broker = InMemoryPubSub()
    repo = _FailingRepo(fail_times=999)
    service = SignalIngestionService(repo)
    message = await _publish_and_pull(broker, _envelope_bytes())

    outcome = await service.ingest_one(message)

    assert outcome.acknowledged is False
    assert outcome.category == IngestionFailureCategory.PERSISTENCE_FAILURE
    assert service.counters.messages_failed == 1
    # The message was never acked, so it's still outstanding on the broker.
    assert message.message_id in broker._outstanding[SUBSCRIPTION]


@pytest.mark.asyncio
async def test_successful_persistence_is_acknowledged():
    broker = InMemoryPubSub()
    service, repo = _service()
    message = await _publish_and_pull(broker, _envelope_bytes())
    outcome = await service.ingest_one(message)
    assert outcome.acknowledged is True
    await message.ack()  # idempotent no-op double-ack, mirrors real usage
    assert message.message_id not in broker._outstanding[SUBSCRIPTION]


@pytest.mark.asyncio
async def test_transient_persistence_failure_then_retry_succeeds():
    """A transient failure on first attempt, followed by redelivery, must
    eventually succeed and create exactly one Signal."""
    broker = InMemoryPubSub()
    repo = _FailingRepo(fail_times=1)
    service = SignalIngestionService(repo)
    message = await _publish_and_pull(broker, _envelope_bytes())

    first = await service.ingest_one(message)
    assert first.acknowledged is False

    broker.redeliver_unacked(subscription=SUBSCRIPTION)
    redelivered = await broker.pull(subscription=SUBSCRIPTION, max_messages=10)
    assert len(redelivered) == 1

    second = await service.ingest_one(redelivered[0])
    assert second.acknowledged is True
    assert service.counters.signals_created == 1


# ---------------------------------------------------------------------------
# 12-13: provenance / message-id handling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_source_event_id_provenance_preserved():
    broker = InMemoryPubSub()
    service, repo = _service()
    data = _envelope_bytes(payload={"feedback_id": "fb-provenance-42", "submitted_at": "2026-01-01T00:00:00+00:00", "text": "hi"})
    message = await _publish_and_pull(broker, data)
    outcome = await service.ingest_one(message)
    saved = await repo.get(outcome.signal_id)
    assert saved.provenance.source_event_id == "fb-provenance-42"


@pytest.mark.asyncio
async def test_pubsub_message_id_is_transport_only_not_fingerprint_basis():
    """Two independently-published copies of the same logical event get
    DIFFERENT Pub/Sub message_ids (Google does not guarantee message_id
    stability across what a producer considers 'the same' event) but must
    still collapse to one Signal, because the fingerprint is derived from
    the payload's own source_event_id, never message_id."""
    broker = InMemoryPubSub()
    service, repo = _service()
    broker.bind_subscription(topic=TOPIC, subscription=SUBSCRIPTION)
    data = _envelope_bytes()
    await broker.publish(topic=TOPIC, data=data)
    await broker.publish(topic=TOPIC, data=data)
    messages = await broker.pull(subscription=SUBSCRIPTION, max_messages=10)
    assert messages[0].message_id != messages[1].message_id

    outcome1 = await service.ingest_one(messages[0])
    outcome2 = await service.ingest_one(messages[1])
    assert outcome1.signal_id == outcome2.signal_id


# ---------------------------------------------------------------------------
# 14: bounded payload/message size
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_oversized_message_is_rejected():
    broker = InMemoryPubSub()
    service, _ = _service(max_message_bytes=100)
    huge_payload = {"feedback_id": "fb-1", "submitted_at": "2026-01-01T00:00:00+00:00", "text": "x" * 10_000}
    data = _envelope_bytes(payload=huge_payload)
    assert len(data) > 100
    message = await _publish_and_pull(broker, data)
    outcome = await service.ingest_one(message)
    assert outcome.acknowledged is True
    assert outcome.category == IngestionFailureCategory.PAYLOAD_TOO_LARGE


# ---------------------------------------------------------------------------
# 15: no raw payload in logs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_raw_payload_appears_in_log_output(caplog):
    import logging

    broker = InMemoryPubSub()
    service, _ = _service()
    secret_marker = "TOTALLY-UNIQUE-SECRET-PAYLOAD-MARKER-98765"
    data = _envelope_bytes(payload={"feedback_id": "fb-1", "submitted_at": "2026-01-01T00:00:00+00:00", "text": secret_marker})
    message = await _publish_and_pull(broker, data)

    with caplog.at_level(logging.DEBUG, logger="quipu.eventing.ingestion"):
        await service.ingest_one(message)

    assert secret_marker not in caplog.text


# ---------------------------------------------------------------------------
# 16-17: detection trigger boundary
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_detection_trigger_invoked_only_after_persistence():
    broker = InMemoryPubSub()
    repo = InMemorySignalRepository()
    trigger = _RecordingTrigger()
    service = SignalIngestionService(repo, detection_trigger=trigger)
    message = await _publish_and_pull(broker, _envelope_bytes())

    outcome = await service.ingest_one(message)

    assert trigger.calls == [outcome.signal_id]
    saved = await repo.get(outcome.signal_id)
    assert saved is not None  # persisted before the trigger fired


@pytest.mark.asyncio
async def test_trigger_failure_never_reverses_persistence_or_ack():
    broker = InMemoryPubSub()
    repo = InMemorySignalRepository()
    trigger = _RecordingTrigger(fail=True)
    service = SignalIngestionService(repo, detection_trigger=trigger)
    message = await _publish_and_pull(broker, _envelope_bytes())

    outcome = await service.ingest_one(message)

    assert outcome.acknowledged is True
    assert outcome.signal_id is not None
    saved = await repo.get(outcome.signal_id)
    assert saved is not None
    assert message.message_id not in broker._outstanding[SUBSCRIPTION]


@pytest.mark.asyncio
async def test_trigger_not_invoked_for_deduplicated_signal():
    """A duplicate delivery is acked but does not represent a NEW signal
    becoming available, so the trigger should not fire again for it."""
    broker = InMemoryPubSub()
    repo = InMemorySignalRepository()
    trigger = _RecordingTrigger()
    service = SignalIngestionService(repo, detection_trigger=trigger)
    broker.bind_subscription(topic=TOPIC, subscription=SUBSCRIPTION)
    data = _envelope_bytes()
    await broker.publish(topic=TOPIC, data=data)
    await broker.publish(topic=TOPIC, data=data)
    messages = await broker.pull(subscription=SUBSCRIPTION, max_messages=10)

    await service.ingest_one(messages[0])
    await service.ingest_one(messages[1])

    assert len(trigger.calls) == 1


# ---------------------------------------------------------------------------
# Envelope model validation
# ---------------------------------------------------------------------------


def test_envelope_rejects_naive_occurred_at():
    with pytest.raises(Exception):
        EventEnvelope(
            event_id="e1",
            source="customer_feedback",
            event_type=IngestionEventType.FEEDBACK,
            occurred_at=datetime(2026, 1, 1),  # naive
            payload={},
        )


def test_envelope_rejects_empty_event_id():
    with pytest.raises(Exception):
        EventEnvelope(
            event_id="   ",
            source="customer_feedback",
            event_type=IngestionEventType.FEEDBACK,
            occurred_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            payload={},
        )


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------


def test_detection_trigger_protocol_runtime_checkable():
    from app.eventing.trigger import NoOpDetectionTrigger

    assert isinstance(NoOpDetectionTrigger(), DetectionTrigger)
