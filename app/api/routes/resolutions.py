"""ResolutionResult query routes, plus the one remediation command this
control plane exposes — POST /resolutions/{resolution_id}/remediate,
which delegates directly to
OrchestrationService.start_remediation_from_resolution(). That method
already re-derives authorization/target agent deterministically from the
persisted ResolutionResult (never trusting resolution.target_agent) — see
app/orchestration/service.py's own docstring. This route accepts NO body:
there is nothing for a caller to supply — strategy, risk, and target agent
are exactly what start_remediation_from_resolution already re-derives, and
accepting any of them here would be exactly the "trust target_agent from
the request" this task explicitly forbids.
"""

import time
from datetime import datetime

from fastapi import APIRouter, Depends

from app.api.container import ApiContainer
from app.api.dependencies import get_container
from app.api.pagination import bounded_limit
from app.api.schemas.resolutions import ResolutionSummary
from app.api.schemas.workflows import WorkflowDetail
from app.core.observability import get_logger
from app.domain import RemediationRisk, RemediationStrategy
from app.persistence.errors import EntityNotFoundError
from app.persistence.repositories.resolution import ResolutionQuery

logger = get_logger("quipu.api.resolutions")
router = APIRouter(prefix="/resolutions", tags=["resolutions"])


@router.get("", response_model=list[ResolutionSummary])
async def list_resolutions(
    detection_id: str | None = None,
    remediation_strategy: RemediationStrategy | None = None,
    risk: RemediationRisk | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int = Depends(bounded_limit),
    container: ApiContainer = Depends(get_container),
) -> list[ResolutionSummary]:
    started = time.perf_counter()
    query = ResolutionQuery(detection_id=detection_id, remediation_strategy=remediation_strategy, risk=risk, since=since, until=until, limit=limit)
    resolutions = await container.resolution_repo.query(query)
    logger.info("api.query op=list_resolutions count=%d duration_ms=%.1f", len(resolutions), (time.perf_counter() - started) * 1000)
    return [ResolutionSummary.from_domain(r) for r in resolutions]


@router.get("/{resolution_id}", response_model=ResolutionSummary)
async def get_resolution(resolution_id: str, container: ApiContainer = Depends(get_container)) -> ResolutionSummary:
    resolution = await container.resolution_repo.get(resolution_id)
    if resolution is None:
        raise EntityNotFoundError("ResolutionResult", resolution_id)
    return ResolutionSummary.from_domain(resolution)


@router.post("/{resolution_id}/remediate", response_model=WorkflowDetail)
async def remediate_resolution(resolution_id: str, container: ApiContainer = Depends(get_container)) -> WorkflowDetail:
    started = time.perf_counter()
    workflow = await container.orchestration.start_remediation_from_resolution(resolution_id)
    logger.info(
        "api.command op=remediate_resolution resolution_id=%s workflow_id=%s status=%s duration_ms=%.1f",
        resolution_id,
        workflow.workflow_id,
        workflow.status.value,
        (time.perf_counter() - started) * 1000,
    )
    return WorkflowDetail.from_domain(workflow)
