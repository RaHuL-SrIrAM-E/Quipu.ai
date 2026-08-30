"""Tests for the migrated Planning Agent (Level 1.5).

No real Gemini/ADK model call, no real Jira, no real knowledge backend, no
real Firestore — everything is faked/mocked. app.agents.planning.InMemoryRunner
and .JiraClient are monkeypatched at their import site in that module.
"""

import json
from pathlib import Path

import pytest
from google.genai import types
from pydantic import ValidationError

from app.agent_runtime.capabilities import AgentCapability, CapabilityError
from app.agent_runtime.context import AgentContext
from app.agent_runtime.status import AgentStatus
from app.agents.planning import PlanningAgent, PlanOutput, _planning_llm_agent, planning_agent
from app.core.observability import get_logger
from app.domain import AgentInput, ArtifactType, KnowledgeItem, KnowledgeRequest, KnowledgeType, Ticket, WorkflowStatus
from app.persistence.memory import InMemoryAgentExecutionRepository, InMemoryArtifactRepository

logger = get_logger("test.planning_agent")


VALID_PLAN = {
    "feature_summary": "Add dark mode",
    "architecture_notes": "Add a theme provider in the frontend module.",
    "affected_components": [{"name": "frontend", "reason": "theming"}],
    "tasks": [
        {"id": "t1", "description": "add theme provider", "depends_on": []},
        {"id": "t2", "description": "wire toggle", "depends_on": ["t1"]},
    ],
    "dependencies": [],
    "acceptance_criteria": ["toggle switches theme"],
    "risks": [{"description": "flash of wrong theme", "mitigation": "ssr theme cookie"}],
}


# ---- fakes ------------------------------------------------------------------


class FakeArtifactGateway:
    def __init__(self):
        self.saved: dict[tuple[str, str], object] = {}

    async def get(self, workflow_id, artifact_id):
        return self.saved.get((workflow_id, artifact_id))

    async def save(self, workflow_id, artifact):
        self.saved[(workflow_id, artifact.artifact_id)] = artifact
        return artifact


class FakeToolGateway:
    async def execute(self, request):
        raise NotImplementedError


class FakeKnowledgeGateway:
    def __init__(self, items=None, raise_error: Exception | None = None):
        self._items = items or []
        self._raise_error = raise_error
        self.last_request: KnowledgeRequest | None = None

    async def search(self, request: KnowledgeRequest) -> list[KnowledgeItem]:
        self.last_request = request
        if self._raise_error:
            raise self._raise_error
        return self._items


class FakeJiraClient:
    """Patched in for app.agents.planning.JiraClient — no network."""

    calls: list[dict] = []
    raise_error: Exception | None = None

    def __init__(self):
        pass

    def create_story(self, summary: str, description: str) -> dict:
        if FakeJiraClient.raise_error:
            raise FakeJiraClient.raise_error
        key = f"QP-{len(FakeJiraClient.calls) + 1}"
        FakeJiraClient.calls.append({"summary": summary, "description": description})
        return {"key": key, "url": f"https://example.atlassian.net/browse/{key}"}


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


def make_fake_runner(final_text: str | None = None, raise_error: Exception | None = None):
    async def _events(**kwargs):
        if raise_error:
            raise raise_error
        if final_text is not None:
            yield _FakeEvent(final_text)

    class _FakeRunner:
        def __init__(self, agent, app_name):
            self.session_service = _FakeSessionService()

        def run_async(self, **kwargs):
            return _events(**kwargs)

    return _FakeRunner


def make_agent_input(**overrides) -> AgentInput:
    defaults = dict(
        workflow_id="wf-1",
        agent_name="planning_agent",
        ticket=Ticket(title="Add dark mode", description="Users want a dark theme toggle."),
    )
    defaults.update(overrides)
    return AgentInput(**defaults)


def make_context(**overrides) -> AgentContext:
    defaults = dict(
        workflow_id="wf-1",
        execution_id="exec-1",
        knowledge=FakeKnowledgeGateway(),
        tools=FakeToolGateway(),
        artifacts=FakeArtifactGateway(),
        executions=InMemoryAgentExecutionRepository(),
    )
    defaults.update(overrides)
    return AgentContext(**defaults)


@pytest.fixture(autouse=True)
def _reset_fake_jira():
    FakeJiraClient.calls = []
    FakeJiraClient.raise_error = None
    yield


# ---- Agent/runtime ------------------------------------------------------------


def test_planning_agent_has_stable_identity():
    agent = PlanningAgent()
    assert agent.identity.agent_id == "planning_agent"
    assert agent.identity.name == "Planning Agent"


def test_planning_agent_has_expected_capabilities():
    agent = PlanningAgent()
    assert agent.capabilities == {
        AgentCapability.READ_TICKET,
        AgentCapability.QUERY_KNOWLEDGE,
        AgentCapability.READ_REPOSITORY,
        AgentCapability.READ_ARTIFACT,
        AgentCapability.WRITE_ARTIFACT,
        AgentCapability.CREATE_PLAN,
        AgentCapability.WRITE_JIRA,
    }


@pytest.mark.asyncio
async def test_lifecycle_executes_through_quipu_agent(monkeypatch):
    monkeypatch.setattr("app.agents.planning.InMemoryRunner", make_fake_runner(json.dumps(VALID_PLAN)))
    monkeypatch.setattr("app.agents.planning.JiraClient", FakeJiraClient)

    agent = PlanningAgent()
    assert agent.status == AgentStatus.CREATED
    output = await agent.execute(make_agent_input(), make_context())
    assert agent.status == AgentStatus.COMPLETED
    assert output.status == WorkflowStatus.COMPLETED


@pytest.mark.asyncio
async def test_failure_transitions_correctly(monkeypatch):
    monkeypatch.setattr("app.agents.planning.InMemoryRunner", make_fake_runner(raise_error=RuntimeError("gemini down")))

    agent = PlanningAgent()
    output = await agent.execute(make_agent_input(), make_context())
    assert agent.status == AgentStatus.COMPLETED  # QuipuAgent.execute() only goes FAILED on an uncaught exception
    assert output.status == WorkflowStatus.FAILED
    assert output.errors[0].code == "PLANNING_LLM_FAILURE"


@pytest.mark.asyncio
async def test_planning_still_uses_shared_llm_call_timeout(monkeypatch):
    """PlanningAgent must remain on Settings.llm_call_timeout_seconds —
    unaffected by CodegenAgent's separate codegen_llm_call_timeout_seconds
    (see tests/test_codegen_agent.py).

    Patches app.agents.planning's own module-level `settings` object
    directly, NOT a fresh get_settings() call — other test modules
    (test_cloud_logging_client.py, test_cloud_monitoring_client.py) call
    get_settings.cache_clear() during the full suite run, which would
    otherwise make get_settings() return a different Settings instance
    than the one planning.py captured at import time, silently no-op'ing
    this test's monkeypatch."""
    import asyncio

    import app.agents.planning as planning_module

    monkeypatch.setattr(planning_module.settings, "llm_call_timeout_seconds", 0.05)
    monkeypatch.setattr(planning_module.settings, "codegen_llm_call_timeout_seconds", 60.0)

    async def _slow_events(**kwargs):
        await asyncio.sleep(0.5)
        yield _FakeEvent(json.dumps(VALID_PLAN))

    class _SlowRunner:
        def __init__(self, agent, app_name):
            self.session_service = _FakeSessionService()

        def run_async(self, **kwargs):
            return _slow_events(**kwargs)

    monkeypatch.setattr("app.agents.planning.InMemoryRunner", _SlowRunner)

    agent = PlanningAgent()
    output = await agent.execute(make_agent_input(), make_context())

    assert output.status == WorkflowStatus.FAILED
    assert output.errors[0].code == "PLANNING_LLM_FAILURE"
    assert "did not complete within 0.05" in output.errors[0].message


# ---- Input --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ticket_reaches_planning_agent_via_agent_input(monkeypatch):
    captured_state = {}

    async def _events(**kwargs):
        yield _FakeEvent(json.dumps(VALID_PLAN))

    class _CapturingSessionService:
        async def create_session(self, **kwargs):
            captured_state.update(kwargs.get("state", {}))
            return _FakeSession()

    class _CapturingRunner:
        def __init__(self, agent, app_name):
            self.session_service = _CapturingSessionService()

        def run_async(self, **kwargs):
            return _events(**kwargs)

    monkeypatch.setattr("app.agents.planning.InMemoryRunner", _CapturingRunner)
    monkeypatch.setattr("app.agents.planning.JiraClient", FakeJiraClient)

    agent = PlanningAgent()
    ticket = Ticket(title="Add CSV export", description="Users want to export reports as CSV.")
    await agent.execute(make_agent_input(ticket=ticket), make_context())

    assert "Add CSV export" in captured_state["ticket_summary"]
    assert "export reports as CSV" in captured_state["ticket_summary"]


def test_feature_request_session_state_is_no_longer_the_primary_contract():
    """AgentInput.ticket is the primary contract now, not arbitrary session
    state — PlanningAgent._perform() takes a typed AgentInput; nothing calls
    into the agent by stuffing a bare 'feature_request' string into session
    state directly. The internal LlmAgent's instruction still reads
    feature_request as a fallback (for the legacy planning_agent path /
    backward compatibility), but PlanningAgent itself always sets
    ticket_summary from agent_input.ticket, which _build_instruction prefers.
    """
    from inspect import signature

    perform_params = list(signature(PlanningAgent._perform).parameters)
    assert "agent_input" in perform_params
    assert perform_params[1] == "agent_input"  # not session state, not a raw string


# ---- Knowledge ------------------------------------------------------------


@pytest.mark.asyncio
async def test_planning_agent_can_access_knowledge_gateway(monkeypatch):
    captured_state = {}

    async def _events(**kwargs):
        yield _FakeEvent(json.dumps(VALID_PLAN))

    class _CapturingSessionService:
        async def create_session(self, **kwargs):
            captured_state.update(kwargs.get("state", {}))
            return _FakeSession()

    class _CapturingRunner:
        def __init__(self, agent, app_name):
            self.session_service = _CapturingSessionService()

        def run_async(self, **kwargs):
            return _events(**kwargs)

    monkeypatch.setattr("app.agents.planning.InMemoryRunner", _CapturingRunner)
    monkeypatch.setattr("app.agents.planning.JiraClient", FakeJiraClient)

    gateway = FakeKnowledgeGateway()
    agent = PlanningAgent()
    await agent.execute(make_agent_input(), make_context(knowledge=gateway))

    assert captured_state["_knowledge_gateway"] is gateway


@pytest.mark.asyncio
async def test_planning_retrieval_profile_is_used():
    from app.knowledge.policies import get_retrieval_policy

    gateway = FakeKnowledgeGateway(
        items=[
            KnowledgeItem(
                document_id="doc-1",
                title="Theming patterns",
                content="use CSS variables",
                knowledge_type=KnowledgeType.ARCHITECTURE_PATTERN,
                source="confluence",
            )
        ]
    )

    class _FakeToolContext:
        def __init__(self, state):
            self.state = state

    from app.tools.knowledge_tools import query_enterprise_knowledge

    tool_context = _FakeToolContext({"_knowledge_gateway": gateway, "workflow_id": "wf-1"})
    result = await query_enterprise_knowledge("theming", "architecture_pattern", tool_context)

    assert len(result) == 1
    policy = get_retrieval_policy("planning_agent")
    assert gateway.last_request.knowledge_type in policy.allowed_knowledge_types
    assert gateway.last_request.top_k == policy.default_top_k


@pytest.mark.asyncio
async def test_knowledge_tool_rejects_out_of_profile_type():
    from app.tools.knowledge_tools import query_enterprise_knowledge

    class _FakeToolContext:
        def __init__(self, state):
            self.state = state

    tool_context = _FakeToolContext({"_knowledge_gateway": FakeKnowledgeGateway(), "workflow_id": "wf-1"})
    with pytest.raises(ValueError):
        await query_enterprise_knowledge("x", "deployment_standard", tool_context)


@pytest.mark.asyncio
async def test_knowledge_results_retain_provenance():
    from app.tools.knowledge_tools import query_enterprise_knowledge

    item = KnowledgeItem(
        document_id="doc-42",
        title="Theming patterns",
        content="use CSS variables",
        knowledge_type=KnowledgeType.ARCHITECTURE_PATTERN,
        source="confluence://space/page-42",
        relevance_score=0.9,
    )
    gateway = FakeKnowledgeGateway(items=[item])

    class _FakeToolContext:
        def __init__(self, state):
            self.state = state

    tool_context = _FakeToolContext({"_knowledge_gateway": gateway, "workflow_id": "wf-1"})
    result = await query_enterprise_knowledge("theming", "architecture_pattern", tool_context)

    assert result[0]["document_id"] == "doc-42"
    assert result[0]["source"] == "confluence://space/page-42"
    assert result[0]["relevance_score"] == 0.9


@pytest.mark.asyncio
async def test_knowledge_gateway_failure_surfaces_correctly():
    from app.tools.knowledge_tools import query_enterprise_knowledge

    gateway = FakeKnowledgeGateway(raise_error=RuntimeError("knowledge service down"))

    class _FakeToolContext:
        def __init__(self, state):
            self.state = state

    tool_context = _FakeToolContext({"_knowledge_gateway": gateway, "workflow_id": "wf-1"})
    with pytest.raises(RuntimeError, match="knowledge service down"):
        await query_enterprise_knowledge("theming", "architecture_pattern", tool_context)


def test_tool_capability_gate_blocks_ungranted_knowledge_access():
    from app.agents.planning import _tool_capability_gate

    class _FakeTool:
        name = "query_enterprise_knowledge"

    class _FakeToolContext:
        state = {"_capabilities": {AgentCapability.READ_REPOSITORY}}  # QUERY_KNOWLEDGE not granted

    with pytest.raises(CapabilityError):
        _tool_capability_gate(_FakeTool(), {}, _FakeToolContext())


# ---- Repository tools -----------------------------------------------------


def test_repository_inspection_remains_available():
    tool_names = {t.__name__ for t in _planning_llm_agent.tools if callable(t)}
    assert {"search_files", "read_file", "search_code", "get_project_structure", "get_dependencies"} <= tool_names


def test_existing_repository_tool_behavior_remains_intact(tmp_path: Path):
    from app.tools.repo_tools import get_project_structure, read_file

    (tmp_path / "app.py").write_text("print('hi')")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "mod.py").write_text("x = 1")

    class _FakeToolContext:
        state = {"workspace_path": str(tmp_path)}

    tree = get_project_structure(2, _FakeToolContext())
    assert "app.py" in tree
    assert "sub/" in tree

    content = read_file("app.py", _FakeToolContext())
    assert content == "print('hi')"


# ---- Jira -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_jira_story_creation_occurs_only_after_valid_plan(monkeypatch):
    invalid_plan_json = json.dumps({**VALID_PLAN, "tasks": []})  # fails PlanOutput validation

    monkeypatch.setattr("app.agents.planning.InMemoryRunner", make_fake_runner(invalid_plan_json))
    monkeypatch.setattr("app.agents.planning.JiraClient", FakeJiraClient)

    agent = PlanningAgent()
    output = await agent.execute(make_agent_input(), make_context())

    assert output.status == WorkflowStatus.FAILED
    assert output.errors[0].code == "PLAN_VALIDATION_FAILED"
    assert FakeJiraClient.calls == []  # never reached Jira creation


@pytest.mark.asyncio
async def test_one_jira_story_per_task(monkeypatch):
    monkeypatch.setattr("app.agents.planning.InMemoryRunner", make_fake_runner(json.dumps(VALID_PLAN)))
    monkeypatch.setattr("app.agents.planning.JiraClient", FakeJiraClient)

    agent = PlanningAgent()
    await agent.execute(make_agent_input(), make_context())

    assert len(FakeJiraClient.calls) == len(VALID_PLAN["tasks"])


@pytest.mark.asyncio
async def test_jira_key_populated_on_plan_task(monkeypatch):
    monkeypatch.setattr("app.agents.planning.InMemoryRunner", make_fake_runner(json.dumps(VALID_PLAN)))
    monkeypatch.setattr("app.agents.planning.JiraClient", FakeJiraClient)

    context = make_context()
    agent = PlanningAgent()
    output = await agent.execute(make_agent_input(), context)

    plan_payload = output.artifacts[0].payload
    assert all(task["jira_key"] is not None for task in plan_payload["tasks"])
    assert plan_payload["tasks"][0]["jira_key"] == "QP-1"


@pytest.mark.asyncio
async def test_jira_failure_does_not_produce_false_success(monkeypatch):
    monkeypatch.setattr("app.agents.planning.InMemoryRunner", make_fake_runner(json.dumps(VALID_PLAN)))
    monkeypatch.setattr("app.agents.planning.JiraClient", FakeJiraClient)
    FakeJiraClient.raise_error = RuntimeError("Jira API unreachable")

    agent = PlanningAgent()
    output = await agent.execute(make_agent_input(), make_context())

    assert output.status == WorkflowStatus.FAILED
    assert output.errors[0].code == "JIRA_STORY_CREATION_FAILED"
    assert output.artifacts == []


# ---- Output -----------------------------------------------------------------


def test_plan_output_validation_remains_intact():
    with pytest.raises(ValidationError):
        PlanOutput(**{**VALID_PLAN, "tasks": []})
    with pytest.raises(ValidationError):
        PlanOutput(**{**VALID_PLAN, "tasks": [{"id": "t1", "description": "x", "depends_on": ["t1"]}]})


@pytest.mark.asyncio
async def test_plan_artifact_produced_from_plan_output(monkeypatch):
    monkeypatch.setattr("app.agents.planning.InMemoryRunner", make_fake_runner(json.dumps(VALID_PLAN)))
    monkeypatch.setattr("app.agents.planning.JiraClient", FakeJiraClient)

    agent = PlanningAgent()
    output = await agent.execute(make_agent_input(), make_context())

    assert len(output.artifacts) == 1
    artifact = output.artifacts[0]
    assert artifact.artifact_type == ArtifactType.PLAN
    assert artifact.created_by == "planning_agent"
    assert artifact.payload["feature_summary"] == "Add dark mode"


@pytest.mark.asyncio
async def test_artifact_persistence_uses_artifact_gateway(monkeypatch):
    monkeypatch.setattr("app.agents.planning.InMemoryRunner", make_fake_runner(json.dumps(VALID_PLAN)))
    monkeypatch.setattr("app.agents.planning.JiraClient", FakeJiraClient)

    gateway = FakeArtifactGateway()
    agent = PlanningAgent()
    output = await agent.execute(make_agent_input(), make_context(artifacts=gateway))

    artifact_id = output.artifacts[0].artifact_id
    assert gateway.saved[("wf-1", artifact_id)] is not None


@pytest.mark.asyncio
async def test_workflow_state_references_artifact_not_embeds(monkeypatch):
    from app.domain import Ticket as _Ticket
    from app.domain import WorkflowStage, WorkflowState

    monkeypatch.setattr("app.agents.planning.InMemoryRunner", make_fake_runner(json.dumps(VALID_PLAN)))
    monkeypatch.setattr("app.agents.planning.JiraClient", FakeJiraClient)

    agent = PlanningAgent()
    output = await agent.execute(make_agent_input(), make_context())

    workflow = WorkflowState(
        ticket=_Ticket(title="t", description="d"),
        current_stage=WorkflowStage.PLANNING,
        artifact_ids=[output.artifacts[0].artifact_id],
    )
    assert "artifacts" not in WorkflowState.model_fields
    assert workflow.artifact_ids == [output.artifacts[0].artifact_id]


# ---- Metrics ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_llm_usage_is_captured(monkeypatch):
    monkeypatch.setattr("app.agents.planning.InMemoryRunner", make_fake_runner(json.dumps(VALID_PLAN)))
    monkeypatch.setattr("app.agents.planning.JiraClient", FakeJiraClient)

    agent_input = make_agent_input(execution_id="exec-metrics")
    agent = PlanningAgent()
    output = await agent.execute(agent_input, make_context())
    assert output.metrics is not None
    assert output.metrics.execution_id == "exec-metrics"


@pytest.mark.asyncio
async def test_agent_execution_persisted_with_output_artifact(monkeypatch):
    monkeypatch.setattr("app.agents.planning.InMemoryRunner", make_fake_runner(json.dumps(VALID_PLAN)))
    monkeypatch.setattr("app.agents.planning.JiraClient", FakeJiraClient)

    executions = InMemoryAgentExecutionRepository()
    agent = PlanningAgent()
    output = await agent.execute(make_agent_input(execution_id="exec-99"), make_context(executions=executions, execution_id="exec-99"))

    execution = await executions.get("wf-1", "exec-99")
    assert execution is not None
    assert execution.status == WorkflowStatus.COMPLETED
    assert execution.output_artifact_ids == [output.artifacts[0].artifact_id]


@pytest.mark.asyncio
async def test_agent_execution_marked_failed_on_llm_error(monkeypatch):
    monkeypatch.setattr("app.agents.planning.InMemoryRunner", make_fake_runner(raise_error=RuntimeError("boom")))

    executions = InMemoryAgentExecutionRepository()
    agent = PlanningAgent()
    await agent.execute(make_agent_input(execution_id="exec-fail"), make_context(executions=executions, execution_id="exec-fail"))

    execution = await executions.get("wf-1", "exec-fail")
    assert execution.status == WorkflowStatus.FAILED
    assert execution.error.code == "PLANNING_LLM_FAILURE"


# ---- ADK --------------------------------------------------------------------


def test_adk_llm_agent_uses_plan_output_schema():
    assert _planning_llm_agent.output_schema is PlanOutput
    assert planning_agent.output_schema is PlanOutput


def test_adk_tool_adapters_wire_correct_tools():
    new_tool_names = {t.__name__ for t in _planning_llm_agent.tools if callable(t)}
    legacy_tool_names = {t.__name__ for t in planning_agent.tools if callable(t)}
    assert "query_enterprise_knowledge" in new_tool_names
    assert "create_story" not in new_tool_names  # moved to deterministic post-processing
    assert "create_story" in legacy_tool_names  # legacy path unchanged


def test_adk_specific_dependencies_do_not_leak_into_domain_models():
    import app.domain as domain_pkg

    domain_dir = Path(domain_pkg.__file__).parent
    for path in domain_dir.glob("*.py"):
        text = path.read_text()
        assert "google.adk" not in text, f"{path} references google.adk"
        assert "import google" not in text, f"{path} imports a Google SDK"


# ---- Compatibility ------------------------------------------------------------


def test_existing_app_and_legacy_pipeline_still_import():
    from app.main import app  # noqa: F401
    from app.orchestrator.pipeline import quipu_pipeline

    assert [a.name for a in quipu_pipeline.sub_agents] == ["feature_detection", "planning", "architecture"]

