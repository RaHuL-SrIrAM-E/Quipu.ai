"""Workflow query/command routes. Every handler delegates directly to the
existing WorkflowRepository/ArtifactRepository/AgentExecutionRepository/
DecisionRepository or OrchestrationService — see app/api/container.py.
No handler here decides workflow transition policy, retry budgets, or
agent selection; that all remains OrchestrationService's job unchanged
(Invariant 1/2)."""

import time

from fastapi import APIRouter, Depends

from app.api.container import ApiContainer
from app.api.dependencies import get_container
from app.api.pagination import bounded_limit
from app.api.schemas.workflows import ArtifactSummary, DecisionSummary, ExecutionSummary, WorkflowDetail, WorkflowSummary
from app.core.observability import get_logger
from app.domain import WorkflowStatus
from app.persistence.errors import EntityNotFoundError

logger = get_logger("quipu.api.workflows")
router = APIRouter(prefix="/workflows", tags=["workflows"])


async def _get_workflow_or_404(container: ApiContainer, workflow_id: str):
    workflow = await container.workflow_repo.get(workflow_id)
    if workflow is None:
        raise EntityNotFoundError("WorkflowState", workflow_id)
    return workflow


@router.get("", response_model=list[WorkflowSummary])
async def list_workflows(
    status: WorkflowStatus | None = None,
    limit: int = Depends(bounded_limit),
    container: ApiContainer = Depends(get_container),
) -> list[WorkflowSummary]:
    started = time.perf_counter()
    workflows = await container.workflow_repo.list_recent(status=status, limit=limit)
    logger.info("api.query op=list_workflows count=%d duration_ms=%.1f", len(workflows), (time.perf_counter() - started) * 1000)
    return [WorkflowSummary.from_domain(w) for w in workflows]


@router.get("/{workflow_id}", response_model=WorkflowDetail)
async def get_workflow(workflow_id: str, container: ApiContainer = Depends(get_container)) -> WorkflowDetail:
    workflow = await _get_workflow_or_404(container, workflow_id)
    return WorkflowDetail.from_domain(workflow)


@router.get("/{workflow_id}/artifacts", response_model=list[ArtifactSummary])
async def list_workflow_artifacts(workflow_id: str, container: ApiContainer = Depends(get_container)) -> list[ArtifactSummary]:
    await _get_workflow_or_404(container, workflow_id)
    artifacts = await container.artifact_repo.list_for_workflow(workflow_id)
    return [ArtifactSummary.from_domain(a) for a in artifacts]


@router.get("/{workflow_id}/executions", response_model=list[ExecutionSummary])
async def list_workflow_executions(workflow_id: str, container: ApiContainer = Depends(get_container)) -> list[ExecutionSummary]:
    await _get_workflow_or_404(container, workflow_id)
    executions = await container.execution_repo.list_for_workflow(workflow_id)
    return [ExecutionSummary.from_domain(e) for e in executions]


@router.get("/{workflow_id}/decisions", response_model=list[DecisionSummary])
async def list_workflow_decisions(workflow_id: str, container: ApiContainer = Depends(get_container)) -> list[DecisionSummary]:
    await _get_workflow_or_404(container, workflow_id)
    decisions = await container.decision_repo.list_for_workflow(workflow_id)
    return [DecisionSummary.from_domain(d) for d in decisions]


@router.post("/{workflow_id}/step", response_model=WorkflowDetail)
async def step_workflow(workflow_id: str, container: ApiContainer = Depends(get_container)) -> WorkflowDetail:
    """Delegates directly to OrchestrationService.execute_next_step — the
    SAME step-wise execution mechanism every other caller (DemoHarness,
    tests) already uses. No second workflow engine, no stage/agent
    selection logic here."""
    started = time.perf_counter()
    workflow = await container.orchestration.execute_next_step(workflow_id)
    logger.info(
        "api.command op=step_workflow workflow_id=%s stage=%s status=%s duration_ms=%.1f",
        workflow_id,
        workflow.current_stage.value,
        workflow.status.value,
        (time.perf_counter() - started) * 1000,
    )
    return WorkflowDetail.from_domain(workflow)
