# Codegen Agent (Level 1.7)

## Diagram

```
ArchitectureArtifact
      |
  CodegenAgent
   +-------+--------+
   |                |
Repository       Knowledge
  Tools              |
   |             Agent Search
   +-------+--------+
           |
       Gemini / ADK
           |
  Controlled File Mutation   (write_file: capability-gated, scope-checked, path-safe)
           |
  Scope Validation           (real filesystem, not the model's self-report)
           |
     CodegenOutput
           |
      CodeArtifact
```

No legacy predecessor exists for this agent — unlike Planning/Architecture,
there's no coexistence pattern here; `CodegenAgent` is the only Codegen
implementation. It follows the same `QuipuAgent` + internal-ADK-adapter
shape established in Level 1.5/1.6 (`docs/architecture/planning_agent.md`,
`docs/architecture/architecture_agent.md` — read those first for the shared
mechanics: capability enforcement, the `AgentExecution`/`AgentMetrics`
bridge, the RBAC-bridge pattern).

## 1. Responsibility

Architecture decides **what** should change, **how**, and **which** files.
Codegen decides **how to actually implement** that design in code — nothing
more. It never redesigns the architecture (the prompt says so explicitly)
and never calls another agent (routing is the future orchestrator's job).

## 2. ArchitectureArtifact input

Same pattern as Architecture consuming PlanArtifact (Level 1.6):
`AgentInput.artifact_ids[0]` → `ArtifactGateway.get(workflow_id, id)` → type
check (`ArtifactType.ARCHITECTURE`) → `ArchitectureOutput.model_validate
(payload)`. Three explicit failure codes before any Gemini call:
`ARCHITECTURE_ARTIFACT_MISSING` (empty `artifact_ids` or not found),
`ARCHITECTURE_ARTIFACT_WRONG_TYPE`, `ARCHITECTURE_OUTPUT_INVALID`.

## 3. Repository inspection

Unchanged `REPO_TOOLS` (`search_files`, `read_file`, `search_code`,
`get_project_structure`, `get_dependencies`) — imported directly from
`app.tools.repo_tools`, not duplicated. Codegen is instructed to inspect
existing patterns before writing.

## 4. Enterprise knowledge

Same `query_enterprise_knowledge` tool as Planning/Architecture (generalized
in Level 1.6 to resolve the retrieval profile by `state["_agent_name"]`).
`app.knowledge.policies.AGENT_RETRIEVAL_PROFILES["codegen_agent"]` (Level
1.3A, unchanged) — `CODING_STANDARD`, `SECURITY_POLICY`,
`TECHNOLOGY_STANDARD`, `ARCHITECTURE_PATTERN` — reused, not duplicated.

## 5. Controlled mutation — the core new capability this level

`app/tools/codegen_tools.py::write_file` is the **only** way any Quipu agent
writes to the filesystem. No shell tool exists or is offered to the model.
Enforced, in this exact order, on every call:

1. `AgentCapability.WRITE_CODE` granted (`state["_capabilities"]`) — else
   `{"success": False, "error": "WRITE_CODE capability not granted"}`.
2. Path is not absolute — rejected with a clear error.
3. Path (normalized) is in `state["_allowed_paths"]` — the architecture-
   derived scope (§7). Not in scope → rejected, **not silently widened**.
4. `app.tools.repo_tools._safe_join(root, path)` — reused directly, not
   reimplemented, since this is the security-critical part: resolves the
   path and verifies it doesn't escape `root` via `..`, absolute-path
   override, or (since `.resolve()` follows symlinks) a symlink pointing
   outside the workspace.

Rejections return a result dict rather than raising — the model sees the
failure and can note it in `unresolved_items` instead of the whole turn
crashing. A rejected write **never touches disk** — verified directly for
each rejection path (`test_write_code_capability_required`,
`test_disallowed_file_write_rejected`, `test_path_traversal_rejected`,
`test_absolute_path_rejected`, `test_repository_root_escape_rejected`, all
asserting nothing was written).

`before_tool_callback=_tool_capability_gate` (shared with Planning/
Architecture, `_TOOL_CAPABILITY_MAP` extended with `"write_file":
WRITE_CODE`) is a second, independent enforcement point at the ADK
tool-call boundary — belt-and-suspenders with `write_file`'s own internal
check.

## 6. WRITE_CODE capability

Granted alongside `READ_TICKET, READ_ARTIFACT, QUERY_KNOWLEDGE,
READ_REPOSITORY, WRITE_ARTIFACT`. Explicitly **not** granted: `DEPLOY`,
`WRITE_JIRA`, `RESOLVE_INCIDENT` (asserted in
`test_codegen_agent_expected_capabilities`). `AgentCapability.CREATE_COMMIT`
already exists in the enum but is deliberately **not** granted this level
— see §15.

## 7. Allowed-file scope

```python
allowed_paths = set()
for task_design in architecture.task_designs:
    allowed_paths.update(task_design.files)
```

Computed once per `_perform()` call, directly from the validated
`ArchitectureOutput` — never from the model, never expanded mid-run. Seeded
into `session_state["_allowed_paths"]`, which both `write_file` (enforcement)
and `_build_instruction()` (so the model actually knows the boundary,
rather than discovering it only through repeated rejections) read from the
same source.

## 8. Path safety

Covered in §5 — `_safe_join` verified directly against real traversal
payloads (`/etc/passwd`, `../../etc/passwd`, `a/../../b`) before this task's
code was written, confirming the existing Level 1.5 logic already handles
absolute-path pathlib-join quirks correctly (Python's `Path.__truediv__`
lets an absolute right-hand side override the left, but `_safe_join`'s
postcondition check — `root not in candidate.parents` — catches the
resulting escape regardless of *how* the candidate path was constructed).

## 9. ADK + Gemini

`_codegen_llm_agent` — a real `google.adk.agents.LlmAgent`,
`model=settings.gemini_model` (the same Gemini configuration as Planning/
Architecture), `output_schema=CodegenOutput` (structured generation, no
free-text-plus-parsing fallback), tools = `REPO_TOOLS + KNOWLEDGE_TOOLS +
CODEGEN_TOOLS`. `after_model_callback=_track_usage_metrics` — imported
directly from `app.agents.planning`, not reimplemented (third reuse of that
function now, after Architecture).

## 10. CodegenOutput

```python
class CodegenOutput(BaseModel):
    summary: str                         # non-empty (validated)
    modified_files: list[str]
    created_files: list[str]
    deleted_files: list[str]
    changes: list[FileChange]            # path, change_type (created|modified|deleted), description
    implementation_notes: str
    unresolved_items: list[str]
    tests_to_run: list[str]
```

**Critical property, verified**: the persisted `CodegenOutput`'s
`modified_files`/`created_files`/`deleted_files` are **not** the model's
self-report. `_perform()` snapshots the entire workspace tree (mtimes, via
`_snapshot()`) before and after the ADK run, diffs it (`_diff_snapshot()`),
and overwrites those three fields with the real result
(`codegen_output.model_copy(update={...})`) before persisting. If the model
claims it created a file it never actually wrote, the artifact reflects
zero files created — `test_actual_modified_files_captured_not_llm_self_report`
proves exactly this scenario.

## 11. CodeArtifact

`ArtifactType.CODE_CHANGE` (already existed in the domain enum — no new
`ArtifactType` member added, matching the task's "if it does not already
exist" framing) — same `Artifact` domain model Planning/Architecture use, no
second artifact class. `parent_artifact_ids=[architecture_artifact_id]`.

## 12. Artifact lineage

```
PlanArtifact -> ArchitectureArtifact -> CodeArtifact
```

Each artifact's `parent_artifact_ids` points to exactly the one it was
built from — a real, walkable chain via `Artifact.parent_artifact_ids`
(Level 1.1), not reconstructed or inferred after the fact.

## 13. Execution/audit

Same `AgentExecution`/`AgentMetrics` pattern as Planning/Architecture:
created `RUNNING` at the start (if `context.executions` provided), updated
to `COMPLETED`/`FAILED` with `output_artifact_ids`/`error` at the end.
Answers, today, from persisted state alone: which agent
(`AgentExecution.agent_name`), which workflow, when
(`started_at`/`completed_at`), what it produced
(`output_artifact_ids` → the `CodeArtifact`), and — by walking
`parent_artifact_ids` — which architecture and which plan it was based on.
"Which knowledge was consulted" is *not* fully answerable yet — see §16 for
the same documented gap Planning/Architecture already carry.

## 14. Failure behavior

| Failure | Error code |
|---|---|
| No artifact_ids / artifact not found | `ARCHITECTURE_ARTIFACT_MISSING` |
| Wrong artifact type | `ARCHITECTURE_ARTIFACT_WRONG_TYPE` |
| Architecture payload invalid | `ARCHITECTURE_OUTPUT_INVALID` |
| No workspace checked out | `CODEGEN_WORKSPACE_MISSING` |
| Gemini/ADK/tool call fails | `CODEGEN_LLM_FAILURE` |
| Empty model response | `CODEGEN_EMPTY_RESPONSE` |
| CodegenOutput doesn't validate | `CODEGEN_VALIDATION_FAILED` |
| Real filesystem change outside allowed scope | `CODEGEN_SCOPE_VIOLATION` |
| Artifact save fails | `ARTIFACT_PERSISTENCE_FAILED` |

`CODEGEN_SCOPE_VIOLATION` is genuinely defense-in-depth, not the primary
gate — `write_file` itself structurally cannot write outside
`allowed_paths`, so this only fires if that gate is somehow bypassed. It is
real, not decorative: an early implementation computed the before/after
snapshot only over `allowed_paths` itself, which made the violation check a
structural no-op (it could never see a file it wasn't already told to
watch). Fixed to snapshot the *entire* workspace tree instead — proven by
`test_scope_violation_detected_end_to_end`, which simulates a write landing
outside scope and confirms the agent fails with `CODEGEN_SCOPE_VIOLATION`
rather than silently succeeding. No retry/replan logic exists here — same
as every other migrated agent, that's the future orchestrator's job.

## 15. Git behavior

**This implementation writes files only — no local commit, no remote
push.** `AgentCapability.CREATE_COMMIT` already exists in the enum (added
in Level 1.2) but is **not** granted to `CodegenAgent` in this task,
deliberately: the task explicitly said not to add a large Git abstraction
here, and plain file mutation is sufficient for Codegen's job (producing a
`CodeArtifact` for Testing to consume next). Committing — local or
otherwise — is left for a future level, if and when it's actually needed.

## 16. Future Testing Agent integration

`CodegenOutput.tests_to_run` is already a first-class field, populated by
the model — this is the seam a future Testing Agent consumes: it reads the
`CodeArtifact` (parent-linked to the `ArchitectureArtifact` it implements),
inspects `tests_to_run` for what to run, and produces its own
`ArtifactType.TEST_RESULT` artifact with `parent_artifact_ids=[code_artifact_id]`,
continuing the same lineage chain. Not built in this task.

## Deferred / documented gaps (consistent with Planning/Architecture)

- No knowledge-query-to-artifact lineage field exists on `Artifact` yet
  (same gap Planning/Architecture already carry — deferred, not
  redesigned, per explicit instruction).
- Two RBAC vocabularies (`Permission`/`AgentCapability`) still coexist;
  Codegen doesn't add a third, but doesn't resolve the existing two either
  — it has no legacy `Permission`-based predecessor to bridge against, so
  it uses `AgentCapability` exclusively (arguably the cleanest of the three
  migrated agents on this specific point, simply by not having legacy
  baggage to carry).
- `write_file`'s whole-tree `_snapshot()` scan is O(files in repo) on every
  Codegen run; fine for this task's scope, worth revisiting if repos get
  large enough for it to matter.
