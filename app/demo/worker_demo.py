"""A small, additive worker-level demonstration — separate from
DemoHarness (app/demo/harness.py, unchanged), which demonstrates the
SDLC/incident-remediation *agent* flows. This module demonstrates the
event-consumption *infrastructure* boundary added in a later task:

    Pub/Sub event -> SignalConsumerWorker -> SignalIngestionService
        -> Signal persisted -> DetectionTrigger -> DetectionProcessor
        -> DetectingAgent -> DetectionResult

Uses the real in-memory Pub/Sub broker, the real SignalIngestionService,
SignalConsumerWorker, DetectionProcessor, and DetectingAgent (with its
internal ADK runner monkeypatched, same convention as
app/demo/patching.py — but a DYNAMIC fake here, since the Signal ids
DetectingAgent must reference don't exist until the worker has actually
ingested them) — no Google Cloud credentials required. Not imported by
any production module.
"""

import asyncio
import json
from dataclasses import dataclass
from datetime import datetime, timezone

from google.genai import types

from app.agent_runtime.gateways.detections import RepositoryDetectionGateway
from app.agent_runtime.gateways.signals import RepositorySignalGateway
from app.detection.processor import DetectionProcessor
from app.detection.trigger import DetectionProcessorTrigger
from app.eventing.ingestion_service import SignalIngestionService
from app.eventing.memory_pubsub import InMemoryPubSub
from app.eventing.worker import SignalConsumerWorker
from app.persistence.memory.repositories import InMemoryDetectionRepository, InMemorySignalRepository
from app.persistence.repositories.detection import DetectionQuery
from app.persistence.repositories.signal import SignalQuery

_TOPIC = "worker-demo-topic"
_SUBSCRIPTION = "worker-demo-subscription"


class _FinalEvent:
    def __init__(self, content):
        self.content = content

    def is_final_response(self) -> bool:
        return True


@dataclass
class WorkerDemoResult:
    signal_ids: list[str]
    detection_id: str | None
    detection_type: str | None
    messages_processed: int
    messages_dropped: int


def _feedback_event(feedback_id: str, text: str) -> bytes:
    now = datetime.now(timezone.utc).isoformat()
    body = {
        "event_id": f"worker-demo-{feedback_id}",
        "source": "customer_feedback",
        "event_type": "feedback",
        "occurred_at": now,
        "subject": "reporting",
        "payload": {"feedback_id": feedback_id, "submitted_at": now, "text": text},
        "metadata": {},
    }
    return json.dumps(body).encode("utf-8")


class _EchoingFakeRunner:
    """Reads the REAL evidence set DetectingAgent assembled (the Signal
    ids the worker just persisted, which don't exist ahead of time — see
    module docstring) and echoes them back as supporting_signal_ids,
    rather than a fixed canned response. Same
    _CapturingSessionService/_FakeEvent shape
    tests/test_detecting_agent.py already establishes."""

    class _Session:
        id = "worker-demo-session"

    class _SessionService:
        def __init__(self):
            self.captured_state: dict = {}

        async def create_session(self, **kwargs):
            self.captured_state = kwargs.get("state", {})
            return _EchoingFakeRunner._Session()

    def __init__(self, agent, app_name):
        self.session_service = self._SessionService()

    def run_async(self, **kwargs):
        async def _events():
            evidence_set = self.session_service.captured_state.get("evidence_set") or []
            signal_ids = [item["signal_id"] for item in evidence_set]
            output = {
                "detection_type": "feature_opportunity" if signal_ids else "no_action",
                "title": "Add CSV export" if signal_ids else "No evidence",
                "summary": "Multiple customers requested CSV export." if signal_ids else "No signals available.",
                "rationale": "Independent feedback signals converge on the same request." if signal_ids else "No evidence retrieved.",
                "confidence": 0.85 if signal_ids else 0.0,
                "subject": "reporting",
                "supporting_signal_ids": signal_ids,
                "knowledge_references": [],
            }
            content = types.Content(role="model", parts=[types.Part(text=json.dumps(output))])
            yield _FinalEvent(content)

        return _events()


async def run_worker_demo() -> WorkerDemoResult:
    """Publishes two related customer-feedback events (enough evidence for
    DetectionProcessor's minimum-product-signals gate — see
    app.detection.policy.AggregationPolicy), runs the real
    SignalConsumerWorker against them, and returns what was produced: a
    Signal per event, and one DetectionResult once the worker's
    DetectionTrigger fires DetectionProcessor -> DetectingAgent for the
    second (evidence-satisfying) delivery."""
    import app.agents.detecting as detecting_module
    from app.demo.patching import patched_attr

    signal_repo = InMemorySignalRepository()
    detection_repo = InMemoryDetectionRepository()
    processor = DetectionProcessor(
        signal_gateway=RepositorySignalGateway(signal_repo),
        detection_gateway=RepositoryDetectionGateway(detection_repo),
    )
    ingestion_service = SignalIngestionService(signal_repo, detection_trigger=DetectionProcessorTrigger(processor))

    broker = InMemoryPubSub()
    broker.bind_subscription(topic=_TOPIC, subscription=_SUBSCRIPTION)
    await broker.publish(topic=_TOPIC, data=_feedback_event("worker-demo-fb-1", "Please add CSV export"))
    await broker.publish(topic=_TOPIC, data=_feedback_event("worker-demo-fb-2", "CSV export would help our team a lot"))

    worker = SignalConsumerWorker(broker, ingestion_service, subscription=_SUBSCRIPTION, poll_interval_seconds=0.01, max_concurrency=5)

    with patched_attr(detecting_module, "InMemoryRunner", _EchoingFakeRunner):
        await worker.start()
        for _ in range(500):
            if worker.counters.messages_processed >= 2:
                break
            await asyncio.sleep(0.01)
        await worker.stop()

    signals = await signal_repo.query(SignalQuery(limit=50))
    detections = await detection_repo.query(DetectionQuery(limit=50))

    return WorkerDemoResult(
        signal_ids=[s.signal_id for s in signals],
        detection_id=detections[0].detection_id if detections else None,
        detection_type=detections[0].detection_type.value if detections else None,
        messages_processed=worker.counters.messages_processed,
        messages_dropped=worker.counters.messages_dropped,
    )
