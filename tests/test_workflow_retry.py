"""Tests for OrchestrationService.retry_failed_workflow() — reopening a
FAILED WorkflowState in place (same workflow_id, same artifacts/
executions, resumes at its own current_stage) so it can be re-executed,
without ever creating a second WorkflowState. Structurally the same
"reopen, don't recreate" mechanism start_remediation_from_resolution
already uses for a COMPLETED workflow — see app/orchestration/service.py.
"""

import asyncio
import json

import pytest
from google.genai import types

from app.domain import (
    Artifact,
    ArtifactType,
    FeatureReview,
    ReviewStatus,
    Ticket,
    WorkflowStage,
    WorkflowState,
    WorkflowStatus,
)
from app.demo.fakes import FakeKnowledgeGateway, FakeToolGateway
from app.orchestration.errors import OrchestrationError
from app.orchestration.registry_setup import build_default_registry
from app.orchestration.service import OrchestrationService
from app.persistence.errors import EntityNotFoundError
from app.persistence.memory import (
    InMemoryAgentExecutionRepository,
    InMemoryArtifactRepository,
    InMemoryDecisionRepository,
    InMemoryFeatureReviewRepository,
    InMemoryWorkflowRepository,
)


def make_service(workflow_repo=None, **overrides) -> OrchestrationService:
    return OrchestrationService(
        workflow_repo=workflow_repo or InMemoryWorkflowRepository(),
        artifact_repo=overrides.pop("artifact_repo", None) or InMemoryArtifactRepository(),
        execution_repo=InMemoryAgentExecutionRepository(),
        decision_repo=InMemoryDecisionRepository(),
        registry=build_default_registry(),
        knowledge_gateway=FakeKnowledgeGateway(),
        tool_gateway=FakeToolGateway(),
        **overrides,
    )


async def make_failed_codegen_workflow(workflow_repo, artifact_repo=None) -> WorkflowState:
    """Mirrors the real production shape: Planning + Architecture
    succeeded (their artifacts/executions are attached), then Codegen
    failed (no CODE_CHANGE artifact, no execution_id appended for it —
    matches _fail_workflow's actual behavior, confirmed against the real
    failed production workflow bb05bd82-e894-401a-9a9a-7c7e9be3cfcd)."""
    plan_artifact = Artifact(artifact_type=ArtifactType.PLAN, created_by="planning_agent", payload={})
    architecture_artifact = Artifact(artifact_type=ArtifactType.ARCHITECTURE, created_by="architecture_agent", payload={})
    if artifact_repo is not None:
        await artifact_repo.save("wf-1", plan_artifact)
        await artifact_repo.save("wf-1", architecture_artifact)

    workflow = WorkflowState(
        workflow_id="wf-1",
        ticket=Ticket(title="Add tag-based test filtering", description="Allow tagging/grouping Karate tests."),
        status=WorkflowStatus.FAILED,
        current_stage=WorkflowStage.CODEGEN,
        artifact_ids=[plan_artifact.artifact_id, architecture_artifact.artifact_id],
        execution_ids=["exec-planning-1", "exec-architecture-1"],
        metadata={"failure_reason": "'codegen_agent_llm_call' did not complete within 60.0s"},
    )
    await workflow_repo.create(workflow)
    return workflow


# ---------------------------------------------------------------------------
# A/B. Retry a FAILED workflow — state transition, resume stage, artifacts
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_retry_transitions_failed_to_pending_and_preserves_everything():
    workflow_repo = InMemoryWorkflowRepository()
    artifact_repo = InMemoryArtifactRepository()
    original = await make_failed_codegen_workflow(workflow_repo, artifact_repo)
    service = make_service(workflow_repo, artifact_repo=artifact_repo)

    retried = await service.retry_failed_workflow("wf-1")

    assert retried.workflow_id == original.workflow_id  # same workflow_id — never a new one
    assert retried.status == WorkflowStatus.PENDING
    assert retried.current_stage == WorkflowStage.CODEGEN  # unchanged — resumes exactly where it failed
    assert retried.artifact_ids == original.artifact_ids  # Plan + Architecture preserved
    assert retried.execution_ids == original.execution_ids  # preserved
    assert retried.active_decision_id == original.active_decision_id
    assert retried.active_incident_ids == original.active_incident_ids
    # historical failure_reason retained, not scrubbed — no established
    # convention exists for clearing it, per the task's own instruction
    assert retried.metadata["failure_reason"] == original.metadata["failure_reason"]
    assert retried.metadata["retry_count"] == 1
    assert "last_retried_at" in retried.metadata

    # Only ONE WorkflowState exists in the repository.
    assert len(await workflow_repo.list_recent(status=None)) == 1

    # The existing artifacts are still fetchable exactly as before.
    plan = await artifact_repo.get("wf-1", original.artifact_ids[0])
    architecture = await artifact_repo.get("wf-1", original.artifact_ids[1])
    assert plan is not None and plan.artifact_type == ArtifactType.PLAN
    assert architecture is not None and architecture.artifact_type == ArtifactType.ARCHITECTURE


@pytest.mark.asyncio
async def test_retry_increments_retry_count_on_repeated_failures():
    workflow_repo = InMemoryWorkflowRepository()
    await make_failed_codegen_workflow(workflow_repo)
    service = make_service(workflow_repo)

    first = await service.retry_failed_workflow("wf-1")
    # Simulate a second failure of the retried attempt, then retry again.
    failed_again = first.model_copy(update={"status": WorkflowStatus.FAILED})
    await workflow_repo.update_if_version(first.workflow_id, first.version, failed_again)

    second = await service.retry_failed_workflow("wf-1")

    assert second.metadata["retry_count"] == 2


# ---------------------------------------------------------------------------
# C. Retry rejected for every non-FAILED status
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status",
    [WorkflowStatus.PENDING, WorkflowStatus.RUNNING, WorkflowStatus.COMPLETED, WorkflowStatus.ESCALATED, WorkflowStatus.CANCELLED],
)
async def test_retry_rejects_non_failed_statuses(status):
    workflow_repo = InMemoryWorkflowRepository()
    workflow = WorkflowState(
        workflow_id="wf-1",
        ticket=Ticket(title="t", description="d"),
        status=status,
        current_stage=WorkflowStage.CODEGEN,
    )
    await workflow_repo.create(workflow)
    service = make_service(workflow_repo)

    with pytest.raises(OrchestrationError, match="not FAILED"):
        await service.retry_failed_workflow("wf-1")

    unchanged = await workflow_repo.get("wf-1")
    assert unchanged.status == status  # rejection never mutates anything


# ---------------------------------------------------------------------------
# D. Missing workflow
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_retry_missing_workflow_raises_existing_orchestration_error():
    service = make_service()

    with pytest.raises(OrchestrationError, match="not found"):
        await service.retry_failed_workflow("does-not-exist")


# ---------------------------------------------------------------------------
# E. Concurrency — two simultaneous retries never corrupt state
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_retries_resolve_to_a_single_pending_workflow():
    workflow_repo = InMemoryWorkflowRepository()
    await make_failed_codegen_workflow(workflow_repo)
    service = make_service(workflow_repo)

    results = await asyncio.gather(
        service.retry_failed_workflow("wf-1"), service.retry_failed_workflow("wf-1"), return_exceptions=True
    )

    succeeded = [r for r in results if isinstance(r, WorkflowState)]
    assert len(succeeded) >= 1
    for r in succeeded:
        assert r.status == WorkflowStatus.PENDING
        assert r.workflow_id == "wf-1"

    final = await workflow_repo.get("wf-1")
    assert final.status == WorkflowStatus.PENDING
    assert len(await workflow_repo.list_recent(status=None)) == 1  # still exactly one WorkflowState


# ---------------------------------------------------------------------------
# F. FeatureReview.workflow_id relationship is never touched
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_retry_never_modifies_feature_review_workflow_id():
    workflow_repo = InMemoryWorkflowRepository()
    review_repo = InMemoryFeatureReviewRepository()
    await make_failed_codegen_workflow(workflow_repo)
    review = FeatureReview(detection_id="det-1", status=ReviewStatus.APPROVED, workflow_id="wf-1")
    await review_repo.create(review)
    service = make_service(workflow_repo, review_repo=review_repo)

    await service.retry_failed_workflow("wf-1")

    unchanged_review = await review_repo.get(review.review_id)
    assert unchanged_review.workflow_id == "wf-1"  # untouched — retry never writes to FeatureReview


# ---------------------------------------------------------------------------
# G. End-to-end: retry, then execute_next_step() runs the originally-failed
# stage (Codegen), never re-running Planning/Architecture.
# ---------------------------------------------------------------------------


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


VALID_CODEGEN = {
    "summary": "Implemented tag-based test filtering.",
    "modified_files": [],
    "created_files": [],
    "deleted_files": [],
    "changes": [],
    "implementation_notes": "",
    "unresolved_items": [],
    "tests_to_run": [],
}


@pytest.mark.asyncio
async def test_retry_then_execute_next_step_resumes_at_codegen_only(monkeypatch, tmp_path):
    from app.config import get_settings

    workflow_repo = InMemoryWorkflowRepository()
    artifact_repo = InMemoryArtifactRepository()
    architecture_payload = {
        "design_summary": "x",
        "components": [{"name": "c", "responsibility": "r"}],
        "data_model_changes": [],
        "api_contracts": [],
        "task_designs": [{"task_id": "t1", "approach": "a", "files": []}],
        "risks": [],
    }
    plan_artifact = Artifact(artifact_type=ArtifactType.PLAN, created_by="planning_agent", payload={})
    architecture_artifact = Artifact(artifact_type=ArtifactType.ARCHITECTURE, created_by="architecture_agent", payload=architecture_payload)
    await artifact_repo.save("wf-1", plan_artifact)
    await artifact_repo.save("wf-1", architecture_artifact)
    workflow = WorkflowState(
        workflow_id="wf-1",
        ticket=Ticket(title="t", description="d"),
        status=WorkflowStatus.FAILED,
        current_stage=WorkflowStage.CODEGEN,
        artifact_ids=[plan_artifact.artifact_id, architecture_artifact.artifact_id],
        execution_ids=["exec-planning-1", "exec-architecture-1"],
        metadata={"workspace_path": str(tmp_path)},  # already-valid workspace, as if freshly re-provisioned
    )
    await workflow_repo.create(workflow)
    service = make_service(workflow_repo, artifact_repo=artifact_repo)

    called_agents = []
    real_get = service._registry.get

    def _spy_get(agent_id):
        called_agents.append(agent_id)
        return real_get(agent_id)

    monkeypatch.setattr(service._registry, "get", _spy_get)

    async def _events(**kwargs):
        yield _FakeEvent(json.dumps(VALID_CODEGEN))

    class _FakeRunner:
        def __init__(self, agent, app_name):
            self.session_service = _FakeSessionService()

        def run_async(self, **kwargs):
            return _events(**kwargs)

    monkeypatch.setattr("app.agents.codegen.InMemoryRunner", _FakeRunner)

    retried = await service.retry_failed_workflow("wf-1")
    assert retried.status == WorkflowStatus.PENDING
    assert retried.current_stage == WorkflowStage.CODEGEN

    result = await service.execute_next_step("wf-1")

    assert called_agents == ["codegen_agent"]  # never planning_agent/architecture_agent again
    assert result.current_stage == WorkflowStage.TESTING  # advanced past Codegen
    assert plan_artifact.artifact_id in result.artifact_ids
    assert architecture_artifact.artifact_id in result.artifact_ids
