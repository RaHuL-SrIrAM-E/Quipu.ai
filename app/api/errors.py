"""Central exception -> HTTP mapping. Every existing application error
hierarchy (persistence, orchestration, feature review, verification,
capability) is translated here, once — route handlers never catch these
themselves and never construct an HTTPException from a raw exception
message (Invariant 1: no business logic, and no error-shaping logic
either, inside a handler). See docs/architecture/control_plane_api.md
"Error handling".

Never leaks: stack traces, Firestore/Google SDK exception internals,
Gemini errors, or the internal exception's own str() for a generic
failure — those are logged server-side (with the correlation id) and
replaced with a fixed, safe message before reaching the client.
"""

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.agent_runtime.capabilities import CapabilityError
from app.core.observability import get_logger
from app.feature_review.service import (
    DetectionNotFoundError,
    FeatureReviewError,
    InsufficientEvidenceError,
    InvalidDetectionTypeError,
    InvalidReviewTransitionError,
    ReviewNotFoundError,
    TicketCreationFailedError,
    UnauthorizedReviewerError,
)
from app.orchestration.errors import InvalidTransitionError, OrchestrationError, UnknownAgentError
from app.persistence.errors import DuplicateEntityError, EntityNotFoundError, VersionConflictError
from app.verification.errors import VerificationError

logger = get_logger("quipu.api.errors")


def _error(request: Request, status_code: int, error: str, detail: str) -> JSONResponse:
    correlation_id = getattr(request.state, "correlation_id", None)
    return JSONResponse(status_code=status_code, content={"error": error, "detail": detail, "correlation_id": correlation_id})


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        return _error(request, 422, "validation_error", "the request did not match the expected shape")

    @app.exception_handler(EntityNotFoundError)
    async def _not_found(request: Request, exc: EntityNotFoundError) -> JSONResponse:
        return _error(request, 404, "not_found", f"{exc.entity_type} '{exc.entity_id}' not found")

    @app.exception_handler(DuplicateEntityError)
    async def _duplicate(request: Request, exc: DuplicateEntityError) -> JSONResponse:
        return _error(request, 409, "conflict", f"{exc.entity_type} '{exc.entity_id}' already exists")

    @app.exception_handler(VersionConflictError)
    async def _version_conflict(request: Request, exc: VersionConflictError) -> JSONResponse:
        return _error(request, 409, "version_conflict", "the resource was modified concurrently — reload and retry")

    @app.exception_handler(CapabilityError)
    async def _capability_error(request: Request, exc: CapabilityError) -> JSONResponse:
        return _error(request, 403, "forbidden", "the requested action is not authorized")

    @app.exception_handler(UnauthorizedReviewerError)
    async def _unauthorized_reviewer(request: Request, exc: UnauthorizedReviewerError) -> JSONResponse:
        return _error(request, 403, "forbidden", "only a human reviewer identity may approve or reject a feature review")

    @app.exception_handler(ReviewNotFoundError)
    async def _review_not_found(request: Request, exc: ReviewNotFoundError) -> JSONResponse:
        return _error(request, 404, "not_found", str(exc))

    @app.exception_handler(DetectionNotFoundError)
    async def _detection_not_found(request: Request, exc: DetectionNotFoundError) -> JSONResponse:
        return _error(request, 404, "not_found", str(exc))

    @app.exception_handler(InvalidDetectionTypeError)
    async def _invalid_detection_type(request: Request, exc: InvalidDetectionTypeError) -> JSONResponse:
        return _error(request, 422, "business_rule_violation", str(exc))

    @app.exception_handler(InsufficientEvidenceError)
    async def _insufficient_evidence(request: Request, exc: InsufficientEvidenceError) -> JSONResponse:
        return _error(request, 422, "business_rule_violation", str(exc))

    @app.exception_handler(InvalidReviewTransitionError)
    async def _invalid_review_transition(request: Request, exc: InvalidReviewTransitionError) -> JSONResponse:
        return _error(request, 409, "invalid_transition", str(exc))

    @app.exception_handler(TicketCreationFailedError)
    async def _ticket_creation_failed(request: Request, exc: TicketCreationFailedError) -> JSONResponse:
        logger.warning("api.dependency_failure detail=ticket_creation_failed review_id=%s", exc.review_id)
        return _error(request, 503, "dependency_unavailable", "the external tracker (Jira) could not be reached — try again")

    @app.exception_handler(FeatureReviewError)
    async def _feature_review_error(request: Request, exc: FeatureReviewError) -> JSONResponse:
        return _error(request, 422, "business_rule_violation", str(exc))

    @app.exception_handler(VerificationError)
    async def _verification_error(request: Request, exc: VerificationError) -> JSONResponse:
        return _error(request, 422, "business_rule_violation", str(exc))

    @app.exception_handler(UnknownAgentError)
    async def _unknown_agent(request: Request, exc: UnknownAgentError) -> JSONResponse:
        return _error(request, 404, "not_found", str(exc))

    @app.exception_handler(InvalidTransitionError)
    async def _invalid_transition(request: Request, exc: InvalidTransitionError) -> JSONResponse:
        return _error(request, 409, "invalid_transition", str(exc))

    @app.exception_handler(OrchestrationError)
    async def _orchestration_error(request: Request, exc: OrchestrationError) -> JSONResponse:
        return _error(request, 422, "business_rule_violation", str(exc))

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        correlation_id = getattr(request.state, "correlation_id", None)
        logger.exception("api.unhandled_exception correlation_id=%s path=%s", correlation_id, request.url.path)
        return _error(request, 500, "internal_error", "an internal error occurred")
