# End-to-End Quipu Demo Harness

## Running it

```
python -m app.demo.run --scenario feature
python -m app.demo.run --scenario incident
python -m app.demo.run --scenario both
```

No live Gemini, Jira, Cloud Monitoring, Cloud Logging, Cloud Run, or
Firestore credentials required. Exits non-zero if any scenario's
verification fails, so `python -m app.demo.run` is also a usable CI smoke
check. Prints a per-step pass/fail log followed by a JSON `DemoSummary`.

## Why this exists

Every prior level built and tested one component in isolation. This
harness proves the whole thing is one coherent platform by running the
two most important journeys — Feature Discovery → SDLC and Production
Incident → Remediation — through the **real, unmodified** domain models,
agent-runtime, and orchestration logic, with fakes only at the external
system boundary (Gemini, Jira, Cloud Monitoring/Logging/Run). It is a
demonstration and regression harness, not a new execution path: nothing
under `app/demo/` is imported by any production module.

```
FeatureReviewService  ≠  OrchestrationService  ≠  the agents
```

The harness doesn't blur this — it just calls each one, in the same order
a real deployment would, and inspects what each one actually persisted
afterward.

## Architecture

```
app/demo/
  __init__.py     - re-exports DemoHarness/DemoSummary
  fakes.py        - fixture data + fake ADK runners/Jira/Cloud Run/
                    Cloud Monitoring/Cloud Logging clients (external
                    boundary only — no orchestration logic faked)
  patching.py     - a plain context-manager equivalent of pytest's
                    monkeypatch, for substituting InMemoryRunner/JiraClient
                    at each agent module's own import site
  summary.py      - DemoSummary / StepEvidence (the machine-readable result)
  verify.py       - evidence-first verification functions — every check
                    re-reads from a repository, never trusts an
                    in-memory variable
  harness.py      - DemoHarness: wires real repositories + the real
                    OrchestrationService + the real FeatureReviewService,
                    and runs both scenarios
  run.py          - CLI entry point
```

## Scenario 1 — Feature Discovery → SDLC

```python
DemoHarness().run_feature_flow()
```

1. **Real signal normalization** — `app.signals.adapters.normalize_customer_feedback`/
   `normalize_support_feedback` (Level 3, unmodified) build three product
   `Signal`s from feedback-shaped payloads, persisted via
   `InMemorySignalRepository`.
2. **DetectingAgent** (Level 3.2, unmodified), domain `product`, run
   standalone with a faked internal `LlmAgent` response claiming
   `FEATURE_OPPORTUNITY` citing the three real signal ids — the agent's own
   evidence-first validation, retrieval bounding, and persistence are all
   real.
3. **FeatureReviewService** (Level 3.4, unmodified) — `create_review()`
   then `approve()` with `reviewer_type=HUMAN` and the real
   `REVIEW_FEATURE_OPPORTUNITY` capability check, calling a fake
   `JiraClient` (deterministic, no LLM — Feature Review was never an
   agent).
4. **`OrchestrationService.start_workflow_from_review()`** (Level 3.5,
   unmodified) creates the real `WorkflowState` at `PLANNING`.
5. **`OrchestrationService.run_to_completion()`** — the real step-wise
   path — invokes `PlanningAgent → ArchitectureAgent → CodegenAgent →
   TestingAgent → DeploymentAgent` through the real `AgentRegistry`, each
   with its own real internal ADK `LlmAgent` (faked only at the
   `InMemoryRunner` construction point). `TestingAgent` runs a genuine
   `pytest` subprocess against a real temp workspace; `DeploymentAgent`
   calls the real `deploy_cloud_run` tool with an injected fake
   `CloudRunDeployer`.
6. **`MonitoringAgent`** (Level 3.1, unmodified) observes the new
   deployment with fake `CloudMonitoringClient`/`CloudLoggingClient`
   injected via its own constructor (no module patching needed — this is
   MonitoringAgent's existing injection seam), producing real,
   production-normalized post-deployment `Signal`s — closing the loop the
   diagram below shows.
7. The real `build_happy_path_sequential_agent()` is additionally
   constructed (not re-executed) against the same registry, to visibly
   prove the ADK `SequentialAgent` mechanism works — see "ADK usage"
   below for why it isn't the primary execution path here.

```
Customer/Support Signals → DetectingAgent → FEATURE_OPPORTUNITY
    → FeatureReviewService (human approval) → Ticket
    → OrchestrationService.start_workflow_from_review()
    → Planning → Architecture → Codegen → Testing → Deployment
    → MonitoringAgent (post-deployment evidence)
```

## Scenario 2 — Production Incident → Remediation

```python
DemoHarness().run_incident_flow()
```

1. A minimal **real original SDLC run** (`start_workflow` →
   `run_to_completion`) produces an already-deployed service — the
   `WorkflowState` remediation will later reopen.
2. **`MonitoringAgent`** observes an elevated error rate and a real
   application-error log entry (fake clients, real normalization),
   correlated to the deployment via `deployment_artifact_id`.
3. **`DetectingAgent`**, domain `operational`, faked to claim `INCIDENT`.
4. **`IncidentResolutionAgent`** (Level 3.3, unmodified) diagnoses it,
   faked to propose `CODE_FIX` — and, adversarially, to also claim
   `target_agent="deployment_agent"`. `IncidentResolutionAgent`'s own
   `_apply_safety_policy` already derives and persists the *correct*
   `target_agent` (`codegen_agent`) regardless of what the fake claimed —
   the model's raw claim never survives into the persisted
   `ResolutionResult` at all.
5. **`OrchestrationService.start_remediation_from_resolution()`** (Level
   3.6, unmodified) reopens the *same* original `WorkflowState` at
   `CODEGEN` — never reading `target_agent`, deriving the stage purely
   from the validated `remediation_strategy`.
6. **A real testing failure**: the on-disk test is deliberately broken;
   the remediation's own `Codegen → Testing` attempt genuinely fails, and
   the existing deterministic `code_defect` routing sends it back to
   `codegen_agent` — Deployment is never reached. The test is then fixed
   and the retry succeeds through to `Deployment`.
7. **`MonitoringAgent`** observes the remediated service; the workflow's
   `metadata["remediation_outcome"]` is `"deployed_pending_verification"`
   — never `"incident_resolved"` (see Level 3.6's own documented
   distinction, reused unchanged).
8. **An unsafe/high-risk case**: a second `INCIDENT` detection and a
   `CODE_FIX` proposal with `risk="high"` is run through
   `IncidentResolutionAgent` — its own safety policy downgrades the
   persisted `ResolutionResult` to `ESCALATE` before it's ever saved.
   `start_remediation_from_resolution()` on that resolution moves the
   workflow to `WorkflowStatus.ESCALATED` — zero new `AgentExecution`
   records, verified directly by counting them before/after.
9. **Idempotent re-submission** of the original successful
   `resolution_id` returns the same, already-`COMPLETED` workflow — no
   second Codegen/Testing/Deployment run.
10. `DetectionResult`/`ResolutionResult` are re-read and confirmed
    byte-identical to what was persisted before any remediation executed.
11. The real `build_recovery_loop_agent()` is additionally constructed
    (not used as the primary retry mechanism — see "ADK usage" below) to
    prove the ADK `LoopAgent` mechanism works.

```
Monitoring/operational Signals → DetectingAgent → INCIDENT
    → IncidentResolutionAgent → ResolutionResult
    → OrchestrationService.start_remediation_from_resolution()
    → Codegen → Testing (real failure → retry → real pass) → Deployment
    → MonitoringAgent (post-remediation evidence, never auto-"resolved")
```

## Evidence-first requirement

`DemoSummary.record(name, passed, detail)` is called at every important
transition in both scenarios, and every `detail` string is built from a
**fresh re-read** of the relevant repository (`app/demo/verify.py`) — never
from a Python variable the harness happens to still be holding from three
steps earlier. `DemoSummary.finalize()` sets `verification_status="passed"`
only if every recorded step passed; the CLI (`app/demo/run.py`) exits
non-zero otherwise. There is no code path that prints "success" without
having checked persisted state first.

## Failure paths exercised

| # | Path | Where |
|---|---|---|
| 1 | Successful feature flow | Scenario 1, full run |
| 2 | Successful `CODE_FIX` remediation | Scenario 2, steps 4–7 |
| 3 | Testing failure blocks Deployment | Scenario 2, step 6 (first attempt) |
| 4 | Unsafe/high-risk remediation escalated | Scenario 2, step 8 |
| 5 | Idempotent rerun (review, workflow, remediation) | Scenario 1 steps 3–4; Scenario 2 step 9 |
| 6 | Orchestration retry/recovery | Scenario 2, step 6 (real `code_defect` → retry → real pass) |

None of the existing safety policies were weakened to make any of these
pass — every failure path is a real code path already proven in
`tests/test_incident_remediation.py`/`tests/test_feature_to_sdlc.py`,
exercised again here end-to-end.

## ADK usage — what's exercised and why

| Mechanism | Used as | Why |
|---|---|---|
| Agent-internal `LlmAgent` (Planning/Architecture/Codegen/Testing/Deployment/Detecting/IncidentResolution) | **Primary** — every agent invocation in both scenarios goes through its own real internal ADK `LlmAgent`, only the `InMemoryRunner` construction is substituted | This is the actual reasoning boundary in production; faking it further would mean not testing the agents at all |
| Step-wise `OrchestrationService.execute_next_step`/`run_to_completion` | **Primary execution path** for both scenarios | The production-recommended default (`docs/architecture/orchestration.md` §19) — the only path that produces durable, per-stage `WorkflowState` evidence, which the evidence-first requirement above depends on |
| `SequentialAgent` (`build_happy_path_sequential_agent`) | **Constructed and verified**, not used to drive the main scenario | Running the whole SDLC through it loses per-stage `WorkflowState` persistence (its own module docstring says so) — using it as primary here would contradict the evidence-first requirement. Still genuinely built against the real registry, and its `sub_agents` order is asserted in `tests/test_demo_feature_flow.py` |
| `LoopAgent` (`build_recovery_loop_agent`) | **Constructed and verified**, not used to drive remediation's retry | Level 3.6 already documented (`docs/architecture/incident_remediation.md` §9) why remediation intentionally uses the step-wise retry path instead — wiring `LoopAgent` in as well would mean two overlapping recovery mechanisms. Still genuinely built and asserted (`tests/test_demo_incident_flow.py`) |
| Orchestration decision `LlmAgent` (`propose_decision`) | **Not triggered** | Only reached for genuinely ambiguous, mixed-classification Testing failures (`app.orchestration.decisions.deterministic_action` returning `None`); both demo scenarios use single, deterministic failure classifications on purpose — forcing a mixed case just to trigger this LLM call would be exactly the "LLM theater" the task explicitly warns against. It's exercised elsewhere (`tests/test_orchestration.py`) |

## Google services

| Service | Real or faked in the demo? |
|---|---|
| Google ADK | Real — every agent-internal `LlmAgent`, `SequentialAgent`, `LoopAgent` construction is real ADK code |
| Gemini | Faked — `InMemoryRunner` substitution (the model's response is deterministic fixture text, not a live API call) |
| Agent Search / Discovery Engine | Not exercised — the demo's fake `KnowledgeGateway` returns an empty result; no agent's reasoning depends on knowledge for these deterministic fixtures |
| Firestore | Faked — `InMemory*Repository` implementations of the exact same repository Protocols `FirestoreXRepository` satisfies; swapping to real Firestore is a constructor-argument change, not a rewrite |
| Cloud Run | Faked — `FakeCloudRunDeployer` injected at the same seam `deploy_cloud_run` already exposes for tests |
| Cloud Monitoring / Cloud Logging | Faked — `FakeCloudMonitoringClient`/`FakeCloudLoggingClient` injected via `MonitoringAgent`'s own constructor |
| Jira | Faked — `FakeJiraClient`, same `create_story(summary, description)` shape as the real `JiraClient` |

No Pub/Sub, no HTTP API, no UI, no new agent — none introduced, per
explicit scope.

## Security properties demonstrated

- **No unsafe operation bypasses an authorization gate**: the high-risk
  remediation case never reaches `AgentExecution` — verified by counting
  executions before/after.
- **Testing failure cannot result in Deployment**: proven with a real,
  broken on-disk test file, not a faked verdict.
- **Incident remediation never trusts a model-supplied target agent**:
  the fake proposal claims `target_agent="deployment_agent"`; the
  persisted `ResolutionResult` and the resulting workflow stage both show
  `codegen_agent`.
- **Feature review cannot be self-approved by an agent**: `FeatureReviewService.approve()`
  requires `reviewer_type=DecisionSource.HUMAN` — nothing in the demo (or
  in production) can satisfy that from agent code.
- **Demo output is derived from persisted/domain evidence**: every
  `DemoSummary` field and every `StepEvidence.detail` comes from
  `app/demo/verify.py` re-reading a repository, never from an assumed
  in-memory state.

## Tests

`tests/test_demo_feature_flow.py` (11) and `tests/test_demo_incident_flow.py`
(12) — 23 new tests running the real `DemoHarness` and asserting on both
the returned `DemoSummary` and independently re-fetched repository state
(provenance chain integrity, idempotency, capability boundaries, ADK
construction). No live credentials required.

Full suite: **849 passed, 6 skipped** (pre-existing gated integration
tests) — no regressions.

## Known limitation

`OrchestrationService.start_workflow_from_review`/
`start_remediation_from_resolution` don't accept a `workspace_path` (a
pre-existing gap noted in Level 3.5/3.6's own test suites). The harness
sets it directly on the resulting `WorkflowState.metadata` via the same
`update_if_version()` every production caller already uses, immediately
after workflow creation and before any code-touching stage runs — not a
new mechanism, and not something that changes *whether* the workflow is
authorized to proceed, only *where* Codegen/Testing find a real
filesystem to work in. A real deployment needs to supply this some other
way (e.g. a CI job checking out the repo first); wiring that cleanly into
the production entry points themselves is a reasonable follow-up, not
addressed in this task.
