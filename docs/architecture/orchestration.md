# Quipu Orchestration Layer (Level 2.0)

## Normal flow

```
Ticket
  |
SequentialAgent (or step-wise OrchestrationService.execute_next_step)
  +-- Planning
  +-- Architecture
  +-- Codegen
  +-- Testing
```

## Decision flow

```
TestArtifact
    |
overall_status == "passed"?  --yes--> ProposedDecision(CONTINUE)  [deterministic, no Gemini call]
    |no
failures share ONE classification with a known routing?
    |yes                                    |no
deterministic ProposedDecision      Orchestration LlmAgent (Gemini) -> ProposedDecision
    |________________________________________|
                    |
            Transition Policy (can_transition + retry budget)
                    |
         invalid? -> downgrade to ESCALATE (orchestrator, not Gemini, decides this)
                    |
              Next Agent / terminal status
```

## Recovery flow

```
Codegen
   |
Testing
   |
failure classified code_defect
   |
LoopAgent (Codegen -> Testing -> evaluate), bounded by orchestration_loop_max_iterations
   |
PASS -> stop        architecture_defect/unknown -> stop        max_iterations -> ADK stops on its own
```

---

## 1. Quipu orchestration responsibilities

Separated cleanly from agent execution, per the task's core principle:

| Agents own | Orchestrator owns |
|---|---|
| reasoning, domain work, tools, capabilities, own lifecycle | workflow progression, agent selection, artifact handoffs, decisions, retry/replan routing, workflow state, recovery, escalation, orchestration-level observability |

No agent (`PlanningAgent`, `ArchitectureAgent`, `CodegenAgent`, `TestingAgent`)
imports or calls another agent, directly or through the orchestrator's
internals — every agent-to-agent handoff goes through a persisted `Artifact`
and `OrchestrationService`.

## 2. Agent Registry

`app/orchestration/registry_setup.py::build_default_registry()` — one
function, registers the four implemented agents into the existing
`AgentRegistry` (Level 1.2, unchanged). The orchestrator resolves agents
exclusively through `registry.get(agent_id)`
(`app/orchestration/transitions.py::STAGE_TO_AGENT_ID` maps stage → id) —
nothing hardcodes `PlanningAgent()` etc. inline in the service. Adding
Deployment/Monitoring/Detecting/Incident-Resolution later is one more
`registry.register(...)` call plus one more `STAGE_ORDER` entry.

## 3. WorkflowState

Unchanged domain model (Level 1.1/1.4), authoritative. One additive change
this level: `WorkflowStatus` gained `ESCALATED` (distinct from `FAILED` —
a workflow deliberately handed to a human, not one that crashed). `CREATED`
from the task's lifecycle list maps onto the existing `PENDING`, not a
duplicate state.

## 4. Agent invocation

No new `AgentInvocation` model was introduced. `AgentInput` (Level 1.1)
already represents exactly what was needed — `workflow_id`, `agent_name`,
`ticket`, `artifact_ids`, `context`, `execution_id` — so a second contract
would have duplicated it. The one place the task's concern ("don't rely on
`artifact_ids[0]` as an undocumented convention") is addressed head-on:
`OrchestrationService._build_agent_input()` is the **single, explicit,
documented** place that decides which artifact goes into `artifact_ids` —
`workflow.artifact_ids[-1]` (the most recently produced one). Individual
agents (`ArchitectureAgent`, `CodegenAgent`, `TestingAgent`) still read
`artifact_ids[0]` internally (Level 1.6-1.8, by convention — "the one input
artifact this agent expects"), but the orchestrator is the component that
guarantees that convention holds by construction, not an accident of caller
discipline.

## 5. Artifact-driven handoffs

`OrchestrationService.execute_next_step()` never passes an agent's output
directly to the next agent in memory. Every stage's `AgentOutput.artifacts[0]`
is what the agent itself already persisted via `ArtifactGateway` (Level
1.5-1.8); the orchestrator's only additional job is recording that
artifact's id into `WorkflowState.artifact_ids` (`_advance_to_next_stage`),
so it becomes the next stage's documented input.

## 6. SequentialAgent

`app/orchestration/adk/sequential.py::build_happy_path_sequential_agent()`
constructs a **real** `google.adk.agents.SequentialAgent` with four
`QuipuAgentAdkAdapter` sub-agents (Planning → Architecture → Codegen →
Testing) — verified constructible and correctly ordered
(`test_sequential_agent_exists_for_happy_path`). It is a genuine, usable
execution mechanism, not decorative — but it is **not** the
production-recommended default path. See §19.

## 7. Orchestration LlmAgent

`app/orchestration/adk/decision_agent.py::decision_agent` — a real
`LlmAgent`, **zero tools**, `output_schema=ProposedDecision`. It receives
only `WorkflowEvidence` (stage, test status, failure classifications, retry
count/budget) as its user message and returns a `ProposedDecision`
(`action`, `target_agent`, `reason`, `confidence`) — it cannot invoke an
agent, read the repository, or query knowledge; it has no path to do
anything but recommend. `propose_decision()` is the one function that
actually runs it (via `InMemoryRunner`, injectable for tests), and it is the
**only** function in `app.orchestration.service` that touches `google.adk`.

**This LLM is invoked only for the cases that genuinely need reasoning** —
see §8. Most testing failures resolve deterministically without ever
calling Gemini for a decision; this is a deliberate cost/latency choice, not
a shortcut around "using ADK meaningfully" — the LlmAgent is real,
constructed, structured-output-validated, and exercised by
`test_ambiguous_classification_calls_decision_agent`.

## 8. Decision model

**No new decision domain model.** `app.domain.Decision` /
`DecisionAction` / `DecisionSource` (Level 1.1, unchanged) are reused as-is.
`Decision.target_agent` (already existed) is what makes the existing
`RETRY`/`REPLAN` actions expressive enough without inventing
`RETRY_CODEGEN`, `REPLAN_ARCHITECTURE`, etc. — `RETRY` +
`target_agent="codegen_agent"` *is* "retry codegen." `ProposedDecision`
(`app/orchestration/decisions.py`) is the one small addition: the
ADK-facing shape the model is allowed to produce, deliberately **without** a
`source` field — the model can never claim to be the orchestrator;
`build_decision()` sets `source` only after policy validation passes.

`FailureClassification` (Level 1.8's enum, in `app.agents.testing`) gained
one additive member: `ARCHITECTURE_DEFECT` — needed because the task's own
routing examples require distinguishing "the design is wrong" from "the
code is wrong," and no existing classification covered that.

Routing table (`app/orchestration/decisions.py::_DETERMINISTIC_ROUTING`),
matching the task's examples exactly:

| Classification | Action | Target |
|---|---|---|
| `CODE_DEFECT` | `RETRY` | `codegen_agent` |
| `ARCHITECTURE_DEFECT` | `REPLAN` | `architecture_agent` |
| `TEST_DEFECT` | `RETRY` | `testing_agent` |
| `ENVIRONMENT_FAILURE` | `RETRY` | `testing_agent` |
| `UNKNOWN` | `ESCALATE` | — |

`DEPENDENCY_FAILURE` and any **mixed** set of classifications across
multiple failures are intentionally absent from this table — those go to
the orchestration LlmAgent (§7) rather than a default I'd have had to guess
at.

## 9. Transition policy

`app/orchestration/transitions.py::can_transition()` — the application-level
graph, checked **before** any proposed decision executes, regardless of
source (deterministic table or Gemini). `RETRY`/`REPLAN` are constrained to
an explicit allow-list (`_ALLOWED_RETRY_TARGETS`) per current stage — from
`TESTING`, only `codegen_agent`, `architecture_agent`, and `testing_agent`
itself are reachable; `planning_agent` is deliberately **not** in that set,
directly implementing the task's explicit "do not let the LLM request
Testing → Planning" example
(`test_invalid_transition_rejected`). `SKIP`/`WAIT`/`ROLLBACK` are rejected
outright — not supported by this level's orchestrator.

**A rejected proposal is never silently dropped or blindly followed** —
`OrchestrationService.handle_decision()` catches `InvalidTransitionError`
and downgrades to `ESCALATE` with `source=ORCHESTRATOR`, so an invalid or
policy-violating model recommendation always still produces a safe,
auditable outcome.

## 10. LoopAgent / recovery

`app/orchestration/adk/loop.py::build_recovery_loop_agent()` — a real
`LoopAgent` (`sub_agents=[codegen_adapter, testing_adapter,
_LoopEvaluator]`, `max_iterations` from
`settings.orchestration_loop_max_iterations`, default 3). `_LoopEvaluator`
is a small custom `BaseAgent` that reads the TestArtifact the loop's own
Testing adapter just produced and sets ADK's `EventActions.escalate` (which
`LoopAgent` treats as "stop iterating now") when the test passed, or when
the failure classification is one Codegen retrying can't fix
(`architecture_defect`/`unknown`) — verified directly
(`test_loop_evaluator_stops_on_pass`,
`test_loop_evaluator_continues_on_code_defect`,
`test_loop_evaluator_stops_on_architecture_defect`). If neither condition
fires, ADK's own `max_iterations` bound stops the loop — never an unbounded
Python recursion.

**This loop is a genuine, constructed, tested ADK artifact** — it is not
currently the path `OrchestrationService`'s primary retry flow takes (that
flow is the simpler, Firestore-durable step-wise retry described in §12);
it's available as an ADK-native alternative for a caller that wants an
in-process repair cycle without a Firestore round-trip between every
attempt. Documented as a real but secondary mechanism, not vaporware.

## 11. Retry semantics

`app.config.Settings`: `max_codegen_retries` (2), `max_test_retries` (2),
`max_architecture_replans` (1), `orchestration_loop_max_iterations` (3) —
all named, typed, externalized settings, not literals scattered through the
codebase. Retry counts are tracked per-target-agent in
`WorkflowState.metadata["retry_count:<agent_id>"]` (no new persistence
model — reuses the existing `metadata: dict` field). Budget exhaustion
(`_check_retry_budget`) raises `RetryLimitExceededError`, caught by
`handle_decision` and downgraded to `ESCALATE` — verified by
`test_retry_limit_enforced`.

## 12. Escalation

`WorkflowStatus.ESCALATED` (§3). Set whenever: retry budget exhausted,
classification is `UNKNOWN`, the decision agent itself fails/returns
invalid output (`propose_decision`'s own fallback), or a proposed decision
fails transition-policy validation. An escalated workflow still has its
`active_decision_id` pointing at a persisted `Decision` explaining why — no
silent "stuck" state. No human-approval backend exists yet (out of scope,
per the task) — a future UI/service reads `WorkflowStatus.ESCALATED` +
`Decision.reason` directly from Firestore.

## 13. Firestore persistence

No new persistence code. `OrchestrationService` is constructed with the
existing `WorkflowRepository`/`ArtifactRepository`/
`AgentExecutionRepository`/`DecisionRepository` Protocols (Level 1.4) —
Firestore or in-memory, injected, unknown to the service itself. Every test
in this level uses the in-memory implementations; production wiring is a
constructor-argument swap, nothing more.

## 14. Optimistic concurrency

Every `WorkflowState` mutation in `OrchestrationService` goes through
`WorkflowRepository.update_if_version()` (Level 1.4, unchanged) — never a
plain `update()`. Two workers racing to advance the same workflow: the
first `update_if_version` call wins, the second raises `VersionConflictError`
— verified directly (`test_version_conflicts_handled`,
`test_concurrent_update_cannot_silently_overwrite`, the latter asserting
the loser's write was rejected, not silently applied on top).
`OrchestrationService` itself does not currently catch and retry a
`VersionConflictError` internally — a caller (future API layer, or a retry
wrapper) is expected to re-read and re-attempt. **Documented limitation**:
no automatic conflict-retry loop exists in this level.

## 15. Crash recovery / idempotency

`OrchestrationService._reconcile_stage()` runs at the top of every
`execute_next_step()` call (so `resume_workflow()` — a thin alias for
`execute_next_step` — gets it for free): it looks for a `COMPLETED`
`AgentExecution` for the current stage's agent whose `output_artifact_ids`
aren't yet reflected in `WorkflowState.artifact_ids`, and if found,
advances from that durable evidence instead of re-invoking the agent.
Proven with a real simulated crash (`test_resume_after_simulated_crash`):
Planning runs for real, then the workflow document is manually rolled back
to look like Planning never finished while leaving its `AgentExecution`
and `PlanArtifact` intact — `resume_workflow()` reconciles and advances
without a second Planning execution, verified by counting
`AgentExecution` rows for `planning_agent` (stays at 1).

**No distributed transaction was built** — the design explicitly follows
the task's own prescription: durable evidence (`AgentExecution` +
`Artifact`, both written by the agent itself before `execute_next_step`
returns) + idempotent resume (`_reconcile_stage`) + explicit reconciliation,
rather than trying to coordinate Gemini + Firestore + the filesystem
atomically. **Documented gap**: if a crash happens *between* an agent
persisting its `AgentExecution`/`Artifact` and the orchestrator's own
`update_if_version` call recording it in `WorkflowState`, reconciliation
handles it correctly (proven above). If a crash happens *mid-agent*, before
its own persistence completes, there is no partial-execution evidence to
reconcile from at all — the next `execute_next_step` call simply re-invokes
the agent from scratch, which is safe (agents don't have side effects
outside their own controlled tool boundaries — Codegen's `write_file` is
scoped and idempotent-by-overwrite, Testing's `run_tests` is read-only)
but not literally exactly-once at the sub-agent-tool-call level.

## 16. Capability/security behavior

The orchestrator does not bypass the Quipu runtime: it calls
`quipu_agent.execute(agent_input, context)`, and every agent independently
re-enforces its own `AgentCapability` set exactly as it did standalone
(Level 1.5-1.8, unchanged) — `test_capability_requirements_respected`
confirms `CodegenAgent` still has `WRITE_CODE` and still lacks `DEPLOY`
even when resolved through the registry. This is the defense-in-depth the
task asked for: orchestrator policy (transition graph, retry budget) +
agent capability (unchanged per-agent enforcement) + tool capability
(unchanged `before_tool_callback` gates inside each agent) — three
independent layers, none bypassed by this level's addition.

## 17. Standalone agent compatibility

Nothing about this level makes an agent depend on a workflow to run.
`test_agents_still_execute_independently` constructs a bare `PlanningAgent`
with a hand-built `AgentInput`/`AgentContext` (no `OrchestrationService`
anywhere) and confirms it still executes and completes — exactly the
pre-Level-2.0 usage pattern, unchanged.

## 18. ADK / Gemini integration

`google.adk` imports are confined to `app/orchestration/adk/` — verified
directly (`test_adk_isolated_from_orchestration_domain_logic`, scanning
`transitions.py`/`decisions.py`/`errors.py` source for `"google.adk"`).
`app/orchestration/service.py` itself imports only `propose_decision` from
`app.orchestration.adk` — one function, not the ADK SDK — matching the
"framework-independent orchestration + Google ADK adapter" split the task
asked for (the top-level `app.orchestration` package's `__init__.py` does
transitively import ADK through `service.py` → `adk/decision_agent.py`,
same as `app.knowledge`/`app.persistence`'s top-level packages transitively
touch their own Google backends through composition — the isolation
guarantee that actually matters, and that holds, is that the *pure business
logic* modules have zero ADK references).

Gemini remains the underlying model everywhere (`settings.gemini_model`,
unchanged) — the decision agent, and every agent it orchestrates, share the
same model configuration. Firestore remains the durable `WorkflowState`
store (Level 1.4, unchanged). Agent Search remains the enterprise knowledge
platform (Level 1.3, unchanged, untouched by this level — orchestration
itself performs no knowledge retrieval of its own, per the task's explicit
instruction).

### SequentialAgent vs. step-wise execution — the actual production default

Two ways to run the happy path exist, both real:

1. **`OrchestrationService.execute_next_step()` / `run_to_completion()`**
   (step-wise) — one stage per call, a durable `update_if_version` write
   after each. This is what every test in this level exercises, and the
   **recommended default**, because it's what makes §14/§15 (concurrency,
   crash recovery) actually work: each stage's durable evidence lands in
   Firestore before the next stage even starts.
2. **The real `SequentialAgent`** (§6) — all four stages in one ADK
   session. Faster for a single synchronous call; if the process dies
   mid-sequence, everything after the last `QuipuAgentAdkAdapter`'s own
   agent-level persistence (which still happens — each `QuipuAgent`
   persists its own `AgentExecution`/`Artifact` regardless of which
   mechanism invoked it) is lost from ADK's perspective, though
   `execute_next_step`'s reconciliation logic (§15) would still recover
   correctly if invoked afterward, since it reads Quipu persistence, not
   ADK session state.

Both call into the exact same `QuipuAgent` instances — no business logic is
duplicated in `SequentialAgent`/`LoopAgent` callbacks, per the task's
explicit instruction.

## 19. Observability

`app/orchestration/service.py` logs (via the existing
`app.core.observability.get_logger`, unchanged): workflow start, every stage
invocation (workflow id, agent id, execution id), every workflow failure
(stage, reason), every rejected/downgraded decision, and every
reconciliation event. No secrets or full code artifacts are logged —
messages reference ids and short reasons, never artifact payloads.

## 20. Future Deployment/Monitoring/Incident integration

`STAGE_ORDER`/`STAGE_TO_AGENT_ID`/`STAGE_TO_ARTIFACT_TYPE`
(`app/orchestration/transitions.py`) are the only places a new stage needs
to be added — plus one `registry.register(...)` call
(`registry_setup.py`) and one new artifact type if needed. Nothing else in
`OrchestrationService` assumes exactly four stages. Deployment is
deliberately **not** wired in this level — the happy path stops at
`COMPLETED` after Testing passes, per explicit scope.
