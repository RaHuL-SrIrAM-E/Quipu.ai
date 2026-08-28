# Architecture Agent (Level 1.6 migration)

## Diagram

```
Planning Agent
      |
  PlanArtifact
      |
  ArtifactGateway
      |
Architecture Agent
   +--------+--------+
   |                 |
Knowledge         Repository
   |                 |
Agent Search        Tools
   |                 |
   +--------+--------+
            |
          Gemini
            |
            v
    ArchitectureOutput
            |
            v
   ArchitectureArtifact
            |
            v
        Firestore
```

Same shape as `docs/architecture/planning_agent.md` — read that first for
the shared reasoning (`QuipuAgent`/ADK-adapter split, capability enforcement
mechanics, the legacy-coexistence pattern). This document covers what's
specific to Architecture: consuming a *persisted* upstream artifact instead
of a raw ticket, and application-authoritative task-coverage validation.

## 1. Existing (pre-1.6) Architecture Agent

`app/agents/architecture.py` exported one thing: `architecture_agent`, a
native ADK `LlmAgent` reading `context.state["plan"]` (raw dict, no
validation that it's actually a well-formed plan), producing
`ArchitectureOutput`, with `validate_task_coverage()` available but never
called from within the agent itself (it was a standalone function for
external callers to invoke). RBAC via `STAGE_ROLES["architecture"]`,
lifecycle via `stage_started`/`stage_completed` into the legacy `StageRun`
table, cost tracking into `RunMetrics`. Invoked only via
`app/orchestrator/pipeline.py`'s `SequentialAgent`, immediately after
`planning_agent` in the same ADK session (hence it could read `state["plan"]`
at all — it relied on `planning_agent`'s `output_key="plan"` having written
into the *same* session).

## 2. New Quipu Architecture Agent

`ArchitectureAgent(QuipuAgent)`, same shape as `PlanningAgent` (Level 1.5):
typed `AgentInput`/`AgentContext` in, `AgentOutput` out, its own internal
ADK `LlmAgent` (`_architecture_llm_agent`) doing the actual reasoning. The
one structurally new thing this level required: Architecture doesn't get
its input as a ticket — it gets it as *another agent's persisted output*.
That's the "PlanArtifact handoff" (§3).

## 3. PlanArtifact handoff — the critical change

**Old**: `architecture_agent` and `planning_agent` had to run in the same
ADK session so `state["plan"]` (written by Planning's `output_key`) was
visible to Architecture. This only works because `SequentialAgent` shares
one session — it would not work across a real process boundary, a retry, or
a future orchestrator dispatching agents independently.

**New**: `AgentInput.artifact_ids` (an existing Level 1.1 field — no new
field needed, matching "do not create a second artifact transport
abstraction") carries the PlanArtifact's id. By convention,
`artifact_ids[0]` is the plan Architecture should design against — the only
input artifact this agent expects. `ArchitectureAgent._perform()`:

```python
plan_artifact = await context.artifacts.get(agent_input.workflow_id, plan_artifact_id)
if plan_artifact.artifact_type != ArtifactType.PLAN: fail(...)
plan = PlanOutput.model_validate(plan_artifact.payload)  # full re-validation
```

This is a real fetch through `ArtifactGateway` (Level 1.2's Protocol,
evolved in 1.5 to take `workflow_id`) — not a shared in-process session,
not a shortcut. Architecture and Planning could now run as genuinely
separate processes/invocations, which is the whole point: this is what
makes a future `SequentialAgent`-of-`QuipuAgent`s (or any other
orchestrator) actually work across agent boundaries instead of only within
one ADK session.

**Session state still exists internally** — once the plan is fetched and
validated, `_perform()` puts `plan.model_dump(mode="json")` into
`session_state["plan"]` so `_build_instruction()` can render it into the
prompt exactly as before. That's an implementation detail of the ADK
adapter, not the inter-agent contract; the *authoritative* source is the
artifact fetch that happened first, with its own validation and failure
modes (§6).

## 4. PlanOutput reconstruction & failure modes

Four distinct, explicitly coded failure paths before any Gemini call
happens, each with its own error code:

- `agent_input.artifact_ids` empty → `PLAN_ARTIFACT_MISSING`
- `ArtifactGateway.get()` returns `None` → `PLAN_ARTIFACT_MISSING`
- `artifact.artifact_type != ArtifactType.PLAN` → `PLAN_ARTIFACT_WRONG_TYPE`
- `PlanOutput.model_validate(artifact.payload)` raises → `PLAN_OUTPUT_INVALID`

No silent continuation with a malformed/wrong-type/missing plan in any of
these cases — each returns `AgentOutput(status=FAILED, errors=[...])`
immediately.

## 5. Knowledge integration

Identical mechanism to Planning (`query_enterprise_knowledge`,
`KnowledgeServiceGateway`), generalized this level so it isn't
Planning-specific: the tool now reads `state["_agent_name"]` (set to
`"architecture_agent"` here, `"planning_agent"` there) to resolve the
correct profile via `get_retrieval_policy(agent_name)` — the existing
Architecture profile (`ARCHITECTURE_PATTERN`, `SECURITY_POLICY`,
`COMPLIANCE`, `TECHNOLOGY_STANDARD`, `DEPLOYMENT_STANDARD`, Level 1.3A,
unchanged) is reused, not duplicated. Out-of-profile `knowledge_type`
values are rejected before the gateway is even called
(`test_out_of_profile_knowledge_type_rejected_for_architecture`).
Provenance (`document_id`/`source`/`relevance_score`) survives the round
trip, same as Planning.

## 6. Repository tools

Unchanged: `search_files`, `read_file`, `search_code`,
`get_project_structure`, `get_dependencies` — the exact same
`REPO_TOOLS` list from `app/tools/repo_tools.py`, imported directly, not
rebuilt. Verified functionally against a real temp directory
(`test_real_repository_references_via_fake_tools`).

## 7. ADK LlmAgent / Gemini

`_architecture_llm_agent` keeps `output_schema=ArchitectureOutput` —
structured generation preserved, no free-form-text-plus-parsing fallback.
Gemini stays the underlying model (`settings.gemini_model`, same as
Planning and the legacy agent). The `before_tool_callback`
(`_tool_capability_gate`, imported from `app.agents.planning` — not
duplicated) and `after_model_callback` (`_track_usage_metrics`, also
imported/reused) are the exact same functions Planning uses; they're
generic over the tool-name→capability map and the `AgentMetrics` accumulator
respectively, so sharing them was the natural choice over writing
Architecture-specific copies.

## 8. ArchitectureOutput

Unchanged: `Component`, `TaskDesign`, `ArchitectureOutput`, and every
validator (non-empty design summary, at least one component, at least one
task design, unique `task_design` ids) — imported, not redefined.

## 9. ArchitectureArtifact

`ArchitectureOutput` → `Artifact(artifact_type=ArtifactType.ARCHITECTURE,
created_by="architecture_agent", parent_artifact_ids=[plan_artifact_id],
payload=architecture.model_dump(mode="json"))` → `context.artifacts.save(
workflow_id, artifact)`. Same `Artifact` domain model Planning uses — no
second artifact type introduced. `parent_artifact_ids` links back to the
PlanArtifact, giving a real (if shallow) lineage chain:
`ArchitectureArtifact -> PlanArtifact`. `WorkflowState` still only
references by id (`test_workflow_state_references_architecture_artifact_not_embeds`).

## 10. Firestore persistence

No new persistence code — exactly the same `ArtifactRepository`/
`AgentExecutionRepository` abstractions from Level 1.4/1.5, reused via
`context.artifacts`/`context.executions`. Nothing architecture-specific
was added to `app.persistence`.

## 11. Capability/RBAC

Capabilities: `READ_TICKET, READ_ARTIFACT, QUERY_KNOWLEDGE,
READ_REPOSITORY, WRITE_ARTIFACT, CREATE_ARCHITECTURE` — the five the task
listed, plus `CREATE_ARCHITECTURE` (already existed in `AgentCapability`
since Level 1.2; included for the same reason Planning got `CREATE_PLAN` —
the direct semantic capability for "this agent's job is to produce an
architecture"). Explicitly does **not** include `WRITE_CODE`, `DEPLOY`,
`WRITE_JIRA`, or `RESOLVE_INCIDENT` — verified by
`test_architecture_agent_has_expected_capabilities`. Same enforcement
mechanism as Planning: upfront `require_capability()` calls plus the shared
`before_tool_callback` gate at actual tool-call time.

**RBAC bridge**: identical pattern and identical debt to Planning's —
`STAGE_ROLES["architecture"].requires(Permission.READ_CODEBASE)` still
guards the legacy `architecture_agent` (via `_rbac_gate`, unchanged) and is
also checked once directly in `ArchitectureAgent._perform()`. Not a third
authorization mechanism — same two systems (`Permission` vs
`AgentCapability`) as Planning, same acknowledged, deferred reconciliation.

## 12. Lifecycle

`QuipuAgent.execute()` owns `CREATED -> INITIALIZING -> RUNNING ->
COMPLETED/FAILED` (Level 1.2, unchanged). No `stage_started`/
`stage_completed` on `_architecture_llm_agent` — same reasoning as
Planning: avoids writing to the SQLAlchemy `StageRun` table from a
Firestore-oriented execution path. The legacy `architecture_agent` keeps
both callbacks, unchanged.

## 13. Metrics

`_track_usage_metrics` — imported directly from `app.agents.planning`, not
reimplemented — accumulates into the `AgentMetrics` stashed in session
state, returned as `AgentOutput.metrics`. The legacy `_track_usage`/
`RunMetrics` path is untouched for `architecture_agent`. Same unreconciled
debt as Planning (two accumulators, two target types), not made worse or
better by this migration — just not duplicated a third time.

## 14. Failure behavior

Every failure mode the task listed is a distinct, explicit branch in
`_perform()`, each returning `AgentOutput(status=FAILED, errors=[AgentError(
code=..., category=...)])`:

| Failure | Error code |
|---|---|
| No artifact_ids given | `PLAN_ARTIFACT_MISSING` |
| Artifact not found | `PLAN_ARTIFACT_MISSING` |
| Wrong artifact type | `PLAN_ARTIFACT_WRONG_TYPE` |
| Plan payload doesn't validate | `PLAN_OUTPUT_INVALID` |
| Gemini/ADK/tool call fails | `ARCHITECTURE_LLM_FAILURE` |
| Empty model response | `ARCHITECTURE_EMPTY_RESPONSE` |
| ArchitectureOutput doesn't validate | `ARCHITECTURE_VALIDATION_FAILED` |
| Task coverage incomplete/unknown ids | `TASK_COVERAGE_INCOMPLETE` |
| Artifact save fails | `ARTIFACT_PERSISTENCE_FAILED` |

`validate_task_coverage()` runs unconditionally after
`ArchitectureOutput` validates and *before* any artifact is persisted —
an incomplete-coverage result never reaches `ArtifactGateway.save()`
(`test_incomplete_coverage_fails_end_to_end` asserts
`output.artifacts == []` in that case). No retry/replan logic exists here
— future orchestrator's job, same as Planning.

## 15. Legacy pipeline coexistence

`architecture_agent` (the original LlmAgent) is untouched and still wired
into `app/orchestrator/pipeline.py`'s `SequentialAgent`, verified by
`test_legacy_sequential_agent_remains_wired`. No orchestrator-level change
invokes `ArchitectureAgent` (the new class) anywhere yet — this task only
prepares it to be invoked by a future orchestrator, per scope. The two
paths (legacy session-state-coupled, new artifact-based) will coexist until
an orchestrator migration happens.
