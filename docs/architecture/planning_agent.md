# Planning Agent (Level 1.5 migration)

## Diagram

```
Ticket
  |
Orchestrator (future — not built in this level)
  |
PlanningAgent (QuipuAgent)                     <- app/agents/planning.py
  |
  +-- Repository Tools     (ADK-native: search_files, read_file, search_code,
  |                          get_project_structure, get_dependencies)
  +-- Enterprise Knowledge  (query_enterprise_knowledge -> KnowledgeGateway
  |                          -> KnowledgeService -> RetrievalBackend)
  +-- Jira                 (deterministic post-validation: JiraClient.create_story)
  |
  v
PlanOutput   (validated: unique task ids, resolvable deps, non-empty fields)
  |
  v
PlanArtifact  (Artifact, artifact_type=PLAN, payload=PlanOutput)
  |
  v
ArtifactGateway -> ArtifactRepository -> Firestore
```

ADK/Gemini are the *execution mechanism* inside `PlanningAgent`, not the
owner of any of the above — `QuipuAgent`, `AgentContext`, and
`app.persistence` own identity, capabilities, lifecycle, and persistence.

## 1. Current (pre-1.5) architecture

`app/agents/planning.py` exported one thing: `planning_agent`, a native ADK
`LlmAgent` with `PlanOutput` as its `output_schema`, `REPO_TOOLS +
JIRA_TOOLS` bound directly, RBAC via `STAGE_ROLES["planning"]` (a
`before_agent_callback`), `stage_started`/`stage_completed` writing straight
to the legacy SQLAlchemy `StageRun` table, and cost tracking into a plain
`RunMetrics` dataclass. Input was `context.state["feature_request"]` — an
untyped string in ADK session state, with no `AgentInput`/`WorkflowState`
contract anywhere in the loop. Invoked only via
`app/orchestrator/pipeline.py`'s `SequentialAgent`.

## 2. Target architecture (this level)

```
Orchestrator -> QuipuAgent.execute() -> PlanningAgent -> ADK adapter/LlmAgent -> Gemini
```

`PlanningAgent(QuipuAgent)` is the new entry point. It receives a typed
`AgentInput` (carrying `Ticket`, not a bare string) and an `AgentContext`
(carrying `KnowledgeGateway`/`ToolGateway`/`ArtifactGateway`/optionally
`AgentExecutionRepository`), and internally drives its own ADK `LlmAgent`
(`_planning_llm_agent`) to do the actual reasoning/tool-calling/structured
output. It never imports a Google client, Jira client, repo client, or
Firestore client directly — only Quipu runtime abstractions.

**Two `LlmAgent` instances exist right now, deliberately** (see §9): the
original `planning_agent` (legacy, `SequentialAgent`-compatible, Jira tool
called inline during the model's own reasoning) is preserved byte-for-byte
so the legacy pipeline keeps working. `_planning_llm_agent` (new, used only
by `PlanningAgent`) drops the Jira tool — Jira creation moved to
deterministic Python code that runs strictly *after* `PlanOutput` validates
(§9) — and gains `query_enterprise_knowledge` plus a
`before_tool_callback` that enforces Quipu capabilities at actual tool-call
time. Both share `PlanOutput`/`AffectedComponent`/`PlanTask`/`Risk` and the
`_build_instruction(include_jira_step: bool)` factory — nothing about the
plan schema or its validation was duplicated or weakened.

## 3. QuipuAgent relationship

`PlanningAgent` implements `identity`, `capabilities`, and `_perform()` (the
`QuipuAgent` base class's hook — `execute()` itself stays concrete on the
base class and owns lifecycle transitions, per Level 1.2). It never calls
another agent; it has no reference to an orchestrator at all.

## 4. ADK relationship

ADK stays exactly what it was: the LLM execution + structured-output +
function-tool-calling mechanism. `PlanningAgent._perform()` constructs an
`InMemoryRunner` around `_planning_llm_agent`, seeds session state from
`AgentInput`/`AgentContext` (not the reverse), runs one turn, and validates
the result against `PlanOutput` itself (not trusting ADK's `output_schema`
enforcement alone — both apply). ADK-specific types
(`CallbackContext`, `ToolContext`, `ReadonlyContext`, `LlmResponse`) appear
only in `app/agents/planning.py` and `app/tools/*.py` — never in
`app.domain`, verified by `test_adk_specific_dependencies_do_not_leak_into_domain_models`.

## 5. KnowledgeGateway usage

`AgentCapability.QUERY_KNOWLEDGE` gates it. If granted, `context.knowledge`
(a `KnowledgeGateway`) is stashed into ADK session state as
`_knowledge_gateway`; the `query_enterprise_knowledge` ADK tool (§7,
`app/tools/knowledge_tools.py`) reads it from there and calls
`gateway.search()` — the model decides *when* to call it, nothing
auto-injects a knowledge context into every prompt. The tool resolves the
Planning retrieval profile via `app.knowledge.policies.get_retrieval_policy
("planning_agent")` (Level 1.3A's existing profile — `ARCHITECTURE_PATTERN`,
`COMPLIANCE`, `TECHNOLOGY_STANDARD`, `HISTORICAL_PROJECT` — reused, not
duplicated) and rejects any `knowledge_type` outside it before the gateway
is even called. `KnowledgeServiceGateway` (new, `app/agent_runtime/gateways/
knowledge.py`) is the first concrete `KnowledgeGateway` implementation:
it wraps a `KnowledgeService` and narrows `KnowledgeContext` to
`KnowledgeItem[]` via the existing `gateway_adapter`, preserving
`document_id`/`source`/`relevance_score` (provenance) all the way back to
the model.

## 6. ToolGateway usage

**Not concretely used for repo/Jira tools in this migration — a deliberate
scope decision.** `ToolGateway`'s Protocol (`execute(ToolRequest) ->
ToolExecution`) is a generic request/response shape; ADK's native
function-tool-calling (the model decides which function, with which args,
mid-generation) is a different paradigm. Routing every `search_files`/
`read_file`/`create_story` call through a generic `ToolGateway.execute()`
round-trip would mean building a real bridge translating each ADK function
call into `ToolRequest`/`ToolExecution` — exactly the "elaborate universal
tool framework" the task said to avoid building for this one agent. Instead,
ADK's own tool-calling *is* treated as the tool boundary the target diagram
calls for: `PlanningAgent -> ADK adapter/LlmAgent -> {REPO_TOOLS,
query_enterprise_knowledge}`. `KnowledgeGateway` and `ArtifactGateway` *are*
concretely wired because Quipu-level code (not ADK) calls them directly —
once for the artifact save, and via the new tool for knowledge.

## 7. Artifact creation

After `PlanOutput` validates and Jira stories are created, `PlanningAgent`
builds an `Artifact` (existing Level 1.1 domain model — no second artifact
model introduced): `artifact_type=ArtifactType.PLAN`, `created_by=
"planning_agent"`, `parent_artifact_ids=agent_input.artifact_ids`,
`payload=plan.model_dump(mode="json")` (the full typed `PlanOutput`,
including populated `jira_key`s). `AgentOutput.artifacts` carries it back to
the caller; nothing embeds it into `WorkflowState` — that model still only
ever holds `artifact_ids` (Level 1.1, unchanged), confirmed by
`test_workflow_state_references_artifact_not_embeds`.

## 8. Persistence

`context.artifacts.save(workflow_id, artifact)` — `ArtifactGateway`'s
Protocol was evolved this level to take `workflow_id` (Artifact has no such
field of its own), aligning it with `ArtifactRepository`
(`app.persistence`, Level 1.4). `RepositoryArtifactGateway` (new, thin) is
the thing that actually bridges the two in production — it just delegates.
Separately, if `context.executions` (an `AgentExecutionRepository`) is
provided, `PlanningAgent` creates an `AgentExecution` row at the start of
`_perform()` and updates it (status, `completed_at`, `output_artifact_ids`,
or `error`) at the end — through the existing repository abstraction, never
inline persistence code in the LLM/tool logic path. `context.executions` is
optional (`None` by default) — a small, additive `AgentContext` field added
this level (Level 1.2's dataclass had no execution-tracking hook before;
Planning is the first agent that needed one).

## 9. Jira integration

**Behavior corrected, not just preserved.** The pre-1.5 implementation let
the LLM call `create_story` *during its own reasoning turn* — meaning Jira
stories could be created before the plan was known to be valid (schema
validation happens after the model's turn completes). The task explicitly
flagged this as wrong. Fixed: `_planning_llm_agent` has no Jira tool at all;
`_create_jira_stories(plan: PlanOutput)` is a plain, deterministic Python
function that only runs after `PlanOutput.model_validate_json()` has
already succeeded, iterating `plan.tasks` and calling
`JiraClient.create_story()` once per task, raising immediately (not
swallowing) on the first failure — so a partial/failed Jira run surfaces as
`AgentOutput.status = FAILED` with error code `JIRA_STORY_CREATION_FAILED`,
never a fabricated success. `PlanTask.jira_key` is still populated exactly
as before. **Trade-off, documented**: the *legacy* `planning_agent` LlmAgent
(used only by the old `SequentialAgent` pipeline) keeps the old inline
behavior unchanged, since fixing it there would mean adding post-processing
to `app/orchestrator/run.py`, which is out of scope for this migration (§13).
Anyone running the legacy pipeline directly still gets the old (technically
premature) Jira-creation timing; anyone going through `PlanningAgent` gets
the corrected sequence.

## 10. Capability/RBAC model

`PlanningAgent.capabilities`: `READ_TICKET`, `QUERY_KNOWLEDGE`,
`READ_REPOSITORY`, `READ_ARTIFACT`, `WRITE_ARTIFACT`, `CREATE_PLAN`,
`WRITE_JIRA`. Two new generic capabilities were added to
`AgentCapability` (`READ_ARTIFACT`, `WRITE_ARTIFACT` — any agent persisting
artifacts, not Planning-specific) plus one narrow, task-specific one
(`WRITE_JIRA` — "create/update an issue in the external tracker," reusing
the *name* of the legacy `Permission.WRITE_JIRA` for continuity, deliberately
**not** a broad `WRITE_ANYTHING`). Enforcement happens at two points: an
upfront `require_capability()` check per capability at the top of
`_perform()`, and — the real enforcement — a `before_tool_callback`
(`_tool_capability_gate`) that raises `CapabilityError` at actual ADK
tool-call time if the tool's mapped capability isn't in the granted set
seeded into session state. **The LLM is never the authority**: it can *ask*
to call a tool, but the callback (Quipu runtime code) decides whether that
call proceeds, regardless of what the model wants.

**RBAC bridge (smallest safe, per the task)**: the legacy
`STAGE_ROLES["planning"].requires(Permission.READ_KNOWLEDGE_BASE)` check is
*not* deleted — it still gates the legacy `planning_agent` LlmAgent via
`_rbac_gate` (a `before_agent_callback`, unchanged), and `PlanningAgent
._perform()` also calls it once directly, since it maps to the same
enterprise-knowledge-access concern `AgentCapability.QUERY_KNOWLEDGE` now
covers. This is two checks on two different invocation paths, not two
systems independently deciding the same thing inconsistently — but it *is*
real, acknowledged debt: `Permission`/`STAGE_ROLES` (`app/core/rbac.py`,
ADK-era) and `AgentCapability` (`app/agent_runtime`, Level 1.2) are still
two separate vocabularies. Full retirement of `Permission` is deferred
until the legacy pipeline itself is migrated or removed.

## 11. Metrics

`_track_usage_metrics` (new, mirrors the legacy `_track_usage` but targets
`AgentMetrics` — Level 1.1's domain model — instead of `RunMetrics`):
accumulates `prompt_tokens`/`completion_tokens`/`total_tokens`/`cost_usd` on
an `AgentMetrics` instance stashed in session state, returned as
`AgentOutput.metrics`. The legacy `_track_usage`/`RunMetrics` path is
untouched, still used by `planning_agent`. **Debt, documented**: these are
two independent accumulators computing the same cost formula against two
different target types; they are not reconciled into one source of truth in
this level, since doing so would mean changing `RunMetrics`/the legacy
orchestrator's accounting, out of scope here.

## 12. Failure behavior

Repository/knowledge/Gemini/tool failures during the run,
empty-model-response, and `PlanOutput` validation failures are all caught
explicitly in `_perform()` and turned into `AgentOutput(status=FAILED,
errors=[AgentError(...)])` with a specific `code`/`category` — never a
fabricated successful plan. `QuipuAgent.execute()`'s own lifecycle still
only flips to `AgentStatus.FAILED` on an *uncaught* exception (Level 1.2
behavior, unchanged) — a handled failure (the common case here) completes
normally at the lifecycle level while still reporting `WorkflowStatus.FAILED`
in the returned `AgentOutput`; that distinction — lifecycle-completed vs.
business-outcome-failed — already existed in Level 1.2 and is preserved,
not reinvented. No retry/replan logic exists anywhere in `PlanningAgent` —
that's explicitly the future orchestrator's job.

## 13. Relationship with the legacy ADK pipeline

`app/orchestrator/{pipeline,run}.py` + `app/core/db_hooks.py` +
`app/db/models.py` (`PipelineRun`/`StageRun`, SQLAlchemy) are untouched.
They still import and run the original `planning_agent` object exactly as
before — `SequentialAgent(sub_agents=[feature_detection_agent,
planning_agent, architecture_agent])` in `app/orchestrator/pipeline.py`
works unchanged (verified by `test_existing_app_and_legacy_pipeline_still_import`).
No orchestrator-level change was made to invoke `PlanningAgent` (the new
class) anywhere yet — per scope, this task only prepares it to be invoked by
a future orchestrator. The two paths will coexist until that orchestrator
migration happens; `PlanningAgent` was designed (typed `AgentInput`/
`AgentContext`, no orchestrator awareness) to be compatible with both a
future `SequentialAgent`-based recovery flow and a `LoopAgent`-based one,
per the task's forward-compatibility requirement, without committing to
either here.
