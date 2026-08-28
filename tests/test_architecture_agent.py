"""Tests for the migrated Architecture Agent (Level 1.6).

No real Gemini/ADK model call, no real knowledge backend, no real Firestore
— everything is faked/mocked. app.agents.architecture.InMemoryRunner is
monkeypatched at its import site in that module.
"""

import json
from pathlib import Path

import pytest
from google.genai import types
from pydantic import ValidationError

from app.agent_runtime.capabilities import AgentCapability
from app.agent_runtime.context import AgentContext
from app.agent_runtime.status import AgentStatus
from app.agents.architecture import ArchitectureAgent, ArchitectureOutput, _architecture_llm_agent, architecture_agent
from app.agents.planning import PlanOutput
from app.domain import (
    AgentInput,
    Artifact,
    ArtifactType,
    KnowledgeItem,
    KnowledgeRequest,
    KnowledgeType,
    Ticket,
    WorkflowStatus,
)
from app.persistence.memory import InMemoryAgentExecutionRepository

VALID_PLAN = PlanOutput(
    feature_summary="Add dark mode",
    architecture_notes="Add a theme provider in the frontend module.",
    affected_components=[{"name": "frontend", "reason": "theming"}],
    tasks=[
        {"id": "t1", "description": "add theme provider", "depends_on": []},
        {"id": "t2", "description": "wire toggle", "depends_on": ["t1"]},
    ],
    acceptance_criteria=["toggle switches theme"],
    risks=[{"description": "flash of wrong theme", "mitigation": "ssr theme cookie"}],
)

VALID_ARCHITECTURE = {
    "design_summary": "Add a ThemeProvider and a settings toggle.",
    "components": [{"name": "ThemeProvider", "responsibility": "holds/applies theme state"}],
    "data_model_changes": [],
    "api_contracts": [],
    "task_designs": [
        {"task_id": "t1", "approach": "create ThemeProvider", "files": ["src/theme.tsx"]},
        {"task_id": "t2", "approach": "wire toggle to context", "files": ["src/Settings.tsx"]},
    ],
    "risks": [{"description": "theme flash on load", "mitigation": "inline script"}],
}


def make_plan_artifact(workflow_id="wf-1", **overrides) -> Artifact:
    defaults = dict(
        artifact_type=ArtifactType.PLAN,
        created_by="planning_agent",
        payload=VALID_PLAN.model_dump(mode="json"),
    )
    defaults.update(overrides)
    return Artifact(**defaults)


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
    def __init__(self, items=None, raise_error: Exception | None = None):
        self._items = items or []
        self._raise_error = raise_error
        self.last_request: KnowledgeRequest | None = None

    async def search(self, request: KnowledgeRequest) -> list[KnowledgeItem]:
        self.last_request = request
        if self._raise_error:
            raise self._raise_error
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
        agent_name="architecture_agent",
        ticket=Ticket(title="Add dark mode", description="Users want a dark theme toggle."),
        artifact_ids=["plan-1"],
    )
    defaults.update(overrides)
    return AgentInput(**defaults)


def make_context(**overrides) -> AgentContext:
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


def make_context_with_plan(plan_artifact_id="plan-1", **overrides) -> AgentContext:
    gateway = FakeArtifactGateway()
    gateway.seed("wf-1", make_plan_artifact(artifact_id=plan_artifact_id))
    return make_context(artifacts=gateway, **overrides)


# ---- Identity/runtime ---------------------------------------------------------


def test_architecture_agent_has_stable_identity():
    agent = ArchitectureAgent()
    assert agent.identity.agent_id == "architecture_agent"
    assert agent.identity.name == "Architecture Agent"


def test_architecture_agent_has_expected_capabilities():
    agent = ArchitectureAgent()
    assert agent.capabilities == {
        AgentCapability.READ_TICKET,
        AgentCapability.READ_ARTIFACT,
        AgentCapability.QUERY_KNOWLEDGE,
        AgentCapability.READ_REPOSITORY,
        AgentCapability.WRITE_ARTIFACT,
        AgentCapability.CREATE_ARCHITECTURE,
    }
    forbidden = {
        AgentCapability.WRITE_CODE,
        AgentCapability.DEPLOY,
        AgentCapability.WRITE_JIRA,
        AgentCapability.RESOLVE_INCIDENT,
    }
    assert agent.capabilities.isdisjoint(forbidden)


@pytest.mark.asyncio
async def test_lifecycle_executes_through_quipu_agent(monkeypatch):
    monkeypatch.setattr("app.agents.architecture.InMemoryRunner", make_fake_runner(json.dumps(VALID_ARCHITECTURE)))

    agent = ArchitectureAgent()
    assert agent.status == AgentStatus.CREATED
    output = await agent.execute(make_agent_input(), make_context_with_plan())
    assert agent.status == AgentStatus.COMPLETED
    assert output.status == WorkflowStatus.COMPLETED


@pytest.mark.asyncio
async def test_failure_transitions_correctly(monkeypatch):
    monkeypatch.setattr("app.agents.architecture.InMemoryRunner", make_fake_runner(raise_error=RuntimeError("gemini down")))

    agent = ArchitectureAgent()
    output = await agent.execute(make_agent_input(), make_context_with_plan())
    assert agent.status == AgentStatus.COMPLETED  # handled failure, not an uncaught exception
    assert output.status == WorkflowStatus.FAILED
    assert output.errors[0].code == "ARCHITECTURE_LLM_FAILURE"


# ---- Plan artifact ----------------------------------------------------------


@pytest.mark.asyncio
async def test_architecture_receives_plan_artifact_reference():
    agent_input = make_agent_input(artifact_ids=["plan-xyz"])
    assert agent_input.artifact_ids == ["plan-xyz"]


@pytest.mark.asyncio
async def test_plan_artifact_loaded_through_artifact_gateway(monkeypatch):
    monkeypatch.setattr("app.agents.architecture.InMemoryRunner", make_fake_runner(json.dumps(VALID_ARCHITECTURE)))

    context = make_context_with_plan(plan_artifact_id="plan-1")
    agent = ArchitectureAgent()
    output = await agent.execute(make_agent_input(artifact_ids=["plan-1"]), context)

    assert output.status == WorkflowStatus.COMPLETED
    assert output.artifacts[0].parent_artifact_ids == ["plan-1"]


@pytest.mark.asyncio
async def test_wrong_artifact_type_rejected():
    gateway = FakeArtifactGateway()
    gateway.seed("wf-1", Artifact(artifact_id="plan-1", artifact_type=ArtifactType.CODE_CHANGE, created_by="x", payload={}))

    agent = ArchitectureAgent()
    output = await agent.execute(make_agent_input(artifact_ids=["plan-1"]), make_context(artifacts=gateway))

    assert output.status == WorkflowStatus.FAILED
    assert output.errors[0].code == "PLAN_ARTIFACT_WRONG_TYPE"


@pytest.mark.asyncio
async def test_missing_artifact_rejected():
    agent = ArchitectureAgent()
    output = await agent.execute(make_agent_input(artifact_ids=["does-not-exist"]), make_context())

    assert output.status == WorkflowStatus.FAILED
    assert output.errors[0].code == "PLAN_ARTIFACT_MISSING"


@pytest.mark.asyncio
async def test_no_artifact_ids_rejected():
    agent = ArchitectureAgent()
    output = await agent.execute(make_agent_input(artifact_ids=[]), make_context())

    assert output.status == WorkflowStatus.FAILED
    assert output.errors[0].code == "PLAN_ARTIFACT_MISSING"


@pytest.mark.asyncio
async def test_invalid_plan_output_payload_rejected():
    gateway = FakeArtifactGateway()
    gateway.seed(
        "wf-1",
        Artifact(artifact_id="plan-1", artifact_type=ArtifactType.PLAN, created_by="planning_agent", payload={"tasks": []}),
    )

    agent = ArchitectureAgent()
    output = await agent.execute(make_agent_input(artifact_ids=["plan-1"]), make_context(artifacts=gateway))

    assert output.status == WorkflowStatus.FAILED
    assert output.errors[0].code == "PLAN_OUTPUT_INVALID"


# ---- Task coverage ------------------------------------------------------------


def test_valid_plan_and_complete_architecture_passes():
    from app.agents.architecture import validate_task_coverage

    architecture = ArchitectureOutput(**VALID_ARCHITECTURE)
    validate_task_coverage(architecture, VALID_PLAN.model_dump(mode="json"))  # must not raise


def test_missing_task_design_fails():
    from app.agents.architecture import validate_task_coverage

    incomplete = {**VALID_ARCHITECTURE, "task_designs": [VALID_ARCHITECTURE["task_designs"][0]]}
    architecture = ArchitectureOutput(**incomplete)
    with pytest.raises(ValueError, match="no task_design for plan task"):
        validate_task_coverage(architecture, VALID_PLAN.model_dump(mode="json"))


def test_unknown_task_design_fails():
    from app.agents.architecture import validate_task_coverage

    extra = {
        **VALID_ARCHITECTURE,
        "task_designs": VALID_ARCHITECTURE["task_designs"] + [{"task_id": "t99", "approach": "x", "files": []}],
    }
    architecture = ArchitectureOutput(**extra)
    with pytest.raises(ValueError, match="unknown plan task"):
        validate_task_coverage(architecture, VALID_PLAN.model_dump(mode="json"))


def test_duplicate_task_design_fails_at_schema_level():
    duplicated = {
        **VALID_ARCHITECTURE,
        "task_designs": [VALID_ARCHITECTURE["task_designs"][0], VALID_ARCHITECTURE["task_designs"][0]],
    }
    with pytest.raises(ValidationError):
        ArchitectureOutput(**duplicated)


@pytest.mark.asyncio
async def test_incomplete_coverage_fails_end_to_end(monkeypatch):
    incomplete = json.dumps({**VALID_ARCHITECTURE, "task_designs": [VALID_ARCHITECTURE["task_designs"][0]]})
    monkeypatch.setattr("app.agents.architecture.InMemoryRunner", make_fake_runner(incomplete))

    agent = ArchitectureAgent()
    output = await agent.execute(make_agent_input(), make_context_with_plan())

    assert output.status == WorkflowStatus.FAILED
    assert output.errors[0].code == "TASK_COVERAGE_INCOMPLETE"
    assert output.artifacts == []


# ---- Knowledge ------------------------------------------------------------


@pytest.mark.asyncio
async def test_architecture_agent_can_query_knowledge_gateway(monkeypatch):
    captured_state = {}

    async def _events(**kwargs):
        yield _FakeEvent(json.dumps(VALID_ARCHITECTURE))

    class _CapturingSessionService:
        async def create_session(self, **kwargs):
            captured_state.update(kwargs.get("state", {}))
            return _FakeSession()

    class _CapturingRunner:
        def __init__(self, agent, app_name):
            self.session_service = _CapturingSessionService()

        def run_async(self, **kwargs):
            return _events(**kwargs)

    monkeypatch.setattr("app.agents.architecture.InMemoryRunner", _CapturingRunner)

    gateway = FakeKnowledgeGateway()
    agent = ArchitectureAgent()
    await agent.execute(make_agent_input(), make_context_with_plan(knowledge=gateway))

    assert captured_state["_knowledge_gateway"] is gateway
    assert captured_state["_agent_name"] == "architecture_agent"


@pytest.mark.asyncio
async def test_architecture_retrieval_profile_is_used():
    from app.knowledge.policies import get_retrieval_policy
    from app.tools.knowledge_tools import query_enterprise_knowledge

    gateway = FakeKnowledgeGateway(
        items=[
            KnowledgeItem(
                document_id="doc-1",
                title="Approved theming pattern",
                content="use CSS variables + context",
                knowledge_type=KnowledgeType.ARCHITECTURE_PATTERN,
                source="confluence",
            )
        ]
    )

    class _FakeToolContext:
        state = {"_knowledge_gateway": gateway, "workflow_id": "wf-1", "_agent_name": "architecture_agent"}

    result = await query_enterprise_knowledge("theming pattern", "architecture_pattern", _FakeToolContext())

    assert len(result) == 1
    policy = get_retrieval_policy("architecture_agent")
    assert gateway.last_request.agent_name == "architecture_agent"
    assert gateway.last_request.knowledge_type in policy.allowed_knowledge_types


@pytest.mark.asyncio
async def test_out_of_profile_knowledge_type_rejected_for_architecture():
    from app.tools.knowledge_tools import query_enterprise_knowledge

    class _FakeToolContext:
        state = {"_knowledge_gateway": FakeKnowledgeGateway(), "workflow_id": "wf-1", "_agent_name": "architecture_agent"}

    # historical_project is in Planning's profile, not Architecture's
    with pytest.raises(ValueError):
        await query_enterprise_knowledge("x", "historical_project", _FakeToolContext())


@pytest.mark.asyncio
async def test_knowledge_provenance_retained_for_architecture():
    from app.tools.knowledge_tools import query_enterprise_knowledge

    item = KnowledgeItem(
        document_id="doc-77",
        title="Security requirement",
        content="all endpoints require auth",
        knowledge_type=KnowledgeType.SECURITY_POLICY,
        source="policy://security/auth",
        relevance_score=0.95,
    )
    gateway = FakeKnowledgeGateway(items=[item])

    class _FakeToolContext:
        state = {"_knowledge_gateway": gateway, "workflow_id": "wf-1", "_agent_name": "architecture_agent"}

    result = await query_enterprise_knowledge("auth requirement", "security_policy", _FakeToolContext())

    assert result[0]["document_id"] == "doc-77"
    assert result[0]["source"] == "policy://security/auth"
    assert result[0]["relevance_score"] == 0.95


# ---- Repository -----------------------------------------------------------


def test_repository_inspection_remains_available():
    tool_names = {t.__name__ for t in _architecture_llm_agent.tools if callable(t)}
    assert {"search_files", "read_file", "search_code", "get_project_structure", "get_dependencies"} <= tool_names


def test_real_repository_references_via_fake_tools(tmp_path: Path):
    from app.tools.repo_tools import get_project_structure, search_files

    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "theme.tsx").write_text("export const ThemeProvider = () => null;")

    class _FakeToolContext:
        state = {"workspace_path": str(tmp_path)}

    tree = get_project_structure(2, _FakeToolContext())
    assert "theme.tsx" in tree

    matches = search_files("**/*.tsx", _FakeToolContext())
    assert "src/theme.tsx" in matches


# ---- Output -----------------------------------------------------------------


def test_architecture_output_validation_remains_intact():
    with pytest.raises(ValidationError):
        ArchitectureOutput(**{**VALID_ARCHITECTURE, "task_designs": []})
    with pytest.raises(ValidationError):
        ArchitectureOutput(**{**VALID_ARCHITECTURE, "components": []})


@pytest.mark.asyncio
async def test_architecture_artifact_produced(monkeypatch):
    monkeypatch.setattr("app.agents.architecture.InMemoryRunner", make_fake_runner(json.dumps(VALID_ARCHITECTURE)))

    agent = ArchitectureAgent()
    output = await agent.execute(make_agent_input(), make_context_with_plan())

    assert len(output.artifacts) == 1
    artifact = output.artifacts[0]
    assert artifact.artifact_type == ArtifactType.ARCHITECTURE
    assert artifact.created_by == "architecture_agent"
    assert artifact.payload["design_summary"] == VALID_ARCHITECTURE["design_summary"]


@pytest.mark.asyncio
async def test_artifact_gateway_save_used(monkeypatch):
    monkeypatch.setattr("app.agents.architecture.InMemoryRunner", make_fake_runner(json.dumps(VALID_ARCHITECTURE)))

    gateway = FakeArtifactGateway()
    gateway.seed("wf-1", make_plan_artifact(artifact_id="plan-1"))
    agent = ArchitectureAgent()
    output = await agent.execute(make_agent_input(), make_context(artifacts=gateway))

    artifact_id = output.artifacts[0].artifact_id
    assert gateway.saved[("wf-1", artifact_id)] is not None


@pytest.mark.asyncio
async def test_workflow_state_references_architecture_artifact_not_embeds(monkeypatch):
    from app.domain import WorkflowStage, WorkflowState

    monkeypatch.setattr("app.agents.architecture.InMemoryRunner", make_fake_runner(json.dumps(VALID_ARCHITECTURE)))

    agent = ArchitectureAgent()
    output = await agent.execute(make_agent_input(), make_context_with_plan())

    workflow = WorkflowState(
        ticket=Ticket(title="t", description="d"),
        current_stage=WorkflowStage.ARCHITECTURE,
        artifact_ids=["plan-1", output.artifacts[0].artifact_id],
    )
    assert "artifacts" not in WorkflowState.model_fields
    assert output.artifacts[0].artifact_id in workflow.artifact_ids


# ---- ADK --------------------------------------------------------------------


def test_adk_llm_agent_uses_architecture_output_schema():
    assert _architecture_llm_agent.output_schema is ArchitectureOutput
    assert architecture_agent.output_schema is ArchitectureOutput


def test_adk_tool_adapters_expose_required_tools():
    new_tool_names = {t.__name__ for t in _architecture_llm_agent.tools if callable(t)}
    assert "query_enterprise_knowledge" in new_tool_names
    assert {"search_files", "read_file", "search_code", "get_project_structure", "get_dependencies"} <= new_tool_names


def test_domain_runtime_isolated_from_google_sdk():
    import app.domain as domain_pkg

    domain_dir = Path(domain_pkg.__file__).parent
    for path in domain_dir.glob("*.py"):
        text = path.read_text()
        assert "google.adk" not in text
        assert "import google" not in text


# ---- Metrics ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_llm_usage_metrics_captured(monkeypatch):
    monkeypatch.setattr("app.agents.architecture.InMemoryRunner", make_fake_runner(json.dumps(VALID_ARCHITECTURE)))

    agent_input = make_agent_input(execution_id="exec-metrics")
    agent = ArchitectureAgent()
    output = await agent.execute(agent_input, make_context_with_plan(execution_id="exec-metrics"))

    assert output.metrics is not None
    assert output.metrics.execution_id == "exec-metrics"


@pytest.mark.asyncio
async def test_agent_execution_persisted_with_output_artifact(monkeypatch):
    monkeypatch.setattr("app.agents.architecture.InMemoryRunner", make_fake_runner(json.dumps(VALID_ARCHITECTURE)))

    executions = InMemoryAgentExecutionRepository()
    agent = ArchitectureAgent()
    output = await agent.execute(
        make_agent_input(execution_id="exec-99"), make_context_with_plan(executions=executions, execution_id="exec-99")
    )

    execution = await executions.get("wf-1", "exec-99")
    assert execution is not None
    assert execution.status == WorkflowStatus.COMPLETED
    assert execution.output_artifact_ids == [output.artifacts[0].artifact_id]


# ---- Compatibility ------------------------------------------------------------


def test_legacy_architecture_agent_remains_importable():
    assert architecture_agent.name == "architecture"
    assert architecture_agent.output_schema is ArchitectureOutput


def test_legacy_sequential_agent_remains_wired():
    from app.orchestrator.pipeline import quipu_pipeline

    assert [a.name for a in quipu_pipeline.sub_agents] == ["feature_detection", "planning", "architecture"]
