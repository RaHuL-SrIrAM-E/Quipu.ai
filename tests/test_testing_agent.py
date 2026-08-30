"""Tests for the Testing Agent (Level 1.8).

No real Gemini/ADK model call, no real knowledge backend, no real Firestore.
run_tests security/execution tests use a real temp pytest project — the
safety boundary and evidence-first behavior must be exercised for real, not
mocked away.
"""

import asyncio
import json
from pathlib import Path

import pytest
from google.genai import types
from pydantic import ValidationError

from app.agent_runtime.capabilities import AgentCapability
from app.agent_runtime.context import AgentContext
from app.agent_runtime.status import AgentStatus
from app.agents.codegen import CodegenOutput
from app.agents.testing import FailureClassification, TestingAgent, TestingOutput, TestStatus, _testing_llm_agent
from app.domain import AgentInput, Artifact, ArtifactType, KnowledgeItem, KnowledgeRequest, KnowledgeType, WorkflowStatus
from app.persistence.memory import InMemoryAgentExecutionRepository
from app.tools.testing_tools import run_tests

VALID_CODEGEN = CodegenOutput(
    summary="Implemented ThemeProvider.",
    created_files=["src/theme.py"],
    changes=[{"path": "src/theme.py", "change_type": "created", "description": "theme provider"}],
)

VALID_TESTING_PASS = {
    "summary": "All targeted tests pass.",
    "overall_status": "passed",
    "test_strategy": "targeted tests for theme module",
    "targeted_tests": ["test_theme.py"],
    "regression_tests": [],
    "failures": [],
    "environment_errors": [],
    "coverage_summary": "",
    "recommendations": [],
}

def make_code_artifact(workflow_id="wf-1", **overrides) -> Artifact:
    defaults = dict(artifact_type=ArtifactType.CODE_CHANGE, created_by="codegen_agent", payload=VALID_CODEGEN.model_dump(mode="json"))
    defaults.update(overrides)
    return Artifact(**defaults)


def make_pytest_project(root: Path, passing: bool = True) -> None:
    (root / "requirements.txt").write_text("pytest\n")
    (root / "test_theme.py").write_text(
        "def test_theme():\n    assert True\n" if passing else "def test_theme():\n    assert False\n"
    )


# ---- fakes ------------------------------------------------------------------


class FakeArtifactGateway:
    def __init__(self):
        self.saved: dict[tuple[str, str], Artifact] = {}

    def seed(self, workflow_id: str, artifact: Artifact) -> None:
        self.saved[(workflow_id, artifact.artifact_id)] = artifact

    async def get(self, workflow_id, artifact_id):
        return self.saved.get((workflow_id, artifact_id))

    async def save(self, workflow_id, artifact):
        self.saved[(workflow_id, artifact.artifact_id)] = artifact
        return artifact


class FakeToolGateway:
    async def execute(self, request):
        raise NotImplementedError


class FakeKnowledgeGateway:
    def __init__(self, items=None):
        self._items = items or []
        self.last_request: KnowledgeRequest | None = None

    async def search(self, request: KnowledgeRequest) -> list[KnowledgeItem]:
        self.last_request = request
        return self._items


class _FakeEvent:
    def __init__(self, text):
        self.content = types.Content(role="model", parts=[types.Part(text=text)])

    def is_final_response(self):
        return True


class _FakeSession:
    id = "session-1"


class _FakeSessionService:
    async def create_session(self, **kwargs):
        return _FakeSession()


class _CapturingSessionService:
    """Captures the real session_state dict TestingAgent._perform() built and
    passed to create_session — the SAME object reference, not a copy — so a
    tool call made against it (via _FakeToolContext(self.captured_state))
    actually mutates what _perform() reads back afterward."""

    def __init__(self):
        self.captured_state: dict = {}

    async def create_session(self, **kwargs):
        self.captured_state = kwargs.get("state", {})
        return _FakeSession()


def make_fake_runner_executing_tests(final_text: str, mode: str = "targeted", test_paths=("test_theme.py",), markers=()):
    """Simulates the model calling run_tests (for real, against whatever
    workspace_path is in the captured session state) once, then returning
    final_text as its structured output."""

    class _FakeRunner:
        def __init__(self, agent, app_name):
            self.session_service = _CapturingSessionService()

        def run_async(self, **kwargs):
            async def _events():
                ctx = _FakeToolContext(self.session_service.captured_state)
                run_tests(mode, list(test_paths), list(markers), ctx)
                yield _FakeEvent(final_text)

            return _events()

    return _FakeRunner


def make_fake_runner_no_tests(final_text: str):
    """Simulates the model returning structured output WITHOUT ever calling
    run_tests (i.e. session_state["_test_executions"] stays empty)."""

    async def _events(**kwargs):
        yield _FakeEvent(final_text)

    class _FakeRunner:
        def __init__(self, agent, app_name):
            self.session_service = _FakeSessionService()

        def run_async(self, **kwargs):
            return _events(**kwargs)

    return _FakeRunner


def make_fake_runner_raising(exc: Exception):
    async def _events(**kwargs):
        raise exc
        yield  # pragma: no cover

    class _FakeRunner:
        def __init__(self, agent, app_name):
            self.session_service = _FakeSessionService()

        def run_async(self, **kwargs):
            return _events(**kwargs)

    return _FakeRunner


def make_agent_input(**overrides) -> AgentInput:
    from app.domain import Ticket

    defaults = dict(
        workflow_id="wf-1",
        agent_name="testing_agent",
        ticket=Ticket(title="Add dark mode", description="Users want a dark theme toggle."),
        artifact_ids=["code-1"],
    )
    defaults.update(overrides)
    return AgentInput(**defaults)


def make_agent_input_with_workspace(workspace: Path, **overrides) -> AgentInput:
    overrides.setdefault("context", {})
    overrides["context"] = {**overrides["context"], "workspace_path": str(workspace)}
    return make_agent_input(**overrides)


def make_context(workspace: Path, **overrides) -> AgentContext:
    gateway = overrides.pop("artifacts", None) or FakeArtifactGateway()
    defaults = dict(
        workflow_id="wf-1",
        execution_id="exec-1",
        knowledge=FakeKnowledgeGateway(),
        tools=FakeToolGateway(),
        artifacts=gateway,
        executions=InMemoryAgentExecutionRepository(),
    )
    defaults.update(overrides)
    return AgentContext(**defaults)


def make_context_with_code(code_artifact_id="code-1", **overrides) -> AgentContext:
    gateway = FakeArtifactGateway()
    gateway.seed("wf-1", make_code_artifact(artifact_id=code_artifact_id))
    workspace = overrides.pop("workspace")
    return make_context(workspace, artifacts=gateway, **overrides)


class _FakeToolContext:
    def __init__(self, state):
        self.state = state


# ---- Runtime ------------------------------------------------------------------


def test_testing_agent_identity():
    agent = TestingAgent()
    assert agent.identity.agent_id == "testing_agent"


def test_testing_agent_expected_capabilities():
    agent = TestingAgent()
    assert agent.capabilities == {
        AgentCapability.READ_ARTIFACT,
        AgentCapability.QUERY_KNOWLEDGE,
        AgentCapability.READ_REPOSITORY,
        AgentCapability.RUN_TESTS,
        AgentCapability.WRITE_ARTIFACT,
    }
    forbidden = {AgentCapability.WRITE_CODE, AgentCapability.DEPLOY, AgentCapability.WRITE_JIRA, AgentCapability.RESOLVE_INCIDENT}
    assert agent.capabilities.isdisjoint(forbidden)


@pytest.mark.asyncio
async def test_lifecycle(monkeypatch, tmp_path: Path):
    make_pytest_project(tmp_path, passing=True)

    monkeypatch.setattr("app.agents.testing.InMemoryRunner", make_fake_runner_executing_tests(json.dumps(VALID_TESTING_PASS)))

    agent = TestingAgent()
    assert agent.status == AgentStatus.CREATED
    output = await agent.execute(make_agent_input_with_workspace(tmp_path), make_context_with_code(workspace=tmp_path))
    assert agent.status == AgentStatus.COMPLETED
    assert output.status == WorkflowStatus.COMPLETED


@pytest.mark.asyncio
async def test_failure_lifecycle(monkeypatch, tmp_path: Path):
    monkeypatch.setattr("app.agents.testing.InMemoryRunner", make_fake_runner_raising(RuntimeError("gemini down")))

    agent = TestingAgent()
    output = await agent.execute(make_agent_input_with_workspace(tmp_path), make_context_with_code(workspace=tmp_path))
    assert agent.status == AgentStatus.COMPLETED
    assert output.status == WorkflowStatus.FAILED
    assert output.errors[0].code == "TESTING_LLM_FAILURE"


# ---- Timeout budget (testing_llm_call_timeout_seconds, separate from the
# shared llm_call_timeout_seconds Planning/Architecture use) --------------


def make_slow_fake_runner(delay_seconds: float, final_text: str):
    """A fake ADK runner whose single turn takes real wall-clock time
    before yielding — the only way to genuinely exercise with_timeout's
    asyncio.wait_for bound rather than a canned instantaneous response."""

    async def _events(**kwargs):
        await asyncio.sleep(delay_seconds)
        yield _FakeEvent(final_text)

    class _FakeRunner:
        def __init__(self, agent, app_name):
            self.session_service = _FakeSessionService()

        def run_async(self, **kwargs):
            return _events(**kwargs)

    return _FakeRunner


@pytest.mark.asyncio
async def test_testing_timeout_uses_dedicated_setting_not_shared(monkeypatch, tmp_path: Path):
    """testing_llm_call_timeout_seconds governs Testing's timeout even when
    the shared llm_call_timeout_seconds is left large — proving Testing
    reads its own setting, not the one Planning/Architecture use.

    Patches app.agents.testing's own module-level `settings` object
    directly, NOT a fresh get_settings() call — other test modules call
    get_settings.cache_clear(), which would otherwise return a different
    Settings instance than the one testing.py captured at import time and
    silently no-op this test's monkeypatch when run as part of the full
    suite (see tests/test_codegen_agent.py for the identical issue)."""
    import app.agents.testing as testing_module

    monkeypatch.setattr(testing_module.settings, "testing_llm_call_timeout_seconds", 0.05)
    monkeypatch.setattr(testing_module.settings, "llm_call_timeout_seconds", 60.0)
    monkeypatch.setattr(
        "app.agents.testing.InMemoryRunner", make_slow_fake_runner(delay_seconds=0.5, final_text=json.dumps(VALID_TESTING_PASS))
    )

    agent = TestingAgent()
    output = await agent.execute(make_agent_input_with_workspace(tmp_path), make_context_with_code(workspace=tmp_path))

    assert output.status == WorkflowStatus.FAILED
    assert output.errors[0].code == "TESTING_LLM_FAILURE"
    assert "did not complete within 0.05" in output.errors[0].message


@pytest.mark.asyncio
async def test_testing_ignores_shared_llm_call_timeout(monkeypatch, tmp_path: Path):
    """The inverse: shrinking the SHARED setting alone must not affect
    Testing — its own dedicated setting is what's actually consulted."""
    import app.agents.testing as testing_module

    monkeypatch.setattr(testing_module.settings, "llm_call_timeout_seconds", 0.05)
    monkeypatch.setattr(testing_module.settings, "testing_llm_call_timeout_seconds", 5.0)
    monkeypatch.setattr(
        "app.agents.testing.InMemoryRunner", make_slow_fake_runner(delay_seconds=0.2, final_text=json.dumps(VALID_TESTING_PASS))
    )

    agent = TestingAgent()
    output = await agent.execute(make_agent_input_with_workspace(tmp_path), make_context_with_code(workspace=tmp_path))

    # A slow-but-within-budget response with no run_tests call still fails
    # (evidence-first — NO_TESTS_EXECUTED), but it must NOT fail on the
    # timeout: proves the dedicated setting, not the shrunk shared one, is
    # what governed how long Testing was allowed to run.
    assert output.status == WorkflowStatus.FAILED
    assert output.errors[0].code == "NO_TESTS_EXECUTED"


@pytest.mark.asyncio
async def test_testing_dedicated_timeout_defaults_to_150_seconds():
    from app.config import get_settings

    assert get_settings().testing_llm_call_timeout_seconds == 150.0


# ---- Input artifact -----------------------------------------------------------


@pytest.mark.asyncio
async def test_code_artifact_loaded_through_gateway(monkeypatch, tmp_path: Path):
    make_pytest_project(tmp_path, passing=True)

    monkeypatch.setattr("app.agents.testing.InMemoryRunner", make_fake_runner_executing_tests(json.dumps(VALID_TESTING_PASS)))
    output = await TestingAgent().execute(make_agent_input_with_workspace(tmp_path), make_context_with_code(workspace=tmp_path))
    assert output.artifacts[0].parent_artifact_ids == ["code-1"]


@pytest.mark.asyncio
async def test_missing_artifact_rejected(tmp_path: Path):
    agent = TestingAgent()
    output = await agent.execute(make_agent_input_with_workspace(tmp_path), make_context(tmp_path))
    assert output.status == WorkflowStatus.FAILED
    assert output.errors[0].code == "CODE_ARTIFACT_MISSING"


@pytest.mark.asyncio
async def test_wrong_artifact_type_rejected(tmp_path: Path):
    gateway = FakeArtifactGateway()
    gateway.seed("wf-1", Artifact(artifact_id="code-1", artifact_type=ArtifactType.PLAN, created_by="x", payload={}))
    agent = TestingAgent()
    output = await agent.execute(make_agent_input_with_workspace(tmp_path), make_context(tmp_path, artifacts=gateway))
    assert output.status == WorkflowStatus.FAILED
    assert output.errors[0].code == "CODE_ARTIFACT_WRONG_TYPE"


@pytest.mark.asyncio
async def test_invalid_codegen_output_rejected(tmp_path: Path):
    gateway = FakeArtifactGateway()
    gateway.seed(
        "wf-1", Artifact(artifact_id="code-1", artifact_type=ArtifactType.CODE_CHANGE, created_by="x", payload={"summary": ""})
    )
    agent = TestingAgent()
    output = await agent.execute(make_agent_input_with_workspace(tmp_path), make_context(tmp_path, artifacts=gateway))
    assert output.status == WorkflowStatus.FAILED
    assert output.errors[0].code == "CODEGEN_OUTPUT_INVALID"


# ---- Repository -----------------------------------------------------------


def test_changed_files_can_be_inspected(tmp_path: Path):
    from app.tools.repo_tools import read_file

    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "theme.py").write_text("class ThemeProvider: pass")
    ctx = _FakeToolContext({"workspace_path": str(tmp_path)})
    assert read_file("src/theme.py", ctx) == "class ThemeProvider: pass"


def test_existing_repository_tools_reused():
    tool_names = {t.__name__ for t in _testing_llm_agent.tools if callable(t)}
    assert {"search_files", "read_file", "search_code", "get_project_structure", "get_dependencies"} <= tool_names


# ---- Knowledge ------------------------------------------------------------


def test_knowledge_tool_available():
    tool_names = {t.__name__ for t in _testing_llm_agent.tools if callable(t)}
    assert "query_enterprise_knowledge" in tool_names


@pytest.mark.asyncio
async def test_testing_retrieval_profile_used():
    from app.knowledge.policies import get_retrieval_policy
    from app.tools.knowledge_tools import query_enterprise_knowledge

    gateway = FakeKnowledgeGateway(
        items=[
            KnowledgeItem(
                document_id="doc-1",
                title="Regression policy",
                content="run full suite for auth changes",
                knowledge_type=KnowledgeType.TESTING_STANDARD,
                source="wiki",
            )
        ]
    )
    ctx = _FakeToolContext({"_knowledge_gateway": gateway, "workflow_id": "wf-1", "_agent_name": "testing_agent"})
    result = await query_enterprise_knowledge("regression policy", "testing_standard", ctx)

    assert len(result) == 1
    policy = get_retrieval_policy("testing_agent")
    assert gateway.last_request.agent_name == "testing_agent"
    assert gateway.last_request.knowledge_type in policy.allowed_knowledge_types


@pytest.mark.asyncio
async def test_knowledge_provenance_preserved():
    from app.tools.knowledge_tools import query_enterprise_knowledge

    item = KnowledgeItem(
        document_id="doc-5",
        title="Coverage requirement",
        content="80% minimum",
        knowledge_type=KnowledgeType.TESTING_STANDARD,
        source="policy://coverage",
        relevance_score=0.7,
    )
    gateway = FakeKnowledgeGateway(items=[item])
    ctx = _FakeToolContext({"_knowledge_gateway": gateway, "workflow_id": "wf-1", "_agent_name": "testing_agent"})
    result = await query_enterprise_knowledge("coverage", "testing_standard", ctx)
    assert result[0]["document_id"] == "doc-5"
    assert result[0]["source"] == "policy://coverage"


# ---- Execution --------------------------------------------------------------


def test_targeted_test_request_works(tmp_path: Path):
    make_pytest_project(tmp_path, passing=True)
    ctx = _FakeToolContext({"_capabilities": {AgentCapability.RUN_TESTS}, "workspace_path": str(tmp_path)})
    result = run_tests("targeted", ["test_theme.py"], [], ctx)
    assert result["success"] is True
    assert result["status"] == "passed"
    assert result["exit_code"] == 0


def test_regression_test_request_works(tmp_path: Path):
    make_pytest_project(tmp_path, passing=True)
    ctx = _FakeToolContext({"_capabilities": {AgentCapability.RUN_TESTS}, "workspace_path": str(tmp_path)})
    result = run_tests("regression", [], [], ctx)
    assert result["success"] is True
    assert result["tests_collected"] >= 1


def test_actual_exit_code_captured(tmp_path: Path):
    make_pytest_project(tmp_path, passing=False)
    ctx = _FakeToolContext({"_capabilities": {AgentCapability.RUN_TESTS}, "workspace_path": str(tmp_path)})
    result = run_tests("targeted", ["test_theme.py"], [], ctx)
    assert result["exit_code"] == 1
    assert result["status"] == "failed"


def test_stdout_stderr_captured(tmp_path: Path):
    make_pytest_project(tmp_path, passing=True)
    ctx = _FakeToolContext({"_capabilities": {AgentCapability.RUN_TESTS}, "workspace_path": str(tmp_path)})
    result = run_tests("targeted", ["test_theme.py"], [], ctx)
    assert "test_theme" in result["stdout"] or "1 passed" in result["stdout"]


def test_test_counts_captured(tmp_path: Path):
    make_pytest_project(tmp_path, passing=True)
    ctx = _FakeToolContext({"_capabilities": {AgentCapability.RUN_TESTS}, "workspace_path": str(tmp_path)})
    result = run_tests("targeted", ["test_theme.py"], [], ctx)
    assert result["tests_passed"] == 1
    assert result["tests_failed"] == 0


def test_duration_captured(tmp_path: Path):
    make_pytest_project(tmp_path, passing=True)
    ctx = _FakeToolContext({"_capabilities": {AgentCapability.RUN_TESTS}, "workspace_path": str(tmp_path)})
    result = run_tests("targeted", ["test_theme.py"], [], ctx)
    assert result["duration_seconds"] >= 0.0


def test_timeout_produces_structured_failure(tmp_path: Path, monkeypatch):
    make_pytest_project(tmp_path, passing=True)

    from app.config import get_settings

    fake_settings = get_settings().model_copy(update={"test_execution_timeout_seconds": 0.0001})
    monkeypatch.setattr("app.tools.testing_tools.get_settings", lambda: fake_settings)

    ctx = _FakeToolContext({"_capabilities": {AgentCapability.RUN_TESTS}, "workspace_path": str(tmp_path)})
    result = run_tests("targeted", ["test_theme.py"], [], ctx)
    assert result["success"] is False
    assert result["status"] == "error"
    assert "timed out" in result["error"]


# ---- Security ---------------------------------------------------------------


def test_run_tests_requires_capability(tmp_path: Path):
    make_pytest_project(tmp_path, passing=True)
    ctx = _FakeToolContext({"_capabilities": set(), "workspace_path": str(tmp_path)})
    result = run_tests("targeted", ["test_theme.py"], [], ctx)
    assert result["success"] is False
    assert "RUN_TESTS" in result["error"]


def test_no_shell_command_channel_exists():
    import inspect

    params = list(inspect.signature(run_tests).parameters)
    assert "command" not in params
    assert "shell" not in params
    assert set(params) == {"mode", "test_paths", "markers", "tool_context"}


def test_dangerous_marker_rejected(tmp_path: Path):
    make_pytest_project(tmp_path, passing=True)
    ctx = _FakeToolContext({"_capabilities": {AgentCapability.RUN_TESTS}, "workspace_path": str(tmp_path)})
    result = run_tests("targeted", ["test_theme.py"], ["slow; rm -rf /"], ctx)
    assert result["success"] is False


def test_path_traversal_rejected(tmp_path: Path):
    make_pytest_project(tmp_path, passing=True)
    ctx = _FakeToolContext({"_capabilities": {AgentCapability.RUN_TESTS}, "workspace_path": str(tmp_path)})
    result = run_tests("targeted", ["../../etc/passwd"], [], ctx)
    assert result["success"] is False


def test_absolute_test_path_rejected(tmp_path: Path):
    make_pytest_project(tmp_path, passing=True)
    ctx = _FakeToolContext({"_capabilities": {AgentCapability.RUN_TESTS}, "workspace_path": str(tmp_path)})
    result = run_tests("targeted", ["/etc/passwd"], [], ctx)
    assert result["success"] is False
    assert "absolute" in result["error"]


def test_no_arbitrary_working_directory_parameter():
    import inspect

    assert "cwd" not in inspect.signature(run_tests).parameters
    assert "working_directory" not in inspect.signature(run_tests).parameters


# ---- Evidence-first behavior --------------------------------------------------


@pytest.mark.asyncio
async def test_passing_execution_produces_passed(monkeypatch, tmp_path: Path):
    make_pytest_project(tmp_path, passing=True)

    monkeypatch.setattr("app.agents.testing.InMemoryRunner", make_fake_runner_executing_tests(json.dumps(VALID_TESTING_PASS)))
    output = await TestingAgent().execute(make_agent_input_with_workspace(tmp_path), make_context_with_code(workspace=tmp_path))
    assert output.artifacts[0].payload["overall_status"] == "passed"


@pytest.mark.asyncio
async def test_failing_execution_produces_failed(monkeypatch, tmp_path: Path):
    make_pytest_project(tmp_path, passing=False)

    monkeypatch.setattr("app.agents.testing.InMemoryRunner", make_fake_runner_executing_tests(json.dumps(VALID_TESTING_PASS)))
    output = await TestingAgent().execute(make_agent_input_with_workspace(tmp_path), make_context_with_code(workspace=tmp_path))
    assert output.artifacts[0].payload["overall_status"] == "failed"


@pytest.mark.asyncio
async def test_model_claiming_pass_cannot_override_actual_failed(monkeypatch, tmp_path: Path):
    """The model's structured output explicitly says overall_status=passed,
    but the real run_tests execution failed — ground truth must win."""
    make_pytest_project(tmp_path, passing=False)
    model_claims_pass = json.dumps({**VALID_TESTING_PASS, "overall_status": "passed"})

    monkeypatch.setattr("app.agents.testing.InMemoryRunner", make_fake_runner_executing_tests(model_claims_pass))
    output = await TestingAgent().execute(make_agent_input_with_workspace(tmp_path), make_context_with_code(workspace=tmp_path))
    assert output.artifacts[0].payload["overall_status"] == "failed"


@pytest.mark.asyncio
async def test_model_claiming_fail_cannot_override_actual_passed(monkeypatch, tmp_path: Path):
    make_pytest_project(tmp_path, passing=True)
    model_claims_fail = json.dumps({**VALID_TESTING_PASS, "overall_status": "failed"})

    monkeypatch.setattr("app.agents.testing.InMemoryRunner", make_fake_runner_executing_tests(model_claims_fail))
    output = await TestingAgent().execute(make_agent_input_with_workspace(tmp_path), make_context_with_code(workspace=tmp_path))
    assert output.artifacts[0].payload["overall_status"] == "passed"


@pytest.mark.asyncio
async def test_no_execution_at_all_is_rejected(monkeypatch, tmp_path: Path):
    monkeypatch.setattr("app.agents.testing.InMemoryRunner", make_fake_runner_no_tests(json.dumps(VALID_TESTING_PASS)))
    output = await TestingAgent().execute(make_agent_input_with_workspace(tmp_path), make_context_with_code(workspace=tmp_path))
    assert output.status == WorkflowStatus.FAILED
    assert output.errors[0].code == "NO_TESTS_EXECUTED"


@pytest.mark.asyncio
async def test_raw_execution_evidence_preserved(monkeypatch, tmp_path: Path):
    make_pytest_project(tmp_path, passing=True)

    monkeypatch.setattr("app.agents.testing.InMemoryRunner", make_fake_runner_executing_tests(json.dumps(VALID_TESTING_PASS)))
    output = await TestingAgent().execute(make_agent_input_with_workspace(tmp_path), make_context_with_code(workspace=tmp_path))
    raw = output.artifacts[0].payload["raw_test_executions"]
    assert len(raw) == 1
    assert raw[0]["exit_code"] == 0


# ---- Failure classification --------------------------------------------------


def test_code_defect_classification():
    failure = {"test_name": "test_auth", "classification": "code_defect", "details": "AssertionError"}
    from app.agents.testing import TestFailure

    assert TestFailure(**failure).classification == FailureClassification.CODE_DEFECT


def test_test_defect_classification():
    from app.agents.testing import TestFailure

    failure = TestFailure(test_name="test_x", classification="test_defect")
    assert failure.classification == FailureClassification.TEST_DEFECT


def test_environment_failure_classification():
    from app.agents.testing import TestFailure

    failure = TestFailure(test_name="test_x", classification="environment_failure")
    assert failure.classification == FailureClassification.ENVIRONMENT_FAILURE


def test_dependency_failure_classification():
    from app.agents.testing import TestFailure

    failure = TestFailure(test_name="test_x", classification="dependency_failure")
    assert failure.classification == FailureClassification.DEPENDENCY_FAILURE


def test_unknown_classification():
    from app.agents.testing import TestFailure

    failure = TestFailure(test_name="test_x", classification="unknown")
    assert failure.classification == FailureClassification.UNKNOWN


def test_invalid_classification_rejected():
    from app.agents.testing import TestFailure

    with pytest.raises(ValidationError):
        TestFailure(test_name="test_x", classification="not_a_real_category")


# ---- Output/artifact ----------------------------------------------------------


def test_testing_output_validates():
    output = TestingOutput(**VALID_TESTING_PASS)
    assert output.overall_status == TestStatus.PASSED
    with pytest.raises(ValidationError):
        TestingOutput(**{**VALID_TESTING_PASS, "summary": ""})
    with pytest.raises(ValidationError):
        TestingOutput(**{**VALID_TESTING_PASS, "overall_status": "bogus"})


@pytest.mark.asyncio
async def test_test_artifact_created(monkeypatch, tmp_path: Path):
    make_pytest_project(tmp_path, passing=True)

    monkeypatch.setattr("app.agents.testing.InMemoryRunner", make_fake_runner_executing_tests(json.dumps(VALID_TESTING_PASS)))
    output = await TestingAgent().execute(make_agent_input_with_workspace(tmp_path), make_context_with_code(workspace=tmp_path))
    assert output.artifacts[0].artifact_type == ArtifactType.TEST_RESULT


def test_test_artifact_references_code_artifact():
    artifact = Artifact(
        artifact_type=ArtifactType.TEST_RESULT, created_by="testing_agent", parent_artifact_ids=["code-1"], payload=VALID_TESTING_PASS
    )
    assert artifact.parent_artifact_ids == ["code-1"]


@pytest.mark.asyncio
async def test_actual_results_represented_in_artifact(monkeypatch, tmp_path: Path):
    make_pytest_project(tmp_path, passing=True)

    monkeypatch.setattr("app.agents.testing.InMemoryRunner", make_fake_runner_executing_tests(json.dumps(VALID_TESTING_PASS)))
    output = await TestingAgent().execute(make_agent_input_with_workspace(tmp_path), make_context_with_code(workspace=tmp_path))
    payload = output.artifacts[0].payload
    assert payload["raw_test_executions"][0]["tests_passed"] == 1


@pytest.mark.asyncio
async def test_artifact_gateway_used(monkeypatch, tmp_path: Path):
    make_pytest_project(tmp_path, passing=True)
    gateway = FakeArtifactGateway()
    gateway.seed("wf-1", make_code_artifact(artifact_id="code-1"))

    monkeypatch.setattr("app.agents.testing.InMemoryRunner", make_fake_runner_executing_tests(json.dumps(VALID_TESTING_PASS)))
    output = await TestingAgent().execute(make_agent_input_with_workspace(tmp_path), make_context(tmp_path, artifacts=gateway))
    artifact_id = output.artifacts[0].artifact_id
    assert gateway.saved[("wf-1", artifact_id)] is not None


# ---- Execution/metrics --------------------------------------------------------


@pytest.mark.asyncio
async def test_agent_execution_references_artifacts(monkeypatch, tmp_path: Path):
    make_pytest_project(tmp_path, passing=True)
    executions = InMemoryAgentExecutionRepository()

    monkeypatch.setattr("app.agents.testing.InMemoryRunner", make_fake_runner_executing_tests(json.dumps(VALID_TESTING_PASS)))
    output = await TestingAgent().execute(
        make_agent_input_with_workspace(tmp_path, execution_id="exec-99"),
        make_context_with_code(workspace=tmp_path, executions=executions, execution_id="exec-99"),
    )
    execution = await executions.get("wf-1", "exec-99")
    assert execution.status == WorkflowStatus.COMPLETED
    assert execution.output_artifact_ids == [output.artifacts[0].artifact_id]


@pytest.mark.asyncio
async def test_metrics_captured(monkeypatch, tmp_path: Path):
    make_pytest_project(tmp_path, passing=True)

    monkeypatch.setattr("app.agents.testing.InMemoryRunner", make_fake_runner_executing_tests(json.dumps(VALID_TESTING_PASS)))
    output = await TestingAgent().execute(
        make_agent_input_with_workspace(tmp_path, execution_id="exec-metrics"), make_context_with_code(workspace=tmp_path)
    )
    assert output.metrics is not None
    assert output.metrics.execution_id == "exec-metrics"


# ---- ADK --------------------------------------------------------------------


def test_internal_llm_agent_uses_gemini():
    from app.config import get_settings

    assert _testing_llm_agent.model == get_settings().gemini_model


def test_internal_llm_agent_uses_structured_testing_output():
    assert _testing_llm_agent.output_schema is TestingOutput


def test_capability_enforcement_at_adk_tool_boundary():
    from app.agent_runtime.capabilities import CapabilityError
    from app.agents.planning import _tool_capability_gate

    class _FakeTool:
        name = "run_tests"

    class _FakeToolCtx:
        state = {"_capabilities": set()}

    with pytest.raises(CapabilityError):
        _tool_capability_gate(_FakeTool(), {}, _FakeToolCtx())


# ---- Regression ---------------------------------------------------------------


def test_existing_app_and_legacy_pipeline_still_import():
    from app.main import app  # noqa: F401
    from app.orchestrator.pipeline import quipu_pipeline

    assert [a.name for a in quipu_pipeline.sub_agents] == ["feature_detection", "planning", "architecture"]
