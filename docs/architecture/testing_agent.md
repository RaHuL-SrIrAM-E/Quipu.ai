# Testing Agent (Level 1.8)

## Diagram

```
CodeArtifact
      |
  TestingAgent
   +-------+--------+
   |                |
Repository        Knowledge
  Tools               |
   |             Agent Search
   +-------+--------+
           |
   ADK LlmAgent / Gemini
           |
       Test Strategy          (targeted vs. regression — model recommends)
           |
   Controlled Test Runner     (run_tests: capability-gated, argv-list only, no shell)
           |
   Actual Test Evidence       (exit_code, stdout/stderr, counts — facts)
           |
     Gemini Analysis          (classification, narrative — interpretation)
           |
     TestingOutput            (overall_status OVERWRITTEN with ground truth)
           |
      TestArtifact
```

**Execution facts vs. LLM interpretation** is the core distinction this
whole design turns on: everything below the "Controlled Test Runner" box is
an unforgeable fact, produced by a real `subprocess.run()`. Everything above
it is Gemini's opinion about those facts — useful for classification and
narrative, never authoritative for pass/fail. See §11.

No legacy predecessor exists for Testing — same situation as Codegen
(Level 1.7). Follows the same `QuipuAgent` + internal-ADK-adapter shape;
read `docs/architecture/codegen_agent.md` and `docs/architecture/planning_agent.md`
first for the shared mechanics this document doesn't repeat.

## 1. Responsibility

Determine and execute the appropriate tests against a `CodeArtifact`, then
analyze the actual evidence. Testing does not fix code (§16), does not
redesign architecture, does not deploy (§17), and never calls another
agent — it reports; the future orchestrator routes.

## 2. CodeArtifact input

Same pattern as Codegen consuming ArchitectureArtifact:
`AgentInput.artifact_ids[0]` → `ArtifactGateway.get` → type check
(`ArtifactType.CODE_CHANGE`) → `CodegenOutput.model_validate(payload)`.
Explicit failure codes: `CODE_ARTIFACT_MISSING`, `CODE_ARTIFACT_WRONG_TYPE`,
`CODEGEN_OUTPUT_INVALID` — all before any test runs.

## 3. Repository inspection

Unchanged `REPO_TOOLS`, reused directly (not duplicated), same as every
other migrated agent. Testing is instructed to inspect the changed files,
nearby tests, and test configuration before deciding what to run.

## 4. Test strategy

The model recommends a strategy (`test_strategy` field, free text — e.g.
"targeted tests for the theme module, given `src/theme.py` was created")
after inspecting the `CodeArtifact`'s changed files and the repo's existing
test layout. **The model's recommendation is not the enforcement
mechanism** — see §6/§14.

## 5. Targeted tests

`run_tests(mode="targeted", test_paths=[...])` — runs exactly the named
paths (validated: not absolute, resolved within the workspace via
`_safe_join`, reused from `repo_tools.py`).

## 6. Regression tests

`run_tests(mode="regression")` — runs the repo's whole configured suite
(`test_paths` ignored). The task explicitly forbids letting the model *skip*
required regression testing just because a change looks small; this
implementation's concrete enforcement of that principle is narrower but
real: **`TestingAgent._perform()` requires at least one `run_tests` call to
have actually happened** (`session_state["_test_executions"]` non-empty) —
a testing verdict produced without ever running anything fails outright
(`NO_TESTS_EXECUTED`). A fuller *mandatory-regression-suite* policy engine
("always run the full suite for changes touching module X") does not exist
yet — no such policy exists anywhere in the repository today, and building
one is explicitly out of scope for this level. Documented as deferred, not
silently skipped.

## 7. Controlled test execution

`app/tools/testing_tools.py::run_tests` — the only way any Quipu agent runs
tests. Signature: `run_tests(mode, test_paths, markers, tool_context)`.
**There is no `command` or `shell` parameter — structurally, not just by
convention** (`test_no_shell_command_channel_exists` asserts the exact
parameter set). The model can request *what* to run; it can never construct
*how* the command is built — that's application code, always
`[sys.executable, "-m", "pytest", "-q", ...]`, built from a list (never a
shell string), so there is no injection surface even in principle.

## 8. RUN_TESTS capability

Reused `AgentCapability.RUN_TESTS`, which already existed in the enum
(added in Level 1.2) — the task asked to add `EXECUTE_TESTS` "if it does
not exist"; it already existed under a different name, so this migration
reuses it rather than introducing a duplicate capability meaning the same
thing. Enforced twice: inside `run_tests` itself
(`AgentCapability.RUN_TESTS not in granted` → rejected before any
subprocess runs), and via the shared `before_tool_callback`
(`_tool_capability_gate`, extended with `"run_tests":
AgentCapability.RUN_TESTS`) at the ADK tool-call boundary — same
belt-and-suspenders pattern as `WRITE_CODE` in Codegen.

## 9. Command/path security

- No shell metacharacter surface exists (§7) — commands are argv lists.
- `markers` are validated against `^[a-zA-Z0-9_ ]+$` — no `;`, `|`, `` ` ``,
  `$`, or any other shell-significant character can pass through, even
  though they'd be inert anyway (no shell involved) — this is defense in
  depth against a marker value being logged/displayed unsafely elsewhere.
- Test paths: absolute paths rejected outright; relative paths resolved via
  `_safe_join` (verified against real traversal payloads — see
  `docs/architecture/codegen_agent.md` §5/§8 for how that function was
  validated).
- **A real bug was found and fixed during this task**: both this tool and
  Codegen's `write_file` (Level 1.7) used `path.lstrip("./")` to strip a
  leading `"./"` — but `str.lstrip` strips a *set* of characters
  repeatedly, not a prefix string, so `"../../etc/passwd".lstrip("./")`
  silently produced `"etc/passwd"`, neutralizing the traversal characters
  *before* the safety check ever saw them. This did not enable an actual
  filesystem escape (the stripped result always resolved back inside the
  workspace), but it caused the tool to silently reinterpret a malicious or
  malformed path instead of rejecting it, defeating the intended
  fail-loudly behavior and the explicit test for it
  (`test_path_traversal_rejected` initially failed). Fixed in both files by
  switching to `str.removeprefix("./")`, which only removes an exact
  leading `"./"` once. Confirmed with a live traversal payload before and
  after the fix.
- Working directory is fixed to the resolved workspace root
  (`subprocess.run(argv, cwd=root, ...)`) — no `cwd`/`working_directory`
  parameter exists on `run_tests` at all (verified by
  `test_no_arbitrary_working_directory_parameter`).

## 10. Timeout

`app.config.Settings.test_execution_timeout_seconds` (default `120.0`,
externalized like every other tunable in `app.config`, not hardcoded).
`subprocess.run(..., timeout=timeout_seconds)` — a `TimeoutExpired` is
caught and converted into a structured `status="error"` result (not an
unhandled hang, not a crash), verified with a real `0.0001`-second timeout
against a real (fast) test run.

## 11. Evidence-first architecture — the critical property

`run_tests` appends every result (pass, fail, *and* error) to
`session_state["_test_executions"]` — a list, mutated in place, visible to
`TestingAgent._perform()` after the ADK run completes (it's the *same*
Python dict object passed into `create_session(state=session_state)`, so
in-place mutation during tool calls is visible without any extra plumbing).

After validating the model's `TestingOutput`, `_perform()` computes
`_ground_truth_status()` from `_test_executions` alone — **any** `"error"`
result wins over `"failed"`, which wins over `"passed"` — and **overwrites**
`testing_output.overall_status` with it via `model_copy(update=...)`,
regardless of what the model's own JSON said. Proven directly:
`test_model_claiming_pass_cannot_override_actual_failed` (real failing
tests, model's structured output explicitly claims `"passed"` — persisted
artifact still shows `"failed"`) and
`test_model_claiming_fail_cannot_override_actual_passed` (the reverse — a
real pass, model claims `"failed"`, persisted artifact still shows
`"passed"`).

## 12. Gemini + ADK

`_testing_llm_agent` — `output_schema=TestingOutput`, `model=
settings.gemini_model` (same Gemini configuration as every other agent),
tools = `REPO_TOOLS + KNOWLEDGE_TOOLS + TESTING_TOOLS`,
`after_model_callback=_track_usage_metrics` (imported from
`app.agents.planning`, fourth reuse now — never reimplemented per agent).

## 13. Failure classification

`FailureClassification`: `CODE_DEFECT`, `TEST_DEFECT`,
`ENVIRONMENT_FAILURE`, `DEPENDENCY_FAILURE`, `UNKNOWN`. The model assigns
these per-failure (`TestFailure.classification`) — this is exactly the kind
of judgment call evidence-first architecture *is* fine leaving to the LLM
(interpretation, not fact), constrained to a closed enum (invalid values
rejected by pydantic — `test_invalid_classification_rejected`). The raw
execution evidence (`raw_test_executions` in the artifact payload, §15)
stays attached regardless of how a failure was classified, so a future
orchestrator or human reviewer can always check the model's classification
against the actual `stdout`/`stderr`.

## 14. TestingOutput

```python
class TestingOutput(BaseModel):
    summary: str                    # non-empty
    overall_status: TestStatus      # OVERWRITTEN post-hoc — see §11
    test_strategy: str               # non-empty
    targeted_tests: list[str]
    regression_tests: list[str]
    failures: list[TestFailure]      # test_name, classification, details
    environment_errors: list[str]
    coverage_summary: str
    recommendations: list[str]
    execution_ids: list[str]
```

`TestStatus`: `PASSED`, `FAILED`, `ERROR`, `SKIPPED`, `NOT_RUN` — deliberately
distinguishes a real test failure from an infrastructure/environment
failure, per the task's explicit instruction not to collapse those two
into one state.

## 15. TestArtifact / execution-record gap

`ArtifactType.TEST_RESULT` (already existed — no new artifact type added).
`parent_artifact_ids=[code_artifact_id]`, completing
`PlanArtifact → ArchitectureArtifact → CodeArtifact → TestArtifact`.

No dedicated "test execution" domain object was introduced, per the task's
explicit instruction not to build a new persistence subsystem for this. The
raw `run_tests` results (`_test_executions`) are embedded directly in the
artifact payload as `raw_test_executions` — a list of the exact dicts
`run_tests` produced (command, exit_code, duration, stdout/stderr, counts)
— alongside the structured `TestingOutput` fields. **Documented gap**: this
means raw execution evidence is only independently queryable by loading the
whole `TestArtifact` payload, not through its own repository/collection the
way `AgentExecution` is. If per-execution querying (e.g. "show me every
`run_tests` invocation across all workflows") is ever needed, that's a
real, separate persistence concern for a future level — not solved here.

## 16. Execution/audit

Same `AgentExecution`/`AgentMetrics` pattern as every other migrated agent:
created `RUNNING`, updated to `COMPLETED`/`FAILED` with
`output_artifact_ids`/`error`. Combined with `Artifact.parent_artifact_ids`
lineage, "which agent tested this, based on which code change, which
architecture, which plan, and what was the actual result" is answerable
from persisted state alone.

## 17. Failure behavior

| Failure | Error code |
|---|---|
| No artifact_ids / artifact not found | `CODE_ARTIFACT_MISSING` |
| Wrong artifact type | `CODE_ARTIFACT_WRONG_TYPE` |
| CodegenOutput payload invalid | `CODEGEN_OUTPUT_INVALID` |
| No workspace checked out | `TESTING_WORKSPACE_MISSING` |
| Gemini/ADK/tool call fails | `TESTING_LLM_FAILURE` |
| Empty model response | `TESTING_EMPTY_RESPONSE` |
| TestingOutput doesn't validate | `TESTING_VALIDATION_FAILED` |
| No test execution ever happened | `NO_TESTS_EXECUTED` |
| Artifact save fails | `ARTIFACT_PERSISTENCE_FAILED` |

Testing never modifies application code and never deploys — there is no
code path in `TestingAgent`/`run_tests` that writes to the filesystem or
touches infrastructure at all; the only capability-gated action available
is running (read-only, from the repo's own perspective) tests. No
retry/replan logic exists here — a `CODE_DEFECT` classification is reported
in the `TestArtifact`, not acted on; routing it to `CodegenAgent` is the
future orchestrator's job (§18 of the task, explicitly deferred).

## 18. Future orchestrator integration

`TestFailure.classification` is exactly the signal a future orchestrator
needs to route: `CODE_DEFECT → CodegenAgent`, an architecture-level issue
→ `ArchitectureAgent`. Not implemented here — this level only produces the
classified evidence; deciding what to do with it is explicitly out of
scope, same as every prior agent migration.
