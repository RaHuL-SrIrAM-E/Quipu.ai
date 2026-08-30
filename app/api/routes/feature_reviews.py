"""FeatureReview query/command routes. approve()/reject() delegate
entirely to FeatureReviewService — this module contains none of that
service's authorization or state-transition logic (Invariant 1/2).
reviewer_type is always DecisionSource.HUMAN, fixed by
app.api.auth.require_reviewer_identity, never accepted from the request
body — an agent can never self-approve a review through this API.

start_workflow_from_review() is the separate, explicit action that closes
the FeatureReview -> WorkflowState boundary — delegates entirely to
OrchestrationService.start_workflow_from_review(), same "route contains no
business logic" invariant. Never invoked automatically by approve()."""

import time

from fastapi import APIRouter, Depends

from app.api.auth import ReviewerIdentity, require_reviewer_identity
from app.api.container import ApiContainer
from app.api.dependencies import get_container
from app.api.pagination import bounded_limit
from app.api.schemas.feature_reviews import FeatureReviewSummary, ReviewDecisionRequest
from app.api.schemas.workflows import WorkflowDetail
from app.core.observability import get_logger
from app.persistence.errors import EntityNotFoundError
from app.persistence.repositories.feature_review import FeatureReviewQuery

logger = get_logger("quipu.api.feature_reviews")
router = APIRouter(prefix="/feature-reviews", tags=["feature-reviews"])


@router.get("", response_model=list[FeatureReviewSummary])
async def list_feature_reviews(limit: int = Depends(bounded_limit), container: ApiContainer = Depends(get_container)) -> list[FeatureReviewSummary]:
    started = time.perf_counter()
    reviews = await container.review_repo.query(FeatureReviewQuery(limit=limit))
    logger.info("api.query op=list_feature_reviews count=%d duration_ms=%.1f", len(reviews), (time.perf_counter() - started) * 1000)
    return [FeatureReviewSummary.from_domain(r) for r in reviews]


@router.get("/{review_id}", response_model=FeatureReviewSummary)
async def get_feature_review(review_id: str, container: ApiContainer = Depends(get_container)) -> FeatureReviewSummary:
    review = await container.review_repo.get(review_id)
    if review is None:
        raise EntityNotFoundError("FeatureReview", review_id)
    return FeatureReviewSummary.from_domain(review)


@router.post("/{review_id}/approve", response_model=FeatureReviewSummary)
async def approve_feature_review(
    review_id: str,
    body: ReviewDecisionRequest,
    identity: ReviewerIdentity = Depends(require_reviewer_identity),
    container: ApiContainer = Depends(get_container),
) -> FeatureReviewSummary:
    started = time.perf_counter()
    review = await container.review_service.approve(
        review_id,
        reviewer_id=identity.reviewer_id,
        reviewer_type=identity.reviewer_type,
        granted=set(identity.granted),
        review_comment=body.review_comment,
    )
    logger.info(
        "api.command op=approve_feature_review review_id=%s reviewer_id=%s status=%s duration_ms=%.1f",
        review_id,
        identity.reviewer_id,
        review.status.value,
        (time.perf_counter() - started) * 1000,
    )
    return FeatureReviewSummary.from_domain(review)


@router.post("/{review_id}/start-workflow", response_model=WorkflowDetail)
async def start_workflow_from_review(review_id: str, container: ApiContainer = Depends(get_container)) -> WorkflowDetail:
    """The explicit human/authorized-caller action that closes the
    FeatureReview -> WorkflowState boundary — delegates entirely to
    OrchestrationService.start_workflow_from_review(), which already
    re-validates the review is APPROVED (with a ticket) before creating
    anything, and is idempotent (a review that already has a workflow_id
    returns that same WorkflowState). No request body: there is nothing
    for a caller to supply — the ticket/plan come entirely from the
    already-approved FeatureReview, same rationale as
    app.api.routes.resolutions.remediate_resolution. Deliberately never
    called automatically from FeatureReviewService.approve() or the
    Pub/Sub worker — starting engineering work stays a distinct,
    explicit action from approving the opportunity."""
    started = time.perf_counter()
    workflow = await container.orchestration.start_workflow_from_review(review_id)
    logger.info(
        "api.command op=start_workflow_from_review review_id=%s workflow_id=%s status=%s duration_ms=%.1f",
        review_id,
        workflow.workflow_id,
        workflow.status.value,
        (time.perf_counter() - started) * 1000,
    )
    return WorkflowDetail.from_domain(workflow)


@router.post("/{review_id}/reject", response_model=FeatureReviewSummary)
async def reject_feature_review(
    review_id: str,
    body: ReviewDecisionRequest,
    identity: ReviewerIdentity = Depends(require_reviewer_identity),
    container: ApiContainer = Depends(get_container),
) -> FeatureReviewSummary:
    started = time.perf_counter()
    review = await container.review_service.reject(
        review_id,
        reviewer_id=identity.reviewer_id,
        reviewer_type=identity.reviewer_type,
        granted=set(identity.granted),
        review_comment=body.review_comment,
    )
    logger.info(
        "api.command op=reject_feature_review review_id=%s reviewer_id=%s status=%s duration_ms=%.1f",
        review_id,
        identity.reviewer_id,
        review.status.value,
        (time.perf_counter() - started) * 1000,
    )
    return FeatureReviewSummary.from_domain(review)
