"""Pub/Sub-based event ingestion for Quipu Signals (see
docs/architecture/pubsub_signal_ingestion.md).

Pub/Sub transports events. It does not become the Signal domain model —
Signal (app.domain.signal) remains the canonical normalized representation.
This package's job ends at "a Signal is durably persisted"; detection,
diagnosis, and remediation live entirely outside it.

    Cloud/event producers -> Pub/Sub -> SignalIngestionService ->
        (identify -> validate -> normalize via app.signals.adapters ->
         dedup via SignalRepository.find_by_fingerprint) -> Firestore

Framework-independent: nothing outside app/eventing/google_pubsub_client.py
imports google.cloud.pubsub. Everything else in this package (and every
caller of it) depends only on the PubSubPublisher/PubSubConsumer/
PubSubMessage Protocols in app/eventing/protocols.py.
"""

from app.eventing.envelope import EventEnvelope, IngestionEventType
from app.eventing.errors import IngestionError, IngestionFailureCategory
from app.eventing.ingestion_service import IngestOutcome, SignalIngestionService
from app.eventing.protocols import PubSubConsumer, PubSubMessage, PubSubPublisher
from app.eventing.trigger import DetectionTrigger, NoOpDetectionTrigger, SignalAvailableEvent
from app.eventing.worker import SignalConsumerWorker, WorkerCounters

__all__ = [
    "DetectionTrigger",
    "EventEnvelope",
    "IngestOutcome",
    "IngestionError",
    "IngestionEventType",
    "IngestionFailureCategory",
    "NoOpDetectionTrigger",
    "PubSubConsumer",
    "PubSubMessage",
    "PubSubPublisher",
    "SignalAvailableEvent",
    "SignalConsumerWorker",
    "SignalIngestionService",
    "WorkerCounters",
]
