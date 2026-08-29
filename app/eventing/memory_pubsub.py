"""In-memory Pub/Sub implementation of the same Protocols
(app/eventing/protocols.py) the real Google client implements — not a
mock, a genuine (if simplified) message broker: publish() enqueues,
pull() dequeues up to max_messages and marks them outstanding, ack()
removes an outstanding message permanently, and not acking (or calling
nack()) makes it eligible for redelivery again. Used by every ingestion
test so the full pipeline — including at-least-once redelivery — is
exercised without any Google Cloud credentials.
"""

import uuid
from collections import defaultdict, deque

from app.eventing.protocols import PubSubMessage


class InMemoryPubSub:
    def __init__(self) -> None:
        self._queues: dict[str, deque[PubSubMessage]] = defaultdict(deque)
        self._outstanding: dict[str, dict[str, PubSubMessage]] = defaultdict(dict)
        # topic -> subscriptions bound to it, so publish() fans out the same
        # way a real Pub/Sub topic with multiple subscriptions would.
        self._subscriptions_by_topic: dict[str, list[str]] = defaultdict(list)

    def bind_subscription(self, *, topic: str, subscription: str) -> None:
        self._subscriptions_by_topic[topic].append(subscription)

    async def publish(self, *, topic: str, data: bytes, attributes: dict[str, str] | None = None) -> str:
        message_id = str(uuid.uuid4())
        for subscription in self._subscriptions_by_topic.get(topic, [topic]):
            self._queues[subscription].append(
                PubSubMessage(message_id=message_id, data=data, attributes=dict(attributes or {}))
            )
        return message_id

    async def pull(self, *, subscription: str, max_messages: int) -> list[PubSubMessage]:
        queue = self._queues[subscription]
        pulled: list[PubSubMessage] = []
        for _ in range(min(max_messages, len(queue))):
            message = queue.popleft()
            bound = PubSubMessage(
                message_id=message.message_id,
                data=message.data,
                attributes=message.attributes,
                delivery_attempt=message.delivery_attempt,
                ack=self._make_ack(subscription, message.message_id),
                nack=self._make_nack(subscription, message.message_id),
            )
            self._outstanding[subscription][message.message_id] = message
            pulled.append(bound)
        return pulled

    def _make_ack(self, subscription: str, message_id: str):
        async def _ack() -> None:
            self._outstanding[subscription].pop(message_id, None)

        return _ack

    def _make_nack(self, subscription: str, message_id: str):
        async def _nack() -> None:
            message = self._outstanding[subscription].pop(message_id, None)
            if message is not None:
                message.delivery_attempt += 1
                self._queues[subscription].appendleft(message)

        return _nack

    def redeliver_unacked(self, *, subscription: str) -> None:
        """Test helper simulating an expired ack deadline: puts every
        currently-outstanding (never acked, never explicitly nacked)
        message for a subscription back on the queue for redelivery."""
        outstanding = self._outstanding[subscription]
        for message_id in list(outstanding):
            message = outstanding.pop(message_id)
            message.delivery_attempt += 1
            self._queues[subscription].appendleft(message)
