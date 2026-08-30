"""Tests for the Detection -> Action boundary (app/detection/action_trigger.py,
app/detection/action_processor.py) and its wiring into DetectionProcessor
(app/detection/processor.py). Uses the real FeatureReviewService and the
real IncidentResolutionAgent (with its internal ADK runner monkeypatched,
same pattern as tests/test_detection_processing.py — no live Gemini call),
against real in-memory repositories.
"""

import json
import uuid
from datetime import datetime, timezone

import pytest
from google.genai import types

from app.agent_runtime.gateways.artifacts import RepositoryArtifactGateway
from app.agent_runtime.gateways.detections import RepositoryDetectionGateway
from app.agent_runtime.gateways.resolutions import RepositoryResolutionGateway
from app.agent_runtime.gateways.signals import RepositorySignalGateway
from app.agents.incident_resolution import IncidentResolutionAgent
from app.demo.fakes import resolution_proposal
from app.detection.action_processor import ActionProcessingError, DetectionActionProcessor
from app.detection.action_trigger import DetectionAvailableEvent, NoOpActionTrigger
from app.detection.processor import DetectionProcessor
from app.domain import (
    Artifact,
    ArtifactType,
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
from app.eventing.trigger import SignalAvailableEvent
from app.feature_review import FeatureReviewService
from app.persistence.memory.repositories import (
    InMemoryArtifactRepository,
    InMemoryDetectionRepository,
    InMemoryFeatureReviewRepository,
    InMemoryResolutionRepository,
    InMemorySignalRepository,
)
from app.persistence.repositories.detection import DetectionQuery
from app.persistence.repositories.feature_review import FeatureReviewQuery
from app.persistence.repositories.resolution import ResolutionQuery

NOW = datetime.now(timezone.utc)


# ---- ADK fakes for IncidentResolutionAgent (same pattern as
# tests/test_detection_processing.py's DetectingAgent fakes) --------------


class _FakeEvent:
    def __init__(self, text):
        self.content = types.Content(role="model", parts=[types.Part(text=text)])

    def is_final_response(self):
        return True


class _FakeSession:
    id = "session-1"


class _CapturingSessionService:
    async def create_session(self, **kwargs):
        return _FakeSession()


def make_fake_runner_returning(final_text: str):
    async def _events(**kwargs):
        yield _FakeEvent(final_text)

    class _FakeRunner:
        def __init__(self, agent, app_name):
            self.session_service = _CapturingSessionService()

        def run_async(self, **kwargs):
            return _events(**kwargs)

    return _FakeRunner


def make_fake_runner_raising(exc: Exception):
    async def _events(**kwargs):
        raise exc
        yield  # pragma: no cover

    class _FakeRunner:
        def __init__(self, agent, app_name):
            self.session_service = _CapturingSessionService()

        def run_async(self, **kwargs):
            return _events(**kwargs)

    return _FakeRunner


# ---- fixtures -------------------------------------------------------------


def make_signal(source_event_id: str, *, deployment_artifact_id: str | None = None, **overrides) -> Signal:
    subject = overrides.pop("subject", "checkout")
    defaults = dict(
        signal_type=SignalType.APPLICATION_ERROR,
        source=SignalSource.CLOUD_LOGGING,
        severity=SignalSeverity.CRITICAL,
        observed_at=NOW,
        subject=subject,
        summary=f"signal {source_event_id}",
        service_name="checkout",
        environment="production",
        deployment_artifact_id=deployment_artifact_id,
        provenance=SignalProvenance(source_system="test", source_event_id=source_event_id),
        fingerprint=compute_fingerprint(source=SignalSource.CLOUD_LOGGING, source_event_id=source_event_id, subject=subject),
    )
    defaults.update(overrides)
    return Signal(**defaults)


def make_detection(*, detection_type: DetectionType, supporting_signal_ids: list[str], subject: str = "checkout") -> DetectionResult:
    return DetectionResult(
        detection_type=detection_type,
        domain=DetectionDomain.OPERATIONAL,
        title="Elevated error rate",
        summary="Errors observed for checkout in production.",
        rationale="Repeated application errors within a short window.",
        confidence=0.9,
        subject=subject,
        service_name="checkout",
        environment="production",
        supporting_signal_ids=supporting_signal_ids,
        observation_window_minutes=15,
        fingerprint=compute_detection_fingerprint(
            detection_type=detection_type, subject=subject, supporting_signal_ids=supporting_signal_ids, window_minutes=15
        ),
    )


class _Repos:
    def __init__(self):
        self.signal_repo = InMemorySignalRepository()
        self.detection_repo = InMemoryDetectionRepository()
        self.resolution_repo = InMemoryResolutionRepository()
        self.review_repo = InMemoryFeatureReviewRepository()
        self.artifact_repo = InMemoryArtifactRepository()


def make_processor(repos: _Repos) -> DetectionActionProcessor:
    review_service = FeatureReviewService(
        repos.review_repo, RepositoryDetectionGateway(repos.detection_repo), RepositorySignalGateway(repos.signal_repo)
    )
    return DetectionActionProcessor(
        review_service=review_service,
        incident_agent=IncidentResolutionAgent(),
        signal_gateway=RepositorySignalGateway(repos.signal_repo),
        detection_gateway=RepositoryDetectionGateway(repos.detection_repo),
        artifact_gateway=RepositoryArtifactGateway(repos.artifact_repo),
        resolution_gateway=RepositoryResolutionGateway(repos.resolution_repo),
    )


async def save_deploying_artifact(repos: _Repos, *, workflow_id: str) -> str:
    artifact = Artifact(artifact_id=str(uuid.uuid4()), artifact_type=ArtifactType.DEPLOYMENT, created_by="deployment_agent")
    await repos.artifact_repo.save(workflow_id, artifact)
    return artifact.artifact_id


# ---------------------------------------------------------------------------
# no_action
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_action_is_a_cheap_terminal_noop():
    repos = _Repos()
    processor = make_processor(repos)
    detection = make_detection(detection_type=DetectionType.NO_ACTION, supporting_signal_ids=[])
    await repos.detection_repo.save(detection)

    outcome = await processor.process_detection_available(DetectionAvailableEvent(detection_id=detection.detection_id, detection_type=DetectionType.NO_ACTION))

    assert outcome.action == "skipped_no_action"
    assert outcome.review_id is None
    assert outcome.resolution_id is None
    # No review/resolution was ever created for this detection.
    assert await repos.review_repo.find_by_detection_id(detection.detection_id) is None
    assert (await repos.resolution_repo.query(ResolutionQuery(detection_id=detection.detection_id))) == []


# ---------------------------------------------------------------------------
# feature_opportunity
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_feature_opportunity_creates_pending_review():
    repos = _Repos()
    processor = make_processor(repos)
    signal = await repos.signal_repo.save(make_signal("f1", subject="reporting"))
    detection = make_detection(detection_type=DetectionType.FEATURE_OPPORTUNITY, supporting_signal_ids=[signal.signal_id], subject="reporting")
    await repos.detection_repo.save(detection)

    outcome = await processor.process_detection_available(
        DetectionAvailableEvent(detection_id=detection.detection_id, detection_type=DetectionType.FEATURE_OPPORTUNITY)
    )

    assert outcome.action == "review_created"
    review = await repos.review_repo.get(outcome.review_id)
    assert review is not None
    assert review.status == ReviewStatus.PENDING
    assert review.detection_id == detection.detection_id
    # No Jira ticket, no workflow — human approval boundary untouched.
    assert review.ticket is None


@pytest.mark.asyncio
async def test_feature_opportunity_idempotent_on_reprocessing():
    repos = _Repos()
    processor = make_processor(repos)
    signal = await repos.signal_repo.save(make_signal("f2", subject="reporting"))
    detection = make_detection(detection_type=DetectionType.FEATURE_OPPORTUNITY, supporting_signal_ids=[signal.signal_id], subject="reporting")
    await repos.detection_repo.save(detection)
    event = DetectionAvailableEvent(detection_id=detection.detection_id, detection_type=DetectionType.FEATURE_OPPORTUNITY)

    first = await processor.process_detection_available(event)
    second = await processor.process_detection_available(event)

    assert first.review_id == second.review_id
    all_reviews = await repos.review_repo.query(FeatureReviewQuery(limit=500))
    assert len(all_reviews) == 1


# ---------------------------------------------------------------------------
# incident: workflow resolution
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_incident_resolves_owning_workflow_and_invokes_agent(monkeypatch):
    repos = _Repos()
    processor = make_processor(repos)
    workflow_id = "wf-original-deploy"
    artifact_id = await save_deploying_artifact(repos, workflow_id=workflow_id)
    signal = await repos.signal_repo.save(make_signal("i1", deployment_artifact_id=artifact_id))
    detection = make_detection(detection_type=DetectionType.INCIDENT, supporting_signal_ids=[signal.signal_id])
    await repos.detection_repo.save(detection)

    proposal = resolution_proposal(strategy="code_fix", supporting_signal_ids=[signal.signal_id])
    monkeypatch.setattr("app.agents.incident_resolution.InMemoryRunner", make_fake_runner_returning(json.dumps(proposal)))

    outcome = await processor.process_detection_available(DetectionAvailableEvent(detection_id=detection.detection_id, detection_type=DetectionType.INCIDENT))

    assert outcome.action == "resolution_created"
    assert outcome.workflow_id == workflow_id
    resolution = await repos.resolution_repo.get(outcome.resolution_id)
    assert resolution is not None
    assert resolution.workflow_id == workflow_id
    assert resolution.detection_id == detection.detection_id


@pytest.mark.asyncio
async def test_incident_idempotent_on_reprocessing(monkeypatch):
    repos = _Repos()
    processor = make_processor(repos)
    workflow_id = "wf-original-deploy"
    artifact_id = await save_deploying_artifact(repos, workflow_id=workflow_id)
    signal = await repos.signal_repo.save(make_signal("i2", deployment_artifact_id=artifact_id))
    detection = make_detection(detection_type=DetectionType.INCIDENT, supporting_signal_ids=[signal.signal_id])
    await repos.detection_repo.save(detection)

    proposal = resolution_proposal(strategy="code_fix", supporting_signal_ids=[signal.signal_id])
    monkeypatch.setattr("app.agents.incident_resolution.InMemoryRunner", make_fake_runner_returning(json.dumps(proposal)))
    event = DetectionAvailableEvent(detection_id=detection.detection_id, detection_type=DetectionType.INCIDENT)

    first = await processor.process_detection_available(event)
    second = await processor.process_detection_available(event)

    assert first.resolution_id == second.resolution_id
    all_resolutions = await repos.resolution_repo.query(ResolutionQuery(detection_id=detection.detection_id, limit=500))
    assert len(all_resolutions) == 1


# ---------------------------------------------------------------------------
# incident: failure modes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_incident_missing_deployment_artifact_id_fails_safely():
    repos = _Repos()
    processor = make_processor(repos)
    signal = await repos.signal_repo.save(make_signal("i3", deployment_artifact_id=None))
    detection = make_detection(detection_type=DetectionType.INCIDENT, supporting_signal_ids=[signal.signal_id])
    await repos.detection_repo.save(detection)

    with pytest.raises(ActionProcessingError, match="could not resolve an owning WorkflowState"):
        await processor.process_detection_available(DetectionAvailableEvent(detection_id=detection.detection_id, detection_type=DetectionType.INCIDENT))

    # No partial resolution was ever created.
    assert (await repos.resolution_repo.query(ResolutionQuery(detection_id=detection.detection_id))) == []


@pytest.mark.asyncio
async def test_incident_artifact_not_found_fails_safely():
    repos = _Repos()
    processor = make_processor(repos)
    # deployment_artifact_id references an artifact that was never actually saved.
    signal = await repos.signal_repo.save(make_signal("i4", deployment_artifact_id="nonexistent-artifact-id"))
    detection = make_detection(detection_type=DetectionType.INCIDENT, supporting_signal_ids=[signal.signal_id])
    await repos.detection_repo.save(detection)

    with pytest.raises(ActionProcessingError, match="could not resolve an owning WorkflowState"):
        await processor.process_detection_available(DetectionAvailableEvent(detection_id=detection.detection_id, detection_type=DetectionType.INCIDENT))


@pytest.mark.asyncio
async def test_incident_never_infers_workflow_from_service_name(monkeypatch):
    """Two signals share service_name/environment but were deployed by
    DIFFERENT workflows — the processor must resolve the exact workflow
    that owns the FIRST resolvable deployment_artifact_id, never guess
    from service_name/environment matching."""
    repos = _Repos()
    processor = make_processor(repos)
    correct_workflow_id = "wf-correct"
    wrong_workflow_id = "wf-wrong-but-same-service"
    artifact_id = await save_deploying_artifact(repos, workflow_id=correct_workflow_id)
    # A second, unrelated artifact for a different workflow, same service_name/environment.
    await save_deploying_artifact(repos, workflow_id=wrong_workflow_id)

    signal = await repos.signal_repo.save(make_signal("i5", deployment_artifact_id=artifact_id))
    detection = make_detection(detection_type=DetectionType.INCIDENT, supporting_signal_ids=[signal.signal_id])
    await repos.detection_repo.save(detection)

    proposal = resolution_proposal(strategy="code_fix", supporting_signal_ids=[signal.signal_id])
    monkeypatch.setattr("app.agents.incident_resolution.InMemoryRunner", make_fake_runner_returning(json.dumps(proposal)))

    outcome = await processor.process_detection_available(DetectionAvailableEvent(detection_id=detection.detection_id, detection_type=DetectionType.INCIDENT))

    assert outcome.workflow_id == correct_workflow_id
    assert outcome.workflow_id != wrong_workflow_id


@pytest.mark.asyncio
async def test_incident_agent_failure_propagates(monkeypatch):
    repos = _Repos()
    processor = make_processor(repos)
    workflow_id = "wf-original-deploy"
    artifact_id = await save_deploying_artifact(repos, workflow_id=workflow_id)
    signal = await repos.signal_repo.save(make_signal("i6", deployment_artifact_id=artifact_id))
    detection = make_detection(detection_type=DetectionType.INCIDENT, supporting_signal_ids=[signal.signal_id])
    await repos.detection_repo.save(detection)

    monkeypatch.setattr("app.agents.incident_resolution.InMemoryRunner", make_fake_runner_raising(RuntimeError("gemini exploded")))

    # IncidentResolutionAgent catches its own LLM failure internally (never
    # lets a Gemini exception escape execute()) and returns a FAILED
    # AgentOutput instead — DetectionActionProcessor turns that into an
    # ActionProcessingError rather than treating it as a completed run.
    with pytest.raises(ActionProcessingError, match="IncidentResolutionAgent did not complete"):
        await processor.process_detection_available(DetectionAvailableEvent(detection_id=detection.detection_id, detection_type=DetectionType.INCIDENT))

    # No resolution was persisted from the failed attempt.
    assert (await repos.resolution_repo.query(ResolutionQuery(detection_id=detection.detection_id))) == []


class _RaisingIncidentAgent:
    async def execute(self, agent_input, context):
        raise RuntimeError("unexpected crash before the agent's own error handling")


@pytest.mark.asyncio
async def test_incident_agent_unexpected_exception_propagates_as_action_error():
    """Covers the case where execute() itself raises (a bug outside
    IncidentResolutionAgent's own internal try/except), not just a
    gracefully-returned FAILED AgentOutput."""
    repos = _Repos()
    review_service = FeatureReviewService(
        repos.review_repo, RepositoryDetectionGateway(repos.detection_repo), RepositorySignalGateway(repos.signal_repo)
    )
    processor = DetectionActionProcessor(
        review_service=review_service,
        incident_agent=_RaisingIncidentAgent(),
        signal_gateway=RepositorySignalGateway(repos.signal_repo),
        detection_gateway=RepositoryDetectionGateway(repos.detection_repo),
        artifact_gateway=RepositoryArtifactGateway(repos.artifact_repo),
        resolution_gateway=RepositoryResolutionGateway(repos.resolution_repo),
    )
    workflow_id = "wf-original-deploy"
    artifact_id = await save_deploying_artifact(repos, workflow_id=workflow_id)
    signal = await repos.signal_repo.save(make_signal("i7", deployment_artifact_id=artifact_id))
    detection = make_detection(detection_type=DetectionType.INCIDENT, supporting_signal_ids=[signal.signal_id])
    await repos.detection_repo.save(detection)

    with pytest.raises(ActionProcessingError, match="IncidentResolutionAgent execution failed"):
        await processor.process_detection_available(DetectionAvailableEvent(detection_id=detection.detection_id, detection_type=DetectionType.INCIDENT))


@pytest.mark.asyncio
async def test_detection_not_found_fails_safely():
    repos = _Repos()
    processor = make_processor(repos)

    with pytest.raises(ActionProcessingError, match="not found"):
        await processor.process_detection_available(DetectionAvailableEvent(detection_id="does-not-exist", detection_type=DetectionType.INCIDENT))


# ---------------------------------------------------------------------------
# ActionTrigger Protocol conformance / discard semantics
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_on_detection_available_satisfies_action_trigger_protocol():
    repos = _Repos()
    processor = make_processor(repos)
    detection = make_detection(detection_type=DetectionType.NO_ACTION, supporting_signal_ids=[])
    await repos.detection_repo.save(detection)

    result = await processor.on_detection_available(DetectionAvailableEvent(detection_id=detection.detection_id, detection_type=DetectionType.NO_ACTION))

    assert result is None  # discards the rich outcome, matching DetectionTrigger's -> None contract


# ---------------------------------------------------------------------------
# DetectionProcessor wiring: action trigger invoked only after persistence,
# and failures propagate through the existing outer boundary unchanged.
# ---------------------------------------------------------------------------


class _RecordingActionTrigger:
    def __init__(self):
        self.calls: list[DetectionAvailableEvent] = []

    async def on_detection_available(self, event: DetectionAvailableEvent) -> None:
        self.calls.append(event)


class _RaisingActionTrigger:
    async def on_detection_available(self, event: DetectionAvailableEvent) -> None:
        raise ActionProcessingError("boom")


def _detection_processor_setup():
    signal_repo = InMemorySignalRepository()
    detection_repo = InMemoryDetectionRepository()
    return signal_repo, detection_repo


@pytest.mark.asyncio
async def test_detection_processor_invokes_action_trigger_only_after_persistence(monkeypatch):
    signal_repo, detection_repo = _detection_processor_setup()
    trigger = _RecordingActionTrigger()
    processor = DetectionProcessor(
        signal_gateway=RepositorySignalGateway(signal_repo), detection_gateway=RepositoryDetectionGateway(detection_repo), action_trigger=trigger
    )
    signal = await signal_repo.save(make_signal("dp1"))
    output = {
        "detection_type": "incident",
        "title": "t",
        "summary": "s",
        "rationale": "r",
        "confidence": 0.9,
        "severity": "critical",
        "subject": "checkout",
        "supporting_signal_ids": [signal.signal_id],
        "knowledge_references": [],
    }
    monkeypatch.setattr("app.agents.detecting.InMemoryRunner", make_fake_runner_returning(json.dumps(output)))

    outcome = await processor.process_signal_available(SignalAvailableEvent.from_signal(signal))

    # The trigger was called exactly once, with the SAME detection_id that
    # is already retrievable from the DetectionRepository — proving
    # persistence happened first.
    assert len(trigger.calls) == 1
    assert trigger.calls[0].detection_id == outcome.detection_id
    persisted = await detection_repo.get(outcome.detection_id)
    assert persisted is not None


@pytest.mark.asyncio
async def test_detection_processor_defaults_to_noop_action_trigger(monkeypatch):
    """No action_trigger supplied -> NoOpActionTrigger -> detection
    processing succeeds exactly as before this feature existed."""
    signal_repo, detection_repo = _detection_processor_setup()
    processor = DetectionProcessor(signal_gateway=RepositorySignalGateway(signal_repo), detection_gateway=RepositoryDetectionGateway(detection_repo))
    assert isinstance(processor._action_trigger, NoOpActionTrigger)
    signal = await signal_repo.save(make_signal("dp2"))
    output = {
        "detection_type": "no_action",
        "title": "t",
        "summary": "s",
        "rationale": "r",
        "confidence": 0.9,
        "severity": None,
        "subject": "checkout",
        "supporting_signal_ids": [signal.signal_id],
        "knowledge_references": [],
    }
    monkeypatch.setattr("app.agents.detecting.InMemoryRunner", make_fake_runner_returning(json.dumps(output)))

    outcome = await processor.process_signal_available(SignalAvailableEvent.from_signal(signal))

    assert outcome.detection_id is not None


@pytest.mark.asyncio
async def test_action_trigger_failure_propagates_out_of_process_signal_available(monkeypatch):
    """Action processing failures are never swallowed inside
    DetectionProcessor — they propagate to whatever already-existing
    boundary called process_signal_available (SignalIngestionService's
    own trigger-failure handling in the production worker path)."""
    signal_repo, detection_repo = _detection_processor_setup()
    processor = DetectionProcessor(
        signal_gateway=RepositorySignalGateway(signal_repo),
        detection_gateway=RepositoryDetectionGateway(detection_repo),
        action_trigger=_RaisingActionTrigger(),
    )
    signal = await signal_repo.save(make_signal("dp3"))
    output = {
        "detection_type": "incident",
        "title": "t",
        "summary": "s",
        "rationale": "r",
        "confidence": 0.9,
        "severity": "critical",
        "subject": "checkout",
        "supporting_signal_ids": [signal.signal_id],
        "knowledge_references": [],
    }
    monkeypatch.setattr("app.agents.detecting.InMemoryRunner", make_fake_runner_returning(json.dumps(output)))

    with pytest.raises(ActionProcessingError, match="boom"):
        await processor.process_signal_available(SignalAvailableEvent.from_signal(signal))

    # The DetectionResult itself remains persisted despite the action
    # trigger failure — ingestion/detection persistence is never rolled
    # back by a downstream action failure.
    all_detections = await detection_repo.query(DetectionQuery(limit=500))
    assert len(all_detections) == 1
