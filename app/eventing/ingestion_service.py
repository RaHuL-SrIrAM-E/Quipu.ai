"""SignalIngestionService — the framework-independent core of Pub/Sub
Signal ingestion. Owns exactly one responsibility chain:

    PubSubMessage -> bound size -> parse EventEnvelope -> resolve adapter
        (allow-list) -> normalize (existing app.signals.adapters, which
        already sanitizes + computes the fingerprint) -> dedup via
        SignalRepository.find_by_fingerprint -> persist -> ack ->
        DetectionTrigger.on_signal_available

It explicitly does NOT: detect incidents or feature opportunities, call
Gemini, invoke DetectingAgent directly, decide remediation, or create Jira
tickets. See docs/architecture/pubsub_signal_ingestion.md.

Ack discipline (the core idempotency/at-least-once contract): a message is
acknowledged only after either (a) its Signal is durably persisted, or (b)
it is classified as a PERMANENT failure per the documented dead-letter
policy in app/eventing/errors.py. Anything else (a transient/persistence
failure) is left unacknowledged so Pub/Sub redelivers it — this service
never claims exactly-once semantics.
"""

import json
from dataclasses import dataclass, field

from app.config import get_settings
from app.core.observability import get_logger
from app.domain import Signal
from app.eventing.envelope import EventEnvelope
from app.eventing.errors import IngestionError, IngestionFailureCategory
from app.eventing.mapping import SUPPORTED_SOURCES, resolve_adapter
from app.eventing.protocols import PubSubMessage
from app.eventing.trigger import DetectionTrigger, NoOpDetectionTrigger, SignalAvailableEvent
from app.persistence.repositories.signal import SignalRepository

logger = get_logger("quipu.eventing.ingestion")


@dataclass
class IngestOutcome:
    acknowledged: bool
    detail: str
    signal_id: str | None = None
    deduplicated: bool = False
    category: IngestionFailureCategory | None = None


@dataclass
class IngestionCounters:
    messages_received: int = 0
    signals_created: int = 0
    signals_deduplicated: int = 0
    messages_rejected: int = 0  # permanent failures — acked and dropped
    messages_failed: int = 0  # transient failures — left unacknowledged


class SignalIngestionService:
    def __init__(
        self,
        signal_repository: SignalRepository,
        *,
        detection_trigger: DetectionTrigger | None = None,
        max_message_bytes: int | None = None,
    ):
        self._signals = signal_repository
        self._trigger = detection_trigger or NoOpDetectionTrigger()
        self._max_message_bytes = max_message_bytes if max_message_bytes is not None else get_settings().pubsub_max_message_bytes
        self.counters = IngestionCounters()

    async def ingest_one(self, message: PubSubMessage) -> IngestOutcome:
        self.counters.messages_received += 1

        try:
            envelope = self._parse_envelope(message)
            signal = self._normalize(envelope)
        except IngestionError as exc:
            return await self._handle_pre_persist_failure(message, exc)

        existing = await self._signals.find_by_fingerprint(signal.fingerprint)
        if existing is not None:
            self.counters.signals_deduplicated += 1
            logger.info(
                "ingestion.duplicate pubsub_message_id=%s event_id=%s source=%s event_type=%s signal_id=%s",
                message.message_id,
                envelope.event_id,
                envelope.source.value,
                envelope.event_type.value,
                existing.signal_id,
            )
            await message.ack()
            return IngestOutcome(acknowledged=True, signal_id=existing.signal_id, deduplicated=True, detail="duplicate")

        try:
            saved = await self._signals.save(signal)
        except Exception as exc:
            self.counters.messages_failed += 1
            logger.warning(
                "ingestion.persistence_failed pubsub_message_id=%s event_id=%s source=%s event_type=%s error=%s",
                message.message_id,
                envelope.event_id,
                envelope.source.value,
                envelope.event_type.value,
                type(exc).__name__,
            )
            return IngestOutcome(acknowledged=False, category=IngestionFailureCategory.PERSISTENCE_FAILURE, detail="persistence failed, will retry")

        self.counters.signals_created += 1
        logger.info(
            "ingestion.created pubsub_message_id=%s event_id=%s source=%s event_type=%s signal_id=%s",
            message.message_id,
            envelope.event_id,
            envelope.source.value,
            envelope.event_type.value,
            saved.signal_id,
        )
        await message.ack()

        try:
            await self._trigger.on_signal_available(SignalAvailableEvent.from_signal(saved))
        except Exception:
            # The Signal is already persisted and the message already
            # acked — a trigger failure must never retract either. See
            # app/eventing/trigger.py.
            logger.exception("ingestion.trigger_failed signal_id=%s (signal remains persisted, message remains acked)", saved.signal_id)

        return IngestOutcome(acknowledged=True, signal_id=saved.signal_id, detail="created")

    async def _handle_pre_persist_failure(self, message: PubSubMessage, exc: IngestionError) -> IngestOutcome:
        if exc.retryable:
            self.counters.messages_failed += 1
            logger.warning("ingestion.transient_failure pubsub_message_id=%s category=%s detail=%s", message.message_id, exc.category.value, str(exc))
            return IngestOutcome(acknowledged=False, category=exc.category, detail=str(exc))

        self.counters.messages_rejected += 1
        logger.warning("ingestion.rejected pubsub_message_id=%s category=%s detail=%s", message.message_id, exc.category.value, str(exc))
        await message.ack()
        return IngestOutcome(acknowledged=True, category=exc.category, detail=str(exc))

    def _parse_envelope(self, message: PubSubMessage) -> EventEnvelope:
        if len(message.data) > self._max_message_bytes:
            raise IngestionError(IngestionFailureCategory.PAYLOAD_TOO_LARGE, f"message exceeds {self._max_message_bytes} byte bound")
        try:
            raw = json.loads(message.data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise IngestionError(IngestionFailureCategory.MALFORMED_ENVELOPE, f"message body is not valid JSON: {exc}") from exc
        try:
            return EventEnvelope.model_validate(raw)
        except Exception as exc:
            raise IngestionError(IngestionFailureCategory.MALFORMED_ENVELOPE, f"envelope failed validation: {exc}") from exc

    def _normalize(self, envelope: EventEnvelope) -> Signal:
        if envelope.source not in SUPPORTED_SOURCES:
            raise IngestionError(IngestionFailureCategory.UNSUPPORTED_SOURCE, f"unsupported source '{envelope.source.value}'")
        adapter = resolve_adapter(envelope.source, envelope.event_type)
        if adapter is None:
            raise IngestionError(
                IngestionFailureCategory.UNSUPPORTED_EVENT_TYPE,
                f"unsupported event_type '{envelope.event_type.value}' for source '{envelope.source.value}'",
            )
        try:
            return adapter(envelope.payload)
        except ValueError as exc:
            raise IngestionError(IngestionFailureCategory.NORMALIZATION_FAILURE, str(exc)) from exc
