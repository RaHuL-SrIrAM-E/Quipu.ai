"""Framework-independent Pub/Sub abstraction. No google.cloud import
anywhere in this file — see app/eventing/google_pubsub_client.py for the
one module allowed to import the real SDK, and
app/eventing/memory_pubsub.py for the in-memory implementation tests use.

PubSubMessage.ack()/nack() are the only way a consumer influences delivery
— each is a bound async callable the PubSubConsumer implementation wires
per message (closing over its own subscription + transport-layer id), so
SignalIngestionService never needs to know which subscription a message
came from. It acks only after a Signal is durably persisted (or a message
is permanently unprocessable per documented policy — see
app/eventing/errors.py) and never acks anything that should be retried.
This mirrors Pub/Sub's real at-least-once semantics — see
docs/architecture/pubsub_signal_ingestion.md "Ack semantics".
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


@dataclass
class PubSubMessage:
    """One delivered message. `message_id` is the transport-layer id Google
    (or the in-memory fake) assigns — NOT trusted as a Signal idempotency
    key (see EventEnvelope/adapters, which key on producer-supplied
    source_event_id fields instead); it's carried only for logging/
    correlation and dead-letter bookkeeping. `attributes` carries Pub/Sub
    message attributes (unused by ingestion today, but part of the real
    message shape); `data` is the raw envelope body bytes."""

    message_id: str
    data: bytes
    attributes: dict[str, str] = field(default_factory=dict)
    delivery_attempt: int = 1
    ack: Callable[[], Awaitable[None]] = field(default=None, repr=False, compare=False)  # type: ignore[assignment]
    nack: Callable[[], Awaitable[None]] = field(default=None, repr=False, compare=False)  # type: ignore[assignment]


@runtime_checkable
class PubSubPublisher(Protocol):
    async def publish(self, *, topic: str, data: bytes, attributes: dict[str, str] | None = None) -> str:
        """Publishes one message, returning the assigned message_id."""
        ...


@runtime_checkable
class PubSubConsumer(Protocol):
    """A pull-based boundary (matches the real Pub/Sub SDK's synchronous
    pull/acknowledge API — see app/eventing/google_pubsub_client.py) rather
    than a callback-based streaming-pull subscription: simpler to reason
    about ack/nack timing, and sufficient for this task's scope (a bounded
    polling loop, not a persistent streaming worker). Returned messages
    carry their own bound ack()/nack()."""

    async def pull(self, *, subscription: str, max_messages: int) -> list[PubSubMessage]: ...
