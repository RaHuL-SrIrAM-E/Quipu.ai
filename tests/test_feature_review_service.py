"""FeatureReviewService tests. No LLM/ADK anywhere in this module — Feature
Review is deterministic business workflow, not an agent. A fake Jira client
is injected; no real Jira/Firestore required."""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from app.agent_runtime.capabilities import AgentCapability
from app.agent_runtime.gateways.detections import RepositoryDetectionGateway
from app.agent_runtime.gateways.signals import RepositorySignalGateway
from app.domain import (
    DecisionSource,
    DetectionDomain,
    DetectionResult,
    DetectionType,
    ReviewStatus,
    Signal,
    SignalProvenance,
    SignalSeverity,
    SignalSource,
    SignalType,
    compute_detection_fingerprint,
    compute_fingerprint,
)
from app.feature_review import (
    DetectionNotFoundError,
    FeatureReviewService,
    InsufficientEvidenceError,
    InvalidDetectionTypeError,
    InvalidReviewTransitionError,
    ReviewNotFoundError,
    TicketCreationFailedError,
    UnauthorizedReviewerError,
)
from app.persistence.memory import InMemoryDetectionRepository, InMemoryFeatureReviewRepository, InMemorySignalRepository
from app.persistence.repositories.feature_review import FeatureReviewQuery

NOW = datetime.now(timezone.utc)
GRANTED = {AgentCapability.REVIEW_FEATURE_OPPORTUNITY}


class FakeJiraClient:
    def __init__(self, *, fail: bool = False):
        self.calls = 0  # attempted calls, successful or not
        self.successful_calls = 0
        self._fail = fail

    def create_story(self, summary: str, description: str) -> dict:
        self.calls += 1
        if self._fail:
            raise RuntimeError("jira unavailable")
        self.successful_calls += 1
        return {"key": f"QUIPU-{self.successful_calls}", "url": f"https://jira.example.com/browse/QUIPU-{self.successful_calls}"}


def make_signal(source_event_id: str, *, stype=SignalType.CUSTOMER_FEEDBACK, source=SignalSource.CUSTOMER_FEEDBACK, subject="export") -> Signal:
    return Signal(
        signal_type=stype,
        source=source,
        severity=SignalSeverity.INFO,
        observed_at=NOW,
        subject=subject,
        summary=f"signal {source_event_id}",
        provenance=SignalProvenance(source_system="x", source_event_id=source_event_id),
        fingerprint=compute_fingerprint(source=source, source_event_id=source_event_id, subject=subject),
    )


def make_detection(signals, *, detection_type=DetectionType.FEATURE_OPPORTUNITY, domain=DetectionDomain.PRODUCT, **overrides) -> DetectionResult:
    signal_ids = [s.signal_id for s in signals]
    defaults = dict(
        detection_type=detection_type,
        domain=domain,
        title="Excel export requested",
        summary="Multiple customers want Excel export",
        rationale="Repeated, independent feedback over the last two weeks",
        confidence=0.91,
        subject="export",
        supporting_signal_ids=signal_ids,
        observation_window_minutes=10080,
        fingerprint=compute_detection_fingerprint(detection_type=detection_type, subject="export", supporting_signal_ids=signal_ids, window_minutes=10080),
    )
    defaults.update(overrides)
    return DetectionResult(**defaults)


def make_service(*, signal_repo=None, detection_repo=None, review_repo=None, jira_client=None):
    signal_repo = signal_repo or InMemorySignalRepository()
    detection_repo = detection_repo or InMemoryDetectionRepository()
    review_repo = review_repo or InMemoryFeatureReviewRepository()
    service = FeatureReviewService(
        review_repo, RepositoryDetectionGateway(detection_repo), RepositorySignalGateway(signal_repo), jira_client=jira_client or FakeJiraClient()
    )
    return service, signal_repo, detection_repo, review_repo


async def seed(signal_repo, detection_repo, **detection_overrides):
    signal = make_signal("1")
    await signal_repo.save(signal)
    detection = make_detection([signal], **detection_overrides)
    await detection_repo.save(detection)
    return signal, detection


# ---- create_review ----------------------------------------------------------


@pytest.mark.asyncio
async def test_create_review_for_valid_feature_opportunity():
    service, signal_repo, detection_repo, _ = make_service()
    _, detection = await seed(signal_repo, detection_repo)
    review = await service.create_review(detection.detection_id)
    assert review.status == ReviewStatus.PENDING
    assert review.detection_id == detection.detection_id


@pytest.mark.asyncio
async def test_create_review_missing_detection_rejected():
    service, _, _, _ = make_service()
    with pytest.raises(DetectionNotFoundError):
        await service.create_review("does-not-exist")


@pytest.mark.asyncio
async def test_create_review_incident_detection_rejected():
    service, signal_repo, detection_repo, _ = make_service()
    _, detection = await seed(signal_repo, detection_repo, detection_type=DetectionType.INCIDENT, domain=DetectionDomain.OPERATIONAL, severity=SignalSeverity.CRITICAL)
    with pytest.raises(InvalidDetectionTypeError):
        await service.create_review(detection.detection_id)


@pytest.mark.asyncio
async def test_create_review_no_supporting_signal_ids_rejected():
    service, signal_repo, detection_repo, _ = make_service()
    detection = make_detection([], supporting_signal_ids=[])
    await detection_repo.save(detection)
    with pytest.raises(InsufficientEvidenceError):
        await service.create_review(detection.detection_id)


@pytest.mark.asyncio
async def test_create_review_unresolvable_signal_references_rejected():
    service, signal_repo, detection_repo, _ = make_service()
    detection = make_detection([], supporting_signal_ids=["ghost-signal"])
    await detection_repo.save(detection)
    with pytest.raises(InsufficientEvidenceError):
        await service.create_review(detection.detection_id)


@pytest.mark.asyncio
async def test_create_review_idempotent_for_same_detection():
    service, signal_repo, detection_repo, review_repo = make_service()
    _, detection = await seed(signal_repo, detection_repo)
    first = await service.create_review(detection.detection_id)
    second = await service.create_review(detection.detection_id)
    assert first.review_id == second.review_id
    all_reviews = await review_repo.query(FeatureReviewQuery(limit=50))
    assert len(all_reviews) == 1


# ---- approve ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_approve_transitions_pending_to_approved():
    service, signal_repo, detection_repo, _ = make_service()
    _, detection = await seed(signal_repo, detection_repo)
    review = await service.create_review(detection.detection_id)
    approved = await service.approve(review.review_id, reviewer_id="pm@company.com", reviewer_type=DecisionSource.HUMAN, granted=GRANTED)
    assert approved.status == ReviewStatus.APPROVED
    assert approved.ticket is not None
    assert approved.ticket_id == approved.ticket.ticket_id


@pytest.mark.asyncio
async def test_approve_creates_exactly_one_ticket():
    jira = FakeJiraClient()
    service, signal_repo, detection_repo, _ = make_service(jira_client=jira)
    _, detection = await seed(signal_repo, detection_repo)
    review = await service.create_review(detection.detection_id)
    await service.approve(review.review_id, reviewer_id="pm@company.com", reviewer_type=DecisionSource.HUMAN, granted=GRANTED)
    assert jira.calls == 1


@pytest.mark.asyncio
async def test_reject_transitions_pending_to_rejected():
    service, signal_repo, detection_repo, _ = make_service()
    _, detection = await seed(signal_repo, detection_repo)
    review = await service.create_review(detection.detection_id)
    rejected = await service.reject(review.review_id, reviewer_id="pm@company.com", reviewer_type=DecisionSource.HUMAN, granted=GRANTED, review_comment="not a priority")
    assert rejected.status == ReviewStatus.REJECTED
    assert rejected.review_comment == "not a priority"
    assert rejected.ticket is None


# ---- state machine -----------------------------------------------------------


@pytest.mark.asyncio
async def test_approved_to_rejected_transition_forbidden():
    service, signal_repo, detection_repo, _ = make_service()
    _, detection = await seed(signal_repo, detection_repo)
    review = await service.create_review(detection.detection_id)
    await service.approve(review.review_id, reviewer_id="pm@company.com", reviewer_type=DecisionSource.HUMAN, granted=GRANTED)
    with pytest.raises(InvalidReviewTransitionError):
        await service.reject(review.review_id, reviewer_id="pm@company.com", reviewer_type=DecisionSource.HUMAN, granted=GRANTED)


@pytest.mark.asyncio
async def test_rejected_to_approved_transition_forbidden():
    service, signal_repo, detection_repo, _ = make_service()
    _, detection = await seed(signal_repo, detection_repo)
    review = await service.create_review(detection.detection_id)
    await service.reject(review.review_id, reviewer_id="pm@company.com", reviewer_type=DecisionSource.HUMAN, granted=GRANTED)
    with pytest.raises(InvalidReviewTransitionError):
        await service.approve(review.review_id, reviewer_id="pm@company.com", reviewer_type=DecisionSource.HUMAN, granted=GRANTED)


@pytest.mark.asyncio
async def test_duplicate_approval_is_idempotent_not_an_error():
    jira = FakeJiraClient()
    service, signal_repo, detection_repo, _ = make_service(jira_client=jira)
    _, detection = await seed(signal_repo, detection_repo)
    review = await service.create_review(detection.detection_id)
    first = await service.approve(review.review_id, reviewer_id="pm@company.com", reviewer_type=DecisionSource.HUMAN, granted=GRANTED)
    second = await service.approve(review.review_id, reviewer_id="pm2@company.com", reviewer_type=DecisionSource.HUMAN, granted=GRANTED)
    assert first.ticket_id == second.ticket_id
    assert jira.calls == 1  # not called again


@pytest.mark.asyncio
async def test_duplicate_rejection_is_idempotent():
    service, signal_repo, detection_repo, _ = make_service()
    _, detection = await seed(signal_repo, detection_repo)
    review = await service.create_review(detection.detection_id)
    first = await service.reject(review.review_id, reviewer_id="pm@company.com", reviewer_type=DecisionSource.HUMAN, granted=GRANTED)
    second = await service.reject(review.review_id, reviewer_id="pm2@company.com", reviewer_type=DecisionSource.HUMAN, granted=GRANTED)
    assert first.review_id == second.review_id
    assert second.status == ReviewStatus.REJECTED


@pytest.mark.asyncio
async def test_approve_missing_review_rejected():
    service, _, _, _ = make_service()
    with pytest.raises(ReviewNotFoundError):
        await service.approve("does-not-exist", reviewer_id="pm@company.com", reviewer_type=DecisionSource.HUMAN, granted=GRANTED)


# ---- concurrency -----------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_approvals_only_one_ticket_created():
    jira = FakeJiraClient()
    service, signal_repo, detection_repo, _ = make_service(jira_client=jira)
    _, detection = await seed(signal_repo, detection_repo)
    review = await service.create_review(detection.detection_id)

    results = await asyncio.gather(
        service.approve(review.review_id, reviewer_id="pm-a@company.com", reviewer_type=DecisionSource.HUMAN, granted=GRANTED),
        service.approve(review.review_id, reviewer_id="pm-b@company.com", reviewer_type=DecisionSource.HUMAN, granted=GRANTED),
        return_exceptions=True,
    )
    succeeded = [r for r in results if not isinstance(r, Exception)]
    assert len(succeeded) >= 1
    ticket_ids = {r.ticket_id for r in succeeded}
    assert len(ticket_ids) == 1  # both callers agree on exactly one ticket


@pytest.mark.asyncio
async def test_approve_vs_reject_race_only_one_wins():
    service, signal_repo, detection_repo, review_repo = make_service()
    _, detection = await seed(signal_repo, detection_repo)
    review = await service.create_review(detection.detection_id)

    results = await asyncio.gather(
        service.approve(review.review_id, reviewer_id="pm-a@company.com", reviewer_type=DecisionSource.HUMAN, granted=GRANTED),
        service.reject(review.review_id, reviewer_id="pm-b@company.com", reviewer_type=DecisionSource.HUMAN, granted=GRANTED),
        return_exceptions=True,
    )
    final = await review_repo.get(review.review_id)
    assert final.status in (ReviewStatus.APPROVED, ReviewStatus.REJECTED)
    # whichever won, the review is in a single, consistent terminal state
    non_exception_statuses = {r.status for r in results if not isinstance(r, Exception)}
    assert non_exception_statuses.issubset({final.status})


# ---- Ticket content / provenance -----------------------------------------------


@pytest.mark.asyncio
async def test_ticket_preserves_source_detection_id():
    service, signal_repo, detection_repo, _ = make_service()
    _, detection = await seed(signal_repo, detection_repo)
    review = await service.create_review(detection.detection_id)
    approved = await service.approve(review.review_id, reviewer_id="pm@company.com", reviewer_type=DecisionSource.HUMAN, granted=GRANTED)
    assert approved.ticket.source_detection_id == detection.detection_id


@pytest.mark.asyncio
async def test_ticket_preserves_evidence_references():
    service, signal_repo, detection_repo, _ = make_service()
    signal, detection = await seed(signal_repo, detection_repo)
    review = await service.create_review(detection.detection_id)
    approved = await service.approve(review.review_id, reviewer_id="pm@company.com", reviewer_type=DecisionSource.HUMAN, granted=GRANTED)
    assert signal.signal_id in approved.ticket.description


@pytest.mark.asyncio
async def test_ticket_does_not_contain_raw_signal_metadata():
    service, signal_repo, detection_repo, _ = make_service()
    signal = make_signal("1")
    signal = signal.model_copy(update={"metadata": {"customer_email": "leaked@customer.com", "internal_note": "should never appear"}})
    await signal_repo.save(signal)
    detection = make_detection([signal])
    await detection_repo.save(detection)
    review = await service.create_review(detection.detection_id)
    approved = await service.approve(review.review_id, reviewer_id="pm@company.com", reviewer_type=DecisionSource.HUMAN, granted=GRANTED)
    assert "leaked@customer.com" not in approved.ticket.description
    assert "should never appear" not in approved.ticket.description


@pytest.mark.asyncio
async def test_ticket_reflects_reviewer_and_comment():
    service, signal_repo, detection_repo, _ = make_service()
    _, detection = await seed(signal_repo, detection_repo)
    review = await service.create_review(detection.detection_id)
    approved = await service.approve(review.review_id, reviewer_id="pm@company.com", reviewer_type=DecisionSource.HUMAN, granted=GRANTED, review_comment="high customer demand")
    assert "pm@company.com" in approved.ticket.description
    assert "high customer demand" in approved.ticket.description


# ---- Jira failure handling -----------------------------------------------------


@pytest.mark.asyncio
async def test_jira_failure_keeps_review_pending():
    jira = FakeJiraClient(fail=True)
    service, signal_repo, detection_repo, review_repo = make_service(jira_client=jira)
    _, detection = await seed(signal_repo, detection_repo)
    review = await service.create_review(detection.detection_id)
    with pytest.raises(TicketCreationFailedError):
        await service.approve(review.review_id, reviewer_id="pm@company.com", reviewer_type=DecisionSource.HUMAN, granted=GRANTED)
    current = await review_repo.get(review.review_id)
    assert current.status == ReviewStatus.PENDING
    assert current.ticket is None


@pytest.mark.asyncio
async def test_retry_after_jira_recovery_succeeds_without_duplicate():
    jira = FakeJiraClient(fail=True)
    service, signal_repo, detection_repo, review_repo = make_service(jira_client=jira)
    _, detection = await seed(signal_repo, detection_repo)
    review = await service.create_review(detection.detection_id)
    with pytest.raises(TicketCreationFailedError):
        await service.approve(review.review_id, reviewer_id="pm@company.com", reviewer_type=DecisionSource.HUMAN, granted=GRANTED)

    jira._fail = False
    approved = await service.approve(review.review_id, reviewer_id="pm@company.com", reviewer_type=DecisionSource.HUMAN, granted=GRANTED)
    assert approved.status == ReviewStatus.APPROVED
    assert jira.calls == 2  # one failed attempt + one successful retry
    assert jira.successful_calls == 1  # exactly one ticket actually created


@pytest.mark.asyncio
async def test_retry_after_partial_success_does_not_duplicate_ticket():
    """Simulates: Jira succeeded on a prior attempt (ticket recorded on the
    review), but the review is still PENDING (as if the state-transition
    write hadn't completed yet) — retrying approve() must reuse the
    existing ticket, not call Jira again."""
    jira = FakeJiraClient()
    service, signal_repo, detection_repo, review_repo = make_service(jira_client=jira)
    _, detection = await seed(signal_repo, detection_repo)
    review = await service.create_review(detection.detection_id)

    from app.domain import Ticket

    partial_ticket = Ticket(title="[Feature Opportunity] X", description="Y", source_detection_id=detection.detection_id, external_id="QUIPU-99")
    stuck_review = review.model_copy(update={"ticket": partial_ticket, "ticket_id": partial_ticket.ticket_id})
    await review_repo.update_if_version(review.review_id, review.version, stuck_review)

    approved = await service.approve(review.review_id, reviewer_id="pm@company.com", reviewer_type=DecisionSource.HUMAN, granted=GRANTED)
    assert approved.ticket_id == partial_ticket.ticket_id
    assert jira.calls == 0  # never called — reused the already-recorded ticket


# ---- Authorization -----------------------------------------------------------


@pytest.mark.asyncio
async def test_agent_reviewer_type_rejected():
    service, signal_repo, detection_repo, _ = make_service()
    _, detection = await seed(signal_repo, detection_repo)
    review = await service.create_review(detection.detection_id)
    with pytest.raises(UnauthorizedReviewerError):
        await service.approve(review.review_id, reviewer_id="detecting_agent", reviewer_type=DecisionSource.AGENT, granted=GRANTED)


@pytest.mark.asyncio
async def test_system_reviewer_type_rejected():
    service, signal_repo, detection_repo, _ = make_service()
    _, detection = await seed(signal_repo, detection_repo)
    review = await service.create_review(detection.detection_id)
    with pytest.raises(UnauthorizedReviewerError):
        await service.reject(review.review_id, reviewer_id="scheduler", reviewer_type=DecisionSource.SYSTEM, granted=GRANTED)


@pytest.mark.asyncio
async def test_missing_capability_rejected():
    from app.agent_runtime.capabilities import CapabilityError

    service, signal_repo, detection_repo, _ = make_service()
    _, detection = await seed(signal_repo, detection_repo)
    review = await service.create_review(detection.detection_id)
    with pytest.raises(CapabilityError):
        await service.approve(review.review_id, reviewer_id="pm@company.com", reviewer_type=DecisionSource.HUMAN, granted=set())


@pytest.mark.asyncio
async def test_valid_human_reviewer_with_capability_accepted():
    service, signal_repo, detection_repo, _ = make_service()
    _, detection = await seed(signal_repo, detection_repo)
    review = await service.create_review(detection.detection_id)
    approved = await service.approve(review.review_id, reviewer_id="pm@company.com", reviewer_type=DecisionSource.HUMAN, granted=GRANTED)
    assert approved.status == ReviewStatus.APPROVED
    assert approved.reviewer_type == DecisionSource.HUMAN


# ---- Detection immutability -----------------------------------------------------


@pytest.mark.asyncio
async def test_detection_confidence_never_mutated_by_rejection():
    service, signal_repo, detection_repo, _ = make_service()
    _, detection = await seed(signal_repo, detection_repo)
    before = await detection_repo.get(detection.detection_id)
    review = await service.create_review(detection.detection_id)
    await service.reject(review.review_id, reviewer_id="pm@company.com", reviewer_type=DecisionSource.HUMAN, granted=GRANTED)
    after = await detection_repo.get(detection.detection_id)
    assert before == after
    assert after.confidence == 0.91


@pytest.mark.asyncio
async def test_detection_never_mutated_by_approval():
    service, signal_repo, detection_repo, _ = make_service()
    _, detection = await seed(signal_repo, detection_repo)
    before = await detection_repo.get(detection.detection_id)
    review = await service.create_review(detection.detection_id)
    await service.approve(review.review_id, reviewer_id="pm@company.com", reviewer_type=DecisionSource.HUMAN, granted=GRANTED)
    after = await detection_repo.get(detection.detection_id)
    assert before == after


# ---- list_pending / get_review --------------------------------------------------


@pytest.mark.asyncio
async def test_list_pending_returns_only_pending():
    service, signal_repo, detection_repo, _ = make_service()
    signal1 = make_signal("1")
    signal2 = make_signal("2")
    await signal_repo.save(signal1)
    await signal_repo.save(signal2)
    d1 = make_detection([signal1], subject="a", fingerprint=compute_detection_fingerprint(detection_type=DetectionType.FEATURE_OPPORTUNITY, subject="a", supporting_signal_ids=[signal1.signal_id], window_minutes=10080))
    d2 = make_detection([signal2], subject="b", fingerprint=compute_detection_fingerprint(detection_type=DetectionType.FEATURE_OPPORTUNITY, subject="b", supporting_signal_ids=[signal2.signal_id], window_minutes=10080))
    await detection_repo.save(d1)
    await detection_repo.save(d2)
    r1 = await service.create_review(d1.detection_id)
    r2 = await service.create_review(d2.detection_id)
    await service.approve(r1.review_id, reviewer_id="pm@company.com", reviewer_type=DecisionSource.HUMAN, granted=GRANTED)

    pending = await service.list_pending()
    assert len(pending) == 1
    assert pending[0].review_id == r2.review_id


@pytest.mark.asyncio
async def test_get_review_returns_none_when_missing():
    service, _, _, _ = make_service()
    assert await service.get_review("does-not-exist") is None


# ---- Security / no shell -----------------------------------------------------


def test_no_shell_or_llm_surface_in_feature_review_module():
    import inspect

    import app.feature_review.service as service_module

    source = inspect.getsource(service_module)
    assert "subprocess" not in source
    assert "shell=True" not in source
    assert "InMemoryRunner" not in source  # no ADK runner — not an agent
    assert "LlmAgent" not in source
    assert "import google" not in source
    assert "from google" not in source
