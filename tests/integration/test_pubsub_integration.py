"""Real Google Cloud Pub/Sub integration test — NOT part of the normal
test run.

Skipped unless QUIPU_RUN_PUBSUB_INTEGRATION_TESTS=true is set, and even
then requires real GCP credentials (Application Default Credentials) plus
a configured GCP_PROJECT_ID with a real topic/subscription
(PUBSUB_INTEGRATION_TEST_TOPIC / PUBSUB_INTEGRATION_TEST_SUBSCRIPTION,
already bound to that topic). `pytest tests/` never triggers this file's
network call — no personal/CI credentials are ever required for the normal
suite. Publishes one message and consumes+acks it — safe to run repeatedly
against a scratch topic/subscription.
"""

import asyncio
import json
import os
import uuid
from datetime import datetime, timezone

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("QUIPU_RUN_PUBSUB_INTEGRATION_TESTS") != "true",
    reason="set QUIPU_RUN_PUBSUB_INTEGRATION_TESTS=true (plus a real GCP_PROJECT_ID and a topic/subscription) to run",
)


@pytest.mark.asyncio
async def test_real_pubsub_publish_pull_ack_round_trip():
    from app.eventing.google_pubsub_client import GooglePubSubClient

    topic = os.environ.get("PUBSUB_INTEGRATION_TEST_TOPIC")
    subscription = os.environ.get("PUBSUB_INTEGRATION_TEST_SUBSCRIPTION")
    assert topic, "set PUBSUB_INTEGRATION_TEST_TOPIC to a real Pub/Sub topic name"
    assert subscription, "set PUBSUB_INTEGRATION_TEST_SUBSCRIPTION to a real subscription bound to that topic"

    client = GooglePubSubClient()
    marker = str(uuid.uuid4())
    envelope = {
        "event_id": marker,
        "source": "customer_feedback",
        "event_type": "feedback",
        "occurred_at": datetime.now(timezone.utc).isoformat(),
        "subject": "integration-test",
        "payload": {"feedback_id": marker, "submitted_at": datetime.now(timezone.utc).isoformat(), "text": "integration test message"},
        "metadata": {},
    }

    message_id = await client.publish(topic=topic, data=json.dumps(envelope).encode("utf-8"))
    assert message_id

    found = None
    for _ in range(10):
        messages = await client.pull(subscription=subscription, max_messages=10)
        for message in messages:
            body = json.loads(message.data.decode("utf-8"))
            if body.get("event_id") == marker:
                found = message
            else:
                await message.nack()  # don't consume unrelated messages sitting on the subscription
        if found is not None:
            break

    assert found is not None, "published message was not observed on the subscription within the poll budget"
    await found.ack()


@pytest.mark.asyncio
async def test_real_pubsub_ingestion_service_end_to_end():
    from app.eventing.google_pubsub_client import GooglePubSubClient
    from app.eventing.ingestion_service import SignalIngestionService
    from app.persistence.memory.repositories import InMemorySignalRepository

    topic = os.environ.get("PUBSUB_INTEGRATION_TEST_TOPIC")
    subscription = os.environ.get("PUBSUB_INTEGRATION_TEST_SUBSCRIPTION")
    assert topic, "set PUBSUB_INTEGRATION_TEST_TOPIC to a real Pub/Sub topic name"
    assert subscription, "set PUBSUB_INTEGRATION_TEST_SUBSCRIPTION to a real subscription bound to that topic"

    client = GooglePubSubClient()
    repo = InMemorySignalRepository()
    service = SignalIngestionService(repo)

    marker = str(uuid.uuid4())
    envelope = {
        "event_id": marker,
        "source": "customer_feedback",
        "event_type": "feedback",
        "occurred_at": datetime.now(timezone.utc).isoformat(),
        "subject": "integration-test",
        "payload": {"feedback_id": marker, "submitted_at": datetime.now(timezone.utc).isoformat(), "text": "integration test message"},
        "metadata": {},
    }
    await client.publish(topic=topic, data=json.dumps(envelope).encode("utf-8"))

    created_signal_id = None
    for _ in range(10):
        messages = await client.pull(subscription=subscription, max_messages=10)
        for message in messages:
            body = json.loads(message.data.decode("utf-8"))
            if body.get("event_id") != marker:
                await message.nack()
                continue
            outcome = await service.ingest_one(message)
            assert outcome.acknowledged is True
            created_signal_id = outcome.signal_id
        if created_signal_id is not None:
            break

    assert created_signal_id is not None
    saved = await repo.get(created_signal_id)
    assert saved is not None
    assert saved.provenance.source_event_id == marker


@pytest.mark.asyncio
async def test_real_pubsub_signal_consumer_worker_end_to_end():
    """The production SignalConsumerWorker (app.eventing.worker), run
    against a real Pub/Sub subscription for a short bounded window — the
    same worker app.eventing.worker_main wires into a long-running
    process, here run just long enough to observe one published message."""
    from app.eventing.google_pubsub_client import GooglePubSubClient
    from app.eventing.ingestion_service import SignalIngestionService
    from app.eventing.worker import SignalConsumerWorker
    from app.persistence.memory.repositories import InMemorySignalRepository
    from app.persistence.repositories.signal import SignalQuery

    topic = os.environ.get("PUBSUB_INTEGRATION_TEST_TOPIC")
    subscription = os.environ.get("PUBSUB_INTEGRATION_TEST_SUBSCRIPTION")
    assert topic, "set PUBSUB_INTEGRATION_TEST_TOPIC to a real Pub/Sub topic name"
    assert subscription, "set PUBSUB_INTEGRATION_TEST_SUBSCRIPTION to a real subscription bound to that topic"

    client = GooglePubSubClient()
    repo = InMemorySignalRepository()
    service = SignalIngestionService(repo)

    marker = str(uuid.uuid4())
    envelope = {
        "event_id": marker,
        "source": "customer_feedback",
        "event_type": "feedback",
        "occurred_at": datetime.now(timezone.utc).isoformat(),
        "subject": "integration-test",
        "payload": {"feedback_id": marker, "submitted_at": datetime.now(timezone.utc).isoformat(), "text": "worker integration test message"},
        "metadata": {},
    }
    await client.publish(topic=topic, data=json.dumps(envelope).encode("utf-8"))

    worker = SignalConsumerWorker(client, service, subscription=subscription, poll_interval_seconds=1.0)
    await worker.start()
    for _ in range(20):
        signals = await repo.query(SignalQuery(limit=50))
        if any(s.provenance.source_event_id == marker for s in signals):
            break
        await asyncio.sleep(1.0)
    await worker.stop()

    signals = await repo.query(SignalQuery(limit=50))
    assert any(s.provenance.source_event_id == marker for s in signals), "the worker did not observe the published message within the poll budget"
