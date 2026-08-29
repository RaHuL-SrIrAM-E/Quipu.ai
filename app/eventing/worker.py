"""SignalConsumerWorker — the production execution boundary that turns
Pub/Sub ingestion from "a service you can call" into "a thing that
actually runs continuously." Framework-independent: it depends only on
the `PubSubConsumer` Protocol (app/eventing/protocols.py) and
`SignalIngestionService` (app/eventing/ingestion_service.py) — no Google
SDK import here, no Gemini/ADK, no business logic of its own.

This module owns exactly:

    pull messages -> bounded-concurrency dispatch ->
        SignalIngestionService.ingest_one() -> per-message error isolation
        -> counters/structured logs -> graceful start/stop

It does NOT own: envelope parsing, adapter selection, sanitization,
deduplication, persistence, or detection triggering — all of that remains
entirely inside SignalIngestionService/DetectionTrigger, unchanged. See
docs/architecture/pubsub_worker.md.

Sync-pull, not streaming-pull (same documented limitation as
app/eventing/google_pubsub_client.py): the worker's outer loop calls
`PubSubConsumer.pull()` on a fixed interval when idle, rather than holding
a persistent streaming-pull connection. This keeps the worker swappable —
a future streaming-pull `PubSubConsumer` implementation plugs in here
without any change to this file or to SignalIngestionService.
"""

import asyncio
import time
from dataclasses import dataclass

from app.config import get_settings
from app.core.observability import get_logger
from app.eventing.errors import IngestionFailureCategory
from app.eventing.ingestion_service import SignalIngestionService
from app.eventing.protocols import PubSubConsumer, PubSubMessage

logger = get_logger("quipu.eventing.worker")


@dataclass
class WorkerCounters:
    messages_received: int = 0
    messages_processed: int = 0  # acknowledged as a real outcome (created or deduplicated)
    messages_dropped: int = 0  # acknowledged + dropped per permanent-failure policy
    messages_redelivered: int = 0  # left unacknowledged — Pub/Sub will redeliver
    persistence_failures: int = 0
    permanent_failures: int = 0
    processing_errors: int = 0  # unexpected exception out of ingest_one() itself
    starts: int = 0
    stops: int = 0


class SignalConsumerWorker:
    """One instance = one bounded-concurrency consumer loop against a
    single subscription. Owns no ingestion/business logic — every message
    is handed to the injected `SignalIngestionService` unchanged.

    Lifecycle: `start()` launches the loop as a background task and
    returns immediately; `run_forever()` is the blocking convenience a
    process entrypoint uses (`await start(); await` the loop task);
    `stop()` signals shutdown and waits, bounded by
    `shutdown_timeout_seconds`, for in-flight messages to finish before
    cancelling anything still running.
    """

    def __init__(
        self,
        consumer: PubSubConsumer,
        ingestion_service: SignalIngestionService,
        *,
        subscription: str | None = None,
        max_concurrency: int | None = None,
        max_messages_per_pull: int | None = None,
        poll_interval_seconds: float | None = None,
        shutdown_timeout_seconds: float | None = None,
    ):
        settings = get_settings()
        self._consumer = consumer
        self._ingestion = ingestion_service

        self._subscription = subscription if subscription is not None else settings.pubsub_signal_subscription
        if not self._subscription:
            raise ValueError("a subscription must be provided (either explicitly or via Settings.pubsub_signal_subscription)")

        self._max_concurrency = max_concurrency if max_concurrency is not None else settings.pubsub_worker_max_concurrency
        self._max_messages_per_pull = max_messages_per_pull if max_messages_per_pull is not None else settings.pubsub_pull_max_messages
        self._poll_interval = poll_interval_seconds if poll_interval_seconds is not None else settings.pubsub_worker_poll_interval_seconds
        self._shutdown_timeout = shutdown_timeout_seconds if shutdown_timeout_seconds is not None else settings.pubsub_worker_shutdown_timeout_seconds

        self._semaphore = asyncio.Semaphore(self._max_concurrency)
        self._stopping = asyncio.Event()
        self._inflight: set[asyncio.Task] = set()
        self._run_task: asyncio.Task | None = None
        self.counters = WorkerCounters()

    @property
    def is_running(self) -> bool:
        return self._run_task is not None and not self._run_task.done()

    async def start(self) -> None:
        """Idempotent — calling start() while already running is a no-op."""
        if self.is_running:
            return
        self._stopping.clear()
        self.counters.starts += 1
        self._run_task = asyncio.create_task(self._run_loop())
        logger.info(
            "worker.started subscription=%s max_concurrency=%d max_messages_per_pull=%d",
            self._subscription,
            self._max_concurrency,
            self._max_messages_per_pull,
        )

    async def run_forever(self) -> None:
        """Blocking convenience for a process entrypoint: start the loop
        and wait for it (i.e. until stop() is called from elsewhere, or
        this coroutine itself is cancelled — e.g. by a signal handler
        cancelling the enclosing task)."""
        await self.start()
        assert self._run_task is not None
        await self._run_task

    async def stop(self) -> None:
        """Idempotent — calling stop() while not running is a no-op.
        Signals the loop to stop pulling new work, then waits up to
        shutdown_timeout_seconds for the loop and any in-flight message
        processing to finish; anything still running past that deadline
        is cancelled rather than awaited forever."""
        if not self.is_running:
            return
        self._stopping.set()
        run_task = self._run_task

        async def _await_everything() -> None:
            await run_task
            if self._inflight:
                await asyncio.gather(*list(self._inflight), return_exceptions=True)

        try:
            await asyncio.wait_for(_await_everything(), timeout=self._shutdown_timeout)
        except asyncio.TimeoutError:
            logger.warning(
                "worker.shutdown_timeout subscription=%s pending_inflight=%d — cancelling", self._subscription, len(self._inflight)
            )
            run_task.cancel()
            for task in list(self._inflight):
                task.cancel()
            await asyncio.gather(run_task, *list(self._inflight), return_exceptions=True)

        self._run_task = None
        self.counters.stops += 1
        logger.info("worker.stopped subscription=%s", self._subscription)

    async def _run_loop(self) -> None:
        try:
            while not self._stopping.is_set():
                messages = await self._pull_safe()
                if not messages:
                    await self._sleep_or_wake(self._poll_interval)
                    continue
                for message in messages:
                    await self._semaphore.acquire()
                    if self._stopping.is_set():
                        # Shutdown was requested while waiting for
                        # concurrency headroom — release immediately and
                        # leave this message unacknowledged for Pub/Sub to
                        # redeliver rather than starting new work.
                        self._semaphore.release()
                        break
                    task = asyncio.create_task(self._process(message))
                    self._inflight.add(task)
                    task.add_done_callback(self._on_task_done)
        except asyncio.CancelledError:
            pass

    def _on_task_done(self, task: asyncio.Task) -> None:
        self._inflight.discard(task)
        self._semaphore.release()

    async def _pull_safe(self) -> list[PubSubMessage]:
        try:
            return await self._consumer.pull(subscription=self._subscription, max_messages=self._max_messages_per_pull)
        except Exception:
            # A pull-level failure (transient Pub/Sub/network issue) must
            # never crash the worker — log and back off for one poll
            # interval, then try again. No message was received, so
            # nothing here needs ack/nack handling.
            logger.exception("worker.pull_failed subscription=%s", self._subscription)
            await self._sleep_or_wake(self._poll_interval)
            return []

    async def _sleep_or_wake(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._stopping.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass

    async def _process(self, message: PubSubMessage) -> None:
        """Per-message error isolation (§9 of the task): SignalIngestionService
        already classifies and handles every ingestion failure it knows
        about internally (see app/eventing/ingestion_service.py) —
        anything escaping ingest_one() itself is a genuinely unexpected
        bug, logged and swallowed here so ONE poison message can never
        take down the worker or block unrelated messages."""
        self.counters.messages_received += 1
        started = time.perf_counter()
        try:
            outcome = await self._ingestion.ingest_one(message)
        except Exception:
            self.counters.processing_errors += 1
            logger.exception(
                "worker.unexpected_processing_error pubsub_message_id=%s delivery_attempt=%d", message.message_id, message.delivery_attempt
            )
            return
        duration_ms = (time.perf_counter() - started) * 1000

        if outcome.acknowledged:
            if outcome.category is not None:
                self.counters.messages_dropped += 1
                self.counters.permanent_failures += 1
            else:
                self.counters.messages_processed += 1
        else:
            self.counters.messages_redelivered += 1
            if outcome.category == IngestionFailureCategory.PERSISTENCE_FAILURE:
                self.counters.persistence_failures += 1

        # Never the raw message body — only ids/classification/outcome.
        logger.info(
            "worker.message_processed pubsub_message_id=%s delivery_attempt=%d acknowledged=%s category=%s signal_id=%s deduplicated=%s duration_ms=%.1f",
            message.message_id,
            message.delivery_attempt,
            outcome.acknowledged,
            outcome.category.value if outcome.category else None,
            outcome.signal_id,
            outcome.deduplicated,
            duration_ms,
        )
