# Quipu Persistence & Durable Workflow State (Level 1.4)

## The critical distinction

```
Enterprise Knowledge                    Quipu Runtime State
        |                                        |
Cloud Storage / Agent Search            Workflow / Execution / Artifact /
        |                                Decision / Incident
KnowledgeService (app/knowledge)                |
        |                                State Repository (app/persistence)
   (what the org already knows)                 |
                                            Firestore

                                     (what one Quipu run did/decided)
```

Firestore is **not** the Enterprise Knowledge Base. It never stores
`KnowledgeDocument`/`KnowledgeChunk` content or full `KnowledgeItem` results —
those stay in the knowledge platform (`app/knowledge`), reached through
`KnowledgeGateway`/`KnowledgeService`/`RetrievalBackend`, untouched by this
level. Firestore stores what one Quipu workflow *did*: its state, which
agents ran, what they produced, what was decided, what failed.

## 1. Why Firestore

Quipu agent execution is async, potentially long-running, and needs to
survive process restarts (Cloud Run instances are ephemeral — see "Future
workflow recovery" below). Firestore gives durable, low-latency document
storage with native async Python support and, critically, **transactions**
for the one operation Quipu genuinely needs strong consistency on: workflow
version updates. No relational schema migrations, good fit for the
semi-structured shape of `WorkflowState`/`Artifact`/etc.

## 2. What belongs in Firestore

`WorkflowState`, `AgentExecution`, `Artifact` (metadata + payload — see
below), `Decision`, and the provisional `IncidentRecord`. All Level 1.1
domain models or their persistence-local counterparts, all owned by one
specific workflow run.

## 3. What does NOT belong in Firestore

- `KnowledgeDocument` / `KnowledgeChunk` content — lives behind
  `KnowledgeService`, sourced from Cloud Storage / Agent Search.
- Full `KnowledgeItem` search results — ephemeral, re-fetched on demand.
- `KnowledgeQuery` audit records are **references**, not content: when a
  workflow needs to record "this agent used knowledge," store the
  `KnowledgeQuery` (query text, agent, workflow, filters, strategy,
  `result_count` — already a Level 1.3A domain model, small and
  self-contained) inside the relevant `Artifact.payload` or
  `AgentExecution.knowledge_queries` field, never the retrieved
  `KnowledgeDocument`/`KnowledgeChunk` content itself. This is enforced by
  construction, not by a runtime check: nothing in `app.persistence` has a
  code path that accepts a `KnowledgeDocument` or `KnowledgeItem` as input.

## 4. Workflow-centric collection structure

```
workflows/{workflow_id}
workflows/{workflow_id}/executions/{execution_id}
workflows/{workflow_id}/artifacts/{artifact_id}
workflows/{workflow_id}/decisions/{decision_id}
workflows/{workflow_id}/incidents/{incident_id}
```

The `workflows/{workflow_id}` document itself holds only `WorkflowState`'s
own (deliberately thin) fields — `status`, `current_stage`, `version`, and
id-lists (`artifact_ids`, `execution_ids`, `active_incident_ids`). It never
embeds full artifact payloads, execution records, or decisions; those live
in their own subcollection documents, independently queryable and
independently sized. A workflow with 200 executions and 50 artifacts never
makes its own document large.

## 5. Artifact/execution/decision separation

Each has its own `Repository` Protocol (`app/persistence/repositories/`) and
its own subcollection — never nested inside another entity's document. This
is what makes the auditability query pattern (`Workflow -> executions /
decisions / artifacts / incidents`, independently) possible, and it's what
Level 1.1 already established at the domain-model level (`WorkflowState`
holding only `artifact_ids`, never embedded `Artifact` objects) — this level
just gives that separation a real storage backend.

`Artifact` and `Decision` have no `workflow_id` field of their own (they're
generic Level 1.1 domain models, not workflow-specific), so their repository
methods take `workflow_id` explicitly as a parameter — this also matches the
subcollection layout directly, with no collection-group query needed.
`AgentExecution` already carries `workflow_id`, so `create()`/`update()`
don't need it passed separately.

## 6. Optimistic concurrency

`WorkflowState` gained a `version: int` field (Level 1.4, additive,
default `1` — every pre-1.4 construction still works unchanged).
`WorkflowRepository.update_if_version(workflow_id, expected_version,
updated_workflow)` is the safe path: it fails with `VersionConflictError`
if the stored version no longer matches what the caller last read, rather
than silently overwriting a concurrent change from another agent/event.
Plain `update()` still exists for call sites that don't need that guarantee
(e.g. immediately after `create()`, before any concurrent access is
possible). No distributed locks — this is optimistic concurrency, not
pessimistic locking, matching the task's explicit instruction.

`InMemoryWorkflowRepository` enforces the exact same version semantics as
`FirestoreWorkflowRepository`, so unit tests exercise real concurrency
behaviour without a live Firestore connection.

## 7. Firestore transactions

`FirestoreWorkflowRepository.update_if_version()` wraps
`_update_workflow_txn()` — a plain async function using only
`AsyncTransaction`'s **public** `get()`/`set()` methods — with the real
`@firestore.async_transactional` decorator (verified against the installed
`google-cloud-firestore` 2.28.0 client): read the workflow document inside
the transaction, compare `version`, write `version + 1` if it matches,
`VersionConflictError` if it doesn't. No read-then-write outside a
transaction for this operation, per the task's explicit requirement.

`_update_workflow_txn` is deliberately factored out as a standalone,
directly-callable function (not inlined into the decorated closure) so it's
unit-testable against a fake `AsyncTransaction` implementing only the public
`get`/`set` methods — see `tests/test_firestore_persistence.py`. The outer
`@firestore.async_transactional`-wrapped call path is *not* exercised
end-to-end against a fake in tests: `_AsyncTransactional.__call__` reaches
into private `AsyncTransaction` internals (`_read_only`, `_id`, `_begin`,
...) that aren't documented public API and shouldn't be mocked — attempting
this surfaced exactly that `AttributeError` during development, confirming
the concern. The transactional *logic* — the part that actually matters —
has full test coverage; the outer wiring is a few lines, verified by
inspection and by the in-memory repository's parallel implementation of the
same semantics.

## 8. In-memory vs Firestore implementations

`app/persistence/memory/repositories.py` — deterministic, dict-backed,
zero external dependency, used for unit tests and local agent development
before Firestore is configured. `app/persistence/firestore/repositories.py`
— the production implementation. Both satisfy the same `Protocol`s in
`app/persistence/repositories/`, verified with `isinstance()` structural
checks in tests. Swapping one for the other at whichever call site
constructs the repositories is the only change needed.

## 9. Authentication

Application Default Credentials only — `firestore.AsyncClient(project=...)`
is constructed with no explicit credentials argument in
`app/persistence/firestore/client.py`, the one place in the repository that
does so. No key file path, no embedded secret. Locally:
`gcloud auth application-default login`. For local development against the
Firestore emulator, the standard `FIRESTORE_EMULATOR_HOST` environment
variable is read automatically by the Google client — no Quipu-specific
config needed for that. In deployment: a service account or Workload
Identity, entirely outside this repository.

## 10. Relationship with existing SQLAlchemy persistence (`app/db/models.py`)

`PipelineRun`/`StageRun` (SQLAlchemy, SQLite by default via
`app.db.base`) are **not** touched or replaced by this level. They belong
to an earlier, separate execution path: the ADK `SequentialAgent` pipeline
(`app/orchestrator/run.py`, `app/orchestrator/pipeline.py`,
`app/core/db_hooks.py`) built before the Level 1.x framework-agnostic
domain/runtime/persistence architecture existed. That pipeline creates a
`PipelineRun` row per run and a `StageRun` row per ADK agent stage
(`feature_detection`, `planning`, `architecture`), using plain string status
fields, not the `WorkflowStatus`/`WorkflowStage` enums this level's
`WorkflowState` uses.

Conceptually `PipelineRun` ~ `WorkflowState` and `StageRun` ~
`AgentExecution` for that one specific ADK-based pipeline, but they are not
interchangeable today: different status vocabularies, no `version` field, no
Firestore-shaped identifiers, and `app/core/db_hooks.py`'s callbacks import
`google.adk.agents.callback_context.CallbackContext` directly — i.e. that
persistence path is ADK-coupled, exactly what the Level 1.x domain/
runtime/persistence layers are built to avoid. Migrating the ADK pipeline
onto `app.persistence` is future work (part of the eventual "agent
migration stage" mentioned in earlier levels), not performed here — this
level introduces the new persistence boundary cleanly alongside the old one
without touching it, per the task's explicit instruction not to perform a
large migration.

## 11. Future workflow recovery

```
Cloud Run instance -> agent execution -> process crashes
        |
new runtime starts -> Firestore -> load workflow state -> resume
```

Not implemented here — no recovery orchestrator exists yet — but the
persisted shape supports it: `WorkflowState.current_stage` +
`execution_ids` + `artifact_ids` + `version` are enough for a future
recovery path to (a) load the workflow, (b) know what stage it was in and
what already completed (via `AgentExecutionRepository.list_for_workflow`),
and (c) resume with `update_if_version` guaranteeing it won't race a
still-running instance that hasn't actually crashed. `AgentExecution`
already separates `started_at`/`completed_at`/`status`, so "was this
execution actually finished, or did it die mid-flight" is answerable from
persisted state alone.

## Auditability

```
Workflow
  |
  +-- executions   (which agent ran, when, with what result — AgentExecutionRepository)
  +-- decisions    (what was decided and why — DecisionRepository)
  +-- artifacts    (what was produced — ArtifactRepository)
  +-- incidents    (what went wrong — IncidentRepository, provisional)
```

Every one of these is independently queryable by `workflow_id` today. No UI
is built on top of this in this level, but the query shape a future UI
needs ("what happened, which agent ran, what did it produce, what was
decided, what failed, what was retried") is directly answerable from these
four repositories without any additional indexing or denormalization work.
