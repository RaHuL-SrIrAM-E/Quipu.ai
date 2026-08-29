"""RemediationVerification query routes — the endpoint the UI needs to
draw the DEPLOYED != VERIFIED RESOLVED distinction (Invariant 8): outcome
is only ever whatever RemediationVerificationService already persisted —
this route never computes or infers a verification result itself."""

import time
from datetime import datetime

from fastapi import APIRouter, Depends

from app.api.container import ApiContainer
from app.api.dependencies import get_container
from app.api.pagination import bounded_limit
from app.api.schemas.verifications import VerificationSummary
from app.core.observability import get_logger
from app.domain import VerificationOutcome, VerificationStatus
from app.persistence.errors import EntityNotFoundError
from app.persistence.repositories.remediation_verification import RemediationVerificationQuery

logger = get_logger("quipu.api.verifications")
router = APIRouter(prefix="/verifications", tags=["verifications"])


@router.get("", response_model=list[VerificationSummary])
async def list_verifications(
    outcome: VerificationOutcome | None = None,
    status: VerificationStatus | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int = Depends(bounded_limit),
    container: ApiContainer = Depends(get_container),
) -> list[VerificationSummary]:
    started = time.perf_counter()
    query = RemediationVerificationQuery(outcome=outcome, status=status, since=since, until=until, limit=limit)
    verifications = await container.verification_repo.query(query)
    logger.info("api.query op=list_verifications count=%d duration_ms=%.1f", len(verifications), (time.perf_counter() - started) * 1000)
    return [VerificationSummary.from_domain(v) for v in verifications]


@router.get("/{verification_id}", response_model=VerificationSummary)
async def get_verification(verification_id: str, container: ApiContainer = Depends(get_container)) -> VerificationSummary:
    verification = await container.verification_repo.get(verification_id)
    if verification is None:
        raise EntityNotFoundError("RemediationVerification", verification_id)
    return VerificationSummary.from_domain(verification)
