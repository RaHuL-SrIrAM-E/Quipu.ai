# Signal Platform (Level 3)

## Normalization

```
Cloud Monitoring ──┐
Cloud Logging ─────┤
Cloud Run ─────────┤
                   ├──→ Signal
Customer Feedback ─┤
User Behaviour ────┘
```

## Detection (future — not built in this level)

```
Signal
  ↓
Detecting Agent
  ↓
Candidate
  ├── IncidentCandidate
  └── FeatureCandidate
```

## Product loop (future)

```
Customer/User Signals
        ↓
  Detecting Agent
        ↓
  Feature Candidate
        ↓
   Human Review
        ↓
      Ticket
        ↓
   Planning Agent
        ↓
    SDLC pipeline
```

## Operations loop (future)

```
Cloud Run
   ↓
Monitoring
   ↓
 Signals
   ↓
Detecting
   ↓
IncidentCandidate
   ↓
Incident Resolution
   ↓
remediation
   ↓
Testing
   ↓
Deployment
```

---

This level implements **only** the foundation above the dotted line —
Signal itself, its normalization adapters, and its persistence. Monitoring
Agent, Detecting Agent, Incident Resolution Agent, `Candidate`,
`IncidentCandidate`, and `FeatureCandidate` are explicitly not implemented
here; they consume what this level builds.

## 1. Why Signal exists

Every prior level's domain model was built for one specific handoff shape:
`Artifact` for an agent's structured output, `Ticket` for an approved work
item. Neither fits operational telemetry or product feedback: a Cloud
Monitoring alert isn't an agent's output and isn't a request to act on —
forcing it into `Artifact` would mean inventing a fake `created_by` agent
and a fake `parent_artifact_ids` lineage for something no agent produced.
Forcing it into `Ticket` would mean treating raw evidence as if it were
already an approved decision to do work. `Signal` is the missing shape:
evidence that exists on its own, before any agent has looked at it and
before anyone has decided it means anything.

## 2. Signal vs Artifact

| | Artifact | Signal |
|---|---|---|
| Produced by | a Quipu agent, as its structured output | an external/internal source, via an adapter |
| Represents | a completed unit of agent work | an observed fact |
| Lineage | `parent_artifact_ids` (agent-to-agent handoff chain) | `provenance` (trace back to the origin system) |
| Exists relative to | a workflow (created within one) | independent of any workflow — see §10 |

A `Signal` is never wrapped in an `Artifact`, and no agent's output is ever
represented as a `Signal` — these are parallel concepts, not a hierarchy.

## 3. Signal vs Ticket

A `Ticket` is what the organization has decided to act on — it already
implies "start a workflow." A `Signal` implies nothing about action; most
signals will never become a `Ticket` at all (an isolated piece of feedback,
a transient metric blip). The future `Detecting Agent` is the only thing
that turns a cluster of signals into something a human reviews and — only
then, only sometimes — into a `Ticket` that starts a `Planning` workflow.

## 4. Signal vs Candidate (future)

Not implemented in this level — documented here because the boundary
determines what `Signal` is deliberately *not*:

```
Signal     = "what was observed"                       (this level)
Candidate  = "what Quipu believes the observation may represent"  (future Detecting Agent)
Ticket     = "what the organization has decided to act upon"      (existing)
```

`Signal` carries no `diagnosis`, `root_cause`, `candidate_id`, or
`incident_id` field — verified directly
(`test_signal_model_has_no_interpretation_fields`). If Detecting needs to
reference the signals behind a candidate, that reference lives on the
future `Candidate`/`IncidentCandidate`/`FeatureCandidate` model (which will
hold a list of `signal_id`s), never the other way around — `Signal` never
points forward to an interpretation of itself.

## 5. Operational signals

`SignalType`: `METRIC_ANOMALY`, `LOG_ERROR`, `APPLICATION_ERROR`,
`DEPLOYMENT_EVENT`, `AVAILABILITY_DEGRADATION`, `LATENCY_ANOMALY`.
`SignalSource`: `CLOUD_MONITORING`, `CLOUD_LOGGING`, `CLOUD_RUN`. These are
the categories the existing Cloud Run deployment evidence and the planned
Cloud Monitoring/Logging adapters actually need — no speculative additions
(e.g. no `THROUGHPUT_ANOMALY`, no `CPU_SATURATION` — add them when a real
adapter needs them, not now).

## 6. Product signals

`SignalType`: `CUSTOMER_FEEDBACK`, `SUPPORT_FEEDBACK`,
`FEATURE_REQUEST_PATTERN`, `USER_BEHAVIOR`, `ADOPTION_ANOMALY`.
`SignalSource`: `CUSTOMER_FEEDBACK`, `SUPPORT_SYSTEM`,
`PRODUCT_ANALYTICS`, `USER_BEHAVIOR`, `INTERNAL_SYSTEM`. Note
`FEATURE_REQUEST_PATTERN` has no dedicated adapter in this level — a
repeated pattern across many raw `CUSTOMER_FEEDBACK`/`SUPPORT_FEEDBACK`
signals is exactly the kind of aggregation §20/the product loop diagram
describes Detecting doing later; this level only needs the type to exist
so a future adapter (or Detecting itself, producing a derived signal) has
somewhere to put it.

## 7. Signal model

`app/domain/signal.py::Signal` — framework-independent, no Google SDK
imports (verified: `test_domain_signal_module_has_no_google_imports`,
`test_importing_app_domain_does_not_pull_in_google_cloud_monitoring_logging_or_run`,
the latter a real subprocess-level `sys.modules` check, not just a source
scan).

```python
class Signal(BaseModel):
    signal_id: str
    signal_type: SignalType
    source: SignalSource
    severity: SignalSeverity          # info | warning | error | critical
    status: SignalStatus              # ingestion-pipeline state, see §11

    observed_at: datetime             # when the underlying event happened (source-reported)
    ingested_at: datetime             # when Quipu normalized it

    subject: str                      # entity/service/feature this concerns
    summary: str                      # short factual description — never a diagnosis

    service_name: str | None
    environment: str | None
    deployment_artifact_id: str | None  # correlates to a DeploymentArtifact, see §14
    revision: str | None

    evidence: dict[str, Any]          # normalized, sanitized evidence payload
    metadata: dict[str, Any]          # normalized, sanitized supplementary context
    provenance: SignalProvenance      # see §9
    fingerprint: str                  # dedup identity, see §12
```

Deliberately smaller than the task's example field list — no separate
`SignalSource` sub-model (the `SignalSource` enum plus `provenance` already
answers "where did it come from"), no `correlation_id` field (§13:
correlation is Detecting's job, done by *querying* on the fields already
here — `service_name`, `revision`, time range — not by a field this level
would have to guess the shape of).

## 8. Timestamps — timezone-aware UTC only

Unlike `Artifact`/`Ticket` (Level 1.1, which default to naive
`datetime.utcnow()` — an old convention this level does not touch),
`Signal.observed_at`, `Signal.ingested_at`, and
`SignalProvenance.collected_at` all reject a naive `datetime` via a
`field_validator` (`test_naive_observed_at_rejected`,
`test_naive_ingested_at_rejected`,
`test_provenance_naive_collected_at_rejected`) and default to
`datetime.now(timezone.utc)`. This is an intentionally stricter standard
than the models it sits alongside, per the task's explicit instruction not
to introduce more naive-datetime behavior — `app/persistence/serialization.py`
still defensively coerces naive datetimes for the *older* models at the
Firestore boundary, but `Signal` should never need that fallback to trigger.

## 9. Provenance

`SignalProvenance`: `source_system` (required, non-empty — a descriptive
string like `"cloud_monitoring"`, `"zendesk"`), `source_event_id` (the
source's own id for the event — used in the fingerprint, §12),
`source_uri` (a link back to the origin — a console URL, a ticket URL;
never a value containing a secret), `trace_id`, `collected_at`.
Deliberately small: enough to investigate the original event (open the
console, look up the log entry, find the feedback record) without Signal
becoming a dump of the entire source payload. Every adapter in
`app/signals/adapters.py` populates this from the real fields the source
payload actually carries — proven directly for each adapter
(`test_cloud_monitoring_alert_preserves_provenance`,
`test_cloud_logging_preserves_trace_id`, etc).

## 10. Persistence — top-level collection, not workflow-scoped

Every other repository in `app/persistence` (`Artifact`, `AgentExecution`,
`Decision`, `IncidentRecord`) is workflow-scoped — Firestore subcollections
under `workflows/{workflow_id}/...`. `Signal` is deliberately not: most
signals exist *before* any workflow does (a metric anomaly doesn't create a
workflow by itself — Detecting deciding to act on it does, later), and the
future Detecting Agent needs to query across all signals to look for
correlation, not within one workflow's subcollection.

```
signals/{signal_id}
```

`app/persistence/repositories/signal.py::SignalRepository` (Protocol):
`save` (upsert by `signal_id`, same pattern as `ArtifactRepository.save` —
signals are immutable evidence, not create-vs-update), `get`,
`find_by_fingerprint` (§12), `query(SignalQuery)`.

`SignalQuery`: optional `signal_type`, `source`, `service_name`,
`environment`, `severity`, `since`/`until` (time range), and a required
`limit` (default 50, capped at 500 — no unbounded result sets). All filters
are ANDed; this is not a general search API, just the dimensions
Monitoring/Detecting are expected to need per the task's own list.

Implementations: `InMemorySignalRepository`
(`app/persistence/memory/repositories.py`, used for the whole normal test
suite) and `FirestoreSignalRepository`
(`app/persistence/firestore/repositories.py`, top-level `signals`
collection, `.where(filter=FieldFilter(...))` chaining +
`order_by("observed_at", DESCENDING)` + `.limit()`). **Documented
limitation**: combining a range filter (`since`/`until`) with multiple
equality filters may require a Firestore composite index in production —
not pre-created here, since the real combinations Monitoring/Detecting will
issue aren't known yet; Firestore's own error surfaces a console link to
create the needed index the first time an uncovered combination runs.

No new persistence subsystem — `SignalRepository` follows the exact same
Protocol-based, in-memory/Firestore-swappable pattern as every existing
repository, registered the same way in `app/persistence/__init__.py`,
`app/persistence/memory/__init__.py`, `app/persistence/firestore/__init__.py`.

## 11. Signal lifecycle

`SignalStatus`: `OBSERVED` → `INGESTED` → `AVAILABLE`. This tracks
ingestion-pipeline progress only — never Detecting's interpretation, and
the evidence fields (`evidence`, `summary`, `provenance`, etc.) never
change across these states; a Signal is treated as immutable evidence once
persisted. Every adapter in this level is synchronous (payload in, `Signal`
out, in one function call) and produces `AVAILABLE` directly —
`OBSERVED`/`INGESTED` exist for a future asynchronous ingestion pipeline
(e.g. a Pub/Sub-delivered payload staged before validation completes) and
are not reachable through any code path in this level
(`test_signal_defaults_to_available_status`).

## 12. Deduplication

`app/domain/signal.py::compute_fingerprint(source, source_event_id,
subject, window=None)` — a deterministic SHA-256 hex digest over those
four fields. Every adapter computes it when building a `Signal`. This is
the dedup **contract boundary** the task asked for, not an enforcement
mechanism: `SignalRepository.find_by_fingerprint()` lets a future ingestion
pipeline check "have I seen this before?" prior to calling `save()`; the
repository itself does not reject a second `save()` of a signal sharing an
existing fingerprint (proven directly:
`test_duplicate_observation_has_same_fingerprint_and_is_discoverable` shows
two independently-constructed signals for the same underlying event share
a fingerprint, and the repository makes that discoverable without
enforcing anything). `window` lets a caller fold repeated observations
within a time bucket into one fingerprint (e.g. the same metric anomaly
re-reported every minute) when that's the intended dedup granularity for a
source without a stable `source_event_id` — used by the user-behavior
adapter (§6/§15).

## 13. Correlation (future — not implemented)

Deduplication answers "are these the same signal"; correlation answers
"might these different signals describe the same underlying event."
`Signal` carries exactly the fields a future Detecting Agent needs to
correlate without this level guessing at the correlation logic itself:
`service_name` + `revision` + `observed_at` (query a time window across
signal types for the same service/revision — e.g. an error-rate spike, a
latency spike, and a `DEPLOYMENT_EVENT` signal all sharing
`service_name="quipu-api"`, `revision="quipu-api-00007"`, within the same
few minutes). No correlation intelligence, scoring, or `IncidentCandidate`
construction exists in this level.

## 14. Cloud Run / deployment correlation

`Signal.service_name`, `.environment`, `.revision`, and
`.deployment_artifact_id` exist specifically so a production signal can
reference the `DeploymentArtifact` that put a given revision into
production. `app/signals/adapters.py::normalize_cloud_run_deployment` is a
**real, working adapter** — not a stub — that takes an actual
`app.domain.Artifact` (`ArtifactType.DEPLOYMENT`) already produced by
`DeploymentAgent` (Level 2.1) and turns it into a `DEPLOYMENT_EVENT`
signal: `severity=INFO` on `status="succeeded"`, `ERROR` otherwise,
`deployment_artifact_id` set to the artifact's own id, `revision` and
`service_name` carried through directly. No external call is needed — the
evidence already exists in Quipu's own persisted state.

This is what will eventually let Detecting reason "revision X deployed →
errors increased → probable deployment-related incident" — that reasoning
itself is not implemented here; only the correlation metadata is preserved.

## 15. Source adapters

`app/signals/adapters.py` — the boundary between a source-specific payload
and the common `Signal` contract:

```
source event (dict, or an existing Quipu domain object)
      |
   adapter
      |
    Signal
```

`SignalSourceAdapter` (Protocol, one method: `normalize(raw_event) ->
Signal`) is the shared contract; each concrete adapter is a small,
stateless `normalize_...()` function plus a thin class implementing the
Protocol (`test_adapter_classes_satisfy_signal_source_adapter_protocol`).

| Adapter | What it normalizes | Real or contract-only |
|---|---|---|
| `CloudRunDeploymentAdapter` | an `app.domain.Artifact` (`ArtifactType.DEPLOYMENT`) DeploymentAgent already produced | **Real** — no external call, operates on Quipu's own data |
| `CloudMonitoringAlertAdapter` | Cloud Monitoring's notification-channel webhook JSON shape | Normalization logic is real; the payload must already have been delivered to Quipu by some future mechanism (§18) |
| `CloudLoggingEntryAdapter` | a Cloud Logging `LogEntry` JSON shape | Same as above |
| `CustomerFeedbackAdapter` / `SupportFeedbackAdapter` / `UserBehaviorAdapter` | an internal, Quipu-defined payload shape | Normalization logic is real; no external product-analytics/support-system SDK exists in this level at all (§27 explicitly excludes those connectors) |

**No adapter calls a live Google API.** The two operational adapters parse
an *already-received* payload matching a real, documented Google schema
(Cloud Monitoring's webhook notification format, Cloud Logging's `LogEntry`
format) — they do not poll, subscribe, or authenticate to anything. The
actual delivery mechanism (a Pub/Sub push subscription, or an HTTP webhook
endpoint receiving Cloud Monitoring's POST) is a future integration,
explicitly out of scope here since building it means an HTTP surface,
which Level 3 excludes (§22/§27). This is intentionally not claimed as a
live integration — see §20.

Malformed input is rejected loudly: every adapter validates its required
fields up front and raises `ValueError` on a missing/malformed payload
(`test_cloud_monitoring_missing_required_field_rejected`,
`test_cloud_logging_missing_required_field_rejected`,
`test_customer_feedback_missing_required_field_rejected`,
`test_user_behavior_missing_required_field_rejected`) rather than
constructing a partially-populated `Signal`.

## 16. Normalization vs. domain

No provider-specific transformation logic lives in `app.domain` — the
`Signal` model itself has no idea Cloud Monitoring or Zendesk exist; all of
that lives in `app/signals/adapters.py`, a separate top-level package (same
shape as `app.knowledge`'s `backends/` split), which depends on
`app.domain` but not the reverse.

## 17. Security / PII boundary

`app/signals/sanitize.py::sanitize_metadata()` — every adapter runs its
evidence/metadata dict through this before constructing a `Signal`:

- **Secret-shaped keys are redacted**, not just skipped: any key matching
  `password|secret|token|api_key|authorization|credential|private_key|
  access_key|ssn|credit_card|cvv` (case-insensitive) is replaced with
  `"[REDACTED]"`, recursively through nested dicts/lists
  (`test_sanitize_metadata_redacts_secret_shaped_keys`,
  `test_sanitize_metadata_redacts_nested_secret_keys`).
- **Oversized values are truncated** (2000 chars) — Signal is evidence with
  provenance pointing back to the source, not a dump of an entire raw log
  body or feedback transcript
  (`test_sanitize_metadata_truncates_oversized_values`).
- **No raw PII is added by any adapter itself**: the customer-feedback
  adapter's docstring documents that `customer_ref` must already be
  anonymized by the caller (e.g. a hashed account id) before it reaches
  the adapter — proven that the adapter itself introduces no `email`/
  `name`/`phone` field
  (`test_customer_feedback_does_not_leak_raw_customer_ref_key_shape`).

This is **not** a full data-loss-prevention system (explicitly out of scope,
§17/§27) — it is the documented floor: never store secrets, avoid raw
credentials/tokens, avoid unnecessary PII, truncate raw content. Source
adapters remain primarily responsible for not putting sensitive data into
a `Signal` in the first place; `sanitize_metadata` is a backstop, not the
only control.

## 18. Enterprise Knowledge boundary — unchanged

Signal ingestion never queries `app.knowledge`. No adapter, no
`SignalRepository` method, no code in `app/signals/` imports
`KnowledgeGateway`/`KnowledgeService`/anything from `app.knowledge`. The
future flow stays: `Signals → Detecting Agent → Enterprise Knowledge →
interpretation` — knowledge is contextual reasoning Detecting will invoke,
not something Signal ingestion does on its own. `app.knowledge` itself is
completely untouched by this level (no files under `app/knowledge/`
modified).

## 19. Future Monitoring Agent

Not implemented here. Expected shape: Monitoring periodically (or via a
future Pub/Sub push) pulls Cloud Monitoring/Logging payloads, runs them
through `CloudMonitoringAlertAdapter`/`CloudLoggingEntryAdapter`, checks
`SignalRepository.find_by_fingerprint()` before `save()`ing, and also calls
`normalize_cloud_run_deployment()` on every `DeploymentArtifact`
`DeploymentAgent` produces (wiring that call site — e.g. via
`OrchestrationService` after a successful deployment stage — is itself
future work, not built in this level).

## 20. Future Detecting Agent

Not implemented here. Expected shape: queries `SignalRepository.query()`
across a time window (using `service_name`/`revision`/`environment` for
operational correlation, §13; using `signal_type`/`source` clustering for
product signals), reasons over the results plus enterprise knowledge, and
produces a `Candidate` (`IncidentCandidate` or `FeatureCandidate`) — neither
model exists yet.

## 21. Future Incident Resolution / Feature Candidate flow

Both explicitly deferred. The eventual product loop
(`Customer/User Signals → Detecting → FeatureCandidate → Human Review →
Ticket → Planning → SDLC pipeline`) and operations loop
(`Cloud Run → Monitoring → Signals → Detecting → IncidentCandidate →
Incident Resolution → remediation → Testing → Deployment`) are both
diagrammed at the top of this document as the destination this level's
foundation is built toward — no code for either loop's downstream stages
exists yet.

## 22. Google services planned / implemented

| Service | Status |
|---|---|
| Cloud Run | **Implemented** — `normalize_cloud_run_deployment` is a real, working adapter over `DeploymentAgent`'s own output (no new Google SDK call; reuses Level 2.1's existing Cloud Run integration) |
| Firestore | **Implemented** — `FirestoreSignalRepository`, same client/serialization boundary as every other repository |
| Cloud Monitoring | **Planned** — adapter logic (`CloudMonitoringAlertAdapter`) is real and tested; no live API call, subscription, or credential use exists in this level |
| Cloud Logging | **Planned** — same as Cloud Monitoring |
| Pub/Sub | **Not planned for this level** — explicitly excluded (§22 of the task); adapters are payload-in/Signal-out functions ready to be wired to a Pub/Sub push handler or any other delivery mechanism later |

## 23. Existing systems left unchanged

No agent (`Planning`/`Architecture`/`Codegen`/`Testing`/`Deployment`), no
`OrchestrationService` code, and no `app.knowledge` file was modified for
this level. The only "shared contract" changes are additive: new enum
values are new `StrEnum`s (`SignalType`, `SignalSource`, `SignalSeverity`,
`SignalStatus`) in `app/domain/enums.py`, and new exports in
`app/domain/__init__.py`/`app/persistence/__init__.py` — nothing existing
was renamed, removed, or given new required fields.
