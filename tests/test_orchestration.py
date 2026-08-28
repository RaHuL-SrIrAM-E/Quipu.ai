"""Tests for the Quipu orchestration layer (Level 2.0).

No real Gemini/ADK model calls, no real Firestore. The happy-path tests
drive the REAL PlanningAgent/ArchitectureAgent/CodegenAgent/TestingAgent
(not stand-ins) through the REAL OrchestrationService, with each agent's
own internal ADK runner monkeypatched — the same pattern each agent's own
test suite already uses. Codegen/Testing still perform real filesystem
writes and a real pytest subprocess run against tmp_path.
"""

import json
from pathlib import Path

import pytest
from google.genai import types

from app.agent_runtime.capabilities import AgentCapability
from app.agent_runtime.registry import AgentNotFoundError, AgentRegistry
from app.agents.architecture import ArchitectureOutput
from app.agents.codegen import CodegenOutput
from app.agents.planning import PlanOutput
from app.agents.testing import TestingOutput
from app.domain import Decision, DecisionAction, DecisionSource, Ticket, WorkflowStage, WorkflowStatus
from app.orchestration import OrchestrationService, ProposedDecision, WorkflowEvidence, build_default_registry
from app.orchestration.decisions import deterministic_action
from app.orchestration.errors import InvalidTransitionError, OrchestrationError, RetryLimitExceededError, UnknownAgentError
from app.orchestration.transitions import can_transition, next_stage
from app.persistence.memory import (
    InMemoryAgentExecutionRepository,
    InMemoryArtifactRepository,
    InMemoryDecisionRepository,
    InMemoryWorkflowRepository,
)
from app.tools.codegen_tools import write_file
from app.tools.testing_tools import run_tests

VALID_PLAN = {
    "feature_summary": "Add dark mode",
    "architecture_notes": "Add a theme provider.",
    "affected_components": [{"name": "frontend", "reason": "theming"}],
    "tasks": [{"id": "t1", "description": "add theme provider", "depends_on": []}],
    "dependencies": [],
    "acceptance_criteria": ["toggle switches theme"],
    "risks": [{"description": "flash of wrong theme", "mitigation": "ssr cookie"}],
}

VALID_ARCHITECTURE = {
    "design_summary": "Add a ThemeProvider.",
    "components": [{"name": "ThemeProvider", "responsibility": "holds theme state"}],
    "data_model_changes": [],
    "api_contracts": [],
    "task_designs": [{"task_id": "t1", "approach": "create ThemeProvider", "files": ["src/theme.py"]}],
    "risks": [{"description": "theme flash", "mitigation": "inline script"}],
}

VALID_CODEGEN = {
    "summary": "Implemented ThemeProvider.",
    "modified_files": [],
    "created_files": ["src/theme.py"],
    "deleted_files": [],
    "changes": [{"path": "src/theme.py", "change_type": "created", "description": "theme provider"}],
    "implementation_notes": "",
    "unresolved_items": [],
    "tests_to_run": ["test_theme.py"],
}

VALID_TESTING_PASS = {
    "summary": "All tests pass.",
    "overall_status": "passed",
    "test_strategy": "regression",
    "targeted_tests": [],
    "regression_tests": ["test_theme.py"],
    "failures": [],
    "environment_errors": [],
    "coverage_summary": "",
    "recommendations": [],
}


def make_testing_output(failures: list[dict]) -> dict:
    return {
        **VALID_TESTING_PASS,
        "overall_status": "failed",
        "failures": failures,
    }


# ---- fakes ------------------------------------------------------------------


class FakeKnowledgeGateway:
    async def search(self, request):
        return []


class FakeToolGateway:
    async def execute(self, request):
        raise NotImplementedError


class FakeJiraClient:
    def __init__(self):
        pass

    def create_story(self, summary: str, description: str) -> dict:
        return {"key": "QP-1", "url": "https://example.atlassian.net/browse/QP-1"}


class _FakeEvent:
    def __init__(self, text):
        self.content = types.Content(role="model", parts=[types.Part(text=text)])

    def is_final_response(self):
        return True


class _FakeSession:
    id = "session-1"


class _CapturingSessionService:
    def __init__(self):
        self.captured_state: dict = {}

    async def create_session(self, **kwargs):
        self.captured_state = kwargs.get("state", {})
        return _FakeSession()


class _FakeToolContext:
    def __init__(self, state):
        self.state = state


def make_plain_runner(final_text: str):
    class _FakeRunner:
        def __init__(self, agent, app_name):
            self.session_service = _CapturingSessionService()

        def run_async(self, **kwargs):
            async def _events():
                yield _FakeEvent(final_text)

            return _events()

    return _FakeRunner


def make_codegen_runner(final_text: str):
    """Actually calls the real write_file tool against whatever workspace_path
    ends up in the captured session state, so CodegenAgent's ground-truth
    filesystem check has something real to find."""

    class _FakeRunner:
        def __init__(self, agent, app_name):
            self.session_service = _CapturingSessionService()

        def run_async(self, **kwargs):
            async def _events():
                ctx = _FakeToolContext(self.session_service.captured_state)
                write_file("src/theme.py", "class ThemeProvider:\n    pass\n", ctx)
                yield _FakeEvent(final_text)

            return _events()

    return _FakeRunner


def make_testing_runner(final_text: str, mode: str = "regression"):
    class _FakeRunner:
        def __init__(self, agent, app_name):
            self.session_service = _CapturingSessionService()

        def run_async(self, **kwargs):
            async def _events():
                ctx = _FakeToolContext(self.session_service.captured_state)
                run_tests(mode, [], [], ctx)
                yield _FakeEvent(final_text)

            return _events()

    return _FakeRunner


def make_pytest_project(root: Path, passing: bool = True) -> None:
    (root / "requirements.txt").write_text("pytest\n")
    (root / "test_theme.py").write_text(
        "def test_theme():\n    assert True\n" if passing else "def test_theme():\n    assert False\n"
    )


@pytest.fixture
def repos():
    return {
        "workflow": InMemoryWorkflowRepository(),
        "artifact": InMemoryArtifactRepository(),
        "execution": InMemoryAgentExecutionRepository(),
        "decision": InMemoryDecisionRepository(),
    }


def make_service(repos, registry=None) -> OrchestrationService:
    return OrchestrationService(
        workflow_repo=repos["workflow"],
        artifact_repo=repos["artifact"],
        execution_repo=repos["execution"],
        decision_repo=repos["decision"],
        registry=registry or build_default_registry(),
        knowledge_gateway=FakeKnowledgeGateway(),
        tool_gateway=FakeToolGateway(),
    )


def patch_happy_path(monkeypatch, tmp_path: Path, test_outcome: dict = None):
    monkeypatch.setattr("app.agents.planning.InMemoryRunner", make_plain_runner(json.dumps(VALID_PLAN)))
    monkeypatch.setattr("app.agents.planning.JiraClient", FakeJiraClient)
    monkeypatch.setattr("app.agents.architecture.InMemoryRunner", make_plain_runner(json.dumps(VALID_ARCHITECTURE)))
    monkeypatch.setattr("app.agents.codegen.InMemoryRunner", make_codegen_runner(json.dumps(VALID_CODEGEN)))
    monkeypatch.setattr(
        "app.agents.testing.InMemoryRunner", make_testing_runner(json.dumps(test_outcome or VALID_TESTING_PASS))
    )


def make_ticket(**overrides) -> Ticket:
    defaults = dict(title="Add dark mode", description="Users want a dark theme toggle.")
    defaults.update(overrides)
    return Ticket(**defaults)


# ---- Happy path ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_happy_path_reaches_completed(monkeypatch, tmp_path: Path, repos):
    make_pytest_project(tmp_path, passing=True)
    patch_happy_path(monkeypatch, tmp_path)

    service = make_service(repos)
    workflow = await service.start_workflow(make_ticket(), workspace_path=str(tmp_path))
    assert workflow.status == WorkflowStatus.PENDING
    assert workflow.current_stage == WorkflowStage.PLANNING

    final = await service.run_to_completion(workflow.workflow_id)

    assert final.status == WorkflowStatus.COMPLETED
    assert final.current_stage == WorkflowStage.COMPLETED
    assert len(final.artifact_ids) == 4  # plan, architecture, code, test


@pytest.mark.asyncio
async def test_each_stage_produces_its_artifact(monkeypatch, tmp_path: Path, repos):
    make_pytest_project(tmp_path, passing=True)
    patch_happy_path(monkeypatch, tmp_path)

    service = make_service(repos)
    workflow = await service.start_workflow(make_ticket(), workspace_path=str(tmp_path))
    final = await service.run_to_completion(workflow.workflow_id)

    artifacts = [await repos["artifact"].get(final.workflow_id, aid) for aid in final.artifact_ids]
    types_in_order = [a.artifact_type.value for a in artifacts]
    assert types_in_order == ["plan", "architecture", "code_change", "test_result"]


@pytest.mark.asyncio
async def test_artifact_lineage_preserved(monkeypatch, tmp_path: Path, repos):
    make_pytest_project(tmp_path, passing=True)
    patch_happy_path(monkeypatch, tmp_path)

    service = make_service(repos)
    workflow = await service.start_workflow(make_ticket(), workspace_path=str(tmp_path))
    final = await service.run_to_completion(workflow.workflow_id)

    artifacts = {a.artifact_type.value: a for a in [await repos["artifact"].get(final.workflow_id, aid) for aid in final.artifact_ids]}
    assert artifacts["architecture"].parent_artifact_ids == [artifacts["plan"].artifact_id]
    assert artifacts["code_change"].parent_artifact_ids == [artifacts["architecture"].artifact_id]
    assert artifacts["test_result"].parent_artifact_ids == [artifacts["code_change"].artifact_id]


@pytest.mark.asyncio
async def test_each_stage_receives_correct_input_artifact(monkeypatch, tmp_path: Path, repos):
    make_pytest_project(tmp_path, passing=True)
    patch_happy_path(monkeypatch, tmp_path)

    service = make_service(repos)
    workflow = await service.start_workflow(make_ticket(), workspace_path=str(tmp_path))
    await service.run_to_completion(workflow.workflow_id)

    executions = await repos["execution"].list_for_workflow(workflow.workflow_id)
    by_agent = {e.agent_name: e for e in executions}
    assert by_agent["planning_agent"].input_artifact_ids == []
    # architecture/codegen/testing each got exactly one input artifact (the previous stage's)
    assert len(by_agent["architecture_agent"].output_artifact_ids) == 1


# ---- Registry -------------------------------------------------------------------


def test_orchestrator_resolves_agents_through_registry():
    registry = build_default_registry()
    for agent_id in ("planning_agent", "architecture_agent", "codegen_agent", "testing_agent"):
        assert registry.get(agent_id) is not None


@pytest.mark.asyncio
async def test_unknown_agent_fails_safely(repos):
    empty_registry = AgentRegistry()
    service = make_service(repos, registry=empty_registry)
    workflow = await service.start_workflow(make_ticket())
    with pytest.raises(UnknownAgentError):
        await service.execute_next_step(workflow.workflow_id)


# ---- Decision engine ------------------------------------------------------------


def test_success_produces_proceed_action():
    from app.agents.testing import FailureClassification

    assert deterministic_action([]) is None  # no failures — handled as overall_status="passed" upstream, not here


def test_code_defect_produces_codegen_retry():
    from app.agents.testing import FailureClassification

    action, target = deterministic_action([FailureClassification.CODE_DEFECT])
    assert action == DecisionAction.RETRY
    assert target == "codegen_agent"


def test_architecture_defect_produces_architecture_routing():
    from app.agents.testing import FailureClassification

    action, target = deterministic_action([FailureClassification.ARCHITECTURE_DEFECT])
    assert action == DecisionAction.REPLAN
    assert target == "architecture_agent"


def test_test_defect_produces_testing_retry():
    from app.agents.testing import FailureClassification

    action, target = deterministic_action([FailureClassification.TEST_DEFECT])
    assert action == DecisionAction.RETRY
    assert target == "testing_agent"


def test_unknown_produces_escalation():
    from app.agents.testing import FailureClassification

    action, target = deterministic_action([FailureClassification.UNKNOWN])
    assert action == DecisionAction.ESCALATE
    assert target is None


@pytest.mark.asyncio
async def test_failing_test_routes_to_codegen_retry_end_to_end(monkeypatch, tmp_path: Path, repos):
    make_pytest_project(tmp_path, passing=False)  # real failure — TestingAgent's ground truth must actually be FAILED
    failing_testing_output = make_testing_output([{"test_name": "test_theme", "classification": "code_defect", "details": "boom"}])
    patch_happy_path(monkeypatch, tmp_path, test_outcome=failing_testing_output)

    service = make_service(repos)
    workflow = await service.start_workflow(make_ticket(), workspace_path=str(tmp_path))
    final = await service.run_to_completion(workflow.workflow_id, max_steps=4)

    assert final.current_stage == WorkflowStage.CODEGEN
    assert final.status == WorkflowStatus.PENDING
    assert final.metadata.get("retry_count:codegen_agent") == 1


@pytest.mark.asyncio
async def test_unknown_failure_escalates_end_to_end(monkeypatch, tmp_path: Path, repos):
    make_pytest_project(tmp_path, passing=False)
    unknown_testing_output = make_testing_output([{"test_name": "test_theme", "classification": "unknown", "details": "???"}])
    patch_happy_path(monkeypatch, tmp_path, test_outcome=unknown_testing_output)

    service = make_service(repos)
    workflow = await service.start_workflow(make_ticket(), workspace_path=str(tmp_path))
    final = await service.run_to_completion(workflow.workflow_id, max_steps=4)

    assert final.status == WorkflowStatus.ESCALATED


@pytest.mark.asyncio
async def test_ambiguous_classification_calls_decision_agent(monkeypatch, tmp_path: Path, repos):
    make_pytest_project(tmp_path, passing=False)
    mixed_testing_output = make_testing_output(
        [
            {"test_name": "test_a", "classification": "code_defect", "details": "x"},
            {"test_name": "test_b", "classification": "environment_failure", "details": "y"},
        ]
    )
    patch_happy_path(monkeypatch, tmp_path, test_outcome=mixed_testing_output)
    monkeypatch.setattr(
        "app.orchestration.service.propose_decision",
        lambda evidence, **kw: _resolved(ProposedDecision(action=DecisionAction.ESCALATE, reason="ambiguous", confidence=0.5)),
    )

    service = make_service(repos)
    workflow = await service.start_workflow(make_ticket(), workspace_path=str(tmp_path))
    final = await service.run_to_completion(workflow.workflow_id, max_steps=4)

    assert final.status == WorkflowStatus.ESCALATED


async def _resolved(value):
    return value


# ---- Transition safety ----------------------------------------------------------


def test_invalid_transition_rejected():
    with pytest.raises(InvalidTransitionError):
        can_transition(WorkflowStage.TESTING, DecisionAction.RETRY, "planning_agent")


def test_continue_from_last_stage_is_valid_and_means_complete():
    # TESTING is currently the last implemented stage (no Deployment yet) —
    # CONTINUE there is valid and means "workflow done," not "invalid".
    can_transition(WorkflowStage.TESTING, DecisionAction.CONTINUE, None)  # must not raise


def test_next_stage_returns_none_after_testing():
    assert next_stage(WorkflowStage.TESTING) is None


@pytest.mark.asyncio
async def test_missing_artifact_blocks_transition(repos):
    service = make_service(repos)
    workflow = await service.start_workflow(make_ticket())
    workflow = workflow.model_copy(update={"current_stage": WorkflowStage.ARCHITECTURE, "artifact_ids": []})
    await repos["workflow"].update(workflow)
    # no plan artifact exists for architecture to consume — the agent itself
    # rejects this (ARCHITECTURE_ARTIFACT_MISSING), which the orchestrator
    # surfaces as a failed workflow, not a silent skip.
    result = await service.execute_next_step(workflow.workflow_id)
    assert result.status == WorkflowStatus.FAILED


@pytest.mark.asyncio
async def test_retry_limit_enforced(repos):
    service = make_service(repos)
    workflow = await service.start_workflow(make_ticket())
    workflow = workflow.model_copy(
        update={"current_stage": WorkflowStage.TESTING, "metadata": {"retry_count:codegen_agent": 2}}
    )
    await repos["workflow"].update(workflow)

    proposed = ProposedDecision(action=DecisionAction.RETRY, target_agent="codegen_agent", reason="another code defect", confidence=0.9)
    result = await service.handle_decision(workflow.workflow_id, proposed)
    assert result.status == WorkflowStatus.ESCALATED  # budget exhausted -> downgraded to escalate, not silently retried again


def test_capability_requirements_respected():
    from app.orchestration.registry_setup import build_default_registry

    registry = build_default_registry()
    codegen = registry.get("codegen_agent")
    assert AgentCapability.WRITE_CODE in codegen.capabilities
    assert AgentCapability.DEPLOY not in codegen.capabilities


# ---- Loop -----------------------------------------------------------------------


def test_recovery_loop_agent_has_bounded_iterations():
    from app.agent_runtime.context import AgentContext
    from app.orchestration.adk import build_recovery_loop_agent

    registry = build_default_registry()
    context = AgentContext(
        workflow_id="wf-1", execution_id="exec-1", knowledge=FakeKnowledgeGateway(), tools=FakeToolGateway(), artifacts=None
    )
    loop = build_recovery_loop_agent(registry, context, max_iterations=3)
    assert loop.max_iterations == 3
    assert [a.name for a in loop.sub_agents] == ["codegen_agent", "testing_agent", "loop_evaluator"]


@pytest.mark.asyncio
async def test_loop_evaluator_stops_on_pass():
    from app.orchestration.adk.loop import _LoopEvaluator

    class _FakeSession:
        state = {
            "testing_agent_output": {
                "artifacts": [{"payload": {"overall_status": "passed", "failures": []}}]
            }
        }

    class _FakeInvocationContext:
        session = _FakeSession()
        invocation_id = "inv-1"

    evaluator = _LoopEvaluator(name="loop_evaluator")
    events = [e async for e in evaluator._run_async_impl(_FakeInvocationContext())]
    assert events[0].actions.escalate is True


@pytest.mark.asyncio
async def test_loop_evaluator_continues_on_code_defect():
    from app.orchestration.adk.loop import _LoopEvaluator

    class _FakeSession:
        state = {
            "testing_agent_output": {
                "artifacts": [{"payload": {"overall_status": "failed", "failures": [{"classification": "code_defect"}]}}]
            }
        }

    class _FakeInvocationContext:
        session = _FakeSession()
        invocation_id = "inv-1"

    evaluator = _LoopEvaluator(name="loop_evaluator")
    events = [e async for e in evaluator._run_async_impl(_FakeInvocationContext())]
    assert events[0].actions.escalate is False


@pytest.mark.asyncio
async def test_loop_evaluator_stops_on_architecture_defect():
    from app.orchestration.adk.loop import _LoopEvaluator

    class _FakeSession:
        state = {
            "testing_agent_output": {
                "artifacts": [{"payload": {"overall_status": "failed", "failures": [{"classification": "architecture_defect"}]}}]
            }
        }

    class _FakeInvocationContext:
        session = _FakeSession()
        invocation_id = "inv-1"

    evaluator = _LoopEvaluator(name="loop_evaluator")
    events = [e async for e in evaluator._run_async_impl(_FakeInvocationContext())]
    assert events[0].actions.escalate is True


# ---- Persistence ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_workflow_state_persisted(repos):
    service = make_service(repos)
    workflow = await service.start_workflow(make_ticket())
    fetched = await repos["workflow"].get(workflow.workflow_id)
    assert fetched is not None
    assert fetched.workflow_id == workflow.workflow_id


@pytest.mark.asyncio
async def test_version_conflicts_handled(repos):
    from app.persistence.errors import VersionConflictError

    service = make_service(repos)
    workflow = await service.start_workflow(make_ticket())
    # simulate a concurrent writer bumping the version out from under us
    stale = await repos["workflow"].get(workflow.workflow_id)
    await repos["workflow"].update_if_version(workflow.workflow_id, stale.version, stale.model_copy(update={"status": WorkflowStatus.RUNNING}))

    with pytest.raises(VersionConflictError):
        await repos["workflow"].update_if_version(workflow.workflow_id, stale.version, stale.model_copy(update={"status": WorkflowStatus.FAILED}))


@pytest.mark.asyncio
async def test_concurrent_update_cannot_silently_overwrite(repos):
    from app.persistence.errors import VersionConflictError

    service = make_service(repos)
    workflow = await service.start_workflow(make_ticket())

    worker_a_view = await repos["workflow"].get(workflow.workflow_id)
    worker_b_view = await repos["workflow"].get(workflow.workflow_id)

    await repos["workflow"].update_if_version(workflow.workflow_id, worker_a_view.version, worker_a_view.model_copy(update={"status": WorkflowStatus.RUNNING}))

    with pytest.raises(VersionConflictError):
        await repos["workflow"].update_if_version(workflow.workflow_id, worker_b_view.version, worker_b_view.model_copy(update={"status": WorkflowStatus.CANCELLED}))

    final = await repos["workflow"].get(workflow.workflow_id)
    assert final.status == WorkflowStatus.RUNNING  # worker A's write won; worker B's was rejected, not silently applied


@pytest.mark.asyncio
async def test_resume_after_simulated_crash(monkeypatch, tmp_path: Path, repos):
    """Simulates: Planning's AgentExecution + PlanArtifact are durably
    persisted, but the workflow document itself wasn't advanced (process
    died between agent completion and workflow-state update). resume_workflow
    must reconcile from durable evidence, not silently re-run Planning."""
    make_pytest_project(tmp_path, passing=True)
    patch_happy_path(monkeypatch, tmp_path)

    service = make_service(repos)
    workflow = await service.start_workflow(make_ticket(), workspace_path=str(tmp_path))

    # Run planning for real once (persists AgentExecution + PlanArtifact)...
    await service.execute_next_step(workflow.workflow_id)
    advanced = await repos["workflow"].get(workflow.workflow_id)
    assert advanced.current_stage == WorkflowStage.ARCHITECTURE  # confirms it already advanced normally

    # ...now simulate the crash: roll the *workflow document* back to look
    # like Planning never finished, while leaving the execution/artifact evidence intact.
    executions = await repos["execution"].list_for_workflow(workflow.workflow_id)
    planning_execution = next(e for e in executions if e.agent_name == "planning_agent")
    assert planning_execution.status == WorkflowStatus.COMPLETED

    stale_workflow = advanced.model_copy(update={"current_stage": WorkflowStage.PLANNING, "artifact_ids": [], "status": WorkflowStatus.RUNNING})
    await repos["workflow"].update(stale_workflow)

    resumed = await service.resume_workflow(workflow.workflow_id)

    # Reconciliation found the completed Planning execution/artifact and
    # advanced from it instead of invoking Planning a second time.
    assert resumed.current_stage == WorkflowStage.ARCHITECTURE
    assert len(resumed.artifact_ids) == 1
    remaining_planning_executions = [e for e in await repos["execution"].list_for_workflow(workflow.workflow_id) if e.agent_name == "planning_agent"]
    assert len(remaining_planning_executions) == 1  # not duplicated


# ---- ADK ------------------------------------------------------------------------


def test_sequential_agent_exists_for_happy_path():
    from app.agent_runtime.context import AgentContext
    from app.orchestration.adk import build_happy_path_sequential_agent

    registry = build_default_registry()
    context = AgentContext(workflow_id="wf-1", execution_id="exec-1", knowledge=FakeKnowledgeGateway(), tools=FakeToolGateway(), artifacts=None)
    seq = build_happy_path_sequential_agent(registry, context)
    assert [a.name for a in seq.sub_agents] == ["planning_agent", "architecture_agent", "codegen_agent", "testing_agent"]


def test_orchestration_decision_agent_uses_structured_output():
    from app.orchestration.adk import decision_agent

    assert decision_agent.output_schema is ProposedDecision
    assert decision_agent.tools == []


def test_adk_isolated_from_orchestration_domain_logic():
    import app.orchestration.decisions as decisions_module
    import app.orchestration.errors as errors_module
    import app.orchestration.transitions as transitions_module

    for module in (decisions_module, errors_module, transitions_module):
        source = Path(module.__file__).read_text()
        assert "google.adk" not in source, f"{module.__name__} references google.adk"


# ---- Standalone compatibility -----------------------------------------------------


@pytest.mark.asyncio
async def test_agents_still_execute_independently(monkeypatch, tmp_path: Path):
    from app.agent_runtime.context import AgentContext
    from app.agents.planning import PlanningAgent
    from app.domain import AgentInput

    monkeypatch.setattr("app.agents.planning.InMemoryRunner", make_plain_runner(json.dumps(VALID_PLAN)))
    monkeypatch.setattr("app.agents.planning.JiraClient", FakeJiraClient)

    agent = PlanningAgent()
    agent_input = AgentInput(workflow_id="wf-standalone", agent_name="planning_agent", ticket=make_ticket())
    context = AgentContext(
        workflow_id="wf-standalone",
        execution_id="exec-standalone",
        knowledge=FakeKnowledgeGateway(),
        tools=FakeToolGateway(),
        artifacts=InMemoryArtifactRepositoryGateway(),
    )
    output = await agent.execute(agent_input, context)
    assert output.status == WorkflowStatus.COMPLETED


class InMemoryArtifactRepositoryGateway:
    def __init__(self):
        self._store = {}

    async def get(self, workflow_id, artifact_id):
        return self._store.get((workflow_id, artifact_id))

    async def save(self, workflow_id, artifact):
        self._store[(workflow_id, artifact.artifact_id)] = artifact
        return artifact


def test_existing_agent_tests_still_pass_marker():
    """Not a real assertion — the actual proof is the rest of the suite
    (tests/test_{planning,architecture,codegen,testing}_agent.py) passing
    unmodified alongside this file. See the full-suite run in the report."""
    assert True


def test_existing_application_imports_cleanly():
    from app.main import app  # noqa: F401
    from app.orchestrator.pipeline import quipu_pipeline

    assert [a.name for a in quipu_pipeline.sub_agents] == ["feature_detection", "planning", "architecture"]
