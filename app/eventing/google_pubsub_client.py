"""Thin Google Cloud Pub/Sub client wrapper — the ONLY place in the
repository allowed to import google.cloud.pubsub_v1. Same pattern as
app/core/cloud_run_client.py / cloud_monitoring_client.py /
cloud_logging_client.py: lazy client construction, Application Default
Credentials only (no service-account key files, no embedded credentials,
no shell/gcloud commands), errors translated at the boundary via the
shared app.core.google_api_errors hierarchy.

google-cloud-pubsub's PublisherClient/SubscriberClient are synchronous
(grpc) clients with no async variant, unlike monitoring_v3/logging_v2 —
each blocking call is run via asyncio.to_thread so this class still
satisfies the async PubSubPublisher/PubSubConsumer Protocols
(app/eventing/protocols.py) that the rest of app/eventing/ depends on.
"""

import asyncio

from google.api_core import exceptions as google_exceptions
from google.cloud import pubsub_v1

from app.config import get_settings
from app.core.google_api_errors import GoogleApiConfigError, translate_google_api_error
from app.eventing.protocols import PubSubMessage


class GooglePubSubClient:
    """`publisher`/`subscriber` are injectable for tests — pass fakes;
    production code leaves them unset and real clients are created lazily
    on first use, never at construction time."""

    def __init__(self, publisher: "pubsub_v1.PublisherClient | None" = None, subscriber: "pubsub_v1.SubscriberClient | None" = None):
        settings = get_settings()
        if not settings.gcp_project_id:
            raise GoogleApiConfigError("GCP_PROJECT_ID is not set")
        self.project_id = settings.gcp_project_id
        self._settings = settings
        self._publisher = publisher
        self._subscriber = subscriber

    def _get_publisher(self) -> "pubsub_v1.PublisherClient":
        if self._publisher is None:
            self._publisher = pubsub_v1.PublisherClient()
        return self._publisher

    def _get_subscriber(self) -> "pubsub_v1.SubscriberClient":
        if self._subscriber is None:
            self._subscriber = pubsub_v1.SubscriberClient()
        return self._subscriber

    async def publish(self, *, topic: str, data: bytes, attributes: dict[str, str] | None = None) -> str:
        publisher = self._get_publisher()
        topic_path = publisher.topic_path(self.project_id, topic)
        try:
            return await asyncio.to_thread(
                lambda: publisher.publish(topic_path, data, **(attributes or {})).result(timeout=self._settings.pubsub_api_timeout_seconds)
            )
        except google_exceptions.GoogleAPICallError as exc:
            raise translate_google_api_error(exc, context="Pub/Sub publish") from exc
        except TimeoutError as exc:
            raise translate_google_api_error(exc, context="Pub/Sub publish") from exc

    async def pull(self, *, subscription: str, max_messages: int) -> list[PubSubMessage]:
        subscriber = self._get_subscriber()
        subscription_path = subscriber.subscription_path(self.project_id, subscription)
        try:
            response = await asyncio.to_thread(
                subscriber.pull,
                subscription=subscription_path,
                max_messages=max_messages,
                timeout=self._settings.pubsub_api_timeout_seconds,
            )
        except google_exceptions.GoogleAPICallError as exc:
            raise translate_google_api_error(exc, context="Pub/Sub pull") from exc
        except TimeoutError as exc:
            raise translate_google_api_error(exc, context="Pub/Sub pull") from exc

        messages: list[PubSubMessage] = []
        for received in response.received_messages:
            ack_id = received.ack_id
            messages.append(
                PubSubMessage(
                    message_id=received.message.message_id,
                    data=bytes(received.message.data),
                    attributes=dict(received.message.attributes),
                    delivery_attempt=received.delivery_attempt or 1,
                    ack=self._make_ack(subscriber, subscription_path, ack_id),
                    nack=self._make_nack(subscriber, subscription_path, ack_id),
                )
            )
        return messages

    def _make_ack(self, subscriber: "pubsub_v1.SubscriberClient", subscription_path: str, ack_id: str):
        async def _ack() -> None:
            try:
                await asyncio.to_thread(subscriber.acknowledge, subscription=subscription_path, ack_ids=[ack_id])
            except google_exceptions.GoogleAPICallError as exc:
                raise translate_google_api_error(exc, context="Pub/Sub acknowledge") from exc

        return _ack

    def _make_nack(self, subscriber: "pubsub_v1.SubscriberClient", subscription_path: str, ack_id: str):
        async def _nack() -> None:
            try:
                await asyncio.to_thread(
                    subscriber.modify_ack_deadline, subscription=subscription_path, ack_ids=[ack_id], ack_deadline_seconds=0
                )
            except google_exceptions.GoogleAPICallError as exc:
                raise translate_google_api_error(exc, context="Pub/Sub modify_ack_deadline") from exc

        return _nack
