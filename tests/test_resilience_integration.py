"""Tests proving the resilience layer (app/core/resilience/) is actually
wired into a real external-boundary caller — FeatureReviewService's Jira
call — not just unit-tested in isolation. See
docs/architecture/resilience.md "Jira".
"""

from datetime import datetime, timezone
from unittest.mock import Mock

import pytest
import requests

from app.agent_runtime.capabilities import AgentCapability
from app.agent_runtime.gateways.detections import RepositoryDetectionGateway
from app.agent_runtime.gateways.signals import RepositorySignalGateway
from app.core.resilience.circuit_breaker import CircuitState
from app.domain import (
    DecisionSource,
    DetectionDomain,
    DetectionResult,
    DetectionType,
    Signal,
    SignalProvenance,
    SignalSeverity,
    SignalSource,
    SignalType,
    compute_detection_fingerprint,
    compute_fingerprint,
)
from app.feature_review import FeatureReviewService, TicketCreationFailedError
from app.persistence.memory import InMemoryDetectionRepository, InMemoryFeatureReviewRepository, InMemorySignalRepository

NOW = datetime.now(timezone.utc)
GRANTED = {AgentCapability.REVIEW_FEATURE_OPPORTUNITY}


def _http_error(status: int) -> requests.exceptions.HTTPError:
    response = Mock()
    response.status_code = status
    return requests.exceptions.HTTPError(response=response)


class _CountingJiraClient:
    def __init__(self, side_effects: list):
        self._side_effects = list(side_effects)
        self.calls = 0

    def create_story(self, summary: str, description: str) -> dict:
        self.calls += 1
        effect = self._side_effects.pop(0)
        if isinstance(effect, Exception):
            raise effect
        return effect


async def _setup(jira_client):
    signal_repo = InMemorySignalRepository()
    detection_repo = InMemoryDetectionRepository()
    review_repo = InMemoryFeatureReviewRepository()
    signal = Signal(
        signal_type=SignalType.CUSTOMER_FEEDBACK,
        source=SignalSource.CUSTOMER_FEEDBACK,
        severity=SignalSeverity.INFO,
        observed_at=NOW,
        subject="export",
        summary="please add csv export",
        provenance=SignalProvenance(source_system="x", source_event_id="s1"),
        fingerprint=compute_fingerprint(source=SignalSource.CUSTOMER_FEEDBACK, source_event_id="s1", subject="export"),
    )
    await signal_repo.save(signal)
    detection = DetectionResult(
        detection_type=DetectionType.FEATURE_OPPORTUNITY,
        domain=DetectionDomain.PRODUCT,
        title="Add CSV export",
        summary="summary",
        rationale="rationale",
        confidence=0.9,
        subject="export",
        supporting_signal_ids=[signal.signal_id],
        observation_window_minutes=10080,
        fingerprint=compute_detection_fingerprint(detection_type=DetectionType.FEATURE_OPPORTUNITY, subject="export", supporting_signal_ids=[signal.signal_id], window_minutes=10080),
    )
    await detection_repo.save(detection)
    service = FeatureReviewService(review_repo, RepositoryDetectionGateway(detection_repo), RepositorySignalGateway(signal_repo), jira_client=jira_client)
    review = await service.create_review(detection.detection_id)
    return service, review


@pytest.mark.asyncio
async def test_jira_transient_failure_then_success_is_retried():
    jira_client = _CountingJiraClient([_http_error(503), {"key": "QUIPU-1", "url": "https://x/QUIPU-1"}])
    service, review = await _setup(jira_client)

    approved = await service.approve(review.review_id, reviewer_id="alice", reviewer_type=DecisionSource.HUMAN, granted=GRANTED)

    assert jira_client.calls == 2
    assert approved.ticket.external_id == "QUIPU-1"


@pytest.mark.asyncio
async def test_jira_permanent_failure_is_never_retried():
    jira_client = _CountingJiraClient([_http_error(401)])
    service, review = await _setup(jira_client)

    with pytest.raises(TicketCreationFailedError):
        await service.approve(review.review_id, reviewer_id="alice", reviewer_type=DecisionSource.HUMAN, granted=GRANTED)

    assert jira_client.calls == 1  # never retried


@pytest.mark.asyncio
async def test_jira_circuit_breaker_opens_after_repeated_transient_failures_and_fails_fast():
    jira_client = _CountingJiraClient([_http_error(503)] * 20)
    service, review = await _setup(jira_client)
    # Force a tight breaker for this test so it trips well within the retry budget of ONE approve() call.
    service._jira_breaker._failure_threshold = 1

    with pytest.raises(TicketCreationFailedError):
        await service.approve(review.review_id, reviewer_id="alice", reviewer_type=DecisionSource.HUMAN, granted=GRANTED)

    assert service._jira_breaker.state == CircuitState.OPEN

    calls_before = jira_client.calls
    with pytest.raises(TicketCreationFailedError):
        # A fresh review (idempotent create_review would return the same one, so re-approve the same review) —
        # while OPEN, the breaker must fail fast without ever calling Jira again.
        await service.approve(review.review_id, reviewer_id="alice", reviewer_type=DecisionSource.HUMAN, granted=GRANTED)
    assert jira_client.calls == calls_before  # no new Jira call was made — failed fast
