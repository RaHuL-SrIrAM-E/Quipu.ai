# Post-Remediation Production Verification

## Diagram

```
Incident
  ↓
DetectionResult (INCIDENT)
  ↓
IncidentResolutionAgent → ResolutionResult
  ↓
OrchestrationService.start_remediation_from_resolution (authorization)
  ↓
Codegen → Testing → Deployment
  ↓
workflow.metadata["remediation_outcome"] = "deployed_pending_verification"
  ↓
MonitoringAgent (real production telemetry, unchanged)
  ↓
NEW post-deployment Signals
  ↓
RemediationVerificationService                 (app/verification/)
  │  1. locate the deployment (revision/service/environment) this
  │     resolution produced
  │  2. query SignalRepository for post-deployment Signals in that
  │     scope, within a bounded window
  │  3. deterministically compare against the original incident's
  │     condition (app/verification/policy.py)
  ↓
RemediationVerification, persisted (RemediationVerificationRepository)
  │
  ├── VERIFIED_RESOLVED     → workflow.metadata["remediation_outcome"] = "verified_resolved"
  ├── STILL_DEGRADED        → "still_degraded" — incident remains active
  ├── INSUFFICIENT_EVIDENCE → "insufficient_evidence" — never treated as success
  └── ESCALATED             → "escalated" — needs a human
```

## 1. Why deployment success is insufficient

A workflow reaching `WorkflowStatus.COMPLETED` after remediation means exactly
one thing: `deploy_cloud_run` returned `status="succeeded"`
(`app.agents.deployment`). It says nothing about whether the deployed code
actually addressed the production condition DetectingAgent originally
flagged. Before this task, `OrchestrationService._execute_decision` already
marked this explicitly — `workflow.metadata["remediation_outcome"] =
"deployed_pending_verification"` — precisely so nothing downstream could
mistake "shipped" for "fixed." This task builds the component that turns
"pending" into an actual, evidence-based verdict.

**The invariant this task establishes: Quipu never claims an incident is
resolved solely because remediation deployed successfully.** Only a
`RemediationVerification` with `outcome=VERIFIED_RESOLVED` represents Quipu
having actually checked real production telemetry.

## 2. Baseline evidence

"Baseline" is the original `DetectionResult` (`resolution.detection_id`) and
its `supporting_signal_ids` — the evidence DetectingAgent used to conclude
`INCIDENT` in the first place. `RemediationVerificationService` stores only
**references**: `baseline_detection_id` and `baseline_signal_ids`, plus a
`baseline_summary` compact string (e.g. `"2 signal(s):
application_error(critical), latency_anomaly(info)"`) built from each
Signal's own already-sanitized `signal_type`/`severity` — never the raw
`evidence`/`metadata` dict, never a Cloud Monitoring/Logging payload.

The baseline's Signal *types* (not the Signal objects themselves) also drive
which post-deployment conditions get evaluated — see §4/§9.

## 3. Post-deployment evidence

Retrieved via the **existing** `SignalRepository`/`SignalGateway` — no new
query surface. `RemediationVerificationService` issues one `SignalQuery` per
relevant condition type (mirroring `app.agents.detecting._retrieve_evidence`
and `app.detection.policy.count_related_signals`'s shape), bounded by
`service_name`/`environment`/a time window/`Settings.
verification_max_signals_per_condition`. Only the resulting `signal_id`s are
stored (`post_deployment_signal_ids`, and the subset that actually informed
the outcome as `supporting_signal_ids`) plus a compact `evidence_summary`
dict (`{"application_error": "healthy", "latency_anomaly": "no_evidence"}`)
— never raw payloads.

## 4. Correlation strategy

Deployment correlation uses, in order of preference:

1. **Revision** (`deployment_artifact.payload["revision"]`) — the strongest
   available field. `collect_post_deployment_signals`
   (`app/verification/policy.py`) filters retrieved Signals: a Signal
   carrying a *different* revision than the one being verified is excluded.
   A Signal with no revision at all is kept (best-effort — not every source
   populates it; see §16).
2. **service_name / environment** — used as the `SignalQuery` filter itself
   (an exact match, same as every other Signal query in this codebase).
3. **Time window** (§5) — bounds *when* a Signal must have been observed
   relative to `deployment_artifact.created_at`.

No change was made to `SignalQuery`/`SignalRepository` — revision filtering
is a post-query, application-level filter (the "smallest additive
interface" the task asked for turned out to need no interface change at
all).

The deployment itself is located by scanning
`workflow.artifact_ids` (reversed) for the latest `ArtifactType.DEPLOYMENT`
artifact — the same `_resolve_input_artifact_id`-style pattern
`OrchestrationService` already uses, applied read-only here.

## 5. Verification window

`Settings.verification_window_minutes` (default 30) bounds how long after
`deployment_artifact.created_at` post-deployment evidence is considered.
`Settings.verification_minimum_post_deployment_signals` (default 1) is the
floor below which **no conclusion is drawn** — see §8. Neither is
hard-coded; both are ordinary `app.config.Settings` fields, same convention
as every other Quipu ceiling.

## 6. Verification outcomes

`app.domain.remediation_verification.VerificationOutcome` — a closed set:

| Outcome | Meaning |
|---|---|
| `VERIFIED_RESOLVED` | Sufficient post-deployment evidence exists, and every evaluable original condition returned to healthy. |
| `STILL_DEGRADED` | Sufficient evidence exists, and at least one original condition is still present. |
| `INSUFFICIENT_EVIDENCE` | Missing/too little monitoring data to conclude safely. **Never** treated as success. |
| `ESCALATED` | The verification process itself hit a safety/infrastructure condition needing a human (today: the correlated deployment artifact does not indicate a successful deployment). |

`VerificationStatus` (`IN_PROGRESS` → `COMPLETED`) is a separate field —
the record's own lifecycle, not the outcome (same
pipeline-state-vs-interpretation split as `SignalStatus` one layer down).

## 7. Monitoring integration

`RemediationVerificationService` **never calls MonitoringAgent** — it
consumes Signals MonitoringAgent already produced, through
`SignalRepository`. The intended operational sequence (also what
`app/demo/harness.py` now demonstrates) is: deployment completes →
MonitoringAgent runs (on whatever schedule/trigger already exists —
unchanged by this task) → verification runs afterward and finds that
evidence. No Cloud Monitoring/Cloud Logging client is constructed anywhere
in `app/verification/` — `app.core.cloud_monitoring_client`/
`app.core.cloud_logging_client` remain untouched, still used only by
`app.agents.monitoring`.

## 8. Idempotency

`app.domain.remediation_verification.compute_verification_key(resolution_id,
deployment_artifact_id, revision, window_minutes)` is the idempotency key —
and is reused directly as `RemediationVerification.verification_id`, the
same "deterministic id doubles as the create()-level race guard" pattern
`OrchestrationService.start_workflow_from_review` already established.
`verify_remediation()`:

1. Checks `find_by_idempotency_key()` first — a completed prior verification
   for the exact same deployment is returned unchanged, no re-collection.
2. Otherwise calls `RemediationVerificationRepository.create()` with the
   deterministic id. A concurrent caller computing the *same* id gets
   `DuplicateEntityError` and returns whatever is currently stored instead
   of doing the work twice (see
   `test_concurrent_verification_attempts_are_safe`).
3. Finishes with `update_if_version()` (`IN_PROGRESS` → `COMPLETED`) —
   `RemediationVerificationRepository` mirrors `FeatureReviewRepository`'s
   optimistic-concurrency shape, not Signal/Detection/Resolution's simple
   upsert, because this record has a real state transition to protect.

No exactly-once claim is made anywhere — same discipline as
`docs/architecture/pubsub_signal_ingestion.md` §7 and
`docs/architecture/event_driven_detection.md` §10.

## 9. Deterministic verification policy (comparison with the original incident)

`app/verification/policy.py` — small and closed, covering exactly the
`SignalType`s MonitoringAgent's own adapters already produce
(`METRIC_ANOMALY`, `LATENCY_ANOMALY`, `APPLICATION_ERROR`, `LOG_ERROR`,
`AVAILABILITY_DEGRADATION`). The original detection's supporting Signal
*types* determine which of these get evaluated — a condition type the
original incident didn't involve is never checked.

Two evaluation shapes, because MonitoringAgent itself emits these two
families asymmetrically:

- **Always-emitted** (`METRIC_ANOMALY`, `LATENCY_ANOMALY`) — MonitoringAgent
  creates one of these whenever there was any traffic to observe, healthy
  or not. Absence post-deployment therefore means **no evidence**, not
  health. `METRIC_ANOMALY`/reuses MonitoringAgent's own already-computed
  `Signal.severity` (`app.agents.monitoring._classify_error_rate`) —
  anything other than `INFO` is degraded. `LATENCY_ANOMALY` is the one type
  MonitoringAgent deliberately leaves at `SignalSeverity.INFO` (its own
  comment: *"thresholding latency is a future policy addition... not
  implemented here"*) — this task is that addition, scoped narrowly to
  verification's own comparison via `Settings.
  verification_latency_p99_threshold_ms`, never inside MonitoringAgent
  itself.
- **Presence-only** (`APPLICATION_ERROR`, `LOG_ERROR`,
  `AVAILABILITY_DEGRADATION`) — MonitoringAgent only ever creates these in
  the *bad* case (a matching ERROR+ log entry; zero active instances).
  Their absence post-deployment **is** the healthy signal. This is a
  deliberate, documented asymmetry — treating "no log-error Signal" as
  "insufficient evidence" here would make a genuinely healthy remediation
  unverifiable forever, since MonitoringAgent has no "everything's fine"
  log signal to emit. See §16 for the limitation this creates.

If a condition cannot be meaningfully evaluated (an always-emitted type with
zero post-deployment evidence), the whole verification returns
`INSUFFICIENT_EVIDENCE` rather than guessing — see §16's degraded → success
precedence rule below.

Outcome precedence (`decide_outcome`): zero total evidence, or below the
configured minimum → `INSUFFICIENT_EVIDENCE`. Otherwise: any degraded
condition → `STILL_DEGRADED` (degradation evidence always wins). Otherwise:
any always-emitted condition with no evidence → `INSUFFICIENT_EVIDENCE`.
Otherwise → `VERIFIED_RESOLVED`.

## 10. Relationship to DetectingAgent

Verification is explicitly **not** "run DetectingAgent again and see if it
says no incident." `DetectingAgent`/Gemini are never invoked from
`app/verification/` at all — this is a deterministic evidence comparison
(§2 of the task: *"do not use Gemini merely to compare before vs after
metrics"*). `DetectingAgent` remains free to independently detect a
**new/related** incident from the same post-deployment Signals (through the
normal event-driven detection path,
`docs/architecture/event_driven_detection.md`) — that is a separate,
unrelated detection cycle, not something `RemediationVerificationService`
triggers or depends on.

## 11. Relationship to IncidentResolutionAgent

Verification never re-authorizes, re-diagnoses, or changes a
`ResolutionResult`. `resolution.target_agent` and
`resolution.remediation_strategy` are read nowhere in
`app/verification/service.py` (enforced by
`test_verification_ignores_target_agent_entirely`/
`test_verification_never_changes_remediation_strategy`) — correlation is
purely via the workflow's own deployment artifact, never a model-supplied
routing field. A `STILL_DEGRADED` outcome does **not** automatically start a
new remediation cycle; it only updates
`workflow.metadata["remediation_outcome"]` (§13) as a structured signal the
existing incident flow (a human, or a future orchestration hook) can act
on.

## 12. Why verification is deterministic rather than another LLM agent

Same reasoning `app.agents.monitoring`'s own module docstring already
establishes for observation ("do not use Gemini merely for mechanical API
translation"): whether a post-deployment metric's already-computed severity
indicates a problem, or whether a latency value crosses a configured
threshold, is a mechanical comparison — there is no genuinely ambiguous
reasoning task here for Gemini to add value to. `RemediationVerificationService`
is a "not an agent" component, the same precedent
`FeatureReviewService`/`OrchestrationService` already established:
deterministic application logic, zero LLM calls, direct repository
injection instead of the agent-facing gateway layer. No new `QuipuAgent`
was added.

## 13. Incident lifecycle

No new `WorkflowStatus` value and no second state machine — reuses the
existing `workflow.metadata["remediation_outcome"]` marker
(`app.orchestration.service._execute_decision`, unchanged) additively:

```
"deployed_pending_verification"   (existing, set by OrchestrationService)
        ↓ verify_remediation() claims the idempotency key
"verification_in_progress"        (new — set by RemediationVerificationService)
        ↓ outcome decided
"verified_resolved" | "still_degraded" | "insufficient_evidence" | "escalated"
```

This mirror write is **best-effort**: it uses `WorkflowRepository.
update_if_version` and, on a lost concurrency race, logs a warning and
moves on — the `RemediationVerification` record itself (already durably
saved via its own `update_if_version`) is the authoritative result;
`workflow.metadata` is a convenience for anything reading `WorkflowState`
directly. `WorkflowState.status`/`current_stage` (the real state machine
`OrchestrationService` drives) are never touched by verification.

## 14. Failure semantics

Two independent failure domains:

- **Structural misuse** (`resolution_id` doesn't exist, isn't linked to an
  `INCIDENT` `DetectionResult`, has no `workflow_id`, or that workflow has
  no `DEPLOYMENT` artifact yet) → `VerificationError` is raised. This means
  "you asked the wrong question" — verification was called too early or
  against the wrong id — not a verification outcome.
- **A genuinely valid verification that can't safely conclude** → a
  persisted `RemediationVerification` with `INSUFFICIENT_EVIDENCE` or
  `ESCALATED`. A `SignalRepository.query()` failure (a real Monitoring
  collection outage) propagates as an exception rather than being silently
  swallowed into a false `VERIFIED_RESOLVED` — see
  `test_monitoring_query_failure_never_falsely_resolves`.

## 15. Security

- No code execution, no deployment, no rollback anywhere in
  `app/verification/` — `RemediationVerificationService` exposes exactly
  one public method, `verify_remediation()`
  (`test_verification_service_has_no_deploy_or_rollback_surface`).
- Never bypasses `IncidentResolutionAgent`'s authorization — it reads an
  already-authorized `ResolutionResult`, never proposes or approves one.
- Never trusts a model-supplied routing field (§11).
- Correlation fields (`service_name`, `environment`, `revision`) come from
  the deployment `Artifact`'s own payload — application-controlled state
  DeploymentAgent already produced, never a Pub/Sub/detection-trigger
  payload.

## 16. Limitations / deferred work

- **Presence-only absence is a heuristic, not a guarantee** (§9): Quipu
  cannot currently distinguish "MonitoringAgent checked Cloud Logging and
  found nothing" from "MonitoringAgent never queried Cloud Logging at all"
  — both look identical (zero matching Signals). A future MonitoringAgent
  enhancement (an explicit "collection attempted, N entries found"
  heartbeat Signal) could close this gap without touching verification's
  own policy shape.
- Revision-less Signals are kept rather than excluded (best-effort
  correlation) — a source that never populates `revision` can't be
  strengthened past service/environment/time-window correlation without
  itself being fixed upstream.
- No automatic second remediation cycle on `STILL_DEGRADED` — by design
  (§11/§13 of the task): the existing incident flow decides what happens
  next, not this component.
- No HTTP API/UI surfaces `RemediationVerification` records yet — they are
  fully durable and queryable (`RemediationVerificationRepository.query`)
  but nothing external reads them in this task.
- `Settings.verification_latency_p99_threshold_ms` is a single global
  threshold, not per-service — matches the level of granularity
  `MonitoringAgent`'s own thresholds (`monitoring_error_rate_*_threshold`)
  already use.

## Google services ledger

Unchanged from `docs/architecture/event_driven_detection.md` §15 — no new
Google service. `app/verification/` contains no Google SDK imports at all;
the Google story here is that Cloud Monitoring/Cloud Logging telemetry
(already integrated) is what verification's comparison runs against.
