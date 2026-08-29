"""The detection trigger boundary. SignalIngestionService must not call
DetectingAgent (or any orchestration engine) directly — it invokes this
narrow interface after a Signal is durably persisted, so the two
responsibilities (ingest evidence vs. reason about it) stay separated per
docs/architecture/pubsub_signal_ingestion.md "Why DetectingAgent is not
directly embedded into ingestion".

NoOpDetectionTrigger is the production-safe default for this task: it
records that a Signal became available for detection and does nothing
else. Wiring an actual event-driven DetectingAgent invocation behind this
interface is explicitly deferred to a subsequent task.
"""

from typing import Protocol, runtime_checkable

from app.core.observability import get_logger
from app.domain import Signal

logger = get_logger("quipu.eventing.trigger")


@runtime_checkable
class DetectionTrigger(Protocol):
    async def on_signal_available(self, signal: Signal) -> None: ...


class NoOpDetectionTrigger:
    """Logs that a Signal became available for detection; does not invoke
    DetectingAgent or any orchestration. A failure here must never be
    allowed to affect ingestion's ack decision — see
    SignalIngestionService.ingest_one, which persists+acks before ever
    calling the trigger."""

    async def on_signal_available(self, signal: Signal) -> None:
        logger.info("signal.available_for_detection signal_id=%s signal_type=%s", signal.signal_id, signal.signal_type.value)
