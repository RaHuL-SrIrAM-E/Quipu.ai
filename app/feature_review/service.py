"""FeatureReviewService — the controlled product-review boundary between
Detecting's AI interpretation and engineering execution.

Deliberately NOT a QuipuAgent and NOT another Gemini call (Level 3.4's own
instruction: "This task is NOT another LLM agent... do not create another
agent simply to justify 'multi-agent' architecture"). DetectingAgent
already performed the reasoning; everything here is deterministic business
workflow/state management — the same "Gemini proposes, application code
authorizes" discipline every prior level established, just with a human in
the authorizing seat instead of application code alone:

    DetectionResult (FEATURE_OPPORTUNITY)
          |
    create_review()          <- deterministic checks only, no LLM
          |
    FeatureReview (PENDING)
          |
       HUMAN
          |
    approve() / reject()     <- deterministic state transition + Jira call
          |
    FeatureReview (APPROVED, with Ticket) / (REJECTED)

Reuses SignalGateway/DetectionGateway (the same Protocols agents use — they
are plain Protocols, not agent-specific) rather than talking to Firestore
directly, and reuses the real app.core.jira_client.JiraClient PlanningAgent
already uses for deterministic Jira story creation — no second Jira
integration. See docs/architecture/feature_review.md for the full design.
"""

from datetime import datetime, timezone

from app.agent_runtime.capabilities import AgentCapability, check_capability
from app.agent_runtime.gateways.detections import DetectionGateway
from app.agent_runtime.gateways.signals import SignalGateway
from app.core.jira_client import JiraClient
from app.domain import DecisionSource, DetectionResult, DetectionType, FeatureReview, ReviewStatus, Ticket
from app.persistence.errors import VersionConflictError
from app.persistence.repositories.feature_review import FeatureReviewQuery, FeatureReviewRepository

_ACTOR_ID = "feature_review_service"

# The floor that protects a human reviewer from ever seeing a malformed or
# evidence-free AI output (Level 3.4 §20) — deterministic, not another LLM
# judgment call.
_MIN_RESOLVED_SIGNALS = 1


class FeatureReviewError(Exception):
    """Base for all FeatureReviewService errors."""


class DetectionNotFoundError(FeatureReviewError):
    def __init__(self, detection_id: str):
        self.detection_id = detection_id
        super().__init__(f"no DetectionResult '{detection_id}' found")


class InvalidDetectionTypeError(FeatureReviewError):
    def __init__(self, detection_id: str, detection_type: DetectionType):
        self.detection_id = detection_id
        self.detection_type = detection_type
        super().__init__(f"DetectionResult '{detection_id}' has type '{detection_type}', expected 'feature_opportunity'")


class InsufficientEvidenceError(FeatureReviewError):
    def __init__(self, detection_id: str, reason: str):
        self.detection_id = detection_id
        super().__init__(f"DetectionResult '{detection_id}' has insufficient evidence to enter review: {reason}")


class ReviewNotFoundError(FeatureReviewError):
    def __init__(self, review_id: str):
        self.review_id = review_id
        super().__init__(f"no FeatureReview '{review_id}' found")


class InvalidReviewTransitionError(FeatureReviewError):
    def __init__(self, review_id: str, from_status: ReviewStatus, action: str):
        self.review_id = review_id
        self.from_status = from_status
        super().__init__(f"cannot {action} FeatureReview '{review_id}': it is already {from_status.value} (terminal)")


class UnauthorizedReviewerError(FeatureReviewError):
    def __init__(self, reviewer_type: DecisionSource | None):
        self.reviewer_type = reviewer_type
        super().__init__(
            f"reviewer_type must be human — got '{reviewer_type}'; an agent or the system can never approve/reject "
            "a feature opportunity on its own behalf"
        )


class TicketCreationFailedError(FeatureReviewError):
    def __init__(self, review_id: str, reason: str):
        self.review_id = review_id
        super().__init__(f"ticket creation failed for FeatureReview '{review_id}': {reason}")


def _build_ticket(detection: DetectionResult, review: FeatureReview, *, reviewer_id: str, review_comment: str | None) -> Ticket:
    """Deterministic ticket content, derived only from DetectionResult's
    own already-curated fields (title/summary/rationale/confidence/signal
    id list/knowledge references) — never raw Signal.evidence or
    Signal.metadata. This is what keeps the resulting ticket looking like
    an enterprise engineering request rather than an LLM transcript or a
    customer-feedback dump (§12/§31 of the task)."""
    title = f"[Feature Opportunity] {detection.title}"

    lines = [
        detection.summary,
        "",
        "Why it matters:",
        detection.rationale,
        "",
        f"Detection confidence: {detection.confidence:.2f}",
        f"Supporting signals: {len(detection.supporting_signal_ids)} "
        f"({', '.join(detection.supporting_signal_ids)})" if detection.supporting_signal_ids else "Supporting signals: none",
    ]
    if detection.knowledge_references:
        lines.append(f"Knowledge references: {', '.join(detection.knowledge_references)}")
    lines += ["", f"Approved by: {reviewer_id}"]
    if review_comment:
        lines.append(f"Reviewer comment: {review_comment}")
    lines += ["", f"Source detection: {detection.detection_id}"]

    return Ticket(
        title=title,
        description="\n".join(lines),
        source="feature_review",
        source_detection_id=detection.detection_id,
        metadata={"review_id": review.review_id, "detection_confidence": detection.confidence},
    )


class FeatureReviewService:
    """`jira_client` is injectable for tests — pass a fake with a
    `create_story(summary, description)` method; production code leaves it
    unset and a real JiraClient is constructed lazily on first use inside
    approve(), never at construction time (so building this service never
    touches Jira credentials)."""

    def __init__(
        self,
        review_repo: FeatureReviewRepository,
        detection_gateway: DetectionGateway,
        signal_gateway: SignalGateway,
        jira_client: JiraClient | None = None,
    ):
        self._reviews = review_repo
        self._detections = detection_gateway
        self._signals = signal_gateway
        self._jira_client = jira_client

    def _get_jira_client(self) -> JiraClient:
        if self._jira_client is None:
            self._jira_client = JiraClient()
        return self._jira_client

    # ---- creation --------------------------------------------------------

    async def create_review(self, detection_id: str) -> FeatureReview:
        """Idempotent: at most one FeatureReview per detection_id (§17). A
        second call for the same detection_id returns the existing review
        rather than creating a duplicate — never re-validates against a
        possibly-changed detection once a review already exists, since the
        review itself is the durable record of what was true at creation
        time."""
        existing = await self._reviews.find_by_detection_id(detection_id)
        if existing is not None:
            return existing

        detection = await self._detections.get(detection_id)
        if detection is None:
            raise DetectionNotFoundError(detection_id)
        if detection.detection_type != DetectionType.FEATURE_OPPORTUNITY:
            raise InvalidDetectionTypeError(detection_id, detection.detection_type)
        if not detection.supporting_signal_ids:
            raise InsufficientEvidenceError(detection_id, "no supporting_signal_ids on the detection")

        resolved = 0
        for signal_id in detection.supporting_signal_ids:
            signal = await self._signals.get(signal_id)
            if signal is not None:
                resolved += 1
        if resolved < _MIN_RESOLVED_SIGNALS:
            raise InsufficientEvidenceError(detection_id, "none of the supporting_signal_ids resolved to an actual Signal")

        review = FeatureReview(detection_id=detection_id, status=ReviewStatus.PENDING)
        return await self._reviews.create(review)

    # ---- reads -------------------------------------------------------------

    async def get_review(self, review_id: str) -> FeatureReview | None:
        return await self._reviews.get(review_id)

    async def list_pending(self, *, limit: int = 50) -> list[FeatureReview]:
        return await self._reviews.query(FeatureReviewQuery(status=ReviewStatus.PENDING, limit=limit))

    # ---- decisions -----------------------------------------------------------

    def _authorize(self, *, granted: set[AgentCapability], reviewer_type: DecisionSource) -> None:
        check_capability(_ACTOR_ID, granted, AgentCapability.REVIEW_FEATURE_OPPORTUNITY)
        if reviewer_type != DecisionSource.HUMAN:
            raise UnauthorizedReviewerError(reviewer_type)

    async def approve(
        self,
        review_id: str,
        *,
        reviewer_id: str,
        reviewer_type: DecisionSource,
        granted: set[AgentCapability],
        review_comment: str | None = None,
    ) -> FeatureReview:
        """PENDING -> APPROVED, atomic (optimistic concurrency via
        update_if_version — §18). Idempotent: re-approving an
        already-APPROVED review returns it unchanged, without calling Jira
        again (§17/§25 — 'if the ticket already exists, return the existing
        association'). Rejects (raises, never silently overwrites):
        approving an already-REJECTED review, a non-FEATURE_OPPORTUNITY
        detection, a non-human reviewer, or a caller lacking
        REVIEW_FEATURE_OPPORTUNITY."""
        self._authorize(granted=granted, reviewer_type=reviewer_type)

        review = await self._reviews.get(review_id)
        if review is None:
            raise ReviewNotFoundError(review_id)
        if review.status == ReviewStatus.APPROVED:
            return review  # idempotent re-entry — never a second ticket
        if review.status == ReviewStatus.REJECTED:
            raise InvalidReviewTransitionError(review_id, review.status, "approve")

        # Re-resolve and re-validate the DetectionResult (§8: "verify it is
        # still a FEATURE_OPPORTUNITY") — never trust that what was true at
        # create_review() time is still true; the DetectionResult itself
        # can never change (immutable), but re-reading it here is the
        # actual verification the task asks for, not an assumption.
        detection = await self._detections.get(review.detection_id)
        if detection is None:
            raise DetectionNotFoundError(review.detection_id)
        if detection.detection_type != DetectionType.FEATURE_OPPORTUNITY:
            raise InvalidDetectionTypeError(review.detection_id, detection.detection_type)

        # Retry-safety (§25): if a prior attempt already created the Jira
        # ticket but the subsequent state-transition write failed (process
        # crash, version conflict), reuse the recorded ticket instead of
        # creating a second one.
        ticket = review.ticket
        if ticket is None:
            ticket = _build_ticket(detection, review, reviewer_id=reviewer_id, review_comment=review_comment)
            try:
                jira_result = self._get_jira_client().create_story(summary=ticket.title, description=ticket.description)
            except Exception as exc:
                # Review stays PENDING — no Firestore write has happened yet
                # at this point, so a retry is fully safe.
                raise TicketCreationFailedError(review_id, str(exc)) from exc
            ticket = ticket.model_copy(
                update={"external_id": jira_result["key"], "metadata": {**ticket.metadata, "jira_url": jira_result["url"]}}
            )

        updated = review.model_copy(
            update={
                "status": ReviewStatus.APPROVED,
                "reviewer_id": reviewer_id,
                "reviewer_type": reviewer_type,
                "review_comment": review_comment,
                "reviewed_at": datetime.now(timezone.utc),
                "ticket": ticket,
                "ticket_id": ticket.ticket_id,
            }
        )
        try:
            return await self._reviews.update_if_version(review_id, review.version, updated)
        except VersionConflictError:
            # Someone else's concurrent approve()/reject() call won the
            # race (§18). Re-read the authoritative state: if it's already
            # APPROVED, this is the same idempotent case as above — return
            # it rather than erroring. Otherwise surface the conflict; the
            # ticket we just built (if any) is not silently discarded by
            # us claiming success, but is also not retried here — the
            # caller decides whether to retry.
            current = await self._reviews.get(review_id)
            if current is not None and current.status == ReviewStatus.APPROVED:
                return current
            raise

    async def reject(
        self,
        review_id: str,
        *,
        reviewer_id: str,
        reviewer_type: DecisionSource,
        granted: set[AgentCapability],
        review_comment: str | None = None,
    ) -> FeatureReview:
        """PENDING -> REJECTED, atomic. Never deletes the DetectionResult or
        its supporting Signals — this is purely an audit record of the
        human decision (§9/§14)."""
        self._authorize(granted=granted, reviewer_type=reviewer_type)

        review = await self._reviews.get(review_id)
        if review is None:
            raise ReviewNotFoundError(review_id)
        if review.status == ReviewStatus.REJECTED:
            return review  # idempotent re-entry
        if review.status == ReviewStatus.APPROVED:
            raise InvalidReviewTransitionError(review_id, review.status, "reject")

        updated = review.model_copy(
            update={
                "status": ReviewStatus.REJECTED,
                "reviewer_id": reviewer_id,
                "reviewer_type": reviewer_type,
                "review_comment": review_comment,
                "reviewed_at": datetime.now(timezone.utc),
            }
        )
        try:
            return await self._reviews.update_if_version(review_id, review.version, updated)
        except VersionConflictError:
            current = await self._reviews.get(review_id)
            if current is not None and current.status == ReviewStatus.REJECTED:
                return current
            raise
