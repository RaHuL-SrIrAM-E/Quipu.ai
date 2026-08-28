import pytest
from pydantic import ValidationError

from app.domain import (
    AgentError,
    AgentExecution,
    AgentInput,
    AgentMetrics,
    AgentOutput,
    Artifact,
    ArtifactType,
    Decision,
    DecisionAction,
    DecisionSource,
    ErrorCategory,
    KnowledgeItem,
    KnowledgeRequest,
    KnowledgeType,
    Ticket,
    ToolExecution,
    ToolRequest,
    WorkflowStage,
    WorkflowState,
    WorkflowStatus,
)


def make_ticket(**overrides) -> Ticket:
    defaults = dict(title="Add dark mode", description="Users want a dark theme toggle.")
    defaults.update(overrides)
    return Ticket(**defaults)


# 1. Valid Ticket creation
def test_ticket_creation_valid():
    ticket = make_ticket(source="feature_detection", priority="medium")
    assert ticket.ticket_id
    assert ticket.title == "Add dark mode"
    assert ticket.metadata == {}


def test_ticket_rejects_empty_title():
    with pytest.raises(ValidationError):
        make_ticket(title="   ")


# 2. WorkflowState creation
def test_workflow_state_creation():
    ticket = make_ticket()
    state = WorkflowState(ticket=ticket, current_stage=WorkflowStage.PLANNING)
    assert state.workflow_id
    assert state.status == WorkflowStatus.PENDING
    assert state.artifact_ids == []
    assert state.active_decision_id is None


def test_workflow_state_references_artifacts_by_id_only():
    state = WorkflowState(
        ticket=make_ticket(),
        current_stage=WorkflowStage.PLANNING,
        artifact_ids=["artifact-1", "artifact-2"],
    )
    # WorkflowState must not carry embedded artifact objects, only ids.
    assert "artifacts" not in WorkflowState.model_fields
    assert state.artifact_ids == ["artifact-1", "artifact-2"]


# 3. Artifact creation
def test_artifact_creation():
    artifact = Artifact(
        artifact_type=ArtifactType.PLAN,
        created_by="planning_agent",
        payload={"tasks": []},
    )
    assert artifact.artifact_id
    assert artifact.version == 1
    assert artifact.status == WorkflowStatus.COMPLETED


# 4. Artifact lineage
def test_artifact_lineage():
    root = Artifact(artifact_type=ArtifactType.PLAN, created_by="planning_agent")
    child = Artifact(
        artifact_type=ArtifactType.ARCHITECTURE,
        created_by="architecture_agent",
        parent_artifact_ids=[root.artifact_id],
        version=2,
    )
    assert child.parent_artifact_ids == [root.artifact_id]
    assert child.version == 2


# 5. AgentInput serialization
def test_agent_input_serialization_round_trip():
    agent_input = AgentInput(
        workflow_id="wf-1",
        agent_name="planning_agent",
        ticket=make_ticket(),
        artifact_ids=["artifact-1"],
        context={"repo_url": "https://example.com/repo.git"},
    )
    payload = agent_input.model_dump_json()
    restored = AgentInput.model_validate_json(payload)
    assert restored == agent_input


# 6. AgentOutput serialization
def test_agent_output_serialization_round_trip():
    output = AgentOutput(
        execution_id="exec-1",
        status=WorkflowStatus.COMPLETED,
        artifacts=[Artifact(artifact_type=ArtifactType.PLAN, created_by="planning_agent")],
        messages=["plan created"],
    )
    payload = output.model_dump_json()
    restored = AgentOutput.model_validate_json(payload)
    assert restored == output


# 7. Decision validation
def test_decision_valid():
    decision = Decision(
        action=DecisionAction.CONTINUE,
        target_agent="architecture_agent",
        reason="plan passed validation",
        confidence=0.92,
        source=DecisionSource.ORCHESTRATOR,
    )
    assert decision.action == DecisionAction.CONTINUE
    assert 0.0 <= decision.confidence <= 1.0


def test_decision_rejects_out_of_range_confidence():
    with pytest.raises(ValidationError):
        Decision(
            action=DecisionAction.RETRY,
            reason="transient failure",
            confidence=1.5,
            source=DecisionSource.ORCHESTRATOR,
        )


# 8. Invalid enum values
def test_invalid_enum_values_rejected():
    with pytest.raises(ValidationError):
        Artifact(artifact_type="not_a_real_type", created_by="planning_agent")

    with pytest.raises(ValidationError):
        Decision(
            action="not_a_real_action",
            reason="x",
            confidence=0.5,
            source=DecisionSource.ORCHESTRATOR,
        )

    with pytest.raises(ValidationError):
        WorkflowState(ticket=make_ticket(), current_stage="not_a_real_stage")


# 9. AgentError
def test_agent_error_creation():
    error = AgentError(
        code="LLM_TIMEOUT",
        message="model call exceeded timeout",
        category=ErrorCategory.TIMEOUT,
        recoverable=True,
        retryable=True,
    )
    assert error.category == ErrorCategory.TIMEOUT
    assert error.retryable is True


def test_agent_error_rejects_invalid_category():
    with pytest.raises(ValidationError):
        AgentError(code="X", message="y", category="not_a_real_category")


# 10. KnowledgeRequest
def test_knowledge_request_creation():
    request = KnowledgeRequest(
        agent_name="architecture_agent",
        workflow_id="wf-1",
        query="microservice communication patterns",
        knowledge_type=KnowledgeType.ARCHITECTURE_PATTERN,
        top_k=8,
        require_reranking=True,
    )
    assert request.top_k == 8
    assert request.require_reranking is True
    assert request.filters == {}


def test_knowledge_request_rejects_non_positive_top_k():
    with pytest.raises(ValidationError):
        KnowledgeRequest(
            agent_name="architecture_agent",
            workflow_id="wf-1",
            query="x",
            knowledge_type=KnowledgeType.CODING_STANDARD,
            top_k=0,
        )


# 11. ToolRequest
def test_tool_request_creation():
    request = ToolRequest(
        tool_name="jira",
        operation="create_story",
        parameters={"summary": "Add dark mode toggle"},
        workflow_id="wf-1",
        execution_id="exec-1",
    )
    assert request.tool_name == "jira"
    assert request.parameters["summary"] == "Add dark mode toggle"


# 12. Round-trip serialization/deserialization for the major models
@pytest.mark.parametrize(
    "instance",
    [
        make_ticket(),
        WorkflowState(ticket=make_ticket(), current_stage=WorkflowStage.CODEGEN),
        Artifact(artifact_type=ArtifactType.TEST_RESULT, created_by="testing_agent"),
        Decision(
            action=DecisionAction.ESCALATE,
            reason="repeated failures",
            confidence=0.4,
            source=DecisionSource.AGENT,
        ),
        AgentError(code="E1", message="boom", category=ErrorCategory.INTERNAL),
        AgentMetrics(execution_id="exec-1", latency_ms=120.5, cost_usd=0.002),
        AgentExecution(workflow_id="wf-1", agent_name="testing_agent"),
        KnowledgeItem(
            document_id="doc-1",
            title="Retry policy",
            content="...",
            knowledge_type=KnowledgeType.TECHNOLOGY_STANDARD,
            source="confluence",
        ),
        KnowledgeRequest(
            agent_name="planning_agent",
            workflow_id="wf-1",
            query="x",
            knowledge_type=KnowledgeType.HISTORICAL_PROJECT,
        ),
        ToolRequest(
            tool_name="github",
            operation="open_pr",
            workflow_id="wf-1",
            execution_id="exec-1",
        ),
        ToolExecution(
            tool_name="github",
            operation="open_pr",
            workflow_id="wf-1",
            agent_execution_id="exec-1",
        ),
    ],
)
def test_round_trip_serialization(instance):
    model_cls = type(instance)
    restored = model_cls.model_validate_json(instance.model_dump_json())
    assert restored == instance
