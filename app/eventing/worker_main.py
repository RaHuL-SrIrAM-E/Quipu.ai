"""Process entrypoint for the production Pub/Sub Signal Consumer Worker:

    python -m app.eventing.worker_main

Wires the real Google Pub/Sub client, real Firestore-backed repositories,
and the real DetectionProcessor/DetectingAgent behind the existing
DetectionTrigger boundary — every component reused unchanged from the
existing architecture (app/eventing/, app/detection/, app/agents/detecting.py).
This module adds no business logic: it is construction/wiring plus signal
handling for graceful shutdown, nothing else.

Requires GCP_PROJECT_ID, PUBSUB_SIGNAL_SUBSCRIPTION, and Application
Default Credentials to be configured (see .env.example) — this module is
never imported by the test suite's normal collection path, so it carries
no test-time Google dependency.
"""

import asyncio
import signal

from app.agent_runtime.gateways.detections import RepositoryDetectionGateway
from app.agent_runtime.gateways.signals import RepositorySignalGateway
from app.config import get_settings
from app.core.observability import get_logger
from app.detection.processor import DetectionProcessor
from app.detection.trigger import DetectionProcessorTrigger
from app.eventing.google_pubsub_client import GooglePubSubClient
from app.eventing.ingestion_service import SignalIngestionService
from app.eventing.worker import SignalConsumerWorker
from app.persistence.firestore.client import get_firestore_client
from app.persistence.firestore.repositories import FirestoreDetectionRepository, FirestoreSignalRepository

logger = get_logger("quipu.eventing.worker_main")


async def _run() -> None:
    settings = get_settings()
    if not settings.pubsub_signal_subscription:
        raise SystemExit("PUBSUB_SIGNAL_SUBSCRIPTION must be set to run the Signal Consumer Worker")

    firestore_client = get_firestore_client()
    signal_repo = FirestoreSignalRepository(firestore_client)
    detection_repo = FirestoreDetectionRepository(firestore_client)

    processor = DetectionProcessor(
        signal_gateway=RepositorySignalGateway(signal_repo),
        detection_gateway=RepositoryDetectionGateway(detection_repo),
    )
    ingestion_service = SignalIngestionService(signal_repo, detection_trigger=DetectionProcessorTrigger(processor))
    consumer = GooglePubSubClient()

    worker = SignalConsumerWorker(consumer, ingestion_service)

    loop = asyncio.get_running_loop()
    stop_requested = asyncio.Event()

    def _request_stop(sig_name: str) -> None:
        logger.info("worker_main.signal_received signal=%s", sig_name)
        stop_requested.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _request_stop, sig.name)

    await worker.start()
    logger.info("worker_main.running subscription=%s", settings.pubsub_signal_subscription)
    await stop_requested.wait()
    await worker.stop()
    logger.info(
        "worker_main.exit messages_received=%d messages_processed=%d messages_dropped=%d messages_redelivered=%d",
        worker.counters.messages_received,
        worker.counters.messages_processed,
        worker.counters.messages_dropped,
        worker.counters.messages_redelivered,
    )


def main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main()
