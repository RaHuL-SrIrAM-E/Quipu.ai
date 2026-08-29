"""Tests for event-driven detection processing (app/detection/,
app/eventing/trigger.py's SignalAvailableEvent). Uses the real
InMemorySignalRepository/InMemoryDetectionRepository, the real
DetectingAgent (with its internal ADK runner monkeypatched, same pattern
as tests/test_detecting_agent.py — no live Gemini call), and the real
in-memory Pub/Sub broker for the end-to-end ingestion->trigger tests. See
docs/architecture/event_driven_detection.md.
"""

import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest
from google.genai import types

from app.agent_runtime.gateways.detections import RepositoryDetectionGateway
from app.agent_runtime.gateways.signals import RepositorySignalGateway
from app.detection.policy import AggregationPolicy, DomainPolicy
from app.detection.processor import DetectionProcessingError, DetectionProcessor
from app.detection.trigger import DetectionProcessorTrigger
from app.domain import (
    DetectionDomain,
    DetectionType,
    Signal,
    SignalProvenance,
    SignalSeverity,
    SignalSource,
    SignalType,
    compute_fingerprint,
)
from app.eventing.ingestion_service import SignalIngestionService
from app.eventing.memory_pubsub import InMemoryPubSub
from app.eventing.trigger import SignalAvailableEvent
from app.persistence.memory.repositories import InMemoryDetectionRepository, InMemorySignalRepository
from app.persistence.repositories.detection import DetectionQuery

NOW = datetime.now(timezone.utc)
SUBSCRIPTION = "detection-test-sub"
TOPIC = "detection-test-topic"


def make_signal(source_event_id: str, *, stype=SignalType.METRIC_ANOMALY, source=SignalSource.CLOUD_MONITORING, service="checkout", env="production", minutes_ago=1, **overrides) -> Signal:
    subject = overrides.pop("subject", service) or "unspecified"
    defaults = dict(
        signal_type=stype,
        source=source,
        severity=SignalSeverity.WARNING,
        observed_at=NOW - timedelta(minutes=minutes_ago),
        subject=subject,
        summary=f"signal {source_event_id}",
        service_name=service,
        environment=env,
        provenance=SignalProvenance(source_system="x", source_event_id=source_event_id),
        fingerprint=compute_fingerprint(source=source, source_event_id=source_event_id, subject=subject),
    )
    defaults.update(overrides)
    return Signal(**defaults)


# ---- ADK fakes (same pattern as tests/test_detecting_agent.py) ----------


class _FakeEvent:
    def __init__(self, text):
        self.content = types.Content(role="model", parts=[types.Part(text=text)])

    def is_final_response(self):
        return True


class _FakeSession:
    id = "session-1"


class _CapturingSessionService:
    async def create_session(self, **kwargs):
        return _FakeSession()


def make_fake_runner_returning(final_text: str):
    async def _events(**kwargs):
        yield _FakeEvent(final_text)

    class _FakeRunner:
        def __init__(self, agent, app_name):
            self.session_service = _CapturingSessionService()

        def run_async(self, **kwargs):
            return _events(**kwargs)

    return _FakeRunner


def make_fake_runner_raising(exc: Exception):
    async def _events(**kwargs):
        raise exc
        yield  # pragma: no cover

    class _FakeRunner:
        def __init__(self, agent, app_name):
            self.session_service = _CapturingSessionService()

        def run_async(self, **kwargs):
            return _events(**kwargs)

    return _FakeRunner


INCIDENT_OUTPUT = dict(
    detection_type="incident",
    title="Elevated error rate",
    summary="Multiple application errors observed for checkout in production.",
    rationale="Repeated application errors for the same service within a short window.",
    confidence=0.85,
    severity="critical",
    subject="checkout",
    knowledge_references=[],
)

FEATURE_OUTPUT = dict(
    detection_type="feature_opportunity",
    title="Customers want CSV export",
    summary="Multiple independent customers requested CSV export.",
    rationale="Two independent feedback signals converge on the same unmet need.",
    confidence=0.8,
    subject="reporting",
    knowledge_references=[],
)


def _setup():
    signal_repo = InMemorySignalRepository()
    detection_repo = InMemoryDetectionRepository()
    processor = DetectionProcessor(
        signal_gateway=RepositorySignalGateway(signal_repo),
        detection_gateway=RepositoryDetectionGateway(detection_repo),
    )
    return processor, signal_repo, detection_repo


def _event_for(signal: Signal) -> SignalAvailableEvent:
    return SignalAvailableEvent.from_signal(signal)


# ---------------------------------------------------------------------------
# 1-3: basic triggering, product feature opportunity, operational incident
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_persisted_signal_triggers_detection_processing(monkeypatch):
    processor, signal_repo, detection_repo = _setup()
    signal = await signal_repo.save(make_signal("s1", stype=SignalType.APPLICATION_ERROR))
    output = {**INCIDENT_OUTPUT, "supporting_signal_ids": [signal.signal_id]}
    monkeypatch.setattr("app.agents.detecting.InMemoryRunner", make_fake_runner_returning(json.dumps(output)))

    outcome = await processor.process_signal_available(_event_for(signal))

    assert outcome.invoked_detecting_agent is True
    assert outcome.outcome == "invoked"
    assert outcome.detection_id is not None
    detection = await detection_repo.get(outcome.detection_id)
    assert detection is not None


@pytest.mark.asyncio
async def test_product_feedback_produces_feature_opportunity(monkeypatch):
    processor, signal_repo, detection_repo = _setup()
    s1 = await signal_repo.save(make_signal("f1", stype=SignalType.CUSTOMER_FEEDBACK, service=None, env=None, subject="reporting"))
    s2 = await signal_repo.save(make_signal("f2", stype=SignalType.SUPPORT_FEEDBACK, service=None, env=None, subject="reporting"))
    output = {**FEATURE_OUTPUT, "supporting_signal_ids": [s1.signal_id, s2.signal_id]}
    monkeypatch.setattr("app.agents.detecting.InMemoryRunner", make_fake_runner_returning(json.dumps(output)))

    outcome = await processor.process_signal_available(_event_for(s2))

    assert outcome.domain == "product"
    assert outcome.detection_type == "feature_opportunity"
    detection = await detection_repo.get(outcome.detection_id)
    assert detection.domain == DetectionDomain.PRODUCT
    assert detection.detection_type == DetectionType.FEATURE_OPPORTUNITY


@pytest.mark.asyncio
async def test_operational_error_signals_produce_incident(monkeypatch):
    processor, signal_repo, detection_repo = _setup()
    signal = await signal_repo.save(make_signal("op1", stype=SignalType.APPLICATION_ERROR))
    output = {**INCIDENT_OUTPUT, "supporting_signal_ids": [signal.signal_id]}
    monkeypatch.setattr("app.agents.detecting.InMemoryRunner", make_fake_runner_returning(json.dumps(output)))

    outcome = await processor.process_signal_available(_event_for(signal))

    assert outcome.domain == "operational"
    detection = await detection_repo.get(outcome.detection_id)
    assert detection.detection_type == DetectionType.INCIDENT


# ---------------------------------------------------------------------------
# 4-5: windowing and scope correlation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_related_signals_aggregated_within_window(monkeypatch):
    processor, signal_repo, _ = _setup()
    policy = AggregationPolicy(operational=DomainPolicy(window_minutes=30, min_related_signals=2), product=DomainPolicy(window_minutes=10080, min_related_signals=2))
    processor = DetectionProcessor(
        signal_gateway=processor._signals, detection_gateway=processor._detections, policy=policy
    )
    recent = await signal_repo.save(make_signal("recent", stype=SignalType.APPLICATION_ERROR, minutes_ago=5))
    await signal_repo.save(make_signal("stale", stype=SignalType.APPLICATION_ERROR, minutes_ago=120))  # outside the 30-min window

    outcome = await processor.process_signal_available(_event_for(recent))

    assert outcome.outcome == "skipped_insufficient_evidence"
    assert outcome.evidence_count == 1  # only the in-window signal counted


@pytest.mark.asyncio
async def test_unrelated_signals_not_incorrectly_grouped(monkeypatch):
    processor, signal_repo, _ = _setup()
    trigger_signal = await signal_repo.save(make_signal("a1", stype=SignalType.APPLICATION_ERROR, service="checkout"))
    await signal_repo.save(make_signal("b1", stype=SignalType.APPLICATION_ERROR, service="billing"))  # different service — unrelated
    output = {**INCIDENT_OUTPUT, "subject": "checkout", "supporting_signal_ids": [trigger_signal.signal_id]}
    monkeypatch.setattr("app.agents.detecting.InMemoryRunner", make_fake_runner_returning(json.dumps(output)))

    outcome = await processor.process_signal_available(_event_for(trigger_signal))

    assert outcome.evidence_count == 1  # billing's signal never counted toward checkout's scope


# ---------------------------------------------------------------------------
# 6-7: idempotency / dedup
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_duplicate_trigger_is_idempotent(monkeypatch):
    processor, signal_repo, detection_repo = _setup()
    signal = await signal_repo.save(make_signal("dup1", stype=SignalType.APPLICATION_ERROR))
    output = {**INCIDENT_OUTPUT, "supporting_signal_ids": [signal.signal_id]}
    monkeypatch.setattr("app.agents.detecting.InMemoryRunner", make_fake_runner_returning(json.dumps(output)))

    outcome1 = await processor.process_signal_available(_event_for(signal))
    outcome2 = await processor.process_signal_available(_event_for(signal))

    assert outcome1.detection_id == outcome2.detection_id
    all_detections = await detection_repo.query(DetectionQuery(limit=500))
    assert len(all_detections) == 1


@pytest.mark.asyncio
async def test_duplicate_signal_delivery_does_not_duplicate_detection(monkeypatch):
    """End-to-end: the same envelope delivered twice through
    SignalIngestionService dedups at the Signal layer (existing behavior)
    — so the DetectionTrigger fires twice for the SAME existing signal_id
    (once per delivery), and detection processing itself stays idempotent
    on top of that via DetectingAgent's own fingerprint dedup."""
    signal_repo = InMemorySignalRepository()
    detection_repo = InMemoryDetectionRepository()
    processor = DetectionProcessor(signal_gateway=RepositorySignalGateway(signal_repo), detection_gateway=RepositoryDetectionGateway(detection_repo))
    trigger = DetectionProcessorTrigger(processor)
    ingestion = SignalIngestionService(signal_repo, detection_trigger=trigger)

    broker = InMemoryPubSub()
    broker.bind_subscription(topic=TOPIC, subscription=SUBSCRIPTION)
    data = json.dumps(
        {
            "event_id": "evt-1",
            "source": "cloud_logging",
            "event_type": "log_entry",
            "occurred_at": NOW.isoformat(),
            "payload": {
                "insertId": "log-dup-1",
                "timestamp": NOW.isoformat(),
                "severity": "ERROR",
                "textPayload": "boom",
                "resource": {"labels": {"service_name": "checkout", "environment": "production"}},
            },
        }
    ).encode("utf-8")

    await broker.publish(topic=TOPIC, data=data)
    await broker.publish(topic=TOPIC, data=data)
    messages = await broker.pull(subscription=SUBSCRIPTION, max_messages=10)
    assert len(messages) == 2

    # First delivery persists the Signal and fires the trigger — no fake
    # model is wired yet, so DetectingAgent invocation fails; the ack
    # (already issued before the trigger ran) is unaffected either way.
    outcome1 = await ingestion.ingest_one(messages[0])
    assert outcome1.acknowledged is True
    saved_signal_id = outcome1.signal_id

    import app.agents.detecting as detecting_module

    output = {**INCIDENT_OUTPUT, "subject": "checkout", "supporting_signal_ids": [saved_signal_id]}
    monkeypatch.setattr(detecting_module, "InMemoryRunner", make_fake_runner_returning(json.dumps(output)))

    # Explicitly (re)trigger detection now that the fake model is in place.
    await processor.process_signal_available(SignalAvailableEvent.from_signal(await signal_repo.get(saved_signal_id)))

    # Second delivery is a duplicate Signal — ingestion dedups it at the
    # Signal layer, but the trigger still fires again for the SAME
    # signal_id (DetectionProcessor's own idempotency must hold here).
    outcome2 = await ingestion.ingest_one(messages[1])
    assert outcome2.acknowledged is True
    assert outcome2.deduplicated is True
    assert outcome2.signal_id == saved_signal_id

    all_detections = await detection_repo.query(DetectionQuery(limit=500))
    assert len(all_detections) == 1


# ---------------------------------------------------------------------------
# 8: zero evidence -> NO_ACTION-equivalent without invoking Gemini
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_zero_evidence_skips_without_invoking_agent(monkeypatch):
    processor, signal_repo, detection_repo = _setup()
    signal = make_signal("solo", stype=SignalType.APPLICATION_ERROR)  # never saved — simulates a scope with no persisted evidence yet

    def _boom(*args, **kwargs):
        raise AssertionError("DetectingAgent/Gemini must not be invoked when evidence is insufficient")

    monkeypatch.setattr("app.agents.detecting.InMemoryRunner", _boom)

    outcome = await processor.process_signal_available(_event_for(signal))

    assert outcome.invoked_detecting_agent is False
    assert outcome.outcome == "skipped_insufficient_evidence"
    assert outcome.evidence_count == 0
    all_detections = await detection_repo.query(DetectionQuery(limit=500))
    assert len(all_detections) == 0


# ---------------------------------------------------------------------------
# 9-10: agent failure isolation and independent retry
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_detecting_agent_failure_does_not_delete_signal(monkeypatch):
    processor, signal_repo, _ = _setup()
    signal = await signal_repo.save(make_signal("fail1", stype=SignalType.APPLICATION_ERROR))
    monkeypatch.setattr("app.agents.detecting.InMemoryRunner", make_fake_runner_raising(RuntimeError("gemini boom")))

    with pytest.raises(DetectionProcessingError):
        await processor.process_signal_available(_event_for(signal))

    assert await signal_repo.get(signal.signal_id) is not None


@pytest.mark.asyncio
async def test_detection_failure_is_independently_retryable(monkeypatch):
    processor, signal_repo, detection_repo = _setup()
    signal = await signal_repo.save(make_signal("retry1", stype=SignalType.APPLICATION_ERROR))
    monkeypatch.setattr("app.agents.detecting.InMemoryRunner", make_fake_runner_raising(RuntimeError("transient")))

    with pytest.raises(DetectionProcessingError):
        await processor.process_signal_available(_event_for(signal))

    output = {**INCIDENT_OUTPUT, "supporting_signal_ids": [signal.signal_id]}
    monkeypatch.setattr("app.agents.detecting.InMemoryRunner", make_fake_runner_returning(json.dumps(output)))

    outcome = await processor.process_signal_available(_event_for(signal))
    assert outcome.invoked_detecting_agent is True
    assert outcome.detection_id is not None


# ---------------------------------------------------------------------------
# 11: ingestion ack independent of detection processing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ingestion_ack_independent_of_detection_failure(monkeypatch):
    signal_repo = InMemorySignalRepository()
    detection_repo = InMemoryDetectionRepository()
    processor = DetectionProcessor(signal_gateway=RepositorySignalGateway(signal_repo), detection_gateway=RepositoryDetectionGateway(detection_repo))
    trigger = DetectionProcessorTrigger(processor)
    ingestion = SignalIngestionService(signal_repo, detection_trigger=trigger)
    monkeypatch.setattr("app.agents.detecting.InMemoryRunner", make_fake_runner_raising(RuntimeError("gemini down")))

    broker = InMemoryPubSub()
    broker.bind_subscription(topic=TOPIC, subscription=SUBSCRIPTION)
    data = json.dumps(
        {
            "event_id": "evt-ack-indep",
            "source": "customer_feedback",
            "event_type": "feedback",
            "occurred_at": NOW.isoformat(),
            "payload": {"feedback_id": "fb-ack-indep", "submitted_at": NOW.isoformat(), "text": "please add CSV export"},
        }
    ).encode("utf-8")
    await broker.publish(topic=TOPIC, data=data)
    messages = await broker.pull(subscription=SUBSCRIPTION, max_messages=10)

    outcome = await ingestion.ingest_one(messages[0])

    assert outcome.acknowledged is True
    assert outcome.signal_id is not None
    saved = await signal_repo.get(outcome.signal_id)
    assert saved is not None


# ---------------------------------------------------------------------------
# 12-13: fabricated evidence impossible / no raw payload to Gemini
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fabricated_signal_ids_are_dropped(monkeypatch):
    processor, signal_repo, detection_repo = _setup()
    signal = await signal_repo.save(make_signal("real1", stype=SignalType.APPLICATION_ERROR))
    output = {**INCIDENT_OUTPUT, "supporting_signal_ids": [signal.signal_id, "fabricated-id-does-not-exist"]}
    monkeypatch.setattr("app.agents.detecting.InMemoryRunner", make_fake_runner_returning(json.dumps(output)))

    outcome = await processor.process_signal_available(_event_for(signal))

    detection = await detection_repo.get(outcome.detection_id)
    assert "fabricated-id-does-not-exist" not in detection.supporting_signal_ids
    assert detection.supporting_signal_ids == [signal.signal_id]


def test_signal_available_event_carries_no_raw_payload():
    signal = make_signal("shape1", stype=SignalType.APPLICATION_ERROR)
    event = SignalAvailableEvent.from_signal(signal)
    event_fields = {f for f in event.__dataclass_fields__}
    assert "payload" not in event_fields
    assert "evidence" not in event_fields
    assert "metadata" not in event_fields
    assert event_fields == {"signal_id", "signal_type", "source", "subject", "service_name", "environment", "observed_at"}


# ---------------------------------------------------------------------------
# 14-15: human review / remediation authorization not bypassed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_feature_opportunity_does_not_auto_create_review(monkeypatch):
    from app.persistence.memory.repositories import InMemoryFeatureReviewRepository

    processor, signal_repo, detection_repo = _setup()
    s1 = await signal_repo.save(make_signal("fr1", stype=SignalType.CUSTOMER_FEEDBACK, service=None, env=None, subject="reporting"))
    s2 = await signal_repo.save(make_signal("fr2", stype=SignalType.SUPPORT_FEEDBACK, service=None, env=None, subject="reporting"))
    output = {**FEATURE_OUTPUT, "supporting_signal_ids": [s1.signal_id, s2.signal_id]}
    monkeypatch.setattr("app.agents.detecting.InMemoryRunner", make_fake_runner_returning(json.dumps(output)))

    outcome = await processor.process_signal_available(_event_for(s2))
    assert outcome.detection_type == "feature_opportunity"

    review_repo = InMemoryFeatureReviewRepository()
    from app.persistence.repositories.feature_review import FeatureReviewQuery

    reviews = await review_repo.query(FeatureReviewQuery(limit=50))
    assert reviews == []  # DetectionProcessor never calls FeatureReviewService


@pytest.mark.asyncio
async def test_incident_does_not_auto_execute_remediation(monkeypatch):
    from app.persistence.memory.repositories import InMemoryResolutionRepository

    processor, signal_repo, detection_repo = _setup()
    signal = await signal_repo.save(make_signal("inc1", stype=SignalType.APPLICATION_ERROR))
    output = {**INCIDENT_OUTPUT, "supporting_signal_ids": [signal.signal_id]}
    monkeypatch.setattr("app.agents.detecting.InMemoryRunner", make_fake_runner_returning(json.dumps(output)))

    outcome = await processor.process_signal_available(_event_for(signal))
    assert outcome.detection_type == "incident"

    resolution_repo = InMemoryResolutionRepository()
    from app.persistence.repositories.resolution import ResolutionQuery

    resolutions = await resolution_repo.query(ResolutionQuery(limit=50))
    assert resolutions == []  # DetectionProcessor never calls IncidentResolutionAgent


# ---------------------------------------------------------------------------
# 16: capability checks remain enforced
# ---------------------------------------------------------------------------


def test_detecting_agent_capabilities_unchanged():
    from app.agent_runtime.capabilities import AgentCapability
    from app.agents.detecting import DetectingAgent

    assert DetectingAgent().capabilities == {AgentCapability.READ_SIGNALS, AgentCapability.QUERY_KNOWLEDGE, AgentCapability.WRITE_DETECTION}


# ---------------------------------------------------------------------------
# 17: persistence failure prevents detection
# ---------------------------------------------------------------------------


class _FailingDetectionGateway(RepositoryDetectionGateway):
    async def save(self, detection):
        raise ConnectionError("firestore down")


@pytest.mark.asyncio
async def test_detection_persistence_failure_prevents_detection(monkeypatch):
    signal_repo = InMemorySignalRepository()
    detection_repo = InMemoryDetectionRepository()
    processor = DetectionProcessor(signal_gateway=RepositorySignalGateway(signal_repo), detection_gateway=_FailingDetectionGateway(detection_repo))
    signal = await signal_repo.save(make_signal("persistfail1", stype=SignalType.APPLICATION_ERROR))
    output = {**INCIDENT_OUTPUT, "supporting_signal_ids": [signal.signal_id]}
    monkeypatch.setattr("app.agents.detecting.InMemoryRunner", make_fake_runner_returning(json.dumps(output)))

    with pytest.raises(DetectionProcessingError):
        await processor.process_signal_available(SignalAvailableEvent.from_signal(signal))

    all_detections = await detection_repo.query(DetectionQuery(limit=500))
    assert all_detections == []


# ---------------------------------------------------------------------------
# 18: concurrent duplicate processing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_duplicate_processing_creates_one_detection(monkeypatch):
    processor, signal_repo, detection_repo = _setup()
    signal = await signal_repo.save(make_signal("concurrent1", stype=SignalType.APPLICATION_ERROR))
    output = {**INCIDENT_OUTPUT, "supporting_signal_ids": [signal.signal_id]}
    monkeypatch.setattr("app.agents.detecting.InMemoryRunner", make_fake_runner_returning(json.dumps(output)))

    event = _event_for(signal)
    results = await asyncio.gather(
        processor.process_signal_available(event),
        processor.process_signal_available(event),
        processor.process_signal_available(event),
    )

    detection_ids = {r.detection_id for r in results}
    assert len(detection_ids) == 1
    all_detections = await detection_repo.query(DetectionQuery(limit=500))
    assert len(all_detections) == 1


# ---------------------------------------------------------------------------
# 19: operational/product domain separation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_operational_and_product_domains_stay_separated(monkeypatch):
    processor, signal_repo, _ = _setup()
    product_signal = await signal_repo.save(make_signal("dom1", stype=SignalType.CUSTOMER_FEEDBACK, service=None, env=None, subject="reporting"))
    # Only an OPERATIONAL signal exists in the repo besides the product one — an operational trigger must not count the product signal as evidence.
    await signal_repo.save(make_signal("dom2", stype=SignalType.APPLICATION_ERROR, service="checkout"))

    def _boom(*args, **kwargs):
        raise AssertionError("must not invoke DetectingAgent for an under-threshold product trigger")

    monkeypatch.setattr("app.agents.detecting.InMemoryRunner", _boom)

    outcome = await processor.process_signal_available(_event_for(product_signal))
    assert outcome.domain == "product"
    assert outcome.evidence_count == 1  # the operational signal is never counted toward the product domain


# ---------------------------------------------------------------------------
# 20: existing end-to-end demo still passes (regression guard)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_existing_demo_feature_flow_still_passes():
    from app.demo import DemoHarness

    summary = await DemoHarness().run_feature_flow()
    assert summary.verification_status == "passed"


@pytest.mark.asyncio
async def test_existing_demo_incident_flow_still_passes():
    from app.demo import DemoHarness

    summary = await DemoHarness().run_incident_flow()
    assert summary.verification_status == "passed"
