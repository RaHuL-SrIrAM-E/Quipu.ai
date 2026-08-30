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
from app.api.schemas.workflows import ArtifactSummary, DecisionSummary, ExecutionSummary, WorkflowDetail, WorkflowRunResult, WorkflowSummary
from app.config import get_settings
from app.core.observability import get_logger
from app.domain import WorkflowStatus
from app.persistence.errors import EntityNotFoundError

_HUMAN_ACTION_STATUSES = {WorkflowStatus.ESCALATED, WorkflowStatus.FAILED, WorkflowStatus.BLOCKED}

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


@router.post("/{workflow_id}/run", response_model=WorkflowRunResult)
async def run_workflow(workflow_id: str, container: ApiContainer = Depends(get_container)) -> WorkflowRunResult:
    """Delegates entirely to OrchestrationService.run_to_completion() —
    the SAME existing step-wise execute_next_step() loop the '/step'
    command uses, just repeated (bounded by
    Settings.workflow_run_max_iterations) until a terminal status or the
    iteration cap is hit. No second workflow engine, no direct agent
    invocation, no bypass of transition policy/retry budgets/capability
    checks/Firestore versioning — everything below this route is
    identical to what a human clicking 'Run Next Step' repeatedly would
    produce. Safe to call repeatedly: a workflow already in a terminal
    status (COMPLETED/FAILED/ESCALATED/CANCELLED) is returned unchanged
    by execute_next_step() itself, so re-invoking this endpoint never
    duplicates completed work."""
    initial = await _get_workflow_or_404(container, workflow_id)
    initial_decision_count = len(await container.decision_repo.list_for_workflow(workflow_id))

    started = time.perf_counter()
    final = await container.orchestration.run_to_completion(workflow_id, max_steps=get_settings().workflow_run_max_iterations)
    duration_ms = (time.perf_counter() - started) * 1000

    new_artifact_ids = [a for a in final.artifact_ids if a not in initial.artifact_ids]
    stages_executed = []
    for artifact_id in new_artifact_ids:
        artifact = await container.artifact_repo.get(workflow_id, artifact_id)
        if artifact is not None:
            stages_executed.append(artifact.artifact_type.value)
    final_decision_count = len(await container.decision_repo.list_for_workflow(workflow_id))
    retries_used = sum(v for k, v in final.metadata.items() if isinstance(v, int) and k.startswith("retry_count:"))

    result = WorkflowRunResult(
        workflow_id=workflow_id,
        initial_stage=initial.current_stage,
        final_stage=final.current_stage,
        final_status=final.status,
        stages_executed=stages_executed,
        artifacts_created=len(new_artifact_ids),
        decisions_created=final_decision_count - initial_decision_count,
        retries_used=retries_used,
        duration_ms=duration_ms,
        human_action_required=final.status in _HUMAN_ACTION_STATUSES,
    )
    logger.info(
        "api.command op=run_workflow workflow_id=%s initial_stage=%s final_stage=%s final_status=%s stages_executed=%d duration_ms=%.1f",
        workflow_id,
        result.initial_stage.value,
        result.final_stage.value,
        result.final_status.value,
        len(stages_executed),
        duration_ms,
    )
    return result


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


@router.post("/{workflow_id}/retry", response_model=WorkflowDetail)
async def retry_workflow(workflow_id: str, container: ApiContainer = Depends(get_container)) -> WorkflowDetail:
    """Reopens a FAILED workflow so it can be re-executed from the stage it
    failed at — delegates entirely to OrchestrationService.
    retry_failed_workflow(), same "route contains no business logic"
    invariant as /run, /step, /remediate, /start-workflow. No request
    body: there is nothing for a caller to supply, the resume stage is
    exactly the workflow's own current_stage. Retrying does NOT execute
    anything itself — a separate, explicit /run or /step call is still
    required, same as calling start_workflow_from_review()/
    start_remediation_from_resolution() never auto-executes either."""
    started = time.perf_counter()
    workflow = await container.orchestration.retry_failed_workflow(workflow_id)
    logger.info(
        "api.command op=retry_workflow workflow_id=%s stage=%s status=%s duration_ms=%.1f",
        workflow_id,
        workflow.current_stage.value,
        workflow.status.value,
        (time.perf_counter() - started) * 1000,
    )
    return WorkflowDetail.from_domain(workflow)
