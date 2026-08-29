# Monitoring Agent (Level 3.1)

## Diagram

```
Cloud Run
   │
   ├───────────────┐
   ▼               ▼
Monitoring       Logging
   │               │
   └───────┬───────┘
           ▼
    Monitoring Agent
           │
           ▼
        Signals
           │
           ▼
    Signal Repository
           │
           ▼
    Detecting Agent
       (future)
```

```
OBSERVATION
     ↓
Monitoring
     ↓
  Signal
     ↓
DETECTION
     ↓
Detecting
     ↓
Candidate
```

No legacy predecessor exists for Monitoring — same situation as Codegen/
Testing/Deployment. Read `docs/architecture/signal_platform.md` first
(Signal domain model, provenance, deduplication, persistence layout) — this
document only covers what's new for Monitoring, not the Signal contract
itself, which is reused unchanged.

## 1. Responsibility

Observe Cloud Run production telemetry (Cloud Monitoring metrics + Cloud
Logging entries) and normalize it into `Signal`s. Nothing more. Monitoring
answers **"what is happening in the running system"** — it never answers
**"why"**. It creates no `IncidentCandidate`, no `FeatureCandidate`,
modifies no code, deploys nothing, rolls back nothing, and never decides
that an observation constitutes an incident. Those are Detecting's and
Incident Resolution's jobs, neither of which exists yet.

## 2. Observation vs Detection

This is the load-bearing distinction for the whole agent, and it shows up
concretely in one design choice: **severity vs. type**.

`Signal.signal_type` values like `METRIC_ANOMALY`/`LATENCY_ANOMALY` are
inherited from the existing Level 3 taxonomy — Monitoring does not invent
new ones, and did not get to choose less alarming names. What Monitoring
actually controls is `Signal.severity`, and that's where the observation/
detection boundary is enforced:

- **Raw observation**: `error_rate = 0.072` for `quipu-api` over the last 15
  minutes. This is a fact, always recorded if there was traffic to measure.
- **Whether 0.072 is "bad"** is decided by comparing it to
  `settings.monitoring_error_rate_warning_threshold` /
  `_critical_threshold` — two plain config numbers
  (`app/config.py`), not a model call, not a learned model, not a rule
  engine. `_classify_error_rate()` (`app/agents/monitoring.py`) is pure
  arithmetic: `>= critical -> CRITICAL`, `>= warning -> WARNING`, else
  `INFO`. This is documented explicitly as an **operational collection
  policy**, not AI/diagnostic reasoning — the same static-threshold
  boundary the task asked for, never dressed up as intelligence.
- **Whether that crossed threshold means anything** (is it a real problem,
  is it caused by the last deployment, does it need action) is left
  entirely to the future Detecting Agent. Monitoring's job stops at
  producing the evidence with an honest severity label attached.

Latency observations are recorded at `INFO` severity unconditionally in
this level — no latency threshold is configured yet (documented limitation,
§20), so nothing here claims a latency value is anomalous.

## 3. Architecture — why there is no internal Gemini/ADK LlmAgent

Every prior Quipu-native agent (`Planning`/`Architecture`/`Codegen`/
`Testing`/`Deployment`) wraps an internal ADK `LlmAgent`. `MonitoringAgent`
deliberately does not:

```
QuipuAgent
    ↓
MonitoringAgent
    ↓
CloudMonitoringClient / CloudLoggingClient   (deterministic Python)
    ↓
app.signals normalization (reused, not reinvented)
    ↓
SignalRepository (via AgentContext.signals)
```

The task's own instruction is explicit: "do not use Gemini merely for
mechanical API translation" and "it is acceptable for MonitoringAgent to be
mostly deterministic while still following the established agent runtime
contract." Google API response → Signal is a mechanical transformation with
one right answer per input; whether a value crosses a threshold is
arithmetic (§2). There is no genuinely ambiguous judgment call anywhere in
this agent's job for an LLM to add value to — adding one here would be
exactly the "LLM theater" the task warned against. `MonitoringAgent` still
implements the full `QuipuAgent` contract (`identity`, `capabilities`,
`require_capability`, `AgentExecution`/`AgentMetrics` bookkeeping,
`_perform()`) — only the internal "how" differs.

Because there's no ADK `LlmAgent`, there's no ADK `before_tool_callback`
tool boundary either. The three-layer capability enforcement the task asks
for (§20/§21) is still real, just implemented without ADK: **(1)** agent
entry — `self.require_capability(READ_MONITORING)` in `_perform()`;
**(2)** N/A — no ADK tool boundary exists; **(3)** implementation boundary
— `_collect_metrics`/`_collect_logs` take `granted: set[AgentCapability]`
as an **explicit parameter**, not read implicitly from `self.capabilities`,
and re-check it before touching either Google client — the same
"stays safe even if invoked outside the normal path" principle
`deploy_cloud_run`/`run_tests` established, adapted to a class without a
`tool_context.state` to read from.

## 4. Monitoring input

`MonitoringInput` (`app/agents/monitoring.py`), parsed from
`AgentInput.context` — the existing generic extension point every agent
already has (`app.domain.agent_io.AgentInput.context: dict[str, Any]`), not
a new invocation contract:

```python
class MonitoringInput(BaseModel):
    service_name: str | None = None   # None = environment-wide observation
    region: str
    environment: str
    window_minutes: int = 15
    deployment_artifact_id: str | None = None   # optional Cloud Run correlation
    revision: str | None = None
```

`service_name=None` covers "monitor production environment" without a
second agent: `CloudMonitoringClient`'s filter construction simply omits
the `resource.label.service_name` equality clause and groups by service
instead when it's absent (`_cloud_run_filter`,
`app/core/cloud_monitoring_client.py`) — the same query shape handles both
"monitor Cloud Run service X" and "monitor everything in this region". Log
collection (`_collect_logs`) is only run when `service_name` is set — Cloud
Logging's filter needs a concrete `resource.labels.service_name` value to
stay a bounded query (§8), so environment-wide observation in this level
covers metrics only; a future level could enumerate services and loop, but
that's not built here.

`deployment_artifact_id` lets a caller (e.g. a future scheduler invoked
right after a Deployment stage completes) hand Monitoring the exact
`DeploymentArtifact` to correlate against; `_perform()` resolves its
`revision` from that artifact via `context.artifacts.get(...)` if
`revision` wasn't already supplied directly.

## 5. Monitoring output

```python
class MonitoringOutput(BaseModel):
    observation_window_minutes: int
    service_name: str | None
    environment: str
    signal_ids: list[str]
    metrics_observed: list[str]        # e.g. ["error_rate", "latency_p99"]
    logs_observed_count: int
    collection_status: MonitoringCollectionStatus   # complete | partial | failed
    summary: str
    collection_errors: list[str]
```

Grounded in actual collected evidence, never a fabricated claim (§17):
`signal_ids`/`metrics_observed` are populated only from what
`CloudMonitoringClient`/`CloudLoggingClient` actually returned. If a query
returns no data, the corresponding list stays empty — `summary` never says
"system healthy" unless that's what "0 signals, 0 errors" the evidence
actually showed, and never says anything at all about health it didn't
measure. There is no `AgentOutput.artifacts` entry — Monitoring's durable
product is the persisted `Signal`s themselves (already saved via
`context.signals` before `_perform()` returns), not a new `Artifact` type;
`MonitoringOutput` travels back to the caller as a JSON message
(`AgentOutput.messages[1]`), the same boundary every agent already uses for
its human-readable summary.

## 6. Cloud Monitoring integration

`app/core/cloud_monitoring_client.py::CloudMonitoringClient` — the only
file allowed to import `google.cloud.monitoring_v3`. Real
`MetricServiceAsyncClient.list_time_series` calls, ADC-only auth (no
service-account key, no credential path in config). Three narrow methods,
not a generic filter-passthrough:

| Method | Metric | Kind/Type (verified against installed SDK) |
|---|---|---|
| `query_request_count_by_response_class` | `run.googleapis.com/request_count` | DELTA, INT64 — grouped by `metric.label.response_code_class` |
| `query_latency_p99` | `run.googleapis.com/request_latencies` | DELTA, DISTRIBUTION — `ALIGN_PERCENTILE_99` |
| `query_instance_count_by_state` | `run.googleapis.com/container/instance_count` | GAUGE, INT64 — grouped by `metric.label.state` |

These three were chosen because they're exactly what the task asked for
(request count, latency, error rate, instance health) and no more — no
attempt to ingest every Cloud Run metric. `alignment_period` is always set
equal to the requested window, so each `MetricPoint` already represents the
full-window aggregate (no client-side point-by-point math needed for the
window itself); error-rate computation (`errors / total` across
response-class buckets) is the one piece of arithmetic `MonitoringAgent`
does on the result, entirely deterministic.

## 7. Cloud Logging integration

`app/core/cloud_logging_client.py::CloudLoggingClient` — the only file
allowed to import `google.cloud.logging_v2`. Uses the low-level async
`LoggingServiceV2AsyncClient.list_log_entries` (the high-level
`google.cloud.logging.Client` has no async API, and every other Quipu
Google client is async). One narrow method:

```python
async def query_service_logs(self, *, service_name, region, window_minutes, min_severity="ERROR", limit=50) -> list[LogEntryResult]
```

No `filter`/`query` string parameter exists on this method at all — the
filter is built entirely inside the client from typed arguments
(`resource.type`, `resource.labels.service_name`, `resource.labels.location`,
a `severity >=` clause resolved from the `LogSeverity` proto enum, and a
`timestamp >=` clause) — verified directly
(`test_no_arbitrary_log_query_string_parameter_exists`).

## 8. Signal normalization — reused, not reinvented

No second Signal factory was built. Two paths:

- **Logs**: `CloudLoggingClient.to_signal_payload()` shapes a
  `LogEntryResult` into the *exact* dict
  `app.signals.adapters.normalize_cloud_logging_entry` (Level 3) already
  expects (`insertId`, `timestamp`, `severity`, `textPayload`, `logName`,
  `trace`, `resource`) — MonitoringAgent calls that existing function
  directly. Proven end-to-end
  (`test_to_signal_payload_matches_normalize_cloud_logging_entry_shape`).
- **Metrics**: `app.signals.adapters.normalize_cloud_monitoring_metric_observation`
  is a genuinely new function, added because a queried metric observation
  is a fundamentally different evidence shape than what
  `normalize_cloud_monitoring_alert` (Level 3) was built for — that one
  normalizes an *already-delivered alert notification* someone else
  decided crossed a threshold; this one normalizes a *live query result*
  MonitoringAgent computed the threshold decision for itself. Both live in
  `app/signals/adapters.py`, both use the same `Signal` model, the same
  `sanitize_metadata`, the same `compute_fingerprint` — the normalization
  **architecture** is fully reused; only a new source-shape got a new
  function, exactly as the module was designed to be extended.

## 9. Provenance

For metric signals: `provenance.source_system="cloud_monitoring"`,
`collected_at` = the query window's end time. `evidence` carries the
compact, normalized detail (`observation_kind`, `value`, `unit`,
`window_start`/`window_end`, plus per-response-class counts for error-rate
observations) — never the raw SDK response object. For log signals: the
existing `normalize_cloud_logging_entry` provenance (`source_system=
"cloud_logging"`, `source_event_id` = the log entry's `insertId`,
`trace_id` when Cloud Run set one). Nothing dumps a full Google SDK message
into `metadata`.

## 10. Cloud Run correlation

`service_name`/`environment`/`revision`/`deployment_artifact_id` are set on
every Signal Monitoring produces. `environment` needs an explicit note:
Cloud Run's own monitored-resource labels carry `service_name`/
`revision_name`/`location` — never `environment` (that's a Quipu concept,
not a GCP resource label) — so `_collect_logs` backfills it from
`MonitoringInput.environment` after calling the existing adapter, rather
than expecting the adapter to invent a label GCP doesn't provide. `revision`
is resolved either directly from `MonitoringInput.revision` or, if a
`deployment_artifact_id` was supplied, from that `DeploymentArtifact`'s own
`revision` field (Level 2.1) — reusing Deployment's existing evidence
rather than re-deriving it. No inference is performed on top of this
correlation metadata (e.g. "errors increased after this revision") — that
reasoning is explicitly Detecting's, not built here.

## 11. Persistence

Reuses `SignalRepository` unchanged (Level 3) — no second database, no new
collection layout. A new `SignalGateway` Protocol +
`RepositorySignalGateway` (`app/agent_runtime/gateways/signals.py`) was
added, mirroring `ArtifactGateway` exactly, because Monitoring is the first
agent that persists Signals rather than Artifacts — `AgentContext` gained
one new optional field, `signals: SignalGateway | None = None` (same
additive, backward-compatible shape as the `executions` field added in an
earlier level; every other existing agent leaves it `None` and is
unaffected — verified by the full regression suite staying green). Tests
use `InMemorySignalRepository`; production wiring uses
`FirestoreSignalRepository`, top-level `signals/{signal_id}`, unchanged
from Level 3.

## 12. Deduplication

Reuses `compute_fingerprint()` and `SignalRepository.find_by_fingerprint()`
unchanged — no second dedup algorithm.
`MonitoringAgent._save_signal_deduplicated()` checks for an existing
fingerprint before every save; if found, returns the existing `Signal`
instead of writing a duplicate. The rule for "new Signal vs. existing one"
falls directly out of `compute_fingerprint`'s inputs:

- **Metric observations** fingerprint on `(source, "{observation_kind}:
  {service_name}", subject, window="{window_start}/{window_end}")` — the
  same service/metric/window collected twice (e.g. two overlapping
  collection cycles) dedups to one Signal; a **different** window (the next
  collection cycle) is new evidence and gets its own Signal
  (`test_repeated_observation_same_window_deduplicates` /
  `test_different_windows_produce_different_signals`).
- **Log entries** fingerprint on the log entry's own `insertId` (via the
  reused adapter) — Cloud Logging's own deduplication guarantee for that
  field carries through unchanged.

## 13. Security

- **No shell/subprocess/`gcloud`** anywhere in
  `app/agents/monitoring.py`, `app/core/cloud_monitoring_client.py`, or
  `app/core/cloud_logging_client.py` — verified directly
  (`test_no_shell_or_subprocess_surface_in_monitoring_module`).
- **ADC only** — both clients follow the exact `CloudRunDeployer`/
  `FirestoreConfigError` pattern: lazy client construction, no credential
  path in `app.config.Settings`, `GoogleApiConfigError` raised if
  `GCP_PROJECT_ID` is unset.
- **No arbitrary project**: `MonitoringInput` has no `project_id` field at
  all (`test_arbitrary_project_cannot_be_supplied_through_input`) — both
  clients always use `settings.gcp_project_id`.
- **No arbitrary service escaping configured scope**: `region` and
  `environment` are validated against `settings.cloud_run_allowed_regions`/
  `cloud_run_allowed_environments` — the *same* allow-lists Deployment
  already enforces (Level 2.1), reused rather than duplicated, since
  Monitoring only ever observes Cloud Run services.
- **No arbitrary log query**: `CloudLoggingClient.query_service_logs` has
  no `filter`/`query` string parameter (§7) — a caller can only supply the
  five typed, bounded arguments.
- **Bounded windows**: `window_minutes` is rejected above
  `settings.monitoring_max_window_minutes` (default 24h) —
  `MONITORING_WINDOW_TOO_LARGE`.
- **Bounded result counts**: log queries are capped at
  `min(monitoring_log_query_limit, monitoring_log_query_max_limit)`; Cloud
  Monitoring queries always return one aggregated point per label (never a
  raw per-request stream) because `alignment_period` always equals the
  window.
- **Sanitization reused**: every evidence dict passed into a `Signal`
  goes through the existing `app.signals.sanitize.sanitize_metadata` (via
  the two normalization functions, §8) — no new sanitization logic.

## 14. Capability model

`MonitoringAgent.capabilities = {READ_MONITORING, READ_ARTIFACT}`.
`READ_MONITORING` already existed in `AgentCapability` (added in an earlier
level) — reused, not duplicated. `READ_ARTIFACT` is needed only for the
optional `DeploymentArtifact` correlation lookup (§10/§4). Explicitly
**not** granted: `WRITE_CODE`, `DEPLOY`, `WRITE_JIRA`, `RESOLVE_INCIDENT`,
`ROLLBACK`, `CREATE_INCIDENT` — verified directly
(`test_monitoring_agent_capabilities_are_read_only`,
`test_monitoring_agent_capabilities_exclude_mutation_capabilities`). No new
capability was introduced.

## 15. Error handling

`app/core/google_api_errors.py` — a shared translated-error hierarchy
(`GoogleApiConfigError`, `GoogleApiAuthError`, `GoogleApiPermissionError`,
`GoogleApiInvalidRequestError`, `GoogleApiServiceUnavailableError`,
`GoogleApiTimeoutError`, `GoogleApiMalformedResponseError`), following the
exact convention already established independently by
`app/knowledge/backends/google_search.py` and
`app/persistence/firestore/errors.py`. Rather than each of the two new
clients reimplementing that same `google.api_core.exceptions` → category
mapping, both share `translate_google_api_error()`. The two pre-existing
call sites (Agent Search, Firestore) were left untouched — this isn't a
forced refactor of working code, just the shared home for the pattern used
by everything added in this level. Raw Google SDK exceptions never escape
either client — both `try`/`except` around every SDK call and re-raise
through this translation.

## 16. Evidence-first design

Same principle as `TestingAgent`/`DeploymentAgent`: there is no LLM output
anywhere in `MonitoringAgent` that could be mistaken for telemetry, because
there is no LLM call at all (§3). Every `Signal` MonitoringAgent creates
traces directly back to a real `MetricPoint` or `LogEntryResult` a Google
API actually returned — verified directly
(`test_no_signal_created_when_no_telemetry_exists`,
`test_empty_telemetry_produces_no_signals_not_a_health_claim`,
`test_output_summary_never_claims_health_without_evidence`). If Cloud
Monitoring/Logging return nothing, `signal_ids=[]` and the summary reports
"0 signals observed" — never "system healthy," since health was never
established by anything collected.

## 17. Why Monitoring does not diagnose incidents

`MonitoringOutput` has no `diagnosis`/`root_cause`/`incident`/
`candidate_id`/`recommendation` field
(`test_monitoring_output_has_no_diagnosis_fields`). `MonitoringAgent`
writes no `Artifact` of type `INCIDENT`, calls no incident-creation
capability, and holds no capability that would let it (§14). The only
judgment call it makes at all — the severity threshold (§2) — is
deliberately narrow, static, and documented as an operational collection
policy rather than diagnosis: it says "this value crossed a configured
line," never "this means something is wrong" or "this was caused by X."
Deciding what an observation *means* — whether it's worth acting on,
whether several signals together describe one underlying problem — is
Detecting's job.

## 18. Future Detecting relationship

Not implemented here. Expected shape: Detecting periodically (or event-
triggered) calls `SignalRepository.query()` across a time window, using
`service_name`/`revision`/`environment` for operational correlation and
`signal_type`/`source` for clustering, reasons over the results plus
enterprise knowledge, and produces a `Candidate`
(`IncidentCandidate`/`FeatureCandidate` — neither exists yet). Monitoring
does not call Detecting, does not know it exists, and does not query
Enterprise Knowledge itself (§22 of the task — verified directly,
`test_monitoring_agent_never_queries_enterprise_knowledge`): the future
flow stays `Signals → Detecting → Enterprise Knowledge → interpretation`.

## 19. Google services ledger

| Google Service | Quipu Role | Status |
|---|---|---|
| Google ADK | Agent runtime/orchestration | Implemented (used by every prior agent; MonitoringAgent itself doesn't use ADK — see §3) |
| Gemini | Agent reasoning | Implemented (used by every prior agent; not used by MonitoringAgent — see §3) |
| Agent Search | Enterprise knowledge | Implemented (unused by MonitoringAgent — see §18) |
| Firestore | Durable state | Implemented (`FirestoreSignalRepository`, reused unchanged) |
| Cloud Run | Application deployment | Implemented (Level 2.1; MonitoringAgent observes it but doesn't deploy) |
| **Cloud Monitoring** | **Production metrics** | **Implemented this level** — real `MetricServiceAsyncClient.list_time_series` calls |
| **Cloud Logging** | **Production logs** | **Implemented this level** — real `LoggingServiceV2AsyncClient.list_log_entries` calls |

Only marked "Implemented" where real SDK/API calls exist and are exercised
by the code in this repository — no adapter-contract-only entry is listed
as implemented.

## 20. Limitations / deferred work

- **No latency threshold**: latency observations are always recorded at
  `INFO` severity; a configured latency ceiling (mirroring the error-rate
  thresholds) was not added in this level.
- **Environment-wide log collection is not implemented** — only metrics
  support `service_name=None` (§4); logging a whole environment would need
  enumerating its Cloud Run services first (a `ServicesClient.list_services`
  call), not built here.
- **No periodic/scheduled invocation** — this level builds the agent, not a
  scheduler. Something (a future cron/Cloud Scheduler trigger, or a manual
  invocation right after Deployment) must call `MonitoringAgent.execute()`;
  none of that wiring exists yet.
- **No composite Firestore index pre-created** for `SignalRepository.query()`
  combinations Monitoring's own write pattern will produce most often
  (documented already in `docs/architecture/signal_platform.md` §10).
- **Detecting, Incident Resolution, `Candidate`, `IncidentCandidate`,
  `FeatureCandidate`, automatic incident/ticket creation, Pub/Sub, and any
  HTTP ingestion/trigger API** are all explicitly out of scope and not
  implemented — per the task's own boundary.
