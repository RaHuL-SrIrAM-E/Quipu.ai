# Event-Driven Detection

## Diagram

```
Pub/Sub
   ↓
SignalIngestionService  (app/eventing/ingestion_service.py — unchanged responsibility)
   ↓
SignalRepository / Firestore   (persist Signal, ack)
   ↓
DetectionTrigger   (app/eventing/trigger.py — SignalAvailableEvent, not the raw Signal)
   ↓
DetectionProcessor   (app/detection/processor.py)
   │   1. resolve DetectionDomain from signal_type
   │   2. aggregation policy: bounded window + minimum related-signal count
   │   3. construct DetectingInput
   ↓
DetectingAgent   (app/agents/detecting.py — UNCHANGED, existing evidence-first agent)
   │   retrieves its own bounded evidence set, reasons via Gemini,
   │   validates supporting_signal_ids, persists DetectionResult
   ↓
DetectionResult → Firestore   (via the existing DetectionGateway/DetectionRepository)
```

Downstream of a persisted `DetectionResult`, nothing in this task changes:

```
FEATURE_OPPORTUNITY → FeatureReviewService.create_review() → human approval → Ticket → SDLC
INCIDENT            → IncidentResolutionAgent → ResolutionResult → existing authorization/remediation
```

`DetectionProcessor` never calls either of those — it stops at persisting
the `DetectionResult`. See §9/§10.

## 1. Keep ingestion and reasoning separate

`SignalIngestionService` (`app/eventing/ingestion_service.py`) is
unmodified in responsibility: message → validate → normalize → sanitize →
deduplicate → persist Signal → acknowledge. The only change in this task
is *what* it hands to the trigger after persistence succeeds — a
`SignalAvailableEvent` (§2) instead of the full `Signal` — never
DetectingAgent reasoning logic. `app/eventing/` still imports nothing from
`app.agents.detecting`, and therefore nothing from `google.adk`/Gemini.

## 2. DetectionTrigger contract

`app/eventing/trigger.py::SignalAvailableEvent` is the stable reference
that crosses the ingestion → detection boundary:

| Field | Purpose |
|---|---|
| `signal_id` | The only thing a detection processor needs to look the Signal back up. |
| `signal_type`, `source` | Used to resolve a `DetectionDomain` (§4) — never used to load arbitrary code. |
| `subject`, `service_name`, `environment` | Correlation dimensions for aggregation (§4). |
| `observed_at` | For logging/ordering only. |

It deliberately excludes `evidence`, `metadata`, and anything else that
came from a source's raw payload — even though those are already sanitized
by the adapter that produced the Signal (`app.signals.sanitize`), the
trigger boundary itself carries nothing but typed, non-payload fields.
`DetectionTrigger.on_signal_available(event: SignalAvailableEvent) -> None`
is the only method on the Protocol — unchanged shape, evolved payload type.

`NoOpDetectionTrigger` (unchanged, still the safe default) logs and does
nothing. `app.detection.trigger.DetectionProcessorTrigger` is the new real
implementation, living in `app/detection/` — not `app/eventing/` — so that
ADK/Gemini imports never enter the ingestion package (§1).

## 3. DetectionProcessor

`app/detection/processor.py::DetectionProcessor.process_signal_available`:

1. Resolve `DetectionDomain` from `event.signal_type` via
   `app.detection.policy.SIGNAL_TYPE_TO_DOMAIN` — the reverse of
   `app.agents.detecting.DOMAIN_SIGNAL_TYPES` (reused, not duplicated). A
   `SignalType` with no domain mapping is skipped (`outcome=
   "skipped_unmapped_domain"`) — this cannot currently happen for any
   value in the closed `SignalType` enum, but is handled explicitly rather
   than assumed.
2. Evaluate the aggregation policy's minimum-evidence gate (§4).
3. Construct `DetectingInput` (domain, service_name, environment,
   window_minutes, max_signals) and a synthetic `AgentInput`/`AgentContext`
   — the exact same invocation shape `app/demo/harness.py` already uses to
   drive `DetectingAgent` outside a real SDLC workflow (detection has no
   workflow of its own, same as Signal — see
   `docs/architecture/signal_platform.md`).
4. Invoke `DetectingAgent().execute(agent_input, context)` — the existing,
   unmodified `QuipuAgent` contract. No second `DetectingAgent`
   implementation exists anywhere in this task.
5. Read the `detection_id` DetectingAgent's own `_finalize()` already
   persisted (through the existing `DetectionGateway`/
   `DetectionRepository`) out of `AgentOutput.messages[1]` — the same
   extraction the demo harness already relies on. `DetectionProcessor`
   never constructs or saves a `DetectionResult` itself.
6. Return a `DetectionProcessingOutcome` (§14).

## 4. Evidence aggregation

Not a generic streaming/windowing framework — a fixed,
`AggregationPolicy` (`app/detection/policy.py`) with two hard-coded
domains:

| Domain | Default window | Default minimum related signals |
|---|---|---|
| `OPERATIONAL` | 30 min (`Settings.detection_operational_window_minutes`) | 1 (`Settings.detection_min_operational_signals`) |
| `PRODUCT` | 7 days / 10080 min (`Settings.detection_product_window_minutes`) | 2 (`Settings.detection_min_product_signals`) |

Product signals rarely arrive in an operationally-tight window — several
independent feedback/support signals converging over days is the whole
point (§7 of the task) — so PRODUCT gets a wide window and a
minimum-of-two floor (a single piece of feedback alone should not, by
default, warrant a Gemini call). OPERATIONAL gets a short window and a
floor of one, since a single CRITICAL operational signal can already be
actionable; the "one latency anomaly may not be enough" framing is left to
DetectingAgent's own reasoning (its instruction already tells it to weigh
isolated vs. corroborated evidence), not hard-gated at the processor
level, to avoid silently dropping a possibly-real single-signal incident.

**Scope correlation**: `count_related_signals()` and the `DetectingInput`
built from it use `event.service_name`/`event.environment` as an exact
filter — two signals for different `service_name`s are never aggregated
together (verified by
`test_unrelated_signals_not_incorrectly_grouped`). PRODUCT signals
typically carry no `service_name`/`environment` at all (see
`app.signals.adapters`), so PRODUCT correlation today is bounded by
domain + window + `detecting_max_signals` only — `SignalQuery`
(`app.persistence.repositories.signal`) has no subject/feature-area
filter to correlate more tightly on, and adding one would mean touching
`SignalQuery`/`DetectingAgent`, which this task explicitly excludes. This
is a known, documented limitation (§16), not a silent gap.

**Minimum-evidence gate**: `count_related_signals()` (`app/detection/policy.py`)
is a cheap, bounded, deterministic COUNT over the same `SignalQuery`
DetectingAgent's own `_retrieve_evidence` would use — it does not rank,
sort, or bound-and-return signals the way DetectingAgent does; it exists
solely to decide whether invoking DetectingAgent (and therefore Gemini) is
warranted at all. If the count is below the domain's
`min_related_signals`, `DetectionProcessor` returns
`outcome="skipped_insufficient_evidence"` and **never invokes
DetectingAgent** — no Gemini call, no `AgentExecution`, no
`DetectionResult`. This is a second, cheaper check than DetectingAgent's
own zero-evidence NO_ACTION path (§9 of DetectingAgent's own design,
unchanged) — that path still exists and still fires if DetectingAgent's
own retrieval later returns fewer signals than the pre-check counted (a
narrow race window between the count and the real query), and it still
never calls Gemini for zero evidence.

## 5. DetectingAgent (unchanged)

`app/agents/detecting.py` was touched only to make its existing
domain/signal-type taxonomy importable:
`_OPERATIONAL_SIGNAL_TYPES`/`_PRODUCT_SIGNAL_TYPES`/`_DOMAIN_SIGNAL_TYPES`
were renamed to `OPERATIONAL_SIGNAL_TYPES`/`PRODUCT_SIGNAL_TYPES`/
`DOMAIN_SIGNAL_TYPES` (dropping the leading underscore) so
`app/detection/policy.py` can reuse the same taxonomy instead of
redefining it. No other line of `DetectingAgent`'s behavior changed:
evidence retrieval, the Gemini call, `_validate_evidence`'s
fabricated-id rejection, and `_finalize`'s fingerprint dedup are all
exactly as they were.

## 6. DetectionResult (unchanged)

Persisted exactly as before, through the existing `DetectionGateway` →
`DetectionRepository` → `FirestoreDetectionRepository`. `DetectionResult`
remains a top-level record, not an `Artifact` — `DetectionProcessor` does
not touch persistence shape at all; it only calls the agent that already
owns it.

## 7. Product opportunity flow

`DetectionDomain.PRODUCT` signals (`CUSTOMER_FEEDBACK`, `SUPPORT_FEEDBACK`,
`FEATURE_REQUEST_PATTERN`, `USER_BEHAVIOR`, `ADOPTION_ANOMALY`) can produce
`DetectionType.FEATURE_OPPORTUNITY`, exactly as DetectingAgent already
supported. `DetectionProcessor` stops once that `DetectionResult` is
persisted — it does **not** call `FeatureReviewService.create_review()`.
Human review remains mandatory: nothing in this task creates a
`FeatureReview` or a `Ticket` automatically
(`test_feature_opportunity_does_not_auto_create_review`).

## 8. Incident flow

`DetectionDomain.OPERATIONAL` signals can produce `DetectionType.INCIDENT`.
`DetectionProcessor` stops once that `DetectionResult` is persisted — it
does **not** call `IncidentResolutionAgent`. No remediation is ever
automatically authorized or executed by detection processing
(`test_incident_does_not_auto_execute_remediation`); the existing
authorization/safety gates in `IncidentResolutionAgent`/
`OrchestrationService.start_remediation_from_resolution` remain the sole
path to remediation, invoked separately (by `detection_id`), same as
today.

## 9. Failure isolation

Two independent failure domains, deliberately kept apart:

- **Signal persistence failure**: unchanged — `SignalIngestionService`
  never acknowledges and detection never runs (there is no Signal to
  trigger on yet).
- **Detection processing failure**: the Signal is already durably
  persisted and already acknowledged *before* the trigger runs. A
  `DetectionProcessingError` (DetectingAgent/Gemini failure, or a
  `DetectionResult` persistence failure) propagates out of
  `DetectionProcessorTrigger.on_signal_available`, is caught and logged by
  `SignalIngestionService.ingest_one` (unchanged code — see
  `app/eventing/ingestion_service.py`'s existing trigger try/except), and
  **never** un-acknowledges or reprocesses the original Pub/Sub message.

`DetectionProcessingError` has no permanent/transient split the way
ingestion failures do (`app/eventing/errors.py`) — every detection
processing failure is treated as retryable, because retrying means
re-invoking `DetectionProcessor.process_signal_available()` with the same
(idempotent, §10) event, which is always safe.

## 10. Idempotency

`compute_detection_fingerprint()`/`DetectionRepository.find_by_fingerprint()`
(unchanged, `app.domain.detection`) remain the sole detection-identity
mechanism — `DetectionProcessor` adds no second dedup mechanism. Repeated
processing of the same evidence set (same detection_type, subject,
supporting_signal_ids, window) resolves to the same `DetectionResult`
through DetectingAgent's own existing `_finalize()` logic, exactly as it
already worked before this task.

Two things `DetectionProcessor` adds on top, both *invocation-level*
guards rather than a second identity mechanism:

- **Per-scope serialization**: `DetectionProcessor` holds an
  `asyncio.Lock` per `(domain, service_name, environment)` scope so two
  near-simultaneous triggers for the same scope can't both race past
  DetectingAgent's find-then-save fingerprint check and create two
  `DetectionResult`s with the same fingerprint content but different ids —
  an in-memory-only guard (not distributed), sufficient for this task's
  scope and verified by
  `test_concurrent_duplicate_processing_creates_one_detection`.
- **No exactly-once claim**: exactly like ingestion (§7 of
  `docs/architecture/pubsub_signal_ingestion.md`), detection processing
  makes no exactly-once claim. A production Gemini call is not literally
  deterministic between two separate invocations even for identical
  evidence — true dedup safety here rests on the fingerprint, not on
  Gemini producing byte-identical output twice. Tests use a fixed fake
  model response specifically to make this mechanism directly observable.

## 11. Security

- `SignalAvailableEvent` never carries a raw Pub/Sub payload or
  unsanitized data (§2) — `DetectingAgent` only ever sees the same bounded,
  already-sanitized evidence set it always retrieves itself via
  `SignalGateway.query`.
- `(source, event_type)` remain closed allow-lists enforced entirely
  inside `app/eventing/` (unchanged) — nothing about detection processing
  widens that.
- Domain resolution (`SIGNAL_TYPE_TO_DOMAIN`) is a fixed, closed mapping —
  a `SignalType` never controls agent selection, tool selection, Python
  class loading, or a workflow stage. There is exactly one agent
  (`DetectingAgent`) `DetectionProcessor` ever constructs.
- `DetectingAgent.capabilities` (`READ_SIGNALS`, `QUERY_KNOWLEDGE`,
  `WRITE_DETECTION`) are unchanged; `DetectionProcessor` never bypasses
  `require_capability`/`check_capability` — it calls `execute()`, the same
  public entry point every other caller uses.

## 12. Observability

`app/detection/processor.py` logs one structured line per processing
attempt via `app.core.observability.get_logger("quipu.detection.processor")`:
`signal_id`, `domain`, `evidence_count`, `detection_id` (when created),
outcome (`invoked` / `skipped_insufficient_evidence` /
`skipped_unmapped_domain` / `agent_failed`), and `duration_ms`. Never logs
raw signal metadata/evidence or customer payloads — only ids, counts, and
outcomes, same discipline as `app/eventing/ingestion_service.py`. No new
metrics subsystem was introduced; a future caller can aggregate these
structured logs or extend `IngestionCounters`-style counters the same way
`app/eventing/` already does.

## 13. Why ingestion acknowledgement is independent of detection processing

Coupling message acknowledgment to Gemini/DetectingAgent completion would
mean a slow or failing model call turns into an ingestion outage — the
same reasoning `docs/architecture/pubsub_signal_ingestion.md` §13 already
gives for why `DetectingAgent` isn't embedded directly into ingestion. The
Signal is the durable unit of truth once persisted; whether Quipu has
*interpreted* it yet is a separate, independently-retryable concern. This
is why `SignalIngestionService.ingest_one` acknowledges immediately after
`SignalRepository.save()` succeeds and only *then* invokes the trigger,
swallowing (and logging) whatever the trigger raises.

## 14. Future asynchronous detection worker design

Today, `DetectionProcessorTrigger.on_signal_available()` runs
synchronously, in-process, inline with the ingestion pull loop — the
`DetectionTrigger` abstraction (§2) is what makes this swappable without
touching `SignalIngestionService`. No second Google messaging service was
introduced in this task (per its own instruction not to increase the
service count); the tradeoff of adding one is worth stating explicitly:

- **Current (in-process trigger)**: simplest possible correct wiring.
  Detection processing failures are isolated from ack (§9) but are not
  automatically retried — a caller (a worker, a scheduled sweep) would
  need to re-invoke `DetectionProcessor.process_signal_available()` for
  signals whose processing failed, which the idempotency guarantee (§10)
  makes safe to do.
- **Future (a dedicated Pub/Sub detection topic)**: `DetectionProcessorTrigger`
  could instead publish a small `{signal_id}` message to its own topic
  (mirroring `Settings.pubsub_signal_topic`), with a separate consumer
  loop driving `DetectionProcessor` — giving detection processing its own
  independent retry/backoff/dead-letter lifecycle, fully decoupled from
  ingestion's pull loop. This is a real, reasonable next step, but it's a
  second event bus and its own consumer process — deliberately deferred
  rather than built speculatively here (`Settings.pubsub_dead_letter_topic`-
  style configuration would extend the same way `app/eventing/` already
  does if this is picked up later).

## 15. Google services ledger

Unchanged from `docs/architecture/pubsub_signal_ingestion.md` §15 — no new
Google service was introduced in this task. Gemini/ADK usage is entirely
inside the existing, unmodified `DetectingAgent`; `app/detection/` itself
contains no Google SDK imports at all.

## 16. Limitations / deferred work

- PRODUCT-domain correlation is bounded by domain + window + max_signals
  only — no feature-area/subject-based grouping, since `SignalQuery` has
  no such filter and adding one would mean touching
  `DetectingAgent`/`SignalRepository` (out of scope here).
- The per-scope `asyncio.Lock` in `DetectionProcessor` is in-process only
  — it does not protect against two separate processes/workers running
  concurrently against the same Firestore-backed repositories. A
  production deployment with more than one ingestion worker would need a
  distributed lock or would need to accept DetectingAgent's fingerprint
  dedup as the sole (already-adequate, if slightly wasteful on the Gemini
  call) safety net.
- Detection processing has no automatic retry scheduler — failures are
  logged and are safely re-triggerable, but nothing in this task
  re-triggers them automatically (§14).
- `HTTP API`, `UI`, another agent, another database, Workflows, Scheduler,
  a second orchestration engine, automatic human approval, and automatic
  feature deployment without review were all explicitly out of scope and
  remain unimplemented.
