import pytest

from app.agent_runtime import (
    AgentCapability,
    AgentContext,
    AgentIdentity,
    AgentNotFoundError,
    AgentRegistry,
    AgentStatus,
    CapabilityError,
    DuplicateAgentError,
    QuipuAgent,
)
from app.domain import (
    AgentInput,
    AgentOutput,
    Artifact,
    ArtifactType,
    KnowledgeItem,
    KnowledgeRequest,
    KnowledgeType,
    Ticket,
    ToolExecution,
    ToolRequest,
    WorkflowStatus,
)


# ---- fake gateways (no external services) ----------------------------------


class FakeKnowledgeGateway:
    def __init__(self, items: list[KnowledgeItem] | None = None):
        self._items = items or []

    async def search(self, request: KnowledgeRequest) -> list[KnowledgeItem]:
        return self._items


class FakeToolGateway:
    def __init__(self):
        self.calls: list[ToolRequest] = []

    async def execute(self, request: ToolRequest) -> ToolExecution:
        self.calls.append(request)
        return ToolExecution(
            tool_name=request.tool_name,
            operation=request.operation,
            workflow_id=request.workflow_id,
            agent_execution_id=request.execution_id,
            status=WorkflowStatus.COMPLETED,
        )


class FakeArtifactGateway:
    def __init__(self):
        self._store: dict[str, Artifact] = {}

    async def get(self, workflow_id: str, artifact_id: str) -> Artifact | None:
        return self._store.get(artifact_id)

    async def save(self, workflow_id: str, artifact: Artifact) -> Artifact:
        self._store[artifact.artifact_id] = artifact
        return artifact


def make_context(**overrides) -> AgentContext:
    defaults = dict(
        workflow_id="wf-1",
        execution_id="exec-1",
        knowledge=FakeKnowledgeGateway(),
        tools=FakeToolGateway(),
        artifacts=FakeArtifactGateway(),
    )
    defaults.update(overrides)
    return AgentContext(**defaults)


def make_agent_input(**overrides) -> AgentInput:
    defaults = dict(
        workflow_id="wf-1",
        agent_name="echo_agent",
        ticket=Ticket(title="Add dark mode", description="Users want a dark theme."),
    )
    defaults.update(overrides)
    return AgentInput(**defaults)


# ---- minimal concrete agents for testing ------------------------------------


class EchoAgent(QuipuAgent):
    @property
    def identity(self) -> AgentIdentity:
        return AgentIdentity(agent_id="echo_agent", name="Echo Agent", version="1.0.0", description="Echoes input.")

    @property
    def capabilities(self) -> set[AgentCapability]:
        return {AgentCapability.READ_TICKET}

    async def _perform(self, agent_input: AgentInput, context: AgentContext) -> AgentOutput:
        self.require_capability(AgentCapability.READ_TICKET)
        return AgentOutput(
            execution_id=agent_input.execution_id,
            status=WorkflowStatus.COMPLETED,
            messages=[agent_input.ticket.title],
        )


class ExplodingAgent(QuipuAgent):
    @property
    def identity(self) -> AgentIdentity:
        return AgentIdentity(agent_id="exploding_agent", name="Exploding Agent", version="1.0.0", description="Always fails.")

    @property
    def capabilities(self) -> set[AgentCapability]:
        return set()

    async def _perform(self, agent_input: AgentInput, context: AgentContext) -> AgentOutput:
        raise RuntimeError("boom")


class PlannerAgent(QuipuAgent):
    @property
    def identity(self) -> AgentIdentity:
        return AgentIdentity(agent_id="planner_agent", name="Planner Agent", version="1.0.0", description="Plans.")

    @property
    def capabilities(self) -> set[AgentCapability]:
        return {AgentCapability.CREATE_PLAN, AgentCapability.QUERY_KNOWLEDGE}

    async def _perform(self, agent_input: AgentInput, context: AgentContext) -> AgentOutput:
        return AgentOutput(execution_id=agent_input.execution_id, status=WorkflowStatus.COMPLETED)


# 1. AgentIdentity creation
def test_agent_identity_creation():
    identity = AgentIdentity(agent_id="planning_agent", name="Planning Agent", version="1.0.0", description="Plans features.")
    assert identity.agent_id == "planning_agent"


def test_agent_identity_is_frozen():
    identity = AgentIdentity(agent_id="a", name="A", version="1.0.0", description="d")
    with pytest.raises(Exception):
        identity.agent_id = "b"


# 2. Capability enum validation
def test_capability_enum_rejects_unknown_member():
    with pytest.raises(ValueError):
        AgentCapability("not_a_real_capability")


def test_capability_enum_has_expected_members():
    assert AgentCapability.QUERY_KNOWLEDGE in AgentCapability
    assert AgentCapability.DEPLOY in AgentCapability


# 3. Agent lifecycle states
def test_agent_starts_in_created_status():
    agent = EchoAgent()
    assert agent.status == AgentStatus.CREATED


@pytest.mark.asyncio
async def test_agent_reaches_completed_status_on_success():
    agent = EchoAgent()
    await agent.execute(make_agent_input(), make_context())
    assert agent.status == AgentStatus.COMPLETED


# 4. Capability enforcement success
def test_capability_enforcement_success():
    agent = EchoAgent()
    agent.require_capability(AgentCapability.READ_TICKET)  # must not raise


# 5. Capability enforcement failure
def test_capability_enforcement_failure():
    agent = EchoAgent()
    with pytest.raises(CapabilityError):
        agent.require_capability(AgentCapability.DEPLOY)


# 6. AgentContext construction
def test_agent_context_construction():
    context = make_context(metadata={"repo_url": "https://example.com/repo.git"})
    assert context.workflow_id == "wf-1"
    assert context.metadata["repo_url"] == "https://example.com/repo.git"


# 7. KnowledgeGateway test double
@pytest.mark.asyncio
async def test_knowledge_gateway_test_double():
    item = KnowledgeItem(
        document_id="doc-1",
        title="Retry policy",
        content="...",
        knowledge_type=KnowledgeType.TECHNOLOGY_STANDARD,
        source="confluence",
    )
    gateway = FakeKnowledgeGateway(items=[item])
    request = KnowledgeRequest(
        agent_name="planner_agent",
        workflow_id="wf-1",
        query="retry policy",
        knowledge_type=KnowledgeType.TECHNOLOGY_STANDARD,
    )
    results = await gateway.search(request)
    assert results == [item]


# 8. ToolGateway test double
@pytest.mark.asyncio
async def test_tool_gateway_test_double():
    gateway = FakeToolGateway()
    request = ToolRequest(
        tool_name="jira", operation="create_story", workflow_id="wf-1", execution_id="exec-1"
    )
    execution = await gateway.execute(request)
    assert execution.status == WorkflowStatus.COMPLETED
    assert gateway.calls == [request]


# 9. ArtifactGateway test double
@pytest.mark.asyncio
async def test_artifact_gateway_test_double():
    gateway = FakeArtifactGateway()
    artifact = Artifact(artifact_type=ArtifactType.PLAN, created_by="planner_agent")
    assert await gateway.get("wf-1", artifact.artifact_id) is None
    await gateway.save("wf-1", artifact)
    assert await gateway.get("wf-1", artifact.artifact_id) == artifact


# 10. AgentRegistry registration
def test_registry_registration_and_get():
    registry = AgentRegistry()
    agent = EchoAgent()
    registry.register(agent)
    assert registry.get("echo_agent") is agent


# 11. Duplicate agent registration failure
def test_registry_rejects_duplicate_agent_id():
    registry = AgentRegistry()
    registry.register(EchoAgent())
    with pytest.raises(DuplicateAgentError):
        registry.register(EchoAgent())


# 12. Agent lookup
def test_registry_lookup_missing_agent_raises():
    registry = AgentRegistry()
    with pytest.raises(AgentNotFoundError):
        registry.get("does_not_exist")


def test_registry_list_agents():
    registry = AgentRegistry()
    registry.register(EchoAgent())
    registry.register(PlannerAgent())
    assert {a.identity.agent_id for a in registry.list_agents()} == {"echo_agent", "planner_agent"}


# 13. Capability-based agent lookup
def test_registry_find_by_capability():
    registry = AgentRegistry()
    registry.register(EchoAgent())
    registry.register(PlannerAgent())
    found = registry.find_by_capability(AgentCapability.CREATE_PLAN)
    assert [a.identity.agent_id for a in found] == ["planner_agent"]


# 14. Minimal concrete QuipuAgent execution
@pytest.mark.asyncio
async def test_minimal_concrete_agent_execution():
    agent = EchoAgent()
    output = await agent.execute(make_agent_input(), make_context())
    assert output.status == WorkflowStatus.COMPLETED
    assert output.messages == ["Add dark mode"]


# 15. Agent failure handling
@pytest.mark.asyncio
async def test_agent_failure_handling():
    agent = ExplodingAgent()
    with pytest.raises(RuntimeError):
        await agent.execute(make_agent_input(), make_context())
    assert agent.status == AgentStatus.FAILED
