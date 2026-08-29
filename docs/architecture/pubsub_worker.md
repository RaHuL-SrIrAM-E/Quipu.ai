# Pub/Sub Signal Consumer Worker

## Diagram

```
Google Pub/Sub
      ↓
SignalConsumerWorker              (app/eventing/worker.py — THIS component)
  │  pull() -> bounded-concurrency dispatch -> per-message error isolation
  ↓
SignalIngestionService            (unchanged — app/eventing/ingestion_service.py)
  │  parse EventEnvelope -> normalize -> sanitize -> dedup -> persist -> ack
  ↓
SignalRepository / Firestore
  ↓
SignalAvailableEvent -> DetectionTrigger -> DetectionProcessor -> DetectingAgent
  ↓
DetectionResult / Firestore
```

## 1. Worker responsibility

`SignalConsumerWorker` is the execution boundary that turns Pub/Sub
ingestion from "a service you can call" into "a thing that actually runs
continuously." It owns exactly: pulling messages, bounded-concurrency
dispatch, per-message error isolation, graceful start/stop, and structured
observability. It owns **nothing** about envelope parsing, adapter
selection, sanitization, deduplication, persistence, or detection
triggering — all of that remains entirely inside `SignalIngestionService`/
`DetectionTrigger`, byte-for-byte unchanged by this task (Invariant 1/2).

## 2. Relationship to SignalIngestionService

The worker's per-message handler is exactly one line of business logic:
`outcome = await self._ingestion.ingest_one(message)`. Every classification
(malformed/unsupported/normalization-failure/persistence-failure),
every ack/nack decision, and the entire deduplication/dedup-then-persist
sequence happens inside `SignalIngestionService`, which this task does not
modify. The worker only translates `IngestOutcome` into its own counters
and structured logs (§7/§10) — it never second-guesses the ack decision
`ingest_one()` already made.

## 3. Message envelope mapping

Unchanged — the worker does not touch `EventEnvelope` at all.
`SignalIngestionService._parse_envelope()` still does the
Pub/Sub-bytes-to-`EventEnvelope` conversion (`event_id`, `source`,
`event_type`, `occurred_at`, `subject`, `payload`, `metadata`), exactly as
established in `docs/architecture/pubsub_signal_ingestion.md`. No second
envelope type exists anywhere in this task.

## 4. Ack/nack semantics

Preserved exactly, because the worker never makes an ack decision itself —
`message.ack()`/`message.nack()` are only ever called from inside
`SignalIngestionService.ingest_one()` (see that module). The worker's job
is limited to *not interfering*:

| `IngestOutcome` | Worker counter | Pub/Sub outcome |
|---|---|---|
| `acknowledged=True`, `category=None` | `messages_processed` | Ack'd (created or deduplicated) |
| `acknowledged=True`, `category` set | `messages_dropped` + `permanent_failures` | Ack'd + dropped per documented dead-letter policy |
| `acknowledged=False` | `messages_redelivered` (+ `persistence_failures` if that was the cause) | Left unacknowledged — Pub/Sub redelivers |

**Detection processing failure never reverses an ingestion ack**
(Invariant 4): `SignalIngestionService.ingest_one()` already acknowledges
the message and persists the Signal *before* it ever calls
`DetectionTrigger.on_signal_available()`, and it already swallows/logs
whatever that trigger raises (unchanged code, see that module's own
docstring). The worker adds nothing on top — it just observes whatever
`IngestOutcome` comes back, which is always `acknowledged=True` in that
case. Verified directly by
`test_detection_failure_does_not_cause_ingestion_redelivery`.

## 5. Concurrency

`Settings.pubsub_worker_max_concurrency` (default 10) bounds how many
messages are being parsed/normalized/persisted at once, via an
`asyncio.Semaphore` acquired before each message's processing task is
created and released in that task's `done_callback`. This is independent
of `Settings.pubsub_pull_max_messages` (how many messages one `pull()`
call can return) — a pull can return more messages than the concurrency
limit; the extra simply wait for a semaphore slot inside the same
dispatch loop before their processing task starts. One malformed/poison
message can never block or crash unrelated messages: `_process()` wraps
`ingest_one()` in its own `try/except`, and any exception escaping it
(a genuine bug, since `ingest_one()` already classifies every failure it
knows about internally) is logged and swallowed per-message, never
propagated to the loop or to other in-flight tasks.

## 6. Graceful shutdown

`stop()`: (1) sets an `asyncio.Event` the pull loop checks before starting
each new pull cycle and before dispatching each message in a batch — no
new work is accepted once set; (2) waits, bounded by
`Settings.pubsub_worker_shutdown_timeout_seconds` (default 30s), for the
loop to exit and every in-flight message-processing task to finish;
(3) if that deadline is exceeded, cancels the loop task and every
remaining in-flight task rather than waiting forever. `start()`/`stop()`
are both idempotent (calling either while already in that state is a
no-op). `run_forever()` is the blocking convenience a process entrypoint
uses: `await start(); await` the loop task, so an external signal handler
(see `app/eventing/worker_main.py`) can simply call `stop()` to unwind
cleanly.

## 7. Failure isolation

| Failure | Classification (unchanged, `app/eventing/errors.py`) | Worker behavior |
|---|---|---|
| Malformed envelope | Permanent | Counted in `messages_dropped`/`permanent_failures`; loop continues |
| Unsupported source/event type | Permanent | Same |
| Normalization failure | Permanent | Same |
| Firestore/persistence unavailable | Transient | `messages_redelivered` + `persistence_failures`; message left unacknowledged |
| Transient Pub/Sub `pull()` failure | N/A — no message was received | Logged (`worker.pull_failed`), loop backs off one poll interval, then retries |
| DetectionProcessor/DetectingAgent failure | N/A — already isolated by `SignalIngestionService` | No effect on the ingestion ack (§4) |
| Unexpected exception inside `ingest_one()` itself | N/A — should not happen; defense-in-depth | Logged (`worker.unexpected_processing_error`), counted in `processing_errors`, message left un-acked (safe default — redeliverable) |

## 8. Idempotency

No new deduplication mechanism (Invariant 3). The worker adds zero
identity/dedup logic of its own — `compute_fingerprint()` +
`SignalRepository.find_by_fingerprint()`, invoked from inside
`SignalIngestionService.ingest_one()`, remain the sole durable
deduplication boundary, exactly as established in
`docs/architecture/pubsub_signal_ingestion.md`. Pub/Sub `message_id` is
never read for anything but logging/correlation (Invariant 5) —
`test_pubsub_message_id_not_used_as_fingerprint` publishes the identical
envelope body twice (two distinct `message_id`s) and confirms exactly one
Signal is ever created. The worker's own concurrency (§5) is safe under
redelivery/duplicate/concurrent-delivery because it changes nothing about
*what* gets deduplicated — only *how many* `ingest_one()` calls can run at
once, each independently subject to the same fingerprint check.

## 9. Security

- ADC only — the worker never constructs a Google credential itself; it
  is handed a `PubSubConsumer` (in production, `GooglePubSubClient`,
  unmodified — still the only file allowed to import
  `google.cloud.pubsub_v1`).
- Message size bounding, the closed source/event-type allow-list, and all
  payload interpretation remain entirely inside
  `SignalIngestionService`/`app.signals.adapters`, unchanged.
- The worker never executes payload content, never dynamically imports an
  adapter from message data, never constructs a shell command, and never
  makes an outbound HTTP call of its own.
- No raw payload/customer feedback/monitoring body ever reaches a log
  line the worker emits — only `pubsub_message_id`, `delivery_attempt`,
  `acknowledged`, `category`, `signal_id`, `deduplicated`, and
  `duration_ms` (verified by `test_worker_does_not_log_raw_payload`).

## 10. Observability

`WorkerCounters` (`app/eventing/worker.py`): `messages_received`,
`messages_processed`, `messages_dropped`, `messages_redelivered`,
`persistence_failures`, `permanent_failures`, `processing_errors`,
`starts`, `stops`. Reuses `app.core.observability.get_logger` exactly like
every other Quipu component — no new/competing metrics subsystem. One
structured `worker.message_processed` log line per message; `worker.started`/
`worker.stopped` on lifecycle transitions; `worker.pull_failed`/
`worker.unexpected_processing_error`/`worker.shutdown_timeout` for the
failure paths in §7.

## 11. Google Pub/Sub boundary

`app/eventing/google_pubsub_client.py` (unmodified) remains the **only**
file importing `google.cloud.pubsub_v1`. The worker depends solely on the
`PubSubConsumer` Protocol (`pull(subscription, max_messages) ->
list[PubSubMessage]`) — already sufficient, so nothing was added to the
Google client for this task (§2 of the task: extend additively only if
needed; it wasn't). `test_worker_module_has_no_google_sdk_import` and
`test_no_google_sdk_import_leaks_outside_boundary` enforce Invariant 6
structurally, across every file in `app/eventing/` except that one client
module. `app/eventing/worker_main.py` (the process entrypoint) does
construct `GooglePubSubClient`/Firestore repositories, but contains no
business logic of its own — pure wiring, guarded by `if __name__ ==
"__main__"` so it is never imported by the test suite's normal collection
path.

## 12. Current sync-pull limitation

Unchanged from `docs/architecture/pubsub_signal_ingestion.md` §11: the
real Google client uses `SubscriberClient.pull()` (synchronous, wrapped in
`asyncio.to_thread`), not a persistent streaming-pull connection. The
worker's outer loop therefore polls: pull a batch, dispatch it, and if a
pull returns nothing, sleep for `Settings.
pubsub_worker_poll_interval_seconds` (default 5s, interruptible by
`stop()`) before pulling again. This is a real production limitation for
very low end-to-end latency, but is a correct, at-least-once-safe worker
for the ingestion volumes this architecture currently targets — never
silently redesigned into something the client doesn't actually implement.

## 13. Future streaming-pull option

If lower latency is later required, a streaming-pull `PubSubConsumer`
implementation could be added as a second class in
`app/eventing/google_pubsub_client.py` (using
`SubscriberClient.subscribe()`'s callback-based API instead of `pull()`),
exposing the **same** `PubSubConsumer.pull()`-shaped interface — or, if a
genuinely push-based Protocol is warranted, a new method could be added to
`PubSubConsumer` additively. Either way, `SignalConsumerWorker` was
deliberately written against the `PubSubConsumer` Protocol, not against
`GooglePubSubClient` directly, so this swap requires **no change to
`SignalIngestionService`, `DetectionProcessor`, or any business logic at
all** — exactly the abstraction boundary this task was asked to preserve.
