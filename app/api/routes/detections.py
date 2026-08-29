"""DetectionResult query routes — delegate entirely to
DetectionRepository/DetectionQuery."""

import time
from datetime import datetime

from fastapi import APIRouter, Depends

from app.api.container import ApiContainer
from app.api.dependencies import get_container
from app.api.pagination import bounded_limit
from app.api.schemas.detections import DetectionSummary
from app.core.observability import get_logger
from app.domain import DetectionDomain, DetectionType
from app.persistence.errors import EntityNotFoundError
from app.persistence.repositories.detection import DetectionQuery

logger = get_logger("quipu.api.detections")
router = APIRouter(prefix="/detections", tags=["detections"])


@router.get("", response_model=list[DetectionSummary])
async def list_detections(
    detection_type: DetectionType | None = None,
    domain: DetectionDomain | None = None,
    service_name: str | None = None,
    environment: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int = Depends(bounded_limit),
    container: ApiContainer = Depends(get_container),
) -> list[DetectionSummary]:
    started = time.perf_counter()
    query = DetectionQuery(
        detection_type=detection_type, domain=domain, service_name=service_name, environment=environment, since=since, until=until, limit=limit
    )
    detections = await container.detection_repo.query(query)
    logger.info("api.query op=list_detections count=%d duration_ms=%.1f", len(detections), (time.perf_counter() - started) * 1000)
    return [DetectionSummary.from_domain(d) for d in detections]


@router.get("/{detection_id}", response_model=DetectionSummary)
async def get_detection(detection_id: str, container: ApiContainer = Depends(get_container)) -> DetectionSummary:
    detection = await container.detection_repo.get(detection_id)
    if detection is None:
        raise EntityNotFoundError("DetectionResult", detection_id)
    return DetectionSummary.from_domain(detection)
