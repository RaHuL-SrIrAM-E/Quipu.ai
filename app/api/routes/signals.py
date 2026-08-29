"""Signal query routes — delegate entirely to the existing
SignalRepository/SignalQuery (app.persistence.repositories.signal). No
new filtering/query logic lives here."""

import time
from datetime import datetime

from fastapi import APIRouter, Depends

from app.api.container import ApiContainer
from app.api.dependencies import get_container
from app.api.pagination import bounded_limit
from app.api.schemas.signals import SignalDetail, SignalSummary
from app.core.observability import get_logger
from app.domain import SignalSeverity, SignalSource, SignalStatus, SignalType
from app.persistence.errors import EntityNotFoundError
from app.persistence.repositories.signal import SignalQuery

logger = get_logger("quipu.api.signals")
router = APIRouter(prefix="/signals", tags=["signals"])


@router.get("", response_model=list[SignalSummary])
async def list_signals(
    signal_type: SignalType | None = None,
    source: SignalSource | None = None,
    severity: SignalSeverity | None = None,
    status: SignalStatus | None = None,
    service_name: str | None = None,
    environment: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int = Depends(bounded_limit),
    container: ApiContainer = Depends(get_container),
) -> list[SignalSummary]:
    started = time.perf_counter()
    query = SignalQuery(
        signal_type=signal_type,
        source=source,
        severity=severity,
        status=status,
        service_name=service_name,
        environment=environment,
        since=since,
        until=until,
        limit=limit,
    )
    signals = await container.signal_repo.query(query)
    logger.info("api.query op=list_signals count=%d duration_ms=%.1f", len(signals), (time.perf_counter() - started) * 1000)
    return [SignalSummary.from_domain(s) for s in signals]


@router.get("/{signal_id}", response_model=SignalDetail)
async def get_signal(signal_id: str, container: ApiContainer = Depends(get_container)) -> SignalDetail:
    signal = await container.signal_repo.get(signal_id)
    if signal is None:
        raise EntityNotFoundError("Signal", signal_id)
    return SignalDetail.from_domain(signal)
