"""The detection trigger boundary. SignalIngestionService must not call
DetectingAgent (or any orchestration engine) directly — it invokes this
narrow interface after a Signal is durably persisted, so the two
responsibilities (ingest evidence vs. reason about it) stay separated per
docs/architecture/event_driven_detection.md.

SignalAvailableEvent (not the full Signal) is what crosses this boundary —
a small, stable reference: signal_id, signal_type/source, and the
correlation dimensions (service_name/environment/subject) a detection
processor needs to decide what to do next. Deliberately not the raw
Signal.evidence/metadata dict, even though both are already sanitized by
the adapter that produced the Signal (app.signals.adapters) — narrowing
the boundary here means nothing downstream of this Protocol ever needs to
reason about a source's raw payload shape at all.

NoOpDetectionTrigger is a safe default: it records that a Signal became
available and does nothing else. app/detection/trigger.py provides the
real implementation (DetectionProcessorTrigger), which is where
Gemini/ADK-touching code actually lives — never in this module.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable

from app.core.observability import get_logger
from app.domain import Signal, SignalSource, SignalType

logger = get_logger("quipu.eventing.trigger")


@dataclass(frozen=True)
class SignalAvailableEvent:
    """A stable reference to a just-persisted Signal — never the raw
    Pub/Sub payload, never unsanitized data. Built by
    SignalIngestionService from the Signal it just saved."""

    signal_id: str
    signal_type: SignalType
    source: SignalSource
    subject: str
    service_name: str | None
    environment: str | None
    observed_at: datetime

    @classmethod
    def from_signal(cls, signal: Signal) -> "SignalAvailableEvent":
        return cls(
            signal_id=signal.signal_id,
            signal_type=signal.signal_type,
            source=signal.source,
            subject=signal.subject,
            service_name=signal.service_name,
            environment=signal.environment,
            observed_at=signal.observed_at,
        )


@runtime_checkable
class DetectionTrigger(Protocol):
    async def on_signal_available(self, event: SignalAvailableEvent) -> None: ...


class NoOpDetectionTrigger:
    """Logs that a Signal became available for detection; invokes no
    processor. A failure here must never be allowed to affect ingestion's
    ack decision — see SignalIngestionService.ingest_one, which persists
    and acks before ever calling the trigger."""

    async def on_signal_available(self, event: SignalAvailableEvent) -> None:
        logger.info("signal.available_for_detection signal_id=%s signal_type=%s", event.signal_id, event.signal_type.value)
