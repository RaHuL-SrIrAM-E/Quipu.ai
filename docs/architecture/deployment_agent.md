# Deployment Agent (Level 2.1)

## Diagram

```
CodeArtifact (validated by Testing, not re-tested here)
      |
 DeploymentAgent
   +-------+--------+
   |                |
Knowledge      deploy_cloud_run
   |                |
Agent Search    Cloud Run Admin API (google.cloud.run_v2)
   |                |
   +-------+--------+
           |
   ADK LlmAgent / Gemini      (proposes config — service/region/tag/resources)
           |
   Controlled Deploy Tool      (deploy_cloud_run: capability-gated, typed args, no shell)
           |
   Actual Cloud Run Result     (terminal_condition, revision, uri — facts)
           |
     Gemini's DeploymentOutput (summary, strategy, risks, rollback — interpretation)
           |
     status/revision/uri OVERWRITTEN with ground truth
           |
     DeploymentArtifact
```

Same core distinction as Testing (`docs/architecture/testing_agent.md` §11):
everything below "Controlled Deploy Tool" is an unforgeable fact from the
real Cloud Run Admin API. Everything above it is Gemini's opinion — useful
for narrative and risk framing, never authoritative for success/failure.

No legacy predecessor exists for Deployment — same situation as Codegen and
Testing. Follows the same `QuipuAgent` + internal-ADK-adapter shape; read
`docs/architecture/testing_agent.md` and `docs/architecture/codegen_agent.md`
first for shared mechanics (artifact resolution, capability gating,
`_track_usage_metrics`, evidence-first pattern) not repeated here.

## 1. Responsibility

Take an approved, tested `CodeArtifact` and deploy it to Cloud Run through a
controlled tool, then report the actual result. Deployment does not write
code, does not run tests, does not redesign architecture, and does not
monitor/detect/resolve incidents after deployment (explicitly out of scope
for this level — see §12). It never calls another agent; routing a
deployment failure back to Codegen or escalating it is the orchestrator's
job (`app/orchestration/decisions.py::deployment_deterministic_action`).

## 2. CodeArtifact input

Same pattern as Testing consuming Codegen's output:
`AgentInput.artifact_ids[0]` → `ArtifactGateway.get` → type check
(`ArtifactType.CODE_CHANGE`) → `CodegenOutput.model_validate(payload)`.
Explicit failure codes, all raised before Gemini is ever invoked:
`CODE_ARTIFACT_MISSING`, `CODE_ARTIFACT_WRONG_TYPE`,
`CODEGEN_OUTPUT_INVALID`, `DEPLOYMENT_CONFIGURATION_MISSING` (no
`CLOUD_RUN_IMAGE_REGISTRY` configured — checked before any LLM call so a
misconfigured deployment never even reaches Gemini).

Note this is *not* the artifact Testing just produced — Deployment consumes
the same `CodeArtifact` Testing validated, not the `TestArtifact`. See
`app/orchestration/transitions.py::STAGE_INPUT_ARTIFACT_TYPE` and its
comment for why "most recent artifact" was the wrong rule once Deployment
stopped being adjacent-only.

## 3. Enterprise knowledge

Reuses `query_enterprise_knowledge` and the existing `deployment_agent`
retrieval profile (`DEPLOYMENT_STANDARD`, `SECURITY_POLICY`, `COMPLIANCE`,
`OPERATIONS` — already defined in `app/knowledge/policies.py`, not
duplicated here). Consultation is on-demand: the model decides when to call
it, gated the same way as every other tool (§6). The instruction tells the
model to consult it for approved deployment patterns, environment rules,
resource limits, and compliance requirements, and never to invent an
enterprise standard it didn't find.

## 4. DeploymentOutput

```python
class DeploymentOutput(BaseModel):
    deployment_summary: str
    target_platform: DeploymentTarget        # cloud_run (only value today)
    environment: str
    service_name: str
    region: str
    strategy: DeploymentStrategy             # revision (see §5)
    configuration: CloudRunConfiguration     # image_tag, cpu, memory, min/max instances
    pre_deployment_checks: list[str]
    rollback_strategy: str
    risks: list[Risk]                        # reused from planning.py — not redefined

    # Ground truth — always overwritten post-hoc, see §9
    status: DeploymentStatus
    revision: str | None
    service_uri: str | None
    failure_classification: DeploymentFailureClassification | None
    failure_details: str
```

All finite-value fields are closed `StrEnum`s (`DeploymentTarget`,
`DeploymentStrategy`, `DeploymentStatus`, `DeploymentFailureClassification`)
— an unrecognized value is a pydantic `ValidationError`, not silently
accepted text.

## 5. Deployment strategy — deliberately narrow

`DeploymentStrategy` declares `revision`, `rolling`, `blue_green`, `canary`
for future extensibility, but **only `revision` is implemented**. Cloud
Run's native update model is "create a new revision, shift 100% traffic to
it" — that's what `CloudRunDeployer.deploy()` actually does. `blue_green`/
`canary` would require explicit traffic-split configuration this level does
not build. The instruction tells the model not to request them; nothing in
`deploy_cloud_run` accepts a `strategy` argument at all, so there is no code
path that could act on a different strategy even if the model asked for one
— it can only ever be metadata in `DeploymentOutput.strategy`.

## 6. Controlled deployment — no shell, no `gcloud`, no arbitrary image

`app/tools/deployment_tools.py::deploy_cloud_run` is the only way any Quipu
agent deploys anything. Signature: `deploy_cloud_run(service_name, region,
environment, image_tag, cpu, memory, min_instances, max_instances,
tool_context)`. **There is no `command`, `shell`, or `image` (full URI)
parameter — structurally, not just by convention**
(`test_no_shell_command_surface_exists` asserts the exact parameter set).

Validated in order before any API call:
1. `AgentCapability.DEPLOY` granted (§7)
2. `service_name` matches Cloud Run's naming rule (`^[a-z]([a-z0-9-]{0,61}[a-z0-9])?$`)
3. `region` is in `settings.cloud_run_allowed_regions`
4. `environment` is in `settings.cloud_run_allowed_environments`
5. `image_tag` matches a safe tag charset (`^[a-zA-Z0-9_][a-zA-Z0-9_.-]{0,127}$`)
6. `cpu` is one of `1/2/4/8`; `memory` matches `512Mi/1Gi/2Gi/4Gi/8Gi`
7. `0 <= min_instances <= max_instances <= cloud_run_max_instances_ceiling`
8. `CLOUD_RUN_IMAGE_REGISTRY` is configured

Only after every check passes does the tool build
`image = f"{registry}/{service_name}:{image_tag}"` and call
`CloudRunDeployer.deploy()` — the one function in the repository allowed to
touch `google.cloud.run_v2`. **The model can never supply a full image URI
at all** — the tool has no such parameter — which closes the
supply-chain-style risk of a deployment being pointed at an arbitrary or
untrusted image, not just the "no shell" risk. A rejected request returns a
structured `{"success": False, "error": "..."}` dict rather than raising,
so the model sees why and can react — a rejected request never reaches the
Cloud Run API at all.

## 7. DEPLOY capability

`AgentCapability.DEPLOY` already existed in the enum — reused, not
duplicated. Enforced in three places, same belt-and-suspenders pattern as
`WRITE_CODE`/`RUN_TESTS` in prior agents:
- `DeploymentAgent._perform()` calls `self.require_capability(DEPLOY)` before
  starting the ADK run.
- The shared ADK `before_tool_callback` (`_tool_capability_gate`, imported
  from `app/agents/planning.py`, extended with
  `"deploy_cloud_run": AgentCapability.DEPLOY`) rejects the tool call at the
  ADK boundary if the capability isn't in session state.
- `deploy_cloud_run` itself checks `AgentCapability.DEPLOY in
  tool_context.state.get("_capabilities", set())` and rejects before any
  validation or API call — the tool remains safe even if invoked outside the
  normal LLM path (`test_capability_denial_rejects_without_calling_cloud_run`).

`DeploymentAgent.capabilities` is deliberately narrow: `READ_ARTIFACT`,
`QUERY_KNOWLEDGE`, `WRITE_ARTIFACT`, `DEPLOY`. It does **not** hold
`WRITE_CODE`, `WRITE_JIRA`, or `RESOLVE_INCIDENT` — no code path in this
agent needs them (`test_deployment_agent_expected_capabilities` asserts the
exact set and its disjointness from those three).

## 8. Google Cloud integration — Cloud Run Admin API, ADC only

`app/core/cloud_run_client.py::CloudRunDeployer` wraps
`google.cloud.run_v2.ServicesAsyncClient` directly — no `gcloud` subprocess
anywhere in the repository. Authentication is Application Default
Credentials only (`gcloud auth application-default login` locally, Workload
Identity/attached service account in deployment) — no service-account JSON
key file, matching the existing pattern for Agent Search
(`app/knowledge/backends/google_search.py`) and Firestore
(`app/persistence/firestore/client.py`).

`deploy()` builds a `Container` (image, `ResourceRequirements`, env vars), a
`RevisionTemplate` (container + `RevisionScaling`), and a `Service`, then
calls `client.update_service(...)` and awaits
`operation.result(timeout=...)` — the real long-running-operation pattern
the Cloud Run Admin API uses. All tunables (`cloud_run_image_registry`,
`cloud_run_allowed_regions`, `cloud_run_allowed_environments`,
`cloud_run_max_instances_ceiling`, `cloud_run_deploy_timeout_seconds`) live
in `app.config.Settings`, following the existing convention — nothing is
hardcoded.

## 9. Evidence-first architecture — the critical property

`deploy_cloud_run` appends every attempt (success, failure, and
tool/config-level error) to `tool_context.state["_deployment_results"]` — a
list, mutated in place, visible to `DeploymentAgent._perform()` after the
ADK run completes (same in-place-session-state-mutation mechanism as
Testing's `_test_executions`).

After validating the model's `DeploymentOutput`, `_perform()` requires at
least one recorded attempt (`NO_DEPLOYMENT_ATTEMPTED` if the model produced
a verdict without ever calling the tool), then computes ground truth from
the **last** recorded attempt via `_ground_truth_status()`:
- `success: False` (rejected by validation, or a `CloudRunDeploymentError`)
  → `DeploymentStatus.ERROR`
- `success: True` and `status == "succeeded"` (Cloud Run's own
  `terminal_condition.state == CONDITION_SUCCEEDED`) → `SUCCEEDED`
- `success: True` and `status == "failed"` → `FAILED`

This status, plus `revision` and `service_uri`, **overwrites** whatever the
model's `DeploymentOutput` claimed via `model_copy(update=...)` — the model
is instructed not to fill those fields in at all, but even if it did, the
override is unconditional. Proven directly:
`test_model_claiming_success_cannot_override_actual_failure` (Cloud Run
reports failure; the model's own JSON, which never controls `status` in the
first place, cannot change the persisted verdict) and the paired
`test_fake_cloud_run_success_produces_succeeded` /
`test_fake_cloud_run_failure_produces_failed` cases.

## 10. Failure classification

`DeploymentFailureClassification`: `CONFIGURATION_FAILURE`,
`PERMISSION_FAILURE`, `BUILD_FAILURE`, `PLATFORM_FAILURE`,
`HEALTH_CHECK_FAILURE`, `NETWORK_FAILURE`, `UNKNOWN` — a new enum, added
because no existing failure taxonomy in the repository fit deployment
failures (Testing's `FailureClassification` is about code/test defects, not
platform-level outcomes). The model may supply a classification in
`DeploymentOutput.failure_classification`; if a deployment fails and the
model didn't supply one, `_perform()` defaults it to `UNKNOWN` rather than
leaving it null, so routing (`deployment_deterministic_action`, see §11) is
always well-defined.

`failure_details` is populated from the actual tool/API error/message on
failure, never from the model's narrative alone.

## 11. Orchestration integration

`app/orchestration/transitions.py`: `WorkflowStage.DEPLOYMENT` added to
`STAGE_ORDER` (new last stage), `STAGE_TO_AGENT_ID`,
`STAGE_TO_ARTIFACT_TYPE`. `next_stage(TESTING) == DEPLOYMENT`;
`next_stage(DEPLOYMENT) is None` (currently the last implemented stage —
`CONTINUE` from here means "workflow done", not "invalid", per the
Level 2.0 `can_transition` fix). `_ALLOWED_RETRY_TARGETS[DEPLOYMENT] =
{"deployment_agent", "codegen_agent"}` — Deployment can only retry itself
or route back to Codegen (a build/health-check failure suggests the code
itself is broken), never back to Architecture or Planning.

Deployment failure routing is fully deterministic
(`app/orchestration/decisions.py::deployment_deterministic_action`), unlike
Testing's mixed-classification case which sometimes needs the orchestration
LlmAgent — Deployment produces exactly one classification per attempt, so
the lookup is always unambiguous:

| Classification | Action |
|---|---|
| `CONFIGURATION_FAILURE` | RETRY → deployment_agent |
| `PERMISSION_FAILURE` | ESCALATE |
| `BUILD_FAILURE` | RETRY → codegen_agent |
| `PLATFORM_FAILURE` | RETRY → deployment_agent |
| `HEALTH_CHECK_FAILURE` | RETRY → codegen_agent |
| `NETWORK_FAILURE` | RETRY → deployment_agent |
| `UNKNOWN` / missing | ESCALATE |

`OrchestrationService._handle_deployment_result()` applies this after every
Deployment stage execution; `_max_retries_for()` gained
`DEPLOYMENT: settings.max_deployment_retries` (default `2`).

**The existing Codegen↔Testing `LoopAgent` recovery mechanism was NOT
extended to include Deployment.** It remains scoped to Codegen/Testing
recovery only; Deployment retries/escalations are handled entirely through
`OrchestrationService`'s deterministic routing above, not through the
ADK `LoopAgent` construct. The `SequentialAgent` pipeline
(`app/orchestrator/pipeline.py`) was extended to include Deployment as a
fifth sub-agent.

**Input-artifact resolution bugfix**: `execute_next_step()` previously
resolved a stage's input as `workflow.artifact_ids[-1]` (the most recent
artifact) — correct for Planning→Architecture→Codegen→Testing, where each
stage's input is literally the immediately preceding stage's output, but
wrong for Deployment, which needs the `CodeArtifact` (Testing's *input*, not
its output). Fixed with an explicit
`STAGE_INPUT_ARTIFACT_TYPE` map (`transitions.py`) and
`OrchestrationService._resolve_input_artifact_id()`, which scans
`workflow.artifact_ids` newest-first and returns the first artifact matching
the stage's declared input type, rather than assuming adjacency. This
generalizes correctly to any future non-adjacent stage dependency.

New happy path: **Planning → Architecture → Codegen → Testing →
Deployment**, verified end-to-end through the real `OrchestrationService`
in `tests/test_orchestration.py` (`test_happy_path_reaches_completed`,
`test_each_stage_produces_its_artifact`, `test_artifact_lineage_preserved`).

## 12. Rollback — structured metadata only, not a workflow

Per explicit scope: this level does **not** implement rollback execution.
`DeploymentOutput.rollback_strategy` is a free-text field the model fills in
(e.g. "revert to previous revision via Cloud Run traffic split") —
descriptive metadata attached to the artifact, not an action `_perform()`
or any tool can take. There is no `rollback_cloud_run` tool, no traffic-split
management, no revision-history inspection. If a deployment fails, the
orchestrator's response is RETRY or ESCALATE (§11) — never an automatic
rollback. Building real rollback (inspecting revision history, shifting
traffic back, capturing rollback evidence) is explicitly deferred to a
future level.

## 13. DeploymentArtifact / lineage

`ArtifactType.DEPLOYMENT` already existed — no new artifact type added.
`parent_artifact_ids=[code_artifact_id]`, completing `PlanArtifact →
ArchitectureArtifact → CodeArtifact → TestArtifact` with a fifth link:
`CodeArtifact → DeploymentArtifact` (both `TestArtifact` and
`DeploymentArtifact` share the same `CodeArtifact` parent — Deployment
doesn't chain off Testing's artifact, see §2). Persisted via the existing
`ArtifactGateway`/`ArtifactRepository` — no new persistence mechanism.

The payload embeds the full `DeploymentOutput` (ground-truth-overwritten)
plus `raw_deployment_results` — every attempt (success, rejection, or API
error) recorded by `deploy_cloud_run`, same "raw evidence attached
regardless of interpretation" pattern as Testing's `raw_test_executions`.

## 14. Execution/audit

Same `AgentExecution`/`AgentMetrics` pattern as every other agent: created
`RUNNING`, updated to `COMPLETED`/`FAILED` with `output_artifact_ids`/
`error`. `_track_usage_metrics` (imported from `app.agents.planning`, now
reused a fifth time) captures Gemini token/cost metrics the same way for
every agent.

## 15. Failure behavior

| Failure | Error code |
|---|---|
| No artifact_ids / artifact not found | `CODE_ARTIFACT_MISSING` |
| Wrong artifact type | `CODE_ARTIFACT_WRONG_TYPE` |
| CodegenOutput payload invalid | `CODEGEN_OUTPUT_INVALID` |
| `CLOUD_RUN_IMAGE_REGISTRY` not configured | `DEPLOYMENT_CONFIGURATION_MISSING` |
| Gemini/ADK/tool call fails | `DEPLOYMENT_LLM_FAILURE` |
| Empty model response | `DEPLOYMENT_EMPTY_RESPONSE` |
| DeploymentOutput doesn't validate | `DEPLOYMENT_VALIDATION_FAILED` |
| Verdict produced without ever calling deploy_cloud_run | `NO_DEPLOYMENT_ATTEMPTED` |
| Artifact save fails | `ARTIFACT_PERSISTENCE_FAILED` |

A rejected/failed *deployment attempt itself* is not one of these codes —
it's a successful agent execution (`WorkflowStatus.COMPLETED`) producing a
`DeploymentArtifact` whose `status` is `FAILED`/`ERROR`; the orchestrator
routes from there (§11). These error codes are for cases where Deployment
could not even produce a verdict.

## 16. Explicitly out of scope for this level

Monitoring, detecting, incident resolution, and any HTTP API surface for
triggering/inspecting deployments are not implemented here — same
boundary discipline as every prior level. Real rollback execution (§12) is
also deferred.

## 17. Google services actually integrated (hackathon record)

Only services the code actually calls — nothing claimed speculatively:

- **Google ADK** — `LlmAgent`, `InMemoryRunner`, `before_tool_callback`,
  `output_schema` structured output (`_deployment_llm_agent`).
- **Gemini** — `settings.gemini_model`, same configuration as every other
  agent, via ADK.
- **Agent Search / Discovery Engine** — via the existing, reused
  `query_enterprise_knowledge` tool and `deployment_agent` retrieval
  profile; DeploymentAgent does not talk to Discovery Engine directly, it
  goes through the existing `KnowledgeGateway`/`KnowledgeService` layer,
  same as every other agent.
- **Firestore** — via the existing, reused `ArtifactGateway`/
  `AgentExecutionRepository` persistence layer (Firestore-backed
  implementation, already built in a prior level); DeploymentAgent does not
  talk to Firestore directly.
- **Cloud Run** — via `google.cloud.run_v2.ServicesAsyncClient`
  (`app/core/cloud_run_client.py`), the new integration this level adds.
  ADC-only authentication; `update_service` + `operation.result()`.

Not integrated by this level, and not claimed: Cloud Build, Artifact
Registry (the image is assumed already pushed to
`CLOUD_RUN_IMAGE_REGISTRY` — building/pushing it is out of scope), Cloud
Monitoring/Logging beyond what Cloud Run's API response itself returns.

## 18. Testing

`tests/test_deployment_agent.py` (40 tests) covers: identity/capabilities,
lifecycle (success and LLM-failure paths), all four input-validation
failure codes, missing-configuration rejection, the no-shell-surface proof
(`test_no_shell_command_surface_exists`, exact parameter-set assertion),
capability denial at the tool boundary and at the ADK
`before_tool_callback` boundary, every `deploy_cloud_run` validation
rejection (service name, region, environment, image tag, cpu, memory,
instance bounds) via real calls into the real tool function (not mocked
validation logic), proof the model can never supply a full image URI,
fake-success/fake-failure Cloud Run client behavior, evidence-first
overrides, `NO_DEPLOYMENT_ATTEMPTED`, failure-classification defaulting,
`DeploymentOutput` schema validation, artifact creation/lineage/persistence,
knowledge-tool wiring and retrieval-profile usage, ADK agent configuration
(model, output_schema, exact tool set), execution/metrics capture, and a
full-app/registry import-and-registration regression check.

`tests/test_orchestration.py` was extended to cover the 5-stage happy path
end-to-end (Planning→Architecture→Codegen→Testing→Deployment) through the
real `OrchestrationService`, using a `FakeCloudRunDeployer` injected via
`tool_context.state["_cloud_run_deployer"]` — no real Google credentials or
network access required for the normal suite. No dedicated gated Cloud Run
integration test was added in this level (unlike Firestore/Agent
Search/Orchestration, which have real-credential-gated tests) — deferred,
since exercising a real Cloud Run deployment end-to-end needs a live GCP
project and image already pushed to a registry, a heavier fixture
requirement than the existing gated tests; the `CloudRunDeployer` class is
however already structured (`client: run_v2.ServicesAsyncClient | None`
constructor injection) to support one being added later without changes to
`deployment_tools.py` or `deployment.py`.

Full suite: **373 passed, 3 skipped** (the pre-existing gated integration
tests: Google Search, Firestore, Orchestration).
