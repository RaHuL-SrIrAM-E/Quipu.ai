# Pub/Sub Signal Ingestion

## Diagram

```
Cloud Monitoring ──┐
Cloud Logging ─────┤
Customer feedback ─┤
Support events ────┤
Product analytics ─┘
                    ↓
              Google Pub/Sub
                    ↓
         SignalIngestionService
        (app/eventing/ingestion_service.py)
                    ↓
   parse EventEnvelope → resolve adapter (allow-list)
                    ↓
   app.signals.adapters.normalize_*   (existing, unchanged)
       — sanitizes evidence, computes fingerprint —
                    ↓
   SignalRepository.find_by_fingerprint (dedup check)
                    ↓
              SignalRepository.save
                    ↓
                Firestore
                    ↓
      DetectionTrigger.on_signal_available (no-op today)
                    ↓
        [future: Detection orchestration]
```

## 1. Why Pub/Sub exists in Quipu

Every Signal source Quipu already knows how to normalize
(`app/signals/adapters.py`) previously had no real-time delivery mechanism
of its own — MonitoringAgent pulls Cloud Monitoring/Logging on a schedule,
and the product-signal adapters (customer feedback, support, user
behavior) had no ingestion path at all. Pub/Sub is the event-driven
transport that lets external producers (Cloud Monitoring alerting,
Cloud Logging sinks, a feedback/support system, a product-analytics
pipeline) push events to Quipu as they happen, instead of Quipu only ever
polling.

**Pub/Sub transports events. It does not become the Signal domain model.**
`app.domain.signal.Signal` remains the single canonical, normalized
representation of "what was observed" — exactly as it was before this
task. Nothing in `app/eventing/` is persisted, queried, or reasoned about
by any other part of Quipu; only the `Signal` objects it produces are.

## 2. Producer → Pub/Sub → Signal flow

1. A producer publishes a JSON-encoded `EventEnvelope` (§3) to a Pub/Sub
   topic.
2. `SignalIngestionService.ingest_one()` (`app/eventing/ingestion_service.py`)
   pulls one message at a time from a bound `PubSubConsumer`.
3. The message body is bounded, JSON-decoded, and validated into an
   `EventEnvelope`.
4. `(envelope.source, envelope.event_type)` is looked up in a fixed
   allow-list (`app/eventing/mapping.py`) that routes to one of the
   **existing** `app.signals.adapters.normalize_*` functions — no new
   normalization logic is added anywhere in this task.
5. The adapter returns a `Signal` (already sanitized, already
   fingerprinted — see §5, §8 of `docs/architecture/signal_platform.md`).
6. `SignalRepository.find_by_fingerprint()` checks for an existing Signal
   with the same fingerprint (§5 below).
7. If none exists, the Signal is persisted via `SignalRepository.save()`
   and the message is acknowledged.
8. `DetectionTrigger.on_signal_available()` is invoked (§13) — after, and
   only after, persistence succeeded.

## 3. Event envelope

`app/eventing/envelope.py::EventEnvelope`:

| Field | Type | Purpose |
|---|---|---|
| `event_id` | `str` | Producer-assigned correlation/audit id. **Not** the Signal dedup key (§5). |
| `source` | `SignalSource` (existing enum) | Closed set — the same enum every Signal already uses. |
| `event_type` | `IngestionEventType` (new, closed `StrEnum`) | Which adapter payload shape this message carries: `alert`, `metric_observation`, `log_entry`, `feedback`, `pattern`. |
| `occurred_at` | `datetime`, tz-aware required | Envelope-level timestamp, for audit/routing — the Signal's own `observed_at` is still derived by the adapter from `payload` (unchanged adapter behavior). |
| `subject` | `str \| None` | Optional envelope-level hint; adapters derive their own `subject` from `payload` as before. |
| `payload` | `dict[str, Any]` | Passed straight through, unmodified, to the resolved adapter — the exact same shape each adapter already documented and validated. |
| `metadata` | `dict[str, Any]` | Free-form envelope metadata, not forwarded to the Signal. |

`(source, event_type)` is deliberately a **closed, fixed dispatch table**
(`app/eventing/mapping.py::ADAPTER_MAPPING`), not payload-controlled
dynamic dispatch — this is the same "never trust a caller-supplied
routing field" discipline used elsewhere in Quipu (e.g. Incident
Resolution's `STRATEGY_TARGET_AGENT` map).

## 4. Normalization boundary

`SignalIngestionService` never contains normalization logic itself. It
resolves `(source, event_type)` to an adapter function and calls
`adapter(envelope.payload)` — the adapter is 100% the existing,
already-tested `app.signals.adapters` code. If an adapter raises
`ValueError` (its existing "malformed source payload" contract), ingestion
classifies that as `NORMALIZATION_FAILURE` (permanent — see §8) rather
than adding a second, parallel validation layer.

**`normalize_cloud_run_deployment` is intentionally excluded** from the
mapping table: it takes a live `app.domain.Artifact` — Quipu's own
already-persisted deployment state — not an external payload dict, so it
has no Pub/Sub envelope shape to route to. Cloud Run deployment Signals
continue to be produced the way they already were, directly from
DeploymentAgent's artifact (see §12).

## 5. Deduplication / idempotency

Reused unchanged: `app.domain.signal.compute_fingerprint()` and
`SignalRepository.find_by_fingerprint()` — no second dedup mechanism was
built for this task.

The idempotency key is **not** the Pub/Sub `message_id`. Each adapter
already derives its Signal's `fingerprint` from a producer-assigned,
payload-internal identifier (a Cloud Monitoring `incident_id`, a Cloud
Logging `insertId`, a feedback `feedback_id`, etc.) plus `subject` (and,
for the metric-observation adapter, a time window). This matters because:

- Pub/Sub's `message_id` is a **transport-layer** identifier. Google does
  not guarantee it is stable for what a producer considers "the same"
  logical event — republishing the same event (e.g. after a producer-side
  retry) can legitimately get a *different* `message_id`.
- Because fingerprinting never touches `message_id`, two independently
  published copies of the same logical event (different `message_id`s,
  identical payload-internal id) collapse to one Signal. `message_id` is
  carried on `PubSubMessage` purely for logging/correlation.
- Because the payload is byte-identical on a true Pub/Sub *redelivery* of
  the same message (at-least-once, §7), the adapter computes the exact
  same fingerprint every time — redelivery is safe by construction, not by
  special-casing `message_id`.

`tests/test_signal_ingestion.py` proves both cases directly (duplicate
publish with two different `message_id`s, and redelivery of the identical
message after a nack).

## 6. Ack semantics

A message is acknowledged in exactly two cases:

1. **Persistence succeeded** (a new Signal was saved, or an existing one
   was found by fingerprint — either way, the durable state is now
   correct) — see `SignalIngestionService.ingest_one`.
2. **The failure is classified PERMANENT** (§8) — acknowledging drops the
   message deliberately, per documented dead-letter policy, so a single
   malformed message can never wedge a subscription.

Every other outcome (a transient/persistence failure) leaves the message
unacknowledged. `PubSubMessage.ack()`/`.nack()` are bound async callables
supplied by the `PubSubConsumer` implementation per message (§9), so
`SignalIngestionService` never needs subscription bookkeeping of its own.

## 7. At-least-once delivery

Quipu makes **no exactly-once claim anywhere in this system.** Pub/Sub's
default delivery guarantee is at-least-once; `SignalIngestionService` is
built to be safely re-run against the same message any number of times
(§5). This is stated explicitly here so no caller of this service ever
assumes a message is processed exactly once.

## 8. Failure / dead-letter behavior

`app/eventing/errors.py::IngestionFailureCategory`:

| Category | Retryable? | Ack policy |
|---|---|---|
| `MALFORMED_ENVELOPE` | No | Acked (dropped) |
| `PAYLOAD_TOO_LARGE` | No | Acked (dropped) |
| `UNSUPPORTED_SOURCE` | No | Acked (dropped) |
| `UNSUPPORTED_EVENT_TYPE` | No | Acked (dropped) |
| `NORMALIZATION_FAILURE` | No | Acked (dropped) |
| `PERSISTENCE_FAILURE` | Yes | Left unacknowledged |
| `TRANSIENT_FAILURE` | Yes | Left unacknowledged |

Permanent categories are all forms of "this message can never become valid
no matter how many times it's redelivered" — dropping them (rather than
retrying forever) is the poison-message defense this task asks for.
Transient categories are left for Pub/Sub's normal redelivery/backoff to
handle.

A real Pub/Sub dead-letter topic (`Settings.pubsub_dead_letter_topic`) is
**optional, subscription-level deployment configuration** — Google's own
max-delivery-attempts policy on the subscription, not something this
module implements or requires for tests. Local/unit tests never configure
one.

## 9. Security boundary

Every Pub/Sub payload is treated as untrusted input:

- **Bounded size**: `SignalIngestionService` rejects any message whose raw
  `data` exceeds `Settings.pubsub_max_message_bytes` (default 256 KiB)
  before JSON-decoding it.
- **Source/event-type allow-list**: `(source, event_type)` must match a
  fixed entry in `app/eventing/mapping.py::ADAPTER_MAPPING` — there is no
  path from payload content to arbitrary code/class loading.
- **Schema validation**: `EventEnvelope` (Pydantic) plus each adapter's own
  `_require()` field checks.
- **Sanitization reused, not reimplemented**: every adapter already runs
  evidence/metadata through `app.signals.sanitize.sanitize_metadata()`
  before constructing a `Signal` — ingestion does not add or bypass this.
- **No raw payload in logs**: every structured log line in
  `app/eventing/ingestion_service.py` includes only ids, source/event_type,
  and outcome — never `envelope.payload` or the raw `message.data`.
- **No forwarding to Gemini**: `SignalIngestionService` never calls Gemini
  or any LLM; it stops at persisting a `Signal`.
- **No credentials in transit**: the real Google client
  (`app/eventing/google_pubsub_client.py`) uses Application Default
  Credentials only — no service-account key files, no credential paths, no
  shell/gcloud invocations.

## 10. Observability

`app/eventing/ingestion_service.py` uses
`app.core.observability.get_logger("quipu.eventing.ingestion")` for
structured logs on every ingestion attempt (`pubsub_message_id`,
`event_id`, `source`, `event_type`, `signal_id` when created/found,
dedup/outcome) — never the raw payload.

`SignalIngestionService.counters` (an `IngestionCounters` dataclass) tracks
`messages_received`, `signals_created`, `signals_deduplicated`,
`messages_rejected`, `messages_failed` in-process — this reuses the
service's own state rather than introducing a new metrics subsystem;
wiring it into a real metrics backend is left to whatever process runs the
pull loop.

## 11. Google service integration

`app/eventing/google_pubsub_client.py` is the **only** file in the
repository allowed to import `google.cloud.pubsub_v1`, matching the
existing one-file-per-service isolation
(`cloud_run_client.py`/`cloud_monitoring_client.py`/
`cloud_logging_client.py`/`app/persistence/firestore/*.py`/
`app/knowledge/backends/google_search.py`).

`google-cloud-pubsub`'s `PublisherClient`/`SubscriberClient` are
synchronous (grpc) clients with no async variant (unlike
`monitoring_v3`/`logging_v2`, which expose `...AsyncClient`s) — each
blocking call (`publish`, `pull`, `acknowledge`, `modify_ack_deadline`) is
run via `asyncio.to_thread` so `GooglePubSubClient` still satisfies the
async `PubSubPublisher`/`PubSubConsumer` Protocols the rest of
`app/eventing/` depends on. Errors are translated through the existing
shared `app.core.google_api_errors` hierarchy.

Configuration (`app/config.py::Settings`, reusing `gcp_project_id`):

| Setting | Purpose |
|---|---|
| `pubsub_signal_topic` | Topic Signal-producing events are published to. |
| `pubsub_signal_subscription` | Subscription `SignalIngestionService`'s pull loop reads from. |
| `pubsub_dead_letter_topic` | Optional; subscription-level dead-letter policy. |
| `pubsub_max_message_bytes` | Untrusted-input size ceiling (default 256 KiB). |
| `pubsub_pull_max_messages` | Batch size for one `pull()` call. |
| `pubsub_api_timeout_seconds` | Timeout for publish/pull/ack calls. |

**Required GCP setup** (not automated by this task — HTTP API/deployment
wiring is explicitly out of scope, §12):

1. Create a Pub/Sub topic (`gcloud pubsub topics create <topic>`).
2. Create a pull subscription bound to it
   (`gcloud pubsub subscriptions create <sub> --topic=<topic>`).
3. Grant the running service's identity (Application Default
   Credentials — a user account locally, or the Cloud Run service
   account in deployment) `roles/pubsub.publisher` (for producers) and/or
   `roles/pubsub.subscriber` (for `SignalIngestionService`) on that
   topic/subscription.
4. Set `GCP_PROJECT_ID`, `PUBSUB_SIGNAL_TOPIC`, `PUBSUB_SIGNAL_SUBSCRIPTION`
   in `.env` (see `.env.example`).
5. Optionally configure a dead-letter topic + max-delivery-attempts on the
   subscription itself, and set `PUBSUB_DEAD_LETTER_TOPIC` to match.

## 12. What Pub/Sub does NOT do

- It does not become the Signal domain model — `Signal` remains canonical.
- It does not carry Cloud Run deployment events — those still flow
  directly from DeploymentAgent's own `Artifact` (§4).
- It does not detect incidents, detect feature opportunities, call Gemini,
  or decide remediation — `SignalIngestionService` stops at persisting a
  `Signal`.
- It does not provide exactly-once delivery (§7).
- It does not implement an HTTP API, a UI, a new agent, a new
  orchestration engine, Cloud Scheduler/Workflows, an additional database,
  or an arbitrary event-routing DSL. This task builds the event transport
  + ingestion boundary only.

## 13. Why DetectingAgent is not directly embedded into ingestion

Ingestion and reasoning are deliberately separate responsibilities, the
same separation Quipu already enforces between MonitoringAgent (observes)
and DetectingAgent (interprets). Tightly coupling Pub/Sub consumer code to
a live `DetectingAgent` invocation would mean:

- A slow/failing Gemini call could block message acknowledgment, turning a
  reasoning failure into an ingestion outage.
- Every ingested Signal would trigger detection even when Detecting's own
  windowing/batching logic wants to reason about many Signals together,
  not one at a time.

Instead, `app/eventing/trigger.py::DetectionTrigger` is a narrow interface
(`on_signal_available(signal) -> None`) invoked **after** a Signal is
durably persisted and its message acknowledged. `NoOpDetectionTrigger`
(the default) only logs that a Signal became available — it does not
invoke any agent or orchestration. A trigger failure is caught and logged;
it can never retract the persisted Signal or the ack (see
`test_trigger_failure_never_reverses_persistence_or_ack`).

## 14. Future event-driven DetectingAgent integration

> **Update**: implemented in a later task — see
> `docs/architecture/event_driven_detection.md`. `NoOpDetectionTrigger`
> described below has been replaced in production wiring by
> `app.detection.trigger.DetectionProcessorTrigger`, which invokes a new
> `DetectionProcessor` (`app/detection/`) that calls the existing
> `DetectingAgent`. `DetectionTrigger`'s payload also changed from the
> full `Signal` to a smaller `SignalAvailableEvent` — see that doc §2.

We will integrate actual event-driven DetectingAgent invocation in a
subsequent task, behind the same `DetectionTrigger` interface — most
likely a trigger implementation that enqueues a detection request (e.g.
onto Quipu's own orchestration boundary) rather than calling
`DetectingAgent` synchronously from the ingestion path. No orchestration
change was made in this task to accommodate that; `DetectionTrigger` is
the seam future work attaches to.

## 15. Google services ledger

| Google Service | Quipu Role | Status |
|---|---|---|
| Google ADK | Agent runtime/orchestration | Implemented (used by every prior agent; not used by ingestion — it isn't an agent) |
| Gemini | Agent reasoning | Implemented (used by every prior agent; never called by ingestion — see §12) |
| Agent Search | Enterprise knowledge | Implemented (unused by ingestion) |
| Firestore | Durable state | Implemented (`FirestoreSignalRepository`, reused unchanged) |
| Cloud Run | Application deployment | Implemented (Level 2.1; unrelated to ingestion) |
| Cloud Monitoring | Production metrics | Implemented (Level 3.1; now also reachable via Pub/Sub ingestion — see §2) |
| Cloud Logging | Production logs | Implemented (Level 3.1; now also reachable via Pub/Sub ingestion — see §2) |
| **Pub/Sub** | **Event transport for Signal ingestion** | **Implemented this level** — real `PublisherClient`/`SubscriberClient` calls behind `app/eventing/google_pubsub_client.py` |

> **Update**: implemented in a later task — see
> `docs/architecture/pubsub_worker.md`. `SignalConsumerWorker`
> (`app/eventing/worker.py`) is the caller that runs `SignalIngestionService`
> continuously against a live subscription, with bounded concurrency and
> graceful shutdown; `app/eventing/worker_main.py` is the process
> entrypoint. `DetectionTrigger` is also no longer a no-op in production
> wiring — see `docs/architecture/event_driven_detection.md`.

## 16. Limitations / deferred work

- The real Pub/Sub client uses synchronous pull, not streaming-pull — fine
  for this task's bounded polling scope, but a high-throughput production
  deployment may eventually want a streaming-pull worker (see
  `docs/architecture/pubsub_worker.md` §12/§13 for how that would plug in
  without changing `SignalIngestionService`).
- `DetectionTrigger` is a no-op by design (§14) — no event-driven detection
  actually happens yet.
- Product-signal producers (customer feedback, support, user behavior)
  still have no external system integration — the adapters accept
  Quipu-defined internal shapes, unchanged from Level 3.
