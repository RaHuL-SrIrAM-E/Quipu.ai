"""DetectingAgent tests. No real Gemini/ADK call — a fake InMemoryRunner is
monkeypatched in, following the same _CapturingSessionService pattern
established by test_testing_agent.py/test_codegen_agent.py."""

import json
from datetime import datetime, timedelta, timezone

import pytest
from google.genai import types
from pydantic import ValidationError

from app.agent_runtime.capabilities import AgentCapability, CapabilityError
from app.agent_runtime.context import AgentContext
from app.agent_runtime.gateways.detections import RepositoryDetectionGateway
from app.agent_runtime.gateways.signals import RepositorySignalGateway
from app.agent_runtime.status import AgentStatus
from app.agents.detecting import DetectingAgent, DetectingInput, DetectionOutput, _detecting_llm_agent
from app.domain import (
    AgentInput,
    DetectionDomain,
    DetectionType,
    Signal,
    SignalProvenance,
    SignalSeverity,
    SignalSource,
    SignalType,
    Ticket,
    WorkflowStatus,
    compute_fingerprint,
)
from app.persistence.memory import InMemoryAgentExecutionRepository, InMemoryArtifactRepository, InMemoryDetectionRepository, InMemorySignalRepository
from app.persistence.repositories.detection import DetectionQuery

NOW = datetime.now(timezone.utc)


def make_signal(source_event_id: str, *, stype=SignalType.METRIC_ANOMALY, source=SignalSource.CLOUD_MONITORING, service="quipu-api", minutes_ago=1, **overrides) -> Signal:
    subject = overrides.get("subject", service) or "unspecified"
    defaults = dict(
        signal_type=stype,
        source=source,
        severity=SignalSeverity.WARNING,
        observed_at=NOW - timedelta(minutes=minutes_ago),
        subject=subject,
        summary=f"signal {source_event_id}",
        service_name=service,
        environment="production",
        provenance=SignalProvenance(source_system="x", source_event_id=source_event_id),
        fingerprint=compute_fingerprint(source=source, source_event_id=source_event_id, subject=subject),
    )
    defaults.update(overrides)
    return Signal(**defaults)


# ---- ADK fakes (same pattern as test_testing_agent.py / test_codegen_agent.py) --------


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


DEFAULT_OUTPUT = dict(
    detection_type="incident",
    title="Probable deployment-related incident",
    summary="Error rate and latency rose together shortly after deployment.",
    rationale="Deployment event preceded error-rate and latency signals for the same service/revision.",
    confidence=0.9,
    severity="critical",
    subject="quipu-api",
    knowledge_references=[],
)


def make_agent_input(**context_overrides) -> AgentInput:
    context = {"domain": "operational", "service_name": "quipu-api", "environment": "production", "window_minutes": 60}
    context.update(context_overrides)
    return AgentInput(workflow_id="wf-1", agent_name="detecting_agent", ticket=Ticket(title="detect", description="detect"), context=context)


def make_context(**overrides):
    signal_repo = overrides.pop("signal_repo", None) or InMemorySignalRepository()
    detection_repo = overrides.pop("detection_repo", None) or InMemoryDetectionRepository()
    defaults = dict(
        workflow_id="wf-1",
        execution_id="exec-1",
        knowledge=None,
        tools=None,
        artifacts=InMemoryArtifactRepository(),
        executions=InMemoryAgentExecutionRepository(),
        signals=RepositorySignalGateway(signal_repo),
        detections=RepositoryDetectionGateway(detection_repo),
    )
    defaults.update(overrides)
    return AgentContext(**defaults), signal_repo, detection_repo


async def seed_signals(signal_repo, signals):
    for s in signals:
        await signal_repo.save(s)
    return signals


# ---- Runtime ------------------------------------------------------------------


def test_detecting_agent_identity():
    agent = DetectingAgent()
    assert agent.identity.agent_id == "detecting_agent"


def test_detecting_agent_capabilities_are_read_only():
    agent = DetectingAgent()
    assert agent.capabilities == {AgentCapability.READ_SIGNALS, AgentCapability.QUERY_KNOWLEDGE, AgentCapability.WRITE_DETECTION}
    forbidden = {AgentCapability.WRITE_CODE, AgentCapability.DEPLOY, AgentCapability.WRITE_JIRA, AgentCapability.RESOLVE_INCIDENT, AgentCapability.ROLLBACK, AgentCapability.CREATE_INCIDENT}
    assert agent.capabilities.isdisjoint(forbidden)


@pytest.mark.asyncio
async def test_lifecycle_completes(monkeypatch):
    monkeypatch.setattr("app.agents.detecting.InMemoryRunner", make_fake_runner_returning(json.dumps(DEFAULT_OUTPUT)))
    context, signal_repo, _ = make_context()
    signals = await seed_signals(signal_repo, [make_signal("1"), make_signal("2")])
    output_data = {**DEFAULT_OUTPUT, "supporting_signal_ids": [s.signal_id for s in signals]}
    monkeypatch.setattr("app.agents.detecting.InMemoryRunner", make_fake_runner_returning(json.dumps(output_data)))

    agent = DetectingAgent()
    output = await agent.execute(make_agent_input(), context)
    assert agent.status == AgentStatus.COMPLETED
    assert output.status == WorkflowStatus.COMPLETED


# ---- Input validation ---------------------------------------------------------


@pytest.mark.asyncio
async def test_invalid_input_rejected():
    context, _, _ = make_context()
    agent = DetectingAgent()
    output = await agent.execute(make_agent_input(domain="not_a_real_domain"), context)
    assert output.status == WorkflowStatus.FAILED
    assert output.errors[0].code == "DETECTING_INPUT_INVALID"


@pytest.mark.asyncio
async def test_window_exceeding_ceiling_rejected():
    context, _, _ = make_context()
    agent = DetectingAgent()
    output = await agent.execute(make_agent_input(window_minutes=999_999_999), context)
    assert output.status == WorkflowStatus.FAILED
    assert output.errors[0].code == "DETECTING_WINDOW_TOO_LARGE"


@pytest.mark.asyncio
async def test_environment_outside_allowed_scope_rejected():
    context, _, _ = make_context()
    agent = DetectingAgent()
    output = await agent.execute(make_agent_input(environment="totally-unapproved"), context)
    assert output.status == WorkflowStatus.FAILED
    assert output.errors[0].code == "DETECTING_ENVIRONMENT_NOT_ALLOWED"


@pytest.mark.asyncio
async def test_missing_signal_gateway_rejected():
    context, _, _ = make_context()
    context.signals = None
    agent = DetectingAgent()
    output = await agent.execute(make_agent_input(), context)
    assert output.status == WorkflowStatus.FAILED
    assert output.errors[0].code == "DETECTING_SIGNAL_GATEWAY_MISSING"


@pytest.mark.asyncio
async def test_missing_detection_gateway_rejected():
    context, _, _ = make_context()
    context.detections = None
    agent = DetectingAgent()
    output = await agent.execute(make_agent_input(), context)
    assert output.status == WorkflowStatus.FAILED
    assert output.errors[0].code == "DETECTING_DETECTION_GATEWAY_MISSING"


def test_product_domain_does_not_require_service_name():
    detecting_input = DetectingInput(domain=DetectionDomain.PRODUCT, environment="production")
    assert detecting_input.service_name is None


# ---- Signal retrieval / evidence-first: no evidence -----------------------------


@pytest.mark.asyncio
async def test_no_signals_produces_deterministic_no_action_without_calling_llm(monkeypatch):
    called = {"value": False}

    def _boom(*args, **kwargs):
        called["value"] = True
        raise AssertionError("LLM should never be invoked with zero evidence")

    monkeypatch.setattr("app.agents.detecting.InMemoryRunner", _boom)
    context, _, detection_repo = make_context()
    agent = DetectingAgent()
    output = await agent.execute(make_agent_input(), context)
    assert output.status == WorkflowStatus.COMPLETED
    assert not called["value"]
    parsed = json.loads(output.messages[1])
    assert parsed["detection_type"] == "no_action"
    assert parsed["confidence"] == 0.0


# ---- Operational scenarios ------------------------------------------------------


@pytest.mark.asyncio
async def test_deployment_plus_error_spike_produces_incident(monkeypatch):
    context, signal_repo, detection_repo = make_context()
    signals = await seed_signals(
        signal_repo,
        [
            make_signal("dep", stype=SignalType.DEPLOYMENT_EVENT, minutes_ago=10),
            make_signal("err", stype=SignalType.METRIC_ANOMALY, minutes_ago=5),
            make_signal("lat", stype=SignalType.LATENCY_ANOMALY, minutes_ago=4),
        ],
    )
    output_data = {**DEFAULT_OUTPUT, "supporting_signal_ids": [s.signal_id for s in signals]}
    monkeypatch.setattr("app.agents.detecting.InMemoryRunner", make_fake_runner_returning(json.dumps(output_data)))

    agent = DetectingAgent()
    output = await agent.execute(make_agent_input(), context)
    parsed = json.loads(output.messages[1])
    assert parsed["detection_type"] == "incident"
    assert set(parsed["supporting_signal_ids"]) == {s.signal_id for s in signals}


@pytest.mark.asyncio
async def test_isolated_low_severity_signal_can_result_in_no_action(monkeypatch):
    context, signal_repo, _ = make_context()
    signals = await seed_signals(signal_repo, [make_signal("1", stype=SignalType.METRIC_ANOMALY)])
    output_data = {
        **DEFAULT_OUTPUT,
        "detection_type": "no_action",
        "confidence": 0.2,
        "supporting_signal_ids": [signals[0].signal_id],
        "rationale": "Single low-severity signal is not sufficient to conclude an incident.",
    }
    monkeypatch.setattr("app.agents.detecting.InMemoryRunner", make_fake_runner_returning(json.dumps(output_data)))
    agent = DetectingAgent()
    output = await agent.execute(make_agent_input(), context)
    parsed = json.loads(output.messages[1])
    assert parsed["detection_type"] == "no_action"


@pytest.mark.asyncio
async def test_unrelated_signals_do_not_force_incident(monkeypatch):
    context, signal_repo, _ = make_context()
    signals = await seed_signals(signal_repo, [make_signal("1", stype=SignalType.LOG_ERROR), make_signal("2", stype=SignalType.APPLICATION_ERROR)])
    output_data = {**DEFAULT_OUTPUT, "detection_type": "no_action", "confidence": 0.1, "supporting_signal_ids": []}
    monkeypatch.setattr("app.agents.detecting.InMemoryRunner", make_fake_runner_returning(json.dumps(output_data)))
    agent = DetectingAgent()
    output = await agent.execute(make_agent_input(), context)
    parsed = json.loads(output.messages[1])
    assert parsed["detection_type"] == "no_action"


# ---- Product scenarios --------------------------------------------------------


@pytest.mark.asyncio
async def test_repeated_customer_and_support_feedback_produces_feature_opportunity(monkeypatch):
    context, signal_repo, _ = make_context()
    signals = await seed_signals(
        signal_repo,
        [
            make_signal("fb-1", stype=SignalType.CUSTOMER_FEEDBACK, source=SignalSource.CUSTOMER_FEEDBACK, service=None, subject="export"),
            make_signal("fb-2", stype=SignalType.CUSTOMER_FEEDBACK, source=SignalSource.CUSTOMER_FEEDBACK, service=None, subject="export"),
            make_signal("sup-1", stype=SignalType.SUPPORT_FEEDBACK, source=SignalSource.SUPPORT_SYSTEM, service=None, subject="export"),
        ],
    )
    output_data = {
        **DEFAULT_OUTPUT,
        "detection_type": "feature_opportunity",
        "severity": None,
        "subject": "export",
        "summary": "Multiple independent sources request Excel export.",
        "supporting_signal_ids": [s.signal_id for s in signals],
    }
    monkeypatch.setattr("app.agents.detecting.InMemoryRunner", make_fake_runner_returning(json.dumps(output_data)))
    agent = DetectingAgent()
    output = await agent.execute(make_agent_input(domain="product", service_name=None, environment=None, window_minutes=10080), context)
    parsed = json.loads(output.messages[1])
    assert parsed["detection_type"] == "feature_opportunity"
    assert len(parsed["supporting_signal_ids"]) == 3


@pytest.mark.asyncio
async def test_weak_single_feedback_signal_can_result_in_no_action(monkeypatch):
    context, signal_repo, _ = make_context()
    signals = await seed_signals(signal_repo, [make_signal("fb-1", stype=SignalType.CUSTOMER_FEEDBACK, source=SignalSource.CUSTOMER_FEEDBACK, service=None, subject="export")])
    output_data = {**DEFAULT_OUTPUT, "detection_type": "no_action", "confidence": 0.15, "severity": None, "subject": "export", "supporting_signal_ids": [signals[0].signal_id]}
    monkeypatch.setattr("app.agents.detecting.InMemoryRunner", make_fake_runner_returning(json.dumps(output_data)))
    agent = DetectingAgent()
    output = await agent.execute(make_agent_input(domain="product", service_name=None, environment=None), context)
    parsed = json.loads(output.messages[1])
    assert parsed["detection_type"] == "no_action"


@pytest.mark.asyncio
async def test_user_behavior_pattern_detected(monkeypatch):
    context, signal_repo, _ = make_context()
    signals = await seed_signals(signal_repo, [make_signal("ub-1", stype=SignalType.USER_BEHAVIOR, source=SignalSource.USER_BEHAVIOR, service=None, subject="reports")])
    output_data = {**DEFAULT_OUTPUT, "detection_type": "feature_opportunity", "severity": None, "subject": "reports", "supporting_signal_ids": [signals[0].signal_id], "confidence": 0.6}
    monkeypatch.setattr("app.agents.detecting.InMemoryRunner", make_fake_runner_returning(json.dumps(output_data)))
    agent = DetectingAgent()
    output = await agent.execute(make_agent_input(domain="product", service_name=None, environment=None), context)
    parsed = json.loads(output.messages[1])
    assert parsed["detection_type"] == "feature_opportunity"


# ---- Evidence integrity / adversarial tests -----------------------------------


@pytest.mark.asyncio
async def test_fabricated_signal_id_is_rejected_not_trusted(monkeypatch):
    context, signal_repo, _ = make_context()
    signals = await seed_signals(signal_repo, [make_signal("1"), make_signal("2")])
    output_data = {**DEFAULT_OUTPUT, "supporting_signal_ids": ["fake-signal-999"]}
    monkeypatch.setattr("app.agents.detecting.InMemoryRunner", make_fake_runner_returning(json.dumps(output_data)))
    agent = DetectingAgent()
    output = await agent.execute(make_agent_input(), context)
    parsed = json.loads(output.messages[1])
    assert "fake-signal-999" not in parsed["supporting_signal_ids"]
    assert parsed["detection_type"] == "no_action"  # downgraded — no real evidence survived
    assert parsed["confidence"] == 0.0


@pytest.mark.asyncio
async def test_partially_fabricated_ids_keeps_only_real_ones(monkeypatch):
    context, signal_repo, _ = make_context()
    signals = await seed_signals(signal_repo, [make_signal("1"), make_signal("2")])
    output_data = {**DEFAULT_OUTPUT, "supporting_signal_ids": [signals[0].signal_id, "fake-signal-999"]}
    monkeypatch.setattr("app.agents.detecting.InMemoryRunner", make_fake_runner_returning(json.dumps(output_data)))
    agent = DetectingAgent()
    output = await agent.execute(make_agent_input(), context)
    parsed = json.loads(output.messages[1])
    assert parsed["supporting_signal_ids"] == [signals[0].signal_id]
    assert parsed["detection_type"] == "incident"  # 1 verified signal meets the minimum


@pytest.mark.asyncio
async def test_feature_opportunity_claim_with_only_unrelated_low_quality_signal(monkeypatch):
    """The model claims FEATURE_OPPORTUNITY citing one real but weak signal
    — the system persists what the model concluded (that judgment call is
    legitimately the LLM's to make about sufficiency), but the evidence
    reference itself must still be real, not fabricated."""
    context, signal_repo, _ = make_context()
    signals = await seed_signals(signal_repo, [make_signal("1", stype=SignalType.CUSTOMER_FEEDBACK, source=SignalSource.CUSTOMER_FEEDBACK, service=None, subject="x")])
    output_data = {**DEFAULT_OUTPUT, "detection_type": "feature_opportunity", "severity": None, "subject": "x", "supporting_signal_ids": [signals[0].signal_id]}
    monkeypatch.setattr("app.agents.detecting.InMemoryRunner", make_fake_runner_returning(json.dumps(output_data)))
    agent = DetectingAgent()
    output = await agent.execute(make_agent_input(domain="product", service_name=None, environment=None), context)
    parsed = json.loads(output.messages[1])
    assert parsed["supporting_signal_ids"] == [signals[0].signal_id]  # real reference, kept


@pytest.mark.asyncio
async def test_incident_claim_with_zero_retrieved_signals_fails_safely(monkeypatch):
    """No signals were ever retrieved at all — the agent never even calls
    Gemini (see test_no_signals_produces_deterministic_no_action_without_calling_llm),
    so an INCIDENT claim can never reach persistence in this scenario."""
    called = {"value": False}

    def _boom(*args, **kwargs):
        called["value"] = True
        raise AssertionError("must not be called")

    monkeypatch.setattr("app.agents.detecting.InMemoryRunner", _boom)
    context, _, _ = make_context()
    agent = DetectingAgent()
    output = await agent.execute(make_agent_input(service_name="totally-unmonitored-service"), context)
    assert not called["value"]
    parsed = json.loads(output.messages[1])
    assert parsed["detection_type"] == "no_action"


@pytest.mark.asyncio
async def test_all_supporting_signal_ids_verified_against_retrieved_evidence(monkeypatch):
    context, signal_repo, _ = make_context()
    signals = await seed_signals(signal_repo, [make_signal("1"), make_signal("2"), make_signal("3")])
    retrieved_ids = {s.signal_id for s in signals}
    output_data = {**DEFAULT_OUTPUT, "supporting_signal_ids": [signals[0].signal_id, signals[2].signal_id]}
    monkeypatch.setattr("app.agents.detecting.InMemoryRunner", make_fake_runner_returning(json.dumps(output_data)))
    agent = DetectingAgent()
    output = await agent.execute(make_agent_input(), context)
    parsed = json.loads(output.messages[1])
    assert set(parsed["supporting_signal_ids"]).issubset(retrieved_ids)


@pytest.mark.asyncio
async def test_original_signals_remain_unchanged_after_detection(monkeypatch):
    context, signal_repo, _ = make_context()
    signals = await seed_signals(signal_repo, [make_signal("1"), make_signal("2")])
    before = [await signal_repo.get(s.signal_id) for s in signals]
    output_data = {**DEFAULT_OUTPUT, "supporting_signal_ids": [s.signal_id for s in signals]}
    monkeypatch.setattr("app.agents.detecting.InMemoryRunner", make_fake_runner_returning(json.dumps(output_data)))
    agent = DetectingAgent()
    await agent.execute(make_agent_input(), context)
    after = [await signal_repo.get(s.signal_id) for s in signals]
    assert before == after


@pytest.mark.asyncio
async def test_model_cannot_inject_arbitrary_evidence_field():
    """DetectionOutput's schema has no field through which the model could
    smuggle extra unvalidated evidence (e.g. a free-form 'raw_evidence' dict)."""
    assert set(DetectionOutput.model_fields) == {
        "detection_type",
        "title",
        "summary",
        "rationale",
        "confidence",
        "severity",
        "subject",
        "supporting_signal_ids",
        "knowledge_references",
    }


# ---- Knowledge -----------------------------------------------------------------


def test_knowledge_tool_available():
    tool_names = {t.__name__ for t in _detecting_llm_agent.tools if callable(t)}
    assert "query_enterprise_knowledge" in tool_names


def test_no_arbitrary_tool_beyond_knowledge():
    """Detecting has zero tools that could modify anything — only the
    read-only knowledge tool. No signal-fetching tool is exposed to the LLM
    at all (evidence retrieval is deterministic Python in _perform)."""
    tool_names = {t.__name__ for t in _detecting_llm_agent.tools if callable(t)}
    assert tool_names == {"query_enterprise_knowledge"}


@pytest.mark.asyncio
async def test_knowledge_gateway_wired_into_session_state_when_present(monkeypatch):
    captured = {}

    class _CapturingRunner:
        def __init__(self, agent, app_name):
            self.session_service = _CapturingSessionService()

        def run_async(self, **kwargs):
            captured.update(self.session_service.captured_state)

            async def _events():
                yield _FakeEvent(json.dumps(DEFAULT_OUTPUT))

            return _events()

    monkeypatch.setattr("app.agents.detecting.InMemoryRunner", _CapturingRunner)
    context, signal_repo, _ = make_context()
    await seed_signals(signal_repo, [make_signal("1")])

    class _FakeKnowledgeGateway:
        async def search(self, request):
            return []

    context.knowledge = _FakeKnowledgeGateway()
    agent = DetectingAgent()
    await agent.execute(make_agent_input(), context)
    assert captured.get("_knowledge_gateway") is context.knowledge


@pytest.mark.asyncio
async def test_knowledge_references_stored_separately_from_evidence(monkeypatch):
    context, signal_repo, _ = make_context()
    signals = await seed_signals(signal_repo, [make_signal("1")])
    output_data = {**DEFAULT_OUTPUT, "supporting_signal_ids": [signals[0].signal_id], "knowledge_references": ["doc-42"]}
    monkeypatch.setattr("app.agents.detecting.InMemoryRunner", make_fake_runner_returning(json.dumps(output_data)))
    agent = DetectingAgent()
    output = await agent.execute(make_agent_input(), context)
    parsed = json.loads(output.messages[1])
    assert parsed["knowledge_references"] == ["doc-42"]
    assert "doc-42" not in parsed["supporting_signal_ids"]  # knowledge is never evidence


# ---- LLM failure handling -------------------------------------------------------


@pytest.mark.asyncio
async def test_llm_failure_fails_safely(monkeypatch):
    context, signal_repo, _ = make_context()
    await seed_signals(signal_repo, [make_signal("1")])
    monkeypatch.setattr("app.agents.detecting.InMemoryRunner", make_fake_runner_raising(RuntimeError("gemini down")))
    agent = DetectingAgent()
    output = await agent.execute(make_agent_input(), context)
    assert output.status == WorkflowStatus.FAILED
    assert output.errors[0].code == "DETECTING_LLM_FAILURE"


@pytest.mark.asyncio
async def test_empty_response_fails_safely(monkeypatch):
    context, signal_repo, _ = make_context()
    await seed_signals(signal_repo, [make_signal("1")])
    monkeypatch.setattr("app.agents.detecting.InMemoryRunner", make_fake_runner_returning(""))
    agent = DetectingAgent()
    output = await agent.execute(make_agent_input(), context)
    assert output.status == WorkflowStatus.FAILED
    assert output.errors[0].code == "DETECTING_EMPTY_RESPONSE"


@pytest.mark.asyncio
async def test_malformed_json_fails_safely(monkeypatch):
    context, signal_repo, _ = make_context()
    await seed_signals(signal_repo, [make_signal("1")])
    monkeypatch.setattr("app.agents.detecting.InMemoryRunner", make_fake_runner_returning("not valid json"))
    agent = DetectingAgent()
    output = await agent.execute(make_agent_input(), context)
    assert output.status == WorkflowStatus.FAILED
    assert output.errors[0].code == "DETECTING_VALIDATION_FAILED"


@pytest.mark.asyncio
async def test_invalid_confidence_rejected(monkeypatch):
    context, signal_repo, _ = make_context()
    await seed_signals(signal_repo, [make_signal("1")])
    bad_output = {**DEFAULT_OUTPUT, "confidence": 1.5}
    monkeypatch.setattr("app.agents.detecting.InMemoryRunner", make_fake_runner_returning(json.dumps(bad_output)))
    agent = DetectingAgent()
    output = await agent.execute(make_agent_input(), context)
    assert output.status == WorkflowStatus.FAILED
    assert output.errors[0].code == "DETECTING_VALIDATION_FAILED"


@pytest.mark.asyncio
async def test_invalid_detection_type_rejected(monkeypatch):
    context, signal_repo, _ = make_context()
    await seed_signals(signal_repo, [make_signal("1")])
    bad_output = {**DEFAULT_OUTPUT, "detection_type": "root_cause_analysis"}
    monkeypatch.setattr("app.agents.detecting.InMemoryRunner", make_fake_runner_returning(json.dumps(bad_output)))
    agent = DetectingAgent()
    output = await agent.execute(make_agent_input(), context)
    assert output.status == WorkflowStatus.FAILED
    assert output.errors[0].code == "DETECTING_VALIDATION_FAILED"


# ---- Persistence / deduplication ------------------------------------------------


@pytest.mark.asyncio
async def test_detection_persisted_through_detection_repository(monkeypatch):
    context, signal_repo, detection_repo = make_context()
    signals = await seed_signals(signal_repo, [make_signal("1")])
    output_data = {**DEFAULT_OUTPUT, "supporting_signal_ids": [signals[0].signal_id]}
    monkeypatch.setattr("app.agents.detecting.InMemoryRunner", make_fake_runner_returning(json.dumps(output_data)))
    agent = DetectingAgent()
    await agent.execute(make_agent_input(), context)
    all_detections = await detection_repo.query(DetectionQuery(limit=50))
    assert len(all_detections) == 1


@pytest.mark.asyncio
async def test_repeated_run_over_same_evidence_does_not_duplicate(monkeypatch):
    context, signal_repo, detection_repo = make_context()
    signals = await seed_signals(signal_repo, [make_signal("1")])
    output_data = {**DEFAULT_OUTPUT, "supporting_signal_ids": [signals[0].signal_id]}
    monkeypatch.setattr("app.agents.detecting.InMemoryRunner", make_fake_runner_returning(json.dumps(output_data)))
    agent1 = DetectingAgent()
    agent2 = DetectingAgent()
    await agent1.execute(make_agent_input(), context)
    await agent2.execute(make_agent_input(), context)
    all_detections = await detection_repo.query(DetectionQuery(limit=50))
    assert len(all_detections) == 1  # not 2 — same fingerprint


# ---- Capability enforcement -----------------------------------------------------


@pytest.mark.asyncio
async def test_retrieve_evidence_rejects_when_capability_not_granted():
    context, signal_repo, _ = make_context()
    agent = DetectingAgent()
    with pytest.raises(CapabilityError):
        await agent._retrieve_evidence(
            DetectingInput(domain=DetectionDomain.OPERATIONAL, service_name="quipu-api", environment="production"),
            50,
            context.signals,
            granted=set(),
        )


def test_detecting_agent_capabilities_exclude_mutation_capabilities():
    agent = DetectingAgent()
    assert AgentCapability.WRITE_CODE not in agent.capabilities
    assert AgentCapability.DEPLOY not in agent.capabilities
    assert AgentCapability.ROLLBACK not in agent.capabilities
    assert AgentCapability.CREATE_INCIDENT not in agent.capabilities
    assert AgentCapability.RESOLVE_INCIDENT not in agent.capabilities
    assert AgentCapability.WRITE_JIRA not in agent.capabilities


# ---- Security ---------------------------------------------------------------------


def test_no_shell_or_subprocess_surface_in_detecting_module():
    import inspect

    import app.agents.detecting as detecting_module

    source = inspect.getsource(detecting_module)
    assert "subprocess" not in source
    assert "os.system" not in source
    assert "shell=True" not in source


@pytest.mark.asyncio
async def test_signal_retrieval_bounded_by_max_signals(monkeypatch):
    context, signal_repo, _ = make_context()
    many_signals = [make_signal(str(i), minutes_ago=i) for i in range(20)]
    await seed_signals(signal_repo, many_signals)

    captured = {}

    class _CapturingRunner:
        def __init__(self, agent, app_name):
            self.session_service = _CapturingSessionService()

        def run_async(self, **kwargs):
            captured.update(self.session_service.captured_state)

            async def _events():
                yield _FakeEvent(json.dumps(DEFAULT_OUTPUT))

            return _events()

    monkeypatch.setattr("app.agents.detecting.InMemoryRunner", _CapturingRunner)
    agent = DetectingAgent()
    await agent.execute(make_agent_input(max_signals=5), context)
    assert len(captured["evidence_set"]) <= 5


@pytest.mark.asyncio
async def test_max_signals_capped_by_configured_ceiling(monkeypatch):
    from app.config import get_settings

    context, signal_repo, _ = make_context()
    many_signals = [make_signal(str(i), minutes_ago=i) for i in range(60)]
    await seed_signals(signal_repo, many_signals)

    captured = {}

    class _CapturingRunner:
        def __init__(self, agent, app_name):
            self.session_service = _CapturingSessionService()

        def run_async(self, **kwargs):
            captured.update(self.session_service.captured_state)

            async def _events():
                yield _FakeEvent(json.dumps(DEFAULT_OUTPUT))

            return _events()

    monkeypatch.setattr("app.agents.detecting.InMemoryRunner", _CapturingRunner)
    agent = DetectingAgent()
    await agent.execute(make_agent_input(max_signals=10_000), context)
    settings = get_settings()
    assert len(captured["evidence_set"]) <= settings.detecting_max_signals


def test_detecting_input_has_no_raw_query_string_field():
    """The model/caller cannot inject an arbitrary Signal repository filter
    — DetectingInput exposes only typed, bounded fields."""
    assert set(DetectingInput.model_fields) == {"domain", "service_name", "environment", "signal_types", "window_minutes", "max_signals"}
    assert "filter" not in DetectingInput.model_fields
    assert "query" not in DetectingInput.model_fields


@pytest.mark.asyncio
async def test_evidence_dict_does_not_include_raw_signal_metadata_field(monkeypatch):
    """Signal.metadata (a free-form bucket) is never forwarded into the
    LLM's evidence set — only the typed fields plus the already-sanitized
    `evidence` dict."""
    context, signal_repo, _ = make_context()
    signal = make_signal("1", metadata={"internal_note": "should not reach Gemini"})
    await seed_signals(signal_repo, [signal])

    captured = {}

    class _CapturingRunner:
        def __init__(self, agent, app_name):
            self.session_service = _CapturingSessionService()

        def run_async(self, **kwargs):
            captured.update(self.session_service.captured_state)

            async def _events():
                yield _FakeEvent(json.dumps(DEFAULT_OUTPUT))

            return _events()

    monkeypatch.setattr("app.agents.detecting.InMemoryRunner", _CapturingRunner)
    agent = DetectingAgent()
    await agent.execute(make_agent_input(), context)
    assert "metadata" not in captured["evidence_set"][0]
    assert "internal_note" not in json.dumps(captured["evidence_set"])
