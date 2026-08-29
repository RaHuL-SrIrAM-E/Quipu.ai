# Feature Review (Level 3.4)

## Architecture

```
Customer/Product Signals
         │
         ▼
    Detecting Agent
         │
         ▼
 Feature Opportunity
         │
         ▼
   ┌─────────────┐
   │ Feature     │
   │ Review      │
   └──────┬──────┘
          │
     HUMAN REVIEW
      ┌───┴───┐
      ▼       ▼
   APPROVE   REJECT
      │
      ▼
    Ticket
      │
      ▼
  Planning
      │
      ▼
 SDLC Pipeline
```

```
AI proposes.
Human approves.
Existing agents execute.
```

This level implements everything up to and including `Ticket` creation.
Planning invocation and downstream orchestration are explicitly deferred —
see §15.

## 1. Why Feature Review exists

`DetectingAgent` (Level 3.2) already performs the AI reasoning: it looks at
a cluster of product signals and concludes `FEATURE_OPPORTUNITY` with a
confidence score. Nothing about that conclusion should turn into
engineering work on its own — an AI-detected opportunity is a *proposal*,
not a *decision*. Feature Review is the governance boundary that makes that
distinction real: it is where a human explicitly converts "the AI noticed
something" into "we're building this."

## 2. Why it is NOT an agent

This is the single most important architectural constraint on this level.
There is no `FeatureReviewAgent`, no ADK `LlmAgent`, no second Gemini call
anywhere in `app/feature_review/`. `DetectingAgent` already did the
reasoning; asking Gemini a second time to decide whether to approve its own
conclusion would be exactly the "LLM theater" prior levels warned against,
and would quietly erode the human-approval boundary this level exists to
protect. `FeatureReviewService`
(`app/feature_review/service.py`) is a plain, deterministic Python class —
state transitions, validation, and a real (but deterministic) Jira call,
nothing else. Verified structurally: no `InMemoryRunner`, no `LlmAgent`, no
`google` import anywhere in the module
(`test_no_shell_or_llm_surface_in_feature_review_module`).

## 3. Detecting vs Review

| | Detecting (3.2) | Feature Review (3.4) |
|---|---|---|
| Produces | `DetectionResult` (AI interpretation) | `FeatureReview` (human decision) |
| Actor | Gemini | A human (`DecisionSource.HUMAN`) |
| Mutates | Nothing upstream (Signals stay evidence) | Nothing upstream (`DetectionResult` stays evidence) |
| Can approve itself? | N/A | **Never** — see §4 |

`DetectionResult.status` is not repurposed to mean "reviewed" — detection
lifecycle and review lifecycle stay two separate objects connected only by
`FeatureReview.detection_id`, exactly as the task requires (§29).

## 4. AI → human boundary

There is no `confidence > 0.9 → auto-approve` rule anywhere in this
codebase, and there never will be in this level. Even a `confidence=0.99`
`FeatureReview` remains `PENDING` until a human explicitly calls
`approve()`. This is enforced two independent ways, not just one:

1. **Capability**: `approve()`/`reject()` require the caller to hold
   `AgentCapability.REVIEW_FEATURE_OPPORTUNITY` (checked via the existing
   `check_capability()` primitive — no new enforcement mechanism).
2. **Actor identity**: the caller must also pass `reviewer_type=
   DecisionSource.HUMAN` — reusing the existing `DecisionSource` enum
   (`ORCHESTRATOR`/`AGENT`/`HUMAN`/`SYSTEM`) rather than inventing a
   redundant identity system. `AGENT` and `SYSTEM` are rejected outright
   (`UnauthorizedReviewerError`) — proven directly
   (`test_agent_reviewer_type_rejected`,
   `test_system_reviewer_type_rejected`): even if some future caller
   somehow held the capability, a non-human actor still cannot complete a
   review. This is exactly what makes "DetectingAgent cannot approve its
   own feature opportunity" a structural guarantee, not a convention.

## 5. Review state machine

```
        PENDING
           │
      ┌────┴────┐
      ▼         ▼
  APPROVED   REJECTED
```

Both `APPROVED` and `REJECTED` are terminal — `ReviewStatus`
(`app/domain/enums.py`) has exactly three values, no reopening states.
`APPROVED → REJECTED` and `REJECTED → APPROVED` both raise
`InvalidReviewTransitionError`
(`test_approved_to_rejected_transition_forbidden`,
`test_rejected_to_approved_transition_forbidden`). Re-calling `approve()`
on an already-`APPROVED` review or `reject()` on an already-`REJECTED`
review is treated as **idempotent re-entry** (returns the existing review
unchanged, never an error, never a duplicate ticket) rather than a
transition attempt — this is what makes retriggered detection runs safe
(§17).

## 6. Detection immutability

Nothing in `FeatureReviewService` ever calls `.save()` on a
`DetectionResult`, and the service holds no `WRITE_DETECTION` capability at
all. `create_review()`/`approve()`/`reject()` only ever `.get()` the
detection to read it. Proven directly with a before/after comparison
(`test_detection_confidence_never_mutated_by_rejection`,
`test_detection_never_mutated_by_approval`) — a human rejecting an
opportunity never changes the fact that the AI detected it, or at what
confidence.

## 7. Signal provenance

```
Signals
   │
   ▼
DetectionResult          (detection_id)
   │
   ▼
FeatureReview             (detection_id)
   │
   ▼
Ticket                    (source_detection_id)
   │
   ▼
Planning  (future)
```

`Ticket` gained exactly one new, additive, optional field:
`source_detection_id: str | None = None` (`app/domain/ticket.py`) — every
existing caller that builds a `Ticket` without it is unaffected
(`test_ticket_source_detection_id_defaults_none`). Only the `detection_id`
travels forward — the full `DetectionResult` (and definitely not raw
`Signal` content) is never copied into `Ticket`, keeping the provenance
chain as identifiers, not a growing data dump.

## 8. Ticket creation

Fully deterministic — `_build_ticket()` in `app/feature_review/service.py`
constructs the title/description from `DetectionResult`'s own
already-curated fields only: `title`, `summary`, `rationale`, `confidence`,
the count and list of `supporting_signal_ids`, and `knowledge_references`
if present, plus the reviewer's id/comment. It never re-fetches full
`Signal` objects or touches `Signal.evidence`/`Signal.metadata` — proven
directly with a signal whose `metadata` contains a fake leaked email
(`test_ticket_does_not_contain_raw_signal_metadata`): it never appears in
the resulting ticket description. This is what keeps an approved
opportunity's Jira issue looking like an enterprise engineering request,
not an AI transcript or a customer-feedback dump (§12/§31 of the task).

## 9. Jira integration

Reuses `app.core.jira_client.JiraClient` — the exact same client
`PlanningAgent` already uses for deterministic Jira story creation
(`app/agents/planning.py::_create_jira_stories`) — no second Jira
integration was built. `FeatureReviewService` does **not** import the
stale, unused `app/tools/jira_tools.py` ADK tool (that module is dead
legacy code wired to the old `app.core.rbac` permission system, not the
current `AgentCapability` model — the same situation as other legacy stub
files noted in `docs/architecture/monitoring_agent.md`/
`docs/architecture/incident_resolution_agent.md`). `JiraClient` is
constructed lazily inside `approve()`, never at service-construction time,
so building the service never touches Jira credentials
(mirrors `CloudRunDeployer`/`CloudMonitoringClient`'s lazy-client pattern).

## 10. Idempotency

`detection_id` is the natural, sufficient idempotency key for review
creation (§17 of the task: "detection_id should uniquely identify the
Feature Review") — `FeatureReviewRepository.find_by_detection_id()` is
checked first in `create_review()`; a second call for the same
`detection_id` returns the existing review, never a duplicate
(`test_create_review_idempotent_for_same_detection`). No separate
fingerprint function was introduced for this — a direct uniqueness lookup
is simpler and more precise than a hash-based fingerprint here, since
`detection_id` is already the exact, single correct key.

For ticket creation specifically (§25), the idempotency key is the
`FeatureReview.ticket` field itself: `approve()` checks `review.ticket is
None` before ever calling Jira. If a prior attempt already recorded a
ticket on the review (e.g. Jira succeeded but a subsequent write failed),
a retry reuses that recorded ticket instead of calling Jira again —
proven directly by manually staging a "partially completed" review with a
ticket already attached and confirming a retry makes zero further Jira
calls (`test_retry_after_partial_success_does_not_duplicate_ticket`). A
genuine Jira failure (no ticket ever recorded) leaves the review `PENDING`
with no ticket — the next `approve()` call safely attempts Jira again
(`test_jira_failure_keeps_review_pending`,
`test_retry_after_jira_recovery_succeeds_without_duplicate` — exactly one
successful Jira call across a failed attempt plus a successful retry).

**Documented limitation** (§24/§25 of the task explicitly permit this):
Jira cannot participate in the same atomic transaction as the Firestore
write that persists `FeatureReview`. If two truly simultaneous first-time
`approve()` calls both read the review as `PENDING` (with no ticket
recorded yet) before either has written anything, both could call Jira,
producing two real Jira issues even though only one will end up recorded
as `FeatureReview.ticket` (the other becomes an orphaned issue in Jira with
no Quipu record). This is the accepted class of limitation for any
external system that can't join a Firestore transaction — Quipu does not
claim distributed exactly-once semantics here, only that *retries* are
safe and that the *review's own final state* is always single and
consistent (never silently overwritten).

## 11. Concurrency

`PENDING → APPROVED`/`PENDING → REJECTED` go through
`FeatureReviewRepository.update_if_version()` — the same optimistic-
concurrency pattern `WorkflowRepository` already established (Level 1.4):
a stale `expected_version` raises `VersionConflictError` rather than
silently overwriting a concurrent change. `FirestoreFeatureReviewRepository`
mirrors `FirestoreWorkflowRepository`'s transaction shape exactly
(`_update_feature_review_txn`, a standalone, independently-unit-testable
function, same pattern as `_update_workflow_txn` — not written generically
over both entities, since the existing precedent wasn't either).

Two reviewers racing to `approve()` the same review: proven with a real
`asyncio.gather()` race (`test_concurrent_approvals_only_one_ticket_created`)
— regardless of interleaving, both callers agree on exactly one
`ticket_id` in the end (the loser's `VersionConflictError` is caught,
the review is re-read, and since it's already `APPROVED`, the loser
returns that same authoritative state rather than erroring or retrying
into a second ticket). An `approve()` vs. `reject()` race is proven the
same way (`test_approve_vs_reject_race_only_one_wins`) — the final
persisted state is always exactly one of the two outcomes, never a
corrupted mix.

## 12. Authorization

`AgentCapability.REVIEW_FEATURE_OPPORTUNITY` (new, narrow — `app/
agent_runtime/capabilities.py`) is independent of `WRITE_CODE`/`DEPLOY`/
`RESOLVE_INCIDENT`/`WRITE_JIRA` — reviewing a product opportunity has
nothing to do with any of those. `WRITE_JIRA` is *not* required by
`FeatureReviewService` itself (it calls `JiraClient` directly, the same
way `PlanningAgent`'s deterministic post-validation code does, without
going through the ADK tool boundary that `WRITE_JIRA` gates) — this level
introduces exactly one new capability, not two. Authorization is a
two-part check (§4): capability **and** actor type must both hold.

## 13. PII protection

No raw `Signal.evidence`/`Signal.metadata` is ever read by
`FeatureReviewService` at all — not for review creation, not for ticket
content. The only signal-related data used anywhere in this level is
`Signal.get()` calls made purely to confirm evidence *still resolves*
(§5/§20 — a boolean/existence check, the actual signal content is
discarded immediately) during `create_review()`. This reuses the existing
sanitization boundary from Level 3 (`app.signals.sanitize`) by construction
— by never touching raw signal content, Feature Review can't leak what it
never reads.

## 14. Persistence

Follows the exact repository pattern already established: `FeatureReview`
(`app/domain/feature_review.py`) + `FeatureReviewRepository`
(`app/persistence/repositories/feature_review.py`) +
`InMemoryFeatureReviewRepository` + `FirestoreFeatureReviewRepository`,
top-level `feature_reviews/{review_id}` — not workflow-scoped, since a
feature opportunity may exist long before any SDLC workflow does (same
rationale as `signals/`, `detections/`, `resolutions/`).

**Ticket persistence decision**: no new `TicketRepository` was created. The
task explicitly permits this ("do not invent a huge ticket persistence
subsystem beyond what is necessary"), and no standalone `TicketRepository`
existed already to reuse — `WorkflowState` already embeds a `Ticket`
directly rather than referencing one by id, so embedding is the existing
precedent. `FeatureReview.ticket: Ticket | None` follows that same
precedent (plus a denormalized `ticket_id` field for quick audit access
without unpacking the embedded object) rather than introducing a second
persistence subsystem just for tickets originating from feature review.

## 15. Future Planning integration

Not implemented here. `FeatureReview.status == APPROVED` plus
`FeatureReview.ticket` is the entire hand-off surface a future
orchestration step needs:

```
FeatureReview APPROVED
         │
         ▼
      Ticket
         │
         ▼
 OrchestrationService   (future)
         │
         ▼
   PlanningAgent
```

No code here invokes `PlanningAgent`, no `OrchestrationService` dependency
exists anywhere in `FeatureReviewService` (verified — it's constructed from
`FeatureReviewRepository` + `DetectionGateway` + `SignalGateway` +
`JiraClient` only), and nothing here modifies `STAGE_ORDER`/
`STAGE_TO_AGENT_ID` in `app.orchestration.transitions`. Feature Review is a
governance/product workflow surrounding the SDLC, not a stage in it.

## 16. Google services

| Google Service | Used by Feature Review? |
|---|---|
| Gemini | **No** — the intelligence already happened in Detecting; a second LLM call here would be exactly the redundant "multi-agent for its own sake" pattern this level explicitly avoids |
| Google ADK | **No** — not an agent |
| Agent Search | **No** — no knowledge retrieval need; Detecting already consulted knowledge if relevant |
| Cloud Monitoring / Cloud Logging / Cloud Run | **No** — no relationship to production telemetry or deployment |
| Firestore | **Yes** — `FirestoreFeatureReviewRepository`, same durable-persistence role every other repository in this codebase has |

Jira (not a Google service, but the other real external integration this
level uses) is reused via the existing `JiraClient`, not duplicated.

## 17. Testing

`tests/test_feature_review_domain.py` (17), `tests/test_feature_review_persistence.py`
(18), `tests/test_feature_review_service.py` (32) — 67 new tests covering:
domain validation (status/reviewer-type enums, ticket association,
provenance field), repository contract (create/get/find_by_detection_id/
update_if_version with real `VersionConflictError`/`EntityNotFoundError`
behavior, Firestore serialization round-trip), review creation validation
(missing detection, `INCIDENT` rejected, no supporting evidence,
unresolvable signal references, idempotent re-creation), the full state
machine (`PENDING`→`APPROVED`, `PENDING`→`REJECTED`, both terminal
transitions forbidden, duplicate approval/rejection idempotent),
**concurrency** (a real `asyncio.gather()` race for simultaneous approvals
and for an approve-vs-reject race, both asserting a single consistent
final state), ticket content (`source_detection_id` preserved, evidence
signal ids present, raw signal metadata/PII absent, reviewer/comment
present), Jira failure/retry safety (failure keeps the review `PENDING`,
retry after recovery creates exactly one ticket, retry after a
partially-recorded ticket makes zero further Jira calls), authorization
(agent/system reviewer types rejected, missing capability rejected, valid
human reviewer accepted), detection immutability (before/after equality
checks across both approve and reject), and `list_pending`/`get_review`.
No test requires live Jira or Firestore credentials.

Full suite: **760 passed, 6 skipped** (pre-existing gated integration
tests) — no regressions.
