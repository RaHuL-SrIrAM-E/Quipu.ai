from datetime import datetime, timezone

import pytest

from app.domain import (
    AgentExecution,
    Artifact,
    ArtifactType,
    Decision,
    DecisionAction,
    DecisionSource,
    Ticket,
    WorkflowStage,
    WorkflowState,
    WorkflowStatus,
)
from app.persistence import DuplicateEntityError, EntityNotFoundError, IncidentRecord, VersionConflictError
from app.persistence.memory import (
    InMemoryAgentExecutionRepository,
    InMemoryArtifactRepository,
    InMemoryDecisionRepository,
    InMemoryIncidentRepository,
    InMemoryWorkflowRepository,
)
from app.persistence.serialization import from_firestore_dict, to_firestore_dict


def make_ticket(**overrides) -> Ticket:
    defaults = dict(title="Add dark mode", description="Users want a dark theme toggle.")
    defaults.update(overrides)
    return Ticket(**defaults)


def make_workflow(**overrides) -> WorkflowState:
    defaults = dict(ticket=make_ticket(), current_stage=WorkflowStage.PLANNING)
    defaults.update(overrides)
    return WorkflowState(**defaults)


def make_execution(**overrides) -> AgentExecution:
    defaults = dict(workflow_id="wf-1", agent_name="planning_agent")
    defaults.update(overrides)
    return AgentExecution(**defaults)


def make_decision(**overrides) -> Decision:
    defaults = dict(action=DecisionAction.CONTINUE, reason="plan valid", confidence=0.9, source=DecisionSource.ORCHESTRATOR)
    defaults.update(overrides)
    return Decision(**defaults)


# ---- Workflow -----------------------------------------------------------


@pytest.mark.asyncio
async def test_workflow_create():
    repo = InMemoryWorkflowRepository()
    workflow = make_workflow()
    created = await repo.create(workflow)
    assert created.workflow_id == workflow.workflow_id
    assert created.version == 1


@pytest.mark.asyncio
async def test_workflow_retrieve():
    repo = InMemoryWorkflowRepository()
    workflow = make_workflow()
    await repo.create(workflow)
    fetched = await repo.get(workflow.workflow_id)
    assert fetched is not None
    assert fetched.workflow_id == workflow.workflow_id


@pytest.mark.asyncio
async def test_workflow_update():
    repo = InMemoryWorkflowRepository()
    workflow = make_workflow()
    await repo.create(workflow)
    updated = workflow.model_copy(update={"status": WorkflowStatus.RUNNING})
    result = await repo.update(updated)
    assert result.status == WorkflowStatus.RUNNING
    fetched = await repo.get(workflow.workflow_id)
    assert fetched.status == WorkflowStatus.RUNNING


@pytest.mark.asyncio
async def test_workflow_delete():
    repo = InMemoryWorkflowRepository()
    workflow = make_workflow()
    await repo.create(workflow)
    await repo.delete(workflow.workflow_id)
    assert await repo.get(workflow.workflow_id) is None


@pytest.mark.asyncio
async def test_workflow_missing():
    repo = InMemoryWorkflowRepository()
    assert await repo.get("does-not-exist") is None
    with pytest.raises(EntityNotFoundError):
        await repo.update(make_workflow(workflow_id="does-not-exist"))
    with pytest.raises(EntityNotFoundError):
        await repo.delete("does-not-exist")


@pytest.mark.asyncio
async def test_workflow_duplicate():
    repo = InMemoryWorkflowRepository()
    workflow = make_workflow()
    await repo.create(workflow)
    with pytest.raises(DuplicateEntityError):
        await repo.create(workflow)


@pytest.mark.asyncio
async def test_workflow_version_increment():
    repo = InMemoryWorkflowRepository()
    workflow = make_workflow()
    await repo.create(workflow)
    updated = workflow.model_copy(update={"status": WorkflowStatus.RUNNING})
    result = await repo.update_if_version(workflow.workflow_id, 1, updated)
    assert result.version == 2


@pytest.mark.asyncio
async def test_workflow_version_conflict():
    repo = InMemoryWorkflowRepository()
    workflow = make_workflow()
    await repo.create(workflow)
    updated = workflow.model_copy(update={"status": WorkflowStatus.RUNNING})
    await repo.update_if_version(workflow.workflow_id, 1, updated)  # now version 2
    with pytest.raises(VersionConflictError) as exc_info:
        await repo.update_if_version(workflow.workflow_id, 1, updated)  # stale expectation
    assert exc_info.value.expected_version == 1
    assert exc_info.value.actual_version == 2


@pytest.mark.asyncio
async def test_workflow_concurrent_update_semantics():
    """Two 'agents' both read version 1; only the first update_if_version wins."""
    repo = InMemoryWorkflowRepository()
    workflow = make_workflow()
    await repo.create(workflow)

    agent_a_view = workflow.model_copy(update={"status": WorkflowStatus.RUNNING})
    agent_b_view = workflow.model_copy(update={"current_stage": WorkflowStage.ARCHITECTURE})

    await repo.update_if_version(workflow.workflow_id, 1, agent_a_view)
    with pytest.raises(VersionConflictError):
        await repo.update_if_version(workflow.workflow_id, 1, agent_b_view)

    final = await repo.get(workflow.workflow_id)
    assert final.status == WorkflowStatus.RUNNING
    assert final.version == 2


# ---- Artifacts ------------------------------------------------------------


@pytest.mark.asyncio
async def test_artifact_save():
    repo = InMemoryArtifactRepository()
    artifact = Artifact(artifact_type=ArtifactType.PLAN, created_by="planning_agent")
    saved = await repo.save("wf-1", artifact)
    assert saved.artifact_id == artifact.artifact_id


@pytest.mark.asyncio
async def test_artifact_retrieve():
    repo = InMemoryArtifactRepository()
    artifact = Artifact(artifact_type=ArtifactType.PLAN, created_by="planning_agent")
    await repo.save("wf-1", artifact)
    fetched = await repo.get("wf-1", artifact.artifact_id)
    assert fetched is not None
    assert fetched.artifact_id == artifact.artifact_id
    assert await repo.get("wf-1", "unknown") is None


@pytest.mark.asyncio
async def test_artifact_list_for_workflow():
    repo = InMemoryArtifactRepository()
    a1 = Artifact(artifact_type=ArtifactType.PLAN, created_by="planning_agent")
    a2 = Artifact(artifact_type=ArtifactType.ARCHITECTURE, created_by="architecture_agent")
    other_workflow_artifact = Artifact(artifact_type=ArtifactType.PLAN, created_by="planning_agent")
    await repo.save("wf-1", a1)
    await repo.save("wf-1", a2)
    await repo.save("wf-2", other_workflow_artifact)
    results = await repo.list_for_workflow("wf-1")
    assert {a.artifact_id for a in results} == {a1.artifact_id, a2.artifact_id}


# ---- Executions -----------------------------------------------------------


@pytest.mark.asyncio
async def test_execution_create():
    repo = InMemoryAgentExecutionRepository()
    execution = make_execution()
    created = await repo.create(execution)
    assert created.execution_id == execution.execution_id
    with pytest.raises(DuplicateEntityError):
        await repo.create(execution)


@pytest.mark.asyncio
async def test_execution_retrieve():
    repo = InMemoryAgentExecutionRepository()
    execution = make_execution()
    await repo.create(execution)
    fetched = await repo.get(execution.workflow_id, execution.execution_id)
    assert fetched is not None
    assert fetched.agent_name == "planning_agent"


@pytest.mark.asyncio
async def test_execution_list_for_workflow():
    repo = InMemoryAgentExecutionRepository()
    e1 = make_execution(workflow_id="wf-1")
    e2 = make_execution(workflow_id="wf-1")
    e3 = make_execution(workflow_id="wf-2")
    await repo.create(e1)
    await repo.create(e2)
    await repo.create(e3)
    results = await repo.list_for_workflow("wf-1")
    assert {e.execution_id for e in results} == {e1.execution_id, e2.execution_id}


@pytest.mark.asyncio
async def test_execution_update():
    repo = InMemoryAgentExecutionRepository()
    execution = make_execution()
    await repo.create(execution)
    updated = execution.model_copy(update={"status": WorkflowStatus.COMPLETED})
    result = await repo.update(updated)
    assert result.status == WorkflowStatus.COMPLETED
    with pytest.raises(EntityNotFoundError):
        await repo.update(make_execution(workflow_id="wf-1", execution_id="unknown"))


# ---- Decisions --------------------------------------------------------------


@pytest.mark.asyncio
async def test_decision_save():
    repo = InMemoryDecisionRepository()
    decision = make_decision()
    saved = await repo.save("wf-1", decision)
    assert saved.decision_id == decision.decision_id


@pytest.mark.asyncio
async def test_decision_retrieve():
    repo = InMemoryDecisionRepository()
    decision = make_decision()
    await repo.save("wf-1", decision)
    fetched = await repo.get("wf-1", decision.decision_id)
    assert fetched is not None
    assert fetched.action == DecisionAction.CONTINUE


@pytest.mark.asyncio
async def test_decision_list_for_workflow():
    repo = InMemoryDecisionRepository()
    d1 = make_decision()
    d2 = make_decision(action=DecisionAction.REPLAN)
    await repo.save("wf-1", d1)
    await repo.save("wf-1", d2)
    await repo.save("wf-2", make_decision())
    results = await repo.list_for_workflow("wf-1")
    assert {d.decision_id for d in results} == {d1.decision_id, d2.decision_id}


# ---- Incident (provisional) -------------------------------------------------


@pytest.mark.asyncio
async def test_incident_repository_basic_roundtrip():
    repo = InMemoryIncidentRepository()
    incident = IncidentRecord(workflow_id="wf-1", summary="prod 500s spiking")
    await repo.save(incident)
    fetched = await repo.get("wf-1", incident.incident_id)
    assert fetched.summary == "prod 500s spiking"
    assert [i.incident_id for i in await repo.list_for_workflow("wf-1")] == [incident.incident_id]


# ---- Serialization ----------------------------------------------------------


def test_serialization_enum_round_trip():
    workflow = make_workflow(status=WorkflowStatus.RUNNING)
    data = to_firestore_dict(workflow)
    assert data["status"] == "running"
    restored = from_firestore_dict(WorkflowState, data)
    assert restored.status == WorkflowStatus.RUNNING


def test_serialization_uuid_round_trip():
    artifact = Artifact(artifact_type=ArtifactType.PLAN, created_by="planning_agent")
    data = to_firestore_dict(artifact)
    assert data["artifact_id"] == artifact.artifact_id
    restored = from_firestore_dict(Artifact, data)
    assert restored.artifact_id == artifact.artifact_id


def test_serialization_datetime_round_trip_coerces_timezone_aware():
    artifact = Artifact(artifact_type=ArtifactType.PLAN, created_by="planning_agent")
    assert artifact.created_at.tzinfo is None  # domain default is naive (Level 1.1, unchanged)
    data = to_firestore_dict(artifact)
    assert data["created_at"].tzinfo is not None
    assert data["created_at"].tzinfo == timezone.utc
    restored = from_firestore_dict(Artifact, data)
    assert restored.created_at.tzinfo is not None


def test_serialization_already_aware_datetime_preserved():
    now = datetime.now(timezone.utc)
    workflow = make_workflow()
    data = to_firestore_dict(workflow)
    data["metadata"] = {"checked_at": now}
    normalized = to_firestore_dict(from_firestore_dict(WorkflowState, data))
    assert normalized["metadata"]["checked_at"] == now


def test_serialization_nested_payload_round_trip():
    artifact = Artifact(
        artifact_type=ArtifactType.PLAN,
        created_by="planning_agent",
        payload={"tasks": [{"id": "t1", "depends_on": ["t0"]}], "summary": "x"},
    )
    data = to_firestore_dict(artifact)
    assert data["payload"]["tasks"][0]["id"] == "t1"
    restored = from_firestore_dict(Artifact, data)
    assert restored.payload == artifact.payload


# ---- Compatibility ------------------------------------------------------------


def test_existing_app_still_imports():
    from app.main import app  # noqa: F401
    from app.orchestrator.pipeline import quipu_pipeline  # noqa: F401

    assert quipu_pipeline is not None
