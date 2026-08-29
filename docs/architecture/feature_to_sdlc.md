# Feature → SDLC Workflow Integration (Level 3.5)

## Diagram

```
Customer / Support / Behaviour
            │
            ▼
       Signals
            │
            ▼
     DetectingAgent
            │
            ▼
   FEATURE_OPPORTUNITY
            │
            ▼
    FeatureReview
            │
      HUMAN APPROVAL
            │
            ▼
          Ticket
            │
            ▼
   OrchestrationService
            │
            ▼
    WorkflowState
            │
            ▼
      PlanningAgent
            │
            ▼
    ArchitectureAgent
            │
            ▼
       CodegenAgent
            │
            ▼
       TestingAgent
            │
            ▼
     DeploymentAgent
            │
            ▼
     MonitoringAgent
```

```
FeatureReviewService  ≠  OrchestrationService  ≠  PlanningAgent
```

Three components, three responsibilities, none of them aware of how the
others do their job:

| Component | Owns | Does NOT own |
|---|---|---|
| `FeatureReviewService` (Level 3.4) | the review decision, ticket creation | workflow execution, agent invocation |
| `OrchestrationService` (this level extends it) | workflow creation/progression, agent selection | review decisions, Gemini reasoning |
| `PlanningAgent` (unchanged) | turning a ticket into a plan | knowing whether the ticket came from a human or from Feature Review |

This level closes exactly the gap between the first two: an approved
`FeatureReview`'s `Ticket` can now become a `WorkflowState` that
`OrchestrationService.execute_next_step()` drives through Planning like any
other workflow. Nothing about Incident Resolution's integration is touched
— that's explicitly the next task.

## 1. Why Feature Review stays outside STAGE_ORDER

Unchanged from Level 3.4: Feature Review is a governance/product workflow
*surrounding* the SDLC, not a stage inside it. This level adds zero new
`WorkflowStage` values (no `FEATURE_REVIEW`, no `FEATURE_DISCOVERY`) and
does not touch `app/orchestration/transitions.py`'s `STAGE_ORDER`/
`STAGE_TO_AGENT_ID` at all. A workflow only exists *after* a human has
already approved the opportunity — by the time `OrchestrationService` ever
sees a feature-derived `Ticket`, the governance decision is already made
and behind it.

## 2. Human approval boundary — still authoritative

The flow is still, and only ever:

```
Detection → FeatureReview PENDING → HUMAN APPROVAL → Ticket → Workflow
```

There is no code path anywhere that starts a workflow from a `PENDING` or
`REJECTED` review, and no code path that starts one from a
`DetectionResult`/confidence score directly.
`OrchestrationService.start_workflow_from_review()` re-checks
`review.status == ReviewStatus.APPROVED` every single call — proven
directly (`test_rejected_review_cannot_start_workflow`,
`test_pending_review_cannot_start_workflow`). A high-confidence
`FEATURE_OPPORTUNITY` still cannot skip the human step; nothing in this
level weakens that.

## 3. Ticket creation — unchanged, reused

Still entirely `FeatureReviewService`'s job (Level 3.4). This level adds no
new Jira integration and no new ticket-content logic — the `Ticket`
`OrchestrationService` consumes is exactly the one `FeatureReviewService.
approve()` already built and attached to `FeatureReview.ticket`.

## 4. Ticket → Workflow conversion

`OrchestrationService.start_workflow_from_review(review_id)` — the one new
public method this level adds, alongside a backward-compatible extension
of the *existing* `start_workflow(ticket, ...)` entry point (no competing
"start" method was introduced, per the task's explicit instruction to
reuse/evolve rather than duplicate):

```python
async def start_workflow(
    self, ticket: Ticket, *, workspace_path: str | None = None,
    workflow_id: str | None = None, metadata: dict | None = None,
) -> WorkflowState: ...

async def start_workflow_from_review(self, review_id: str) -> WorkflowState: ...
```

`workflow_id`/`metadata` are additive, optional parameters on the existing
`start_workflow()` — every pre-existing caller that only passes `ticket`
(optionally `workspace_path`) is unaffected
(`test_manual_ticket_workflow_creation_unchanged`).
`start_workflow_from_review()` is a thin, validating wrapper around it:
resolve the review, validate it's `APPROVED` with a ticket, then call
`start_workflow()` with that ticket — the actual `WorkflowState` shape
(`status=PENDING`, `current_stage=WorkflowStage.PLANNING`) is identical
either way. `start_workflow_from_review`'s signature takes only
`review_id` — no caller-suppliable `ticket`/`metadata`/`stage` parameter
exists on it at all (`test_arbitrary_source_detection_id_cannot_be_injected_by_caller`,
`test_cannot_bypass_planning_to_invoke_codegen_directly`), so there is no
way to spoof provenance or a starting stage through this entry point.

## 5. Workflow → Planning

Completely unchanged. `execute_next_step()` resolves `planning_agent` from
`STAGE_TO_AGENT_ID[WorkflowStage.PLANNING]` via the same `AgentRegistry`,
builds `AgentInput` from `workflow.ticket` via the same
`_build_agent_input()`, and constructs `AgentContext` the same way — no
special-casing based on `workflow.metadata["source"]` anywhere in this
path. `PlanningAgent` doesn't know or care whether its ticket came from a
human filing a request or from an approved feature opportunity — proven
directly (`test_execute_next_step_invokes_planning_through_registry`,
`test_planning_runs_standalone_with_feature_ticket` — the same
`PlanningAgent`, invoked either through the orchestrator or completely
standalone, with a feature-derived ticket, works identically).

## 6. Provenance chain

```
Signal
   ↓ (supporting_signal_ids)
DetectionResult
   ↓ (detection_id)
FeatureReview
   ↓ (source_detection_id, set once by FeatureReviewService.approve())
Ticket
   ↓ (embedded directly — WorkflowState.ticket: Ticket, unchanged since Level 1.1)
WorkflowState
   ↓ (parent_artifact_ids / created_by, unchanged since Level 1.5+)
PlanArtifact → ArchitectureArtifact → CodeArtifact → TestArtifact → DeploymentArtifact
```

**No new field was needed on `WorkflowState` or `Artifact` at all.**
`WorkflowState.ticket` already embeds the full `Ticket` object (a Level 1.1
design decision, unchanged), and `Ticket.source_detection_id` already
exists (added in Level 3.4) — so once a `WorkflowState` is built from an
approved review's ticket, `workflow.ticket.source_detection_id` already
carries the provenance through, with zero additional plumbing
(`test_workflow_source_detection_id_preserved`,
`test_full_provenance_chain_survives`). `PlanArtifact` (and every artifact
after it) is unchanged — it already references `workflow_id` implicitly
(via the `ArtifactRepository`'s workflow-scoped storage) and
`parent_artifact_ids`, both pre-existing mechanisms; the provenance chain
is inspectable by walking `workflow.ticket.source_detection_id` →
`DetectionRepository.get()` → `detection.supporting_signal_ids` →
`SignalRepository.get()` for each, all through existing gateways.

`WorkflowState.metadata` gained no new *typed* field either —
`start_workflow_from_review()` populates the existing, already-generic
`metadata: dict` with `{"source": "feature_review", "review_id": ...,
"source_detection_id": ...}` (§14/§23 of the task: "if `WorkflowState.
metadata` is currently the accepted location, use it"). A manually-started
workflow's `metadata` stays `{}` exactly as before
(`test_manual_ticket_workflow_creation_unchanged`).

## 7. Idempotency

`detection_id` uniquely identifies a `FeatureReview` (Level 3.4); this
level adds the next link: `FeatureReview.workflow_id: str | None = None`
(one new, additive, optional field) uniquely associates a review with (at
most) one workflow. `start_workflow_from_review()`:

1. If `review.workflow_id` is already set and that workflow exists →
   return it (`test_starting_same_review_twice_returns_same_workflow`).
2. If `review.workflow_id` is set but the workflow was never actually
   created (a crash between the claim write and `WorkflowRepository.
   create()`) → create it now, using the already-claimed id, rather than
   erroring forever or claiming a second one
   (`test_claimed_but_uncreated_workflow_is_recovered_on_retry`).
3. Otherwise → atomically claim a freshly generated id via
   `FeatureReviewRepository.update_if_version()`, then create the
   workflow with that id.

Same pattern as every dedup mechanism established since Level 3
(Signal/Detection/Resolution fingerprints) — but here the "fingerprint" is
just the review's own identity, since a `FeatureReview` already uniquely
identifies at most one downstream workflow by construction, no hashing
needed.

## 8. Concurrency

The claim step (`FeatureReviewRepository.update_if_version()`) is the same
optimistic-concurrency primitive `WorkflowRepository`/`FeatureReview`'s own
approve/reject transitions already use (Level 1.4/3.4) — no Redis, no new
locking service. Two callers racing `start_workflow_from_review()` for the
same review: both may read `workflow_id=None`, but only one's claim write
succeeds; the loser catches `VersionConflictError`, re-reads the review,
and returns the winner's workflow — proven with a real `asyncio.gather()`
race, asserting both callers agree on exactly one `workflow_id`
(`test_concurrent_start_from_review_only_one_workflow_wins`).

**Documented limitation** (same class already accepted in Level 3.4's Jira
integration): if a process crashes strictly *between* the successful claim
write and `WorkflowRepository.create()` ever running, the review is left
pointing at a `workflow_id` with no `WorkflowState` behind it yet. This is
not silently lost — the next `start_workflow_from_review()` call (§7 case
2) detects exactly this and creates the workflow using that already-claimed
id. Quipu does not claim distributed exactly-once semantics across two
independent repositories without a shared transaction; it does guarantee
that a retry is always safe and that no more than one workflow is ever
associated with a given review's `workflow_id` field.

## 9. Jira distinction

Two structurally different Jira objects exist in this architecture, and
this level keeps them that way:

| Jira object | Created by | Represents |
|---|---|---|
| The feature-request ticket | `FeatureReviewService.approve()` (Level 3.4) | The overall approved opportunity |
| One story per plan task | `PlanningAgent._create_jira_stories()` (unchanged, Level 1.5) | An individual engineering task within the plan |

`PlanningAgent` has never created a Jira issue "for the ticket as a whole"
— its Jira behavior (`_create_jira_stories`) has always operated purely
per-`PlanTask`, unconditionally, regardless of ticket origin. There was no
duplication risk to fix: a feature-derived ticket flows into Planning
exactly like a manually-filed one, Planning still creates one story per
task (`test_planning_does_not_recreate_feature_ticket_in_jira`,
`test_existing_manual_ticket_jira_behavior_unchanged`), and it does so
through its own `JiraClient` instance — entirely independent from
`FeatureReviewService`'s. No mechanism was added to "suppress" ticket
recreation because none was ever at risk of happening.

## 10. Security

A feature-derived workflow gets **exactly** the same treatment as a
manually-submitted one — no privileged path exists:

- `execute_next_step()` is untouched; it resolves agents through the same
  `AgentRegistry`, builds the same `AgentInput`/`AgentContext`, and every
  agent still calls `self.require_capability(...)` exactly as before.
- `PlanningAgent.capabilities` is a fixed property — identical regardless
  of which `AgentInput` it's given
  (`test_approval_does_not_grant_extra_agent_capabilities`).
- Approval (Level 3.4's `REVIEW_FEATURE_OPPORTUNITY` capability) authorizes
  exactly one thing: the `FeatureReview` state transition. It has no
  relationship to, and grants nothing toward, any `AgentCapability` any
  SDLC agent holds.
- `start_workflow_from_review()` never accepts a caller-supplied `Ticket`
  or `metadata` — only a `review_id` — so a malicious caller cannot
  fabricate a `Ticket` claiming an arbitrary `source_detection_id` and have
  it treated as if `FeatureReviewService` produced it (§22 adversarial E of
  the task): the only `Ticket` this method ever uses is the one already
  attached to the trusted, previously-validated `FeatureReview` record.
- No orchestration API accepts a caller-chosen starting stage — every
  workflow, feature-derived or not, always begins at `WorkflowStage.
  PLANNING` (§22 adversarial F —
  `test_cannot_bypass_planning_to_invoke_codegen_directly`).

## 11. Existing manual-ticket compatibility

`start_workflow(ticket)` (no `workflow_id`/`metadata` passed) behaves
identically to before this level: `WorkflowState` gets an auto-generated
id and empty `metadata` — proven directly
(`test_manual_ticket_workflow_creation_unchanged`). The legacy ADK
`SequentialAgent` pipeline (`app/orchestrator/pipeline.py`) is untouched;
this level only extends `OrchestrationService`
(`app/orchestration/service.py`), which the legacy pipeline doesn't use.

## 12. Google services involved

| Service | Role in this level |
|---|---|
| Firestore | `FeatureReviewRepository`/`WorkflowRepository` persistence, unchanged repositories, no new collection |
| Jira | Reused unchanged — `FeatureReviewService`'s ticket creation (3.4) and `PlanningAgent`'s per-task stories (1.5), no new client |
| Google ADK | `PlanningAgent`'s existing internal `LlmAgent`, invoked exactly as before |
| Gemini | `PlanningAgent`'s existing reasoning — no new call introduced anywhere in this level |
| Agent Search | `PlanningAgent`'s existing `query_enterprise_knowledge` path, untouched |

The `FeatureReview → Workflow` transition itself
(`start_workflow_from_review`) is pure deterministic Python — no Google
SDK call of any kind.

## 13. Testing

`tests/test_feature_to_sdlc.py` (28 tests) plus 2 added to
`tests/test_feature_review_domain.py` for the new `workflow_id` field — 30
new tests covering: workflow creation from an approved review (ticket
association, `source_detection_id`, `metadata["source"]`, persistence, the
review recording its own `workflow_id`), Planning handoff (correct
`AgentInput` construction, ticket context via the existing mechanism — no
new `state["feature_request"]` convention, standalone execution, execution
through the orchestrator via `AgentRegistry`, enterprise knowledge still
reachable), the full provenance chain, idempotency (same review started
twice, a claimed-but-uncrated workflow recovered on retry), a real
`asyncio.gather()` concurrency race, Jira non-duplication (feature ticket
not recreated, task-level stories still created, manual-ticket Jira
behavior unchanged), and the full adversarial/security set (rejected/
pending review rejected, `INCIDENT` detection rejected at review-creation
time, missing review, missing `review_repo` configuration, no capability
bypass, no caller-suppliable ticket/stage). Also verified structurally
that `FeatureReviewService` imports neither `app.orchestration` nor
`app.agents.planning` — the layering boundary holds by construction, not
just by convention.

Full suite: **788 passed, 6 skipped** (pre-existing gated integration
tests) — no regressions.
