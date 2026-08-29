"""IncidentResolutionAgent tests. No real Gemini/ADK call — a fake
InMemoryRunner is monkeypatched in, same _CapturingSessionService pattern
as test_detecting_agent.py."""

import json
from datetime import datetime, timedelta, timezone

import pytest
from google.genai import types
from pydantic import ValidationError

from app.agent_runtime.capabilities import AgentCapability
from app.agent_runtime.context import AgentContext
from app.agent_runtime.gateways.detections import RepositoryDetectionGateway
from app.agent_runtime.gateways.resolutions import RepositoryResolutionGateway
from app.agent_runtime.gateways.signals import RepositorySignalGateway
from app.agent_runtime.status import AgentStatus
from app.agents.incident_resolution import IncidentResolutionAgent, ResolutionInput, ResolutionProposal, _incident_resolution_llm_agent
from app.domain import (
    AgentInput,
    Artifact,
    ArtifactType,
    DetectionDomain,
    DetectionResult,
    DetectionType,
    RemediationRisk,
    RemediationStrategy,
    Signal,
    SignalProvenance,
    SignalSeverity,
    SignalSource,
    SignalType,
    Ticket,
    WorkflowStatus,
    compute_detection_fingerprint,
    compute_fingerprint,
)
from app.persistence.memory import (
    InMemoryAgentExecutionRepository,
    InMemoryArtifactRepository,
    InMemoryDetectionRepository,
    InMemoryResolutionRepository,
    InMemorySignalRepository,
)
from app.persistence.repositories.resolution import ResolutionQuery

NOW = datetime.now(timezone.utc)


def make_signal(source_event_id: str, *, stype=SignalType.APPLICATION_ERROR, source=SignalSource.CLOUD_LOGGING, minutes_ago=1, **overrides) -> Signal:
    defaults = dict(
        signal_type=stype,
        source=source,
        severity=SignalSeverity.ERROR,
        observed_at=NOW - timedelta(minutes=minutes_ago),
        subject="quipu-api",
        summary=f"signal {source_event_id}",
        service_name="quipu-api",
        environment="production",
        provenance=SignalProvenance(source_system="x", source_event_id=source_event_id),
        fingerprint=compute_fingerprint(source=source, source_event_id=source_event_id, subject="quipu-api"),
    )
    defaults.update(overrides)
    return Signal(**defaults)


def make_detection(signals, **overrides) -> DetectionResult:
    signal_ids = [s.signal_id for s in signals]
    defaults = dict(
        detection_type=DetectionType.INCIDENT,
        domain=DetectionDomain.OPERATIONAL,
        title="Probable incident",
        summary="Error rate spiked",
        rationale="Errors clustered after deployment",
        confidence=0.9,
        severity=SignalSeverity.CRITICAL,
        subject="quipu-api",
        service_name="quipu-api",
        environment="production",
        supporting_signal_ids=signal_ids,
        observation_window_minutes=15,
        fingerprint=compute_detection_fingerprint(detection_type=DetectionType.INCIDENT, subject="quipu-api", supporting_signal_ids=signal_ids, window_minutes=15),
    )
    defaults.update(overrides)
    return DetectionResult(**defaults)


DEFAULT_PROPOSAL = dict(
    diagnosis_summary="Application defect causing errors.",
    probable_root_cause="Null pointer in order handler.",
    root_cause_confidence=0.9,
    remediation_strategy="code_fix",
    remediation_rationale="Verified application-error signals correlate with the recent change.",
    expected_outcome="Error rate returns to baseline.",
    verification_strategy="Re-run the test suite and monitor error rate.",
    risk="low",
    severity="critical",
    target_agent="codegen_agent",
    knowledge_references=[],
)


# ---- ADK fakes ------------------------------------------------------------


class _FakeEvent:
    def __init__(self, text):
        self.content = types.Content(role="model", parts=[types.Part(text=text)])

    def is_final_response(self):
        return True


class _FakeSession:
    id = "session-1"


class _CapturingSessionService:
    def __init__(self):
        self.captured_state: dict = {}

    async def create_session(self, **kwargs):
        self.captured_state = kwargs.get("state", {})
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


def make_agent_input(detection_id: str) -> AgentInput:
    return AgentInput(
        workflow_id="wf-1", agent_name="incident_resolution_agent", ticket=Ticket(title="resolve", description="resolve"), context={"detection_id": detection_id}
    )


def make_context(**overrides):
    signal_repo = overrides.pop("signal_repo", None) or InMemorySignalRepository()
    detection_repo = overrides.pop("detection_repo", None) or InMemoryDetectionRepository()
    resolution_repo = overrides.pop("resolution_repo", None) or InMemoryResolutionRepository()
    defaults = dict(
        workflow_id="wf-1",
        execution_id="exec-1",
        knowledge=None,
        tools=None,
        artifacts=InMemoryArtifactRepository(),
        executions=InMemoryAgentExecutionRepository(),
        signals=RepositorySignalGateway(signal_repo),
        detections=RepositoryDetectionGateway(detection_repo),
        resolutions=RepositoryResolutionGateway(resolution_repo),
    )
    defaults.update(overrides)
    return AgentContext(**defaults), signal_repo, detection_repo, resolution_repo


async def setup_incident(signal_repo, detection_repo, *, signals=None, **detection_overrides) -> tuple[list[Signal], DetectionResult]:
    signals = signals if signals is not None else [make_signal("1"), make_signal("2")]
    for s in signals:
        await signal_repo.save(s)
    detection = make_detection(signals, **detection_overrides)
    await detection_repo.save(detection)
    return signals, detection


# ---- Runtime ------------------------------------------------------------------


def test_incident_resolution_agent_identity():
    agent = IncidentResolutionAgent()
    assert agent.identity.agent_id == "incident_resolution_agent"


def test_capabilities_are_read_and_plan_producing_only():
    agent = IncidentResolutionAgent()
    assert agent.capabilities == {
        AgentCapability.READ_DETECTION,
        AgentCapability.READ_SIGNALS,
        AgentCapability.READ_ARTIFACT,
        AgentCapability.QUERY_KNOWLEDGE,
        AgentCapability.WRITE_RESOLUTION,
    }
    forbidden = {
        AgentCapability.WRITE_CODE,
        AgentCapability.DEPLOY,
        AgentCapability.CREATE_COMMIT,
        AgentCapability.WRITE_JIRA,
        AgentCapability.RESOLVE_INCIDENT,
        AgentCapability.ROLLBACK,
        AgentCapability.RUN_TESTS,
    }
    assert agent.capabilities.isdisjoint(forbidden)


@pytest.mark.asyncio
async def test_lifecycle_completes(monkeypatch):
    context, signal_repo, detection_repo, _ = make_context()
    signals, detection = await setup_incident(signal_repo, detection_repo)
    proposal = {**DEFAULT_PROPOSAL, "supporting_signal_ids": [s.signal_id for s in signals]}
    monkeypatch.setattr("app.agents.incident_resolution.InMemoryRunner", make_fake_runner_returning(json.dumps(proposal)))
    agent = IncidentResolutionAgent()
    output = await agent.execute(make_agent_input(detection.detection_id), context)
    assert agent.status == AgentStatus.COMPLETED
    assert output.status == WorkflowStatus.COMPLETED


# ---- Input validation -----------------------------------------------------


@pytest.mark.asyncio
async def test_invalid_input_rejected():
    context, _, _, _ = make_context()
    agent = IncidentResolutionAgent()
    output = await agent.execute(AgentInput(workflow_id="wf-1", agent_name="incident_resolution_agent", ticket=Ticket(title="x", description="x"), context={}), context)
    assert output.status == WorkflowStatus.FAILED
    assert output.errors[0].code == "RESOLUTION_INPUT_INVALID"


@pytest.mark.asyncio
async def test_missing_detection_rejected():
    context, _, _, _ = make_context()
    agent = IncidentResolutionAgent()
    output = await agent.execute(make_agent_input("does-not-exist"), context)
    assert output.status == WorkflowStatus.FAILED
    assert output.errors[0].code == "DETECTION_NOT_FOUND"


@pytest.mark.asyncio
async def test_feature_opportunity_rejected_before_gemini(monkeypatch):
    called = {"value": False}

    def _boom(*args, **kwargs):
        called["value"] = True
        raise AssertionError("must not call Gemini for a non-incident detection")

    monkeypatch.setattr("app.agents.incident_resolution.InMemoryRunner", _boom)
    context, signal_repo, detection_repo, _ = make_context()
    signals, detection = await setup_incident(
        signal_repo, detection_repo, detection_type=DetectionType.FEATURE_OPPORTUNITY, domain=DetectionDomain.PRODUCT, severity=None
    )
    agent = IncidentResolutionAgent()
    output = await agent.execute(make_agent_input(detection.detection_id), context)
    assert output.status == WorkflowStatus.FAILED
    assert output.errors[0].code == "DETECTION_NOT_AN_INCIDENT"
    assert not called["value"]


@pytest.mark.asyncio
async def test_missing_gateways_rejected():
    context, signal_repo, detection_repo, _ = make_context()
    _, detection = await setup_incident(signal_repo, detection_repo)
    agent = IncidentResolutionAgent()

    context.detections = None
    output = await agent.execute(make_agent_input(detection.detection_id), context)
    assert output.errors[0].code == "RESOLUTION_DETECTION_GATEWAY_MISSING"


# ---- Evidence resolution --------------------------------------------------


@pytest.mark.asyncio
async def test_missing_supporting_signal_escalates_deterministically_without_gemini(monkeypatch):
    called = {"value": False}

    def _boom(*args, **kwargs):
        called["value"] = True
        raise AssertionError("must not call Gemini with zero resolvable evidence")

    monkeypatch.setattr("app.agents.incident_resolution.InMemoryRunner", _boom)
    context, signal_repo, detection_repo, _ = make_context()
    detection = make_detection([], supporting_signal_ids=["signal-that-does-not-exist"])
    await detection_repo.save(detection)
    agent = IncidentResolutionAgent()
    output = await agent.execute(make_agent_input(detection.detection_id), context)
    assert output.status == WorkflowStatus.COMPLETED
    assert not called["value"]
    parsed = json.loads(output.messages[1])
    assert parsed["remediation_strategy"] == "escalate"


@pytest.mark.asyncio
async def test_partial_missing_signals_still_proceeds_with_resolved_ones(monkeypatch):
    context, signal_repo, detection_repo, _ = make_context()
    signals, _ = await setup_incident(signal_repo, detection_repo)
    detection = make_detection(signals, supporting_signal_ids=[signals[0].signal_id, "gone-signal"])
    await detection_repo.save(detection)
    proposal = {**DEFAULT_PROPOSAL, "supporting_signal_ids": [signals[0].signal_id]}
    monkeypatch.setattr("app.agents.incident_resolution.InMemoryRunner", make_fake_runner_returning(json.dumps(proposal)))
    agent = IncidentResolutionAgent()
    output = await agent.execute(make_agent_input(detection.detection_id), context)
    parsed = json.loads(output.messages[1])
    assert parsed["remediation_strategy"] == "code_fix"


@pytest.mark.asyncio
async def test_original_detection_never_mutated(monkeypatch):
    context, signal_repo, detection_repo, _ = make_context()
    signals, detection = await setup_incident(signal_repo, detection_repo)
    before = await detection_repo.get(detection.detection_id)
    proposal = {**DEFAULT_PROPOSAL, "supporting_signal_ids": [s.signal_id for s in signals]}
    monkeypatch.setattr("app.agents.incident_resolution.InMemoryRunner", make_fake_runner_returning(json.dumps(proposal)))
    agent = IncidentResolutionAgent()
    await agent.execute(make_agent_input(detection.detection_id), context)
    after = await detection_repo.get(detection.detection_id)
    assert before == after


# ---- Deployment/artifact correlation ---------------------------------------


@pytest.mark.asyncio
async def test_deployment_artifact_correlation(monkeypatch):
    context, signal_repo, detection_repo, _ = make_context()
    artifact = Artifact(artifact_type=ArtifactType.DEPLOYMENT, created_by="deployment_agent", payload={"status": "succeeded", "revision": "rev-42", "service_name": "quipu-api"})
    await context.artifacts.save("wf-1", artifact)

    signal = make_signal("1", deployment_artifact_id=artifact.artifact_id, revision="rev-42")
    signals, detection = await setup_incident(signal_repo, detection_repo, signals=[signal])

    captured = {}

    class _CapturingRunner:
        def __init__(self, agent, app_name):
            self.session_service = _CapturingSessionService()

        def run_async(self, **kwargs):
            captured.update(self.session_service.captured_state)

            async def _events():
                yield _FakeEvent(json.dumps({**DEFAULT_PROPOSAL, "supporting_signal_ids": [signal.signal_id]}))

            return _events()

    monkeypatch.setattr("app.agents.incident_resolution.InMemoryRunner", _CapturingRunner)
    agent = IncidentResolutionAgent()
    await agent.execute(make_agent_input(detection.detection_id), context)
    assert len(captured["artifact_evidence"]) == 1
    assert captured["artifact_evidence"][0]["revision"] == "rev-42"


# ---- Reasoning scenarios -----------------------------------------------------


@pytest.mark.asyncio
async def test_deployment_regression_recommends_rollback(monkeypatch):
    context, signal_repo, detection_repo, _ = make_context()
    signals, detection = await setup_incident(signal_repo, detection_repo, signals=[make_signal("1", stype=SignalType.DEPLOYMENT_EVENT), make_signal("2", stype=SignalType.METRIC_ANOMALY)])
    proposal = {**DEFAULT_PROPOSAL, "remediation_strategy": "rollback", "rollback_target": "quipu-api-00006", "target_agent": "deployment_agent", "supporting_signal_ids": [s.signal_id for s in signals]}
    monkeypatch.setattr("app.agents.incident_resolution.InMemoryRunner", make_fake_runner_returning(json.dumps(proposal)))
    agent = IncidentResolutionAgent()
    output = await agent.execute(make_agent_input(detection.detection_id), context)
    parsed = json.loads(output.messages[1])
    assert parsed["remediation_strategy"] == "rollback"
    assert parsed["target_agent"] == "deployment_agent"
    assert parsed["rollback_target"] == "quipu-api-00006"


@pytest.mark.asyncio
async def test_application_defect_recommends_code_fix(monkeypatch):
    context, signal_repo, detection_repo, _ = make_context()
    signals, detection = await setup_incident(signal_repo, detection_repo, signals=[make_signal("1", stype=SignalType.APPLICATION_ERROR)])
    proposal = {**DEFAULT_PROPOSAL, "supporting_signal_ids": [s.signal_id for s in signals]}
    monkeypatch.setattr("app.agents.incident_resolution.InMemoryRunner", make_fake_runner_returning(json.dumps(proposal)))
    agent = IncidentResolutionAgent()
    output = await agent.execute(make_agent_input(detection.detection_id), context)
    parsed = json.loads(output.messages[1])
    assert parsed["remediation_strategy"] == "code_fix"
    assert parsed["target_agent"] == "codegen_agent"


@pytest.mark.asyncio
async def test_architecture_defect_recommends_architecture_review(monkeypatch):
    context, signal_repo, detection_repo, _ = make_context()
    signals, detection = await setup_incident(signal_repo, detection_repo, signals=[make_signal("1", stype=SignalType.AVAILABILITY_DEGRADATION)])
    proposal = {**DEFAULT_PROPOSAL, "remediation_strategy": "architecture_review", "target_agent": "architecture_agent", "supporting_signal_ids": [s.signal_id for s in signals]}
    monkeypatch.setattr("app.agents.incident_resolution.InMemoryRunner", make_fake_runner_returning(json.dumps(proposal)))
    agent = IncidentResolutionAgent()
    output = await agent.execute(make_agent_input(detection.detection_id), context)
    parsed = json.loads(output.messages[1])
    assert parsed["remediation_strategy"] == "architecture_review"
    assert parsed["target_agent"] == "architecture_agent"


@pytest.mark.asyncio
async def test_test_defect_recommends_retest(monkeypatch):
    context, signal_repo, detection_repo, _ = make_context()
    signals, detection = await setup_incident(signal_repo, detection_repo, signals=[make_signal("1", stype=SignalType.METRIC_ANOMALY)])
    proposal = {**DEFAULT_PROPOSAL, "remediation_strategy": "retest", "target_agent": "testing_agent", "supporting_signal_ids": [s.signal_id for s in signals]}
    monkeypatch.setattr("app.agents.incident_resolution.InMemoryRunner", make_fake_runner_returning(json.dumps(proposal)))
    agent = IncidentResolutionAgent()
    output = await agent.execute(make_agent_input(detection.detection_id), context)
    parsed = json.loads(output.messages[1])
    assert parsed["remediation_strategy"] == "retest"
    assert parsed["target_agent"] == "testing_agent"


@pytest.mark.asyncio
async def test_insufficient_evidence_escalates(monkeypatch):
    context, signal_repo, detection_repo, _ = make_context()
    signals, detection = await setup_incident(signal_repo, detection_repo)
    proposal = {**DEFAULT_PROPOSAL, "remediation_strategy": "escalate", "target_agent": None, "escalation_recommended": True, "root_cause_confidence": 0.2, "supporting_signal_ids": [s.signal_id for s in signals]}
    monkeypatch.setattr("app.agents.incident_resolution.InMemoryRunner", make_fake_runner_returning(json.dumps(proposal)))
    agent = IncidentResolutionAgent()
    output = await agent.execute(make_agent_input(detection.detection_id), context)
    parsed = json.loads(output.messages[1])
    assert parsed["remediation_strategy"] == "escalate"
    assert parsed["target_agent"] is None


# ---- Deterministic safety policy / adversarial tests ---------------------------


@pytest.mark.asyncio
async def test_arbitrary_target_agent_claim_is_never_trusted(monkeypatch):
    context, signal_repo, detection_repo, _ = make_context()
    signals, detection = await setup_incident(signal_repo, detection_repo)
    proposal = {**DEFAULT_PROPOSAL, "target_agent": "malicious_agent", "supporting_signal_ids": [s.signal_id for s in signals]}
    monkeypatch.setattr("app.agents.incident_resolution.InMemoryRunner", make_fake_runner_returning(json.dumps(proposal)))
    agent = IncidentResolutionAgent()
    output = await agent.execute(make_agent_input(detection.detection_id), context)
    parsed = json.loads(output.messages[1])
    assert parsed["target_agent"] == "codegen_agent"  # deterministically derived from strategy, never "malicious_agent"


@pytest.mark.asyncio
async def test_invalid_strategy_rejected_at_schema_level(monkeypatch):
    context, signal_repo, detection_repo, _ = make_context()
    _, detection = await setup_incident(signal_repo, detection_repo)
    bad_proposal = {**DEFAULT_PROPOSAL, "remediation_strategy": "execute_shell"}
    monkeypatch.setattr("app.agents.incident_resolution.InMemoryRunner", make_fake_runner_returning(json.dumps(bad_proposal)))
    agent = IncidentResolutionAgent()
    output = await agent.execute(make_agent_input(detection.detection_id), context)
    assert output.status == WorkflowStatus.FAILED
    assert output.errors[0].code == "RESOLUTION_VALIDATION_FAILED"


@pytest.mark.asyncio
async def test_fabricated_signal_id_dropped_not_trusted(monkeypatch):
    context, signal_repo, detection_repo, _ = make_context()
    signals, detection = await setup_incident(signal_repo, detection_repo)
    proposal = {**DEFAULT_PROPOSAL, "supporting_signal_ids": [signals[0].signal_id, "fake-signal-999"]}
    monkeypatch.setattr("app.agents.incident_resolution.InMemoryRunner", make_fake_runner_returning(json.dumps(proposal)))
    agent = IncidentResolutionAgent()
    output = await agent.execute(make_agent_input(detection.detection_id), context)
    parsed = json.loads(output.messages[1])
    assert "fake-signal-999" not in parsed["supporting_signal_ids"]
    assert parsed["supporting_signal_ids"] == [signals[0].signal_id]


@pytest.mark.asyncio
async def test_fully_fabricated_signal_ids_forces_escalation(monkeypatch):
    context, signal_repo, detection_repo, _ = make_context()
    _, detection = await setup_incident(signal_repo, detection_repo)
    proposal = {**DEFAULT_PROPOSAL, "supporting_signal_ids": ["fake-1", "fake-2"]}
    monkeypatch.setattr("app.agents.incident_resolution.InMemoryRunner", make_fake_runner_returning(json.dumps(proposal)))
    agent = IncidentResolutionAgent()
    output = await agent.execute(make_agent_input(detection.detection_id), context)
    parsed = json.loads(output.messages[1])
    assert parsed["remediation_strategy"] == "escalate"
    assert parsed["root_cause_confidence"] == 0.0


@pytest.mark.asyncio
async def test_fabricated_artifact_id_dropped(monkeypatch):
    context, signal_repo, detection_repo, _ = make_context()
    signals, detection = await setup_incident(signal_repo, detection_repo)
    proposal = {**DEFAULT_PROPOSAL, "supporting_signal_ids": [s.signal_id for s in signals], "supporting_artifact_ids": ["fake-artifact-id"]}
    monkeypatch.setattr("app.agents.incident_resolution.InMemoryRunner", make_fake_runner_returning(json.dumps(proposal)))
    agent = IncidentResolutionAgent()
    output = await agent.execute(make_agent_input(detection.detection_id), context)
    parsed = json.loads(output.messages[1])
    assert parsed["supporting_artifact_ids"] == []


@pytest.mark.asyncio
async def test_high_confidence_cannot_bypass_missing_evidence(monkeypatch):
    """confidence=0.99 with a fabricated evidence set must still escalate —
    confidence never overrides missing evidence."""
    context, signal_repo, detection_repo, _ = make_context()
    _, detection = await setup_incident(signal_repo, detection_repo)
    proposal = {**DEFAULT_PROPOSAL, "root_cause_confidence": 0.99, "supporting_signal_ids": ["fake-1"]}
    monkeypatch.setattr("app.agents.incident_resolution.InMemoryRunner", make_fake_runner_returning(json.dumps(proposal)))
    agent = IncidentResolutionAgent()
    output = await agent.execute(make_agent_input(detection.detection_id), context)
    parsed = json.loads(output.messages[1])
    assert parsed["remediation_strategy"] == "escalate"


@pytest.mark.asyncio
async def test_rollback_without_target_forces_escalation(monkeypatch):
    context, signal_repo, detection_repo, _ = make_context()
    signals, detection = await setup_incident(signal_repo, detection_repo)
    proposal = {**DEFAULT_PROPOSAL, "remediation_strategy": "rollback", "target_agent": "deployment_agent", "rollback_target": None, "risk": "high", "supporting_signal_ids": [s.signal_id for s in signals]}
    monkeypatch.setattr("app.agents.incident_resolution.InMemoryRunner", make_fake_runner_returning(json.dumps(proposal)))
    agent = IncidentResolutionAgent()
    output = await agent.execute(make_agent_input(detection.detection_id), context)
    parsed = json.loads(output.messages[1])
    assert parsed["remediation_strategy"] == "escalate"
    assert parsed["target_agent"] is None


@pytest.mark.asyncio
async def test_code_fix_without_code_related_evidence_forces_escalation(monkeypatch):
    """CODE_FIX claimed, but the only verified evidence is a
    deployment-event signal — not application-error/log-error."""
    context, signal_repo, detection_repo, _ = make_context()
    signals, detection = await setup_incident(signal_repo, detection_repo, signals=[make_signal("1", stype=SignalType.DEPLOYMENT_EVENT)])
    proposal = {**DEFAULT_PROPOSAL, "remediation_strategy": "code_fix", "supporting_signal_ids": [s.signal_id for s in signals]}
    monkeypatch.setattr("app.agents.incident_resolution.InMemoryRunner", make_fake_runner_returning(json.dumps(proposal)))
    agent = IncidentResolutionAgent()
    output = await agent.execute(make_agent_input(detection.detection_id), context)
    parsed = json.loads(output.messages[1])
    assert parsed["remediation_strategy"] == "escalate"


@pytest.mark.asyncio
async def test_high_risk_forces_escalation_even_with_strong_evidence(monkeypatch):
    context, signal_repo, detection_repo, _ = make_context()
    signals, detection = await setup_incident(signal_repo, detection_repo)
    proposal = {**DEFAULT_PROPOSAL, "risk": "high", "root_cause_confidence": 0.95, "supporting_signal_ids": [s.signal_id for s in signals]}
    monkeypatch.setattr("app.agents.incident_resolution.InMemoryRunner", make_fake_runner_returning(json.dumps(proposal)))
    agent = IncidentResolutionAgent()
    output = await agent.execute(make_agent_input(detection.detection_id), context)
    parsed = json.loads(output.messages[1])
    assert parsed["remediation_strategy"] == "escalate"


@pytest.mark.asyncio
async def test_low_confidence_forces_escalation_despite_high_severity(monkeypatch):
    """severity=HIGH/CRITICAL, root_cause_confidence=0.62, risk=HIGH should
    escalate rather than auto-remediate (task §21 example)."""
    context, signal_repo, detection_repo, _ = make_context()
    signals, detection = await setup_incident(signal_repo, detection_repo)
    proposal = {**DEFAULT_PROPOSAL, "severity": "critical", "root_cause_confidence": 0.62, "risk": "low", "supporting_signal_ids": [s.signal_id for s in signals]}
    monkeypatch.setattr("app.agents.incident_resolution.InMemoryRunner", make_fake_runner_returning(json.dumps(proposal)))
    agent = IncidentResolutionAgent()
    output = await agent.execute(make_agent_input(detection.detection_id), context)
    parsed = json.loads(output.messages[1])
    assert parsed["remediation_strategy"] == "escalate"


def test_model_cannot_inject_arbitrary_output_field():
    assert set(ResolutionProposal.model_fields) == {
        "diagnosis_summary",
        "probable_root_cause",
        "root_cause_confidence",
        "root_cause_candidates",
        "remediation_strategy",
        "remediation_rationale",
        "expected_outcome",
        "verification_strategy",
        "risk",
        "severity",
        "escalation_recommended",
        "target_agent",
        "rollback_target",
        "supporting_signal_ids",
        "supporting_artifact_ids",
        "knowledge_references",
    }


# ---- Knowledge ------------------------------------------------------------


def test_knowledge_tool_available():
    tool_names = {t.__name__ for t in _incident_resolution_llm_agent.tools if callable(t)}
    assert "query_enterprise_knowledge" in tool_names


def test_no_arbitrary_tool_beyond_knowledge():
    tool_names = {t.__name__ for t in _incident_resolution_llm_agent.tools if callable(t)}
    assert tool_names == {"query_enterprise_knowledge"}


@pytest.mark.asyncio
async def test_knowledge_references_kept_separate_from_evidence(monkeypatch):
    context, signal_repo, detection_repo, _ = make_context()
    signals, detection = await setup_incident(signal_repo, detection_repo)
    proposal = {**DEFAULT_PROPOSAL, "supporting_signal_ids": [s.signal_id for s in signals], "knowledge_references": ["doc-99"]}
    monkeypatch.setattr("app.agents.incident_resolution.InMemoryRunner", make_fake_runner_returning(json.dumps(proposal)))
    agent = IncidentResolutionAgent()
    output = await agent.execute(make_agent_input(detection.detection_id), context)
    parsed = json.loads(output.messages[1])
    assert parsed["knowledge_references"] == ["doc-99"]
    assert "doc-99" not in parsed["supporting_signal_ids"]


# ---- LLM failure handling -------------------------------------------------------


@pytest.mark.asyncio
async def test_llm_failure_fails_safely(monkeypatch):
    context, signal_repo, detection_repo, _ = make_context()
    _, detection = await setup_incident(signal_repo, detection_repo)
    monkeypatch.setattr("app.agents.incident_resolution.InMemoryRunner", make_fake_runner_raising(RuntimeError("gemini down")))
    agent = IncidentResolutionAgent()
    output = await agent.execute(make_agent_input(detection.detection_id), context)
    assert output.status == WorkflowStatus.FAILED
    assert output.errors[0].code == "RESOLUTION_LLM_FAILURE"


@pytest.mark.asyncio
async def test_empty_response_fails_safely(monkeypatch):
    context, signal_repo, detection_repo, _ = make_context()
    _, detection = await setup_incident(signal_repo, detection_repo)
    monkeypatch.setattr("app.agents.incident_resolution.InMemoryRunner", make_fake_runner_returning(""))
    agent = IncidentResolutionAgent()
    output = await agent.execute(make_agent_input(detection.detection_id), context)
    assert output.status == WorkflowStatus.FAILED
    assert output.errors[0].code == "RESOLUTION_EMPTY_RESPONSE"


@pytest.mark.asyncio
async def test_malformed_json_fails_safely(monkeypatch):
    context, signal_repo, detection_repo, _ = make_context()
    _, detection = await setup_incident(signal_repo, detection_repo)
    monkeypatch.setattr("app.agents.incident_resolution.InMemoryRunner", make_fake_runner_returning("not valid json"))
    agent = IncidentResolutionAgent()
    output = await agent.execute(make_agent_input(detection.detection_id), context)
    assert output.status == WorkflowStatus.FAILED
    assert output.errors[0].code == "RESOLUTION_VALIDATION_FAILED"


@pytest.mark.asyncio
async def test_invalid_confidence_rejected(monkeypatch):
    context, signal_repo, detection_repo, _ = make_context()
    _, detection = await setup_incident(signal_repo, detection_repo)
    bad_proposal = {**DEFAULT_PROPOSAL, "root_cause_confidence": 1.5}
    monkeypatch.setattr("app.agents.incident_resolution.InMemoryRunner", make_fake_runner_returning(json.dumps(bad_proposal)))
    agent = IncidentResolutionAgent()
    output = await agent.execute(make_agent_input(detection.detection_id), context)
    assert output.status == WorkflowStatus.FAILED
    assert output.errors[0].code == "RESOLUTION_VALIDATION_FAILED"


# ---- Persistence / deduplication ------------------------------------------------


@pytest.mark.asyncio
async def test_resolution_persisted(monkeypatch):
    context, signal_repo, detection_repo, resolution_repo = make_context()
    signals, detection = await setup_incident(signal_repo, detection_repo)
    proposal = {**DEFAULT_PROPOSAL, "supporting_signal_ids": [s.signal_id for s in signals]}
    monkeypatch.setattr("app.agents.incident_resolution.InMemoryRunner", make_fake_runner_returning(json.dumps(proposal)))
    agent = IncidentResolutionAgent()
    await agent.execute(make_agent_input(detection.detection_id), context)
    results = await resolution_repo.query(ResolutionQuery(limit=50))
    assert len(results) == 1
    assert results[0].detection_id == detection.detection_id


@pytest.mark.asyncio
async def test_repeated_run_over_same_detection_does_not_duplicate(monkeypatch):
    context, signal_repo, detection_repo, resolution_repo = make_context()
    signals, detection = await setup_incident(signal_repo, detection_repo)
    proposal = {**DEFAULT_PROPOSAL, "supporting_signal_ids": [s.signal_id for s in signals]}
    monkeypatch.setattr("app.agents.incident_resolution.InMemoryRunner", make_fake_runner_returning(json.dumps(proposal)))
    agent1 = IncidentResolutionAgent()
    agent2 = IncidentResolutionAgent()
    await agent1.execute(make_agent_input(detection.detection_id), context)
    await agent2.execute(make_agent_input(detection.detection_id), context)
    results = await resolution_repo.query(ResolutionQuery(limit=50))
    assert len(results) == 1


# ---- Security -----------------------------------------------------------------


def test_no_shell_or_subprocess_surface():
    import inspect

    import app.agents.incident_resolution as ir_module

    source = inspect.getsource(ir_module)
    assert "subprocess" not in source
    assert "os.system" not in source
    assert "shell=True" not in source


@pytest.mark.asyncio
async def test_evidence_bounded_by_configured_ceiling(monkeypatch):
    from app.config import get_settings

    context, signal_repo, detection_repo, _ = make_context()
    many_signals = [make_signal(str(i), minutes_ago=i) for i in range(80)]
    for s in many_signals:
        await signal_repo.save(s)
    detection = make_detection(many_signals)
    await detection_repo.save(detection)

    captured = {}

    class _CapturingRunner:
        def __init__(self, agent, app_name):
            self.session_service = _CapturingSessionService()

        def run_async(self, **kwargs):
            captured.update(self.session_service.captured_state)

            async def _events():
                yield _FakeEvent(json.dumps({**DEFAULT_PROPOSAL, "supporting_signal_ids": []}))

            return _events()

    monkeypatch.setattr("app.agents.incident_resolution.InMemoryRunner", _CapturingRunner)
    agent = IncidentResolutionAgent()
    await agent.execute(make_agent_input(detection.detection_id), context)
    settings = get_settings()
    assert len(captured["evidence_set"]) <= settings.incident_resolution_max_evidence


def test_resolution_input_has_no_raw_query_surface():
    assert set(ResolutionInput.model_fields) == {"detection_id"}


@pytest.mark.asyncio
async def test_evidence_dict_does_not_include_raw_signal_metadata(monkeypatch):
    context, signal_repo, detection_repo, _ = make_context()
    signal = make_signal("1", metadata={"internal_note": "should not reach Gemini"})
    signals, detection = await setup_incident(signal_repo, detection_repo, signals=[signal])

    captured = {}

    class _CapturingRunner:
        def __init__(self, agent, app_name):
            self.session_service = _CapturingSessionService()

        def run_async(self, **kwargs):
            captured.update(self.session_service.captured_state)

            async def _events():
                yield _FakeEvent(json.dumps({**DEFAULT_PROPOSAL, "supporting_signal_ids": [signal.signal_id]}))

            return _events()

    monkeypatch.setattr("app.agents.incident_resolution.InMemoryRunner", _CapturingRunner)
    agent = IncidentResolutionAgent()
    await agent.execute(make_agent_input(detection.detection_id), context)
    assert "internal_note" not in json.dumps(captured["evidence_set"])
