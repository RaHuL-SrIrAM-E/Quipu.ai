"""DetectionProcessorTrigger — the real app.eventing.trigger.DetectionTrigger
implementation, wired to a DetectionProcessor. Lives outside app/eventing/
specifically so that package never has to import app.agents.detecting (and
therefore never imports google.adk/Gemini) — see
docs/architecture/event_driven_detection.md "Keep ingestion and reasoning
separate".

A DetectionProcessingError raised here propagates to whatever called
on_signal_available — for SignalIngestionService that's an already-caught,
already-logged, ack-independent boundary (see
app/eventing/ingestion_service.py: the message is acknowledged before this
trigger ever runs). This class adds no additional swallowing/retry logic
of its own; DetectionProcessor.process_signal_available is idempotent
(via DetectingAgent's existing fingerprint dedup), so re-invoking it later
with the same event is always safe.
"""

from app.detection.processor import DetectionProcessor
from app.eventing.trigger import SignalAvailableEvent


class DetectionProcessorTrigger:
    def __init__(self, processor: DetectionProcessor):
        self._processor = processor

    async def on_signal_available(self, event: SignalAvailableEvent) -> None:
        await self._processor.process_signal_available(event)
