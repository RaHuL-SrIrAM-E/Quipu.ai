# Incident Resolution → Authorized Remediation Orchestration (Level 3.6)

## Diagram

```
Cloud Monitoring / Logging
          │
          ▼
      Monitoring
          │
          ▼
        Signal
          │
          ▼
    DetectingAgent
          │
          ▼
       INCIDENT
          │
          ▼
IncidentResolutionAgent
          │
          ▼
   ResolutionResult
          │
          ▼
┌─────────────────────┐
│ Authorization       │
│ + Transition Policy │
└──────────┬──────────┘
           │
   ┌───────┼───────────────┐
   ▼       ▼               ▼
CODE_FIX  ARCH REVIEW     ROLLBACK
   │       │               │
   ▼       ▼               ▼
Codegen Architecture    ESCALATE
   │       │            (see §6)
   └───┬───┘
       ▼
    Testing
       │
   PASS ONLY
       ▼
   Deployment
       │
       ▼
    Monitoring
       │
       ▼
  Health Evidence
       │
  ┌────┴────┐
  ▼         ▼
Healthy   Unhealthy
  │         │
  ▼         ▼
(future: Detecting  (future: Detecting
 confirms resolved)  raises a new incident)
```

```
Quipu separates reasoning from execution.

IncidentResolutionAgent answers:  "What should we do?"
OrchestrationService answers:     "Are we allowed to do it, and which
                                    workflow should execute it?"
Specialized agents answer:        "How do we perform it safely?"
```

This is the fourth time this architecture has drawn exactly this line
(Detecting/Monitoring, Feature Review/Planning, and now Incident
Resolution/Remediation) — it is one of Quipu's core differentiators, not a
one-off pattern.

## 1. Incident → Resolution (unchanged)

`DetectingAgent` (3.2) produces a `DetectionResult` with `detection_type=
INCIDENT`. `IncidentResolutionAgent` (3.3) diagnoses it and produces a
`ResolutionResult` — a recommendation, never an execution. Nothing in this
level changes either agent's own internal reasoning or evidence-validation
logic. Two small, additive changes were made to `IncidentResolutionAgent`
to make remediation possible at all (§9 below): `ResolutionResult` gained a
`workflow_id` field, and the previously-private `_STRATEGY_TARGET_AGENT`
map was renamed `STRATEGY_TARGET_AGENT` (no underscore) so the
orchestrator can import and reuse the exact same mapping — not duplicate
it.

## 2. Resolution → authorization

`OrchestrationService.start_remediation_from_resolution(resolution_id)` is
the one new entry point this level adds. Its signature takes **only**
`resolution_id` — no `strategy`, `target_agent`, or `stage` parameter
exists on it at all
(`test_start_remediation_signature_takes_only_resolution_id`), so there is
no way for a caller to inject an unauthorized routing.

Before anything executes, it deterministically re-verifies (§13 A–H of the
task):

1. The `ResolutionResult` exists.
2. Its `detection_id` resolves to a `DetectionResult` with `detection_type
   == INCIDENT` (`test_non_incident_detection_rejected`) — a product
   opportunity can never be reinterpreted as an incident here, mirroring
   the identical check `IncidentResolutionAgent` already makes at
   diagnosis time.
3. `_authorize_remediation_strategy()` — a **backstop**, not a second risk
   policy — re-checks the exact invariants `IncidentResolutionAgent._apply_safety_policy`
   (3.3) already enforces before ever persisting a non-escalation
   `ResolutionResult`: `risk != HIGH`, `root_cause_confidence >=
   settings.incident_resolution_min_confidence_for_auto_remediation`, and
   `supporting_signal_ids` non-empty. Any violation downgrades to
   `ESCALATE` — this function can only ever move a strategy *toward*
   `ESCALATE`, never away from it
   (`test_high_risk_resolution_rejected_downgraded_to_escalate`,
   `test_low_confidence_resolution_cannot_trigger_remediation`,
   `test_fabricated_evidence_resolution_cannot_trigger_remediation`).
4. `ResolutionResult.target_agent` is **never read** anywhere in this
   method. The entry stage is derived purely from the (already-validated,
   closed-enum) `remediation_strategy`, via the exact same
   `STRATEGY_TARGET_AGENT` map `IncidentResolutionAgent` itself uses,
   reusing the existing `_stage_for_agent()` helper — proven directly with
   an adversarial payload claiming `target_agent="deployment_agent"` for a
   `code_fix` strategy: the workflow still lands on `WorkflowStage.CODEGEN`
   (`test_spoofed_target_agent_is_ignored`).

## 3. Strategy mapping

Reused unchanged from Level 3.3 — no new strategies were added (the task's
own instruction: don't add strategies without a proven missing
requirement, and inspection found none):

| `RemediationStrategy` | Entry stage | Notes |
|---|---|---|
| `CODE_FIX` | `WorkflowStage.CODEGEN` | via `STRATEGY_TARGET_AGENT["code_fix"] = "codegen_agent"` |
| `ARCHITECTURE_REVIEW` | `WorkflowStage.ARCHITECTURE` | via `"architecture_agent"` |
| `ROLLBACK` | never executed | always forced to `ESCALATE` — see §6 |
| `ESCALATE` | none | `WorkflowStatus.ESCALATED` |
| `NO_ACTION` | none | recorded, no mutation |

## 4. CODE_FIX flow

```
Codegen → Testing → Deployment → Monitoring (future observation)
```

`start_remediation_from_resolution` sets `current_stage=CODEGEN` on the
workflow and returns — it does **not** itself call `execute_next_step()`
(same pattern `start_workflow`/`start_workflow_from_review` already
established: creation/preparation is a separate step from execution). A
subsequent `execute_next_step()`/`run_to_completion()` call — the exact
same methods every other workflow uses — is what actually invokes
`CodegenAgent`, then `TestingAgent`, then (only on a passing verdict)
`DeploymentAgent`, through the unchanged `AgentRegistry`/`STAGE_TO_AGENT_ID`
path (`test_code_fix_routes_codegen_then_testing_then_deployment`).

## 5. ARCHITECTURE_REVIEW flow

```
Architecture → Codegen → Testing → Deployment → Monitoring
```

Same mechanism, `current_stage=ARCHITECTURE`. Planning is deliberately
**not** re-run — the existing `PlanArtifact` from the original workflow is
still there and is exactly what `ArchitectureAgent` consumes via the
unchanged `STAGE_INPUT_ARTIFACT_TYPE[ARCHITECTURE] = ArtifactType.PLAN`
resolution (`test_architecture_review_routes_full_chain`).

## 6. Rollback behavior — explicitly NOT auto-executed

**No automated Cloud Run rollback exists in this level, and none is
claimed.** Inspection of `DeploymentAgent`/`CloudRunDeployer`
(`app/core/cloud_run_client.py`) confirmed what Level 2.1 already
documented: rollback is structured metadata (`rollback_strategy`) only —
there is no revision-listing call, no traffic-split mutation, no rollback
execution tool anywhere in the codebase. Building one safely (a new,
narrowly-scoped, allow-listed Cloud Run tool for shifting traffic to a
prior revision, with the same service/region/project boundary discipline
`deploy_cloud_run` already has) is real, non-trivial new surface area that
the task explicitly permits deferring: *"If rollback cannot safely satisfy
these constraints: do not implement fake rollback. Escalate."*

`_authorize_remediation_strategy()` therefore unconditionally maps
`ROLLBACK → ESCALATE`, regardless of how well-formed the resolution's
`rollback_target` is (`test_rollback_always_escalates`,
`test_rollback_without_target_still_escalates_never_mutates`). This is
never silently downgraded from a claim of success — the workflow's
`metadata["remediation_strategy"]` is explicitly recorded as `"escalate"`,
never `"rollback"`, so the audit trail is honest about what actually
happened. Building real rollback execution is explicitly deferred, not
implemented here.

## 7. Testing / Deployment gates

Both gates are the **exact same code** every other workflow already goes
through — nothing new was written for remediation specifically:

- **Testing gate**: `_reconcile_stage`/`execute_next_step` route
  `WorkflowStage.CODEGEN → WorkflowStage.TESTING` unconditionally, but
  `WorkflowStage.TESTING → WorkflowStage.DEPLOYMENT` only ever happens via
  `_handle_testing_result()`'s `overall_status == "passed"` check —
  itself computed by `TestingAgent`'s evidence-first ground truth (a real
  `pytest` run, Level 1.8), never the model's claim. A remediation whose
  actual test run fails is routed by the same
  `deterministic_action()`/`handle_decision()` machinery back to
  `codegen_agent`, never forward to Deployment
  (`test_testing_failure_prevents_deployment`,
  `test_deployment_not_reached_without_test_evidence`).
- **Deployment gate**: deployment failures route through the existing
  `deployment_deterministic_action()` — unchanged, never falling back to
  the orchestration LLM (deployment failures are never ambiguous the way
  a mixed Testing failure set can be) — proven directly
  (`test_remediation_deployment_failure_routes_deterministically`).

## 8. Monitoring validation — deployment success ≠ incident resolved

When a remediation workflow's `CONTINUE` decision reaches the end of the
graph (`next_stage(DEPLOYMENT) is None`), `_execute_decision()` now checks
whether `workflow.metadata` shows this was a remediation
(`remediation_resolution_ids` present) and, if so, additionally records
`metadata["remediation_outcome"] = "deployed_pending_verification"` — a
**single conditional line added to existing code**, not a new mechanism.
No new `WorkflowStatus` value was introduced (§21 of the task explicitly
permits one if "genuinely necessary" — it wasn't: the existing `metadata`
dict already had everything needed). A normal, non-incident SDLC
completion never gets this marker
(`test_normal_sdlc_completion_has_no_remediation_outcome_marker`); a
successful remediation always does
(`test_successful_remediation_marked_deployed_pending_verification`) —
and the workflow's `status` is still `COMPLETED`, deliberately **not**
`"incident_resolved"` or any claim beyond "the deployment succeeded."
Determining whether the incident is *actually* resolved requires
Monitoring to observe real post-deployment signals and (eventually)
Detecting to re-evaluate them — explicitly out of scope here (§22 of the
task: no recursive `Detecting → Resolution → Detecting` loop is built in
this level; that pairs with the existing 3.1/3.2 agents unchanged,
whenever a future level wires the loop closed).

## 9. Workflow identity — why the ORIGINAL workflow is reopened

`CodegenAgent` and `ArchitectureAgent` both **hard-require** their
upstream artifact (`ArchitectureArtifact`/`PlanArtifact` respectively —
verified by inspection: both raise `..._ARTIFACT_MISSING` if
`AgentInput.artifact_ids` is empty), and `Artifact` storage is
workflow-scoped (`workflows/{workflow_id}/artifacts/{artifact_id}`). The
only place those artifacts already exist is the **original** workflow that
deployed the code now causing the incident. So remediation does not create
a new `WorkflowState`/`IncidentWorkflow` — it **reopens** that original
workflow: `COMPLETED → PENDING`, `current_stage` jumped to the
strategy-derived entry stage. Every existing mechanism then applies
completely unchanged, because it's the identical `WorkflowState` every
other stage transition already operates on: `_reconcile_stage` crash
recovery, retry budgets, transition policy, the Testing/Deployment
evidence gates — zero new code for any of them.

This is why `ResolutionResult.workflow_id` (a new, additive, optional
field) matters: it's set by `IncidentResolutionAgent` from its own
`AgentInput.workflow_id` — the exact same value that already made
deployment-artifact correlation work in Level 3.3
(`context.artifacts.get(agent_input.workflow_id, ...)`), now also
persisted so the orchestrator can find its way back to that workflow.
`start_remediation_from_resolution` requires it to be set; if
`IncidentResolutionAgent` was invoked without a meaningful originating
workflow context, remediation correctly cannot proceed
(`OrchestrationError`) rather than guessing.

**Not reused: the ADK `LoopAgent`.** The task asks whether the existing
`Codegen → Testing → evaluator` `LoopAgent`
(`app/orchestration/adk/loop.py`) could drive remediation's retry/recovery.
It's deliberately not used here: remediation already runs on the
step-wise `execute_next_step` path (per
`docs/architecture/orchestration.md` §19, the *recommended default*, with
`LoopAgent` documented there as a secondary, ADK-native *alternative*
mechanism to that same path). Wiring `LoopAgent` in on top would mean two
overlapping recovery mechanisms racing over the same
`Codegen ↔ Testing` transitions — exactly what the task says to avoid
("one recovery mechanism is preferable to multiple overlapping loops").
The step-wise path already provides identical
`deterministic_action`/`handle_decision`/retry-budget semantics, reused,
not duplicated.

## 10. Idempotency

`resolution_id` is the idempotency identity (§23 of the task). Every
`WorkflowState` a remediation ever touches accumulates
`metadata["remediation_resolution_ids"]` — a list. Before doing anything
else, `start_remediation_from_resolution` checks whether `resolution_id`
is already in that list; if so, it returns the current workflow state
unchanged — no re-invocation of Codegen, no second Deployment
(`test_same_resolution_submitted_twice_no_duplicate_workflow`,
`test_idempotent_call_does_not_reinvoke_codegen`).

## 11. Concurrency

The claim (recording `resolution_id` into `metadata` alongside the stage
jump) is one atomic `WorkflowRepository.update_if_version()` call — the
exact same optimistic-concurrency primitive every other workflow
transition already uses (Level 1.4). Two callers racing
`start_remediation_from_resolution()` for the same `resolution_id`: only
one's write succeeds; the loser catches `VersionConflictError`, re-reads,
and (since the winner already recorded `resolution_id`) returns the
winner's state — proven with a real `asyncio.gather()` race, both callers
agreeing on the exact same resulting stage
(`test_concurrent_remediation_starts_only_one_authoritative`). No Redis,
no new locking service.

## 12. Crash recovery

Entirely the pre-existing `_reconcile_stage()` mechanism (Level 2.0),
completely unmodified. Proven for the remediation path specifically at all
three points the task calls out: a simulated crash rolling the durable
`WorkflowState` back after Codegen (recovers forward without a duplicate
Codegen execution), after Testing (resumes without a duplicate Testing
execution), and after a completed Deployment (resuming a `COMPLETED`
remediation workflow doesn't redeploy) — see
`test_crash_recovery_after_codegen_does_not_duplicate_codegen`,
`test_crash_recovery_after_testing_resumes_correctly`,
`test_crash_recovery_after_deployment_resumes_to_completion`. As already
documented for the general case (`docs/architecture/orchestration.md`
§15), a crash strictly *mid*-agent (before its own `AgentExecution`/
`Artifact` persistence completes) has no partial evidence to reconcile
from and simply re-runs that one stage from scratch — safe, since agents'
own side effects are scoped/idempotent-by-overwrite, but not literally
exactly-once at the sub-tool-call level. Nothing new here; the same
documented boundary applies to remediation as to every other workflow.

## 13. Artifact / provenance lineage

No new `RemediationArtifact` was created — `ResolutionResult` already
represents the decision, and reopening the original workflow means every
artifact remediation produces (`CodeArtifact`, `TestArtifact`,
`DeploymentArtifact`) lives in the same `workflows/{workflow_id}/artifacts/`
subcollection as the artifacts that came before it, with the same
`parent_artifact_ids` lineage mechanism already in place. The full chain
stays inspectable purely through existing identifiers:

```
Signal.signal_id
   → DetectionResult.supporting_signal_ids / detection_id
      → ResolutionResult.detection_id / resolution_id / workflow_id
         → WorkflowState.metadata["remediation_resolution_ids" / "remediation_detection_id"]
            → Artifact.parent_artifact_ids (CodeArtifact → TestArtifact → DeploymentArtifact)
```

Verified directly: `DetectionResult` and `ResolutionResult` are
byte-for-byte unchanged after a full remediation run completes
(`test_resolution_and_detection_remain_immutable_after_remediation`) — the
recommendation and the diagnosis are permanently separate facts from what
was actually executed, exactly as §12 of the task requires.

## 14. Security

- **No direct tool execution from the orchestrator** — verified
  structurally: `deploy_cloud_run(`, `write_file(`, `run_tests(`,
  `subprocess`, and `shell=True` do not appear anywhere in
  `app/orchestration/service.py`
  (`test_orchestrator_never_calls_deployment_tool_directly`). Every
  action still goes through `AgentRegistry.get(agent_id).execute(...)`,
  same as before this level.
- **No capability bypass**: `CodegenAgent`/`TestingAgent`/`DeploymentAgent`
  are the same registered instances, with the same fixed
  `capabilities` property, invoked through the same `AgentContext`
  construction — none of them gained `RESOLVE_INCIDENT` or any
  remediation-specific capability
  (`test_no_new_capability_bypasses_agent_capabilities`).
- **`IncidentResolutionAgent` still does not hold `RESOLVE_INCIDENT`**
  (unchanged from Level 3.3 — it only ever proposes) — verified directly
  (`test_incident_resolution_agent_still_lacks_resolve_incident_capability`).
- **No new capability was introduced for the orchestrator itself** — the
  task's §26 explicitly permits one "if genuinely required"; it wasn't.
  `start_remediation_from_resolution` is trusted application code, the
  same posture `start_workflow`/`start_workflow_from_review` already had.

## 15. ADK role

Unchanged. `PlanningAgent`/`ArchitectureAgent`/`CodegenAgent`/
`TestingAgent`/`DeploymentAgent` each still own their internal ADK
`LlmAgent`; `IncidentResolutionAgent`'s own internal `LlmAgent` (Level 3.3)
is unmodified. No new Gemini call was introduced anywhere in
`OrchestrationService` for remediation routing — deriving an entry stage
from a closed `RemediationStrategy` enum via a fixed map is deterministic
Python, exactly as the task demands ("Do NOT introduce Gemini reasoning
for deterministic remediation routing").

## 16. Google services

| Service | Role in this level |
|---|---|
| Google ADK | Unchanged — existing agent adapters, `SequentialAgent`/`LoopAgent` remain available (LoopAgent deliberately not wired into remediation, §9) |
| Gemini | Unchanged — `IncidentResolutionAgent`'s existing reasoning; no new call |
| Agent Search | Unchanged — `IncidentResolutionAgent`'s existing knowledge retrieval |
| Firestore | `WorkflowRepository`/`ResolutionRepository`/`DetectionRepository`, all reused, no new collection |
| Cloud Run | `DeploymentAgent`'s existing deployment path, reused for remediation deployments; no new rollback execution (§6) |
| Cloud Monitoring / Cloud Logging | Unchanged — `MonitoringAgent`'s existing telemetry path is what will eventually supply the post-remediation health evidence a future level acts on |

No Pub/Sub, no new persistence subsystem, no new agent, no HTTP API — all
explicitly out of scope and not introduced.

## 17. Testing

`tests/test_incident_remediation.py` (34 tests) plus 2 added to
`tests/test_resolution_domain.py` for the new `workflow_id` field — 36 new
tests covering: authorization (valid `CODE_FIX`/`ARCHITECTURE_REVIEW`
execute, `ROLLBACK` always escalates, `ESCALATE`/`NO_ACTION` invoke no
agents, non-`INCIDENT` detections rejected, missing resolution rejected,
high-risk/low-confidence resolutions already-downgraded by
`IncidentResolutionAgent` are re-confirmed escalated, missing repository
configuration rejected), the full `CODE_FIX` chain with a real Testing
failure genuinely blocking Deployment (breaking the actual on-disk test
file, not just the fake model JSON — proving evidence-first holds for
remediation too), the full `ARCHITECTURE_REVIEW` chain, deterministic
deployment-failure routing, the `remediation_outcome` distinction (present
only on remediation completions, absent on normal SDLC completions),
idempotency (same resolution submitted twice, no duplicate Codegen
execution), a real `asyncio.gather()` concurrency race, crash recovery at
all three stage boundaries (Codegen/Testing/Deployment) with duplicate-
execution assertions, the full adversarial/security set (spoofed
`target_agent` ignored, rollback-without-target still escalates,
low-confidence/fabricated-evidence resolutions cannot trigger remediation,
no direct tool execution, no capability bypass, exact method signature has
no injectable routing parameter), provenance/immutability
(`DetectionResult`/`ResolutionResult` byte-identical before and after a
full remediation run), and regression checks that both the existing
manual-ticket flow and the Level 3.5 Feature → SDLC flow remain unaffected.

Full suite: **824 passed, 6 skipped** (pre-existing gated integration
tests) — no regressions.
