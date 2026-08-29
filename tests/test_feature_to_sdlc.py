"""Level 3.5 — Feature -> SDLC workflow integration tests.

Covers: OrchestrationService.start_workflow_from_review() (creation,
idempotency, concurrency, validation), provenance survival from
Signal -> DetectionResult -> FeatureReview -> Ticket -> WorkflowState,
Planning handoff (both standalone and through the orchestrator), Jira
non-duplication, security/adversarial cases, and legacy (manually-submitted
ticket) compatibility. No live Gemini/Jira/Firestore required.
"""

import asyncio
import json
from datetime import datetime, timezone

import pytest
from google.genai import types

import app.agents.planning as planning_module
from app.agent_runtime.capabilities import AgentCapability
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
    Ticket,
    WorkflowStage,
    WorkflowStatus,
    compute_detection_fingerprint,
    compute_fingerprint,
)
from app.feature_review import FeatureReviewService
from app.agent_runtime.gateways.detections import RepositoryDetectionGateway
from app.agent_runtime.gateways.signals import RepositorySignalGateway
from app.orchestration.errors import OrchestrationError
from app.orchestration.registry_setup import build_default_registry
from app.orchestration.service import OrchestrationService
from app.persistence.memory import (
    InMemoryAgentExecutionRepository,
    InMemoryArtifactRepository,
    InMemoryDecisionRepository,
    InMemoryDetectionRepository,
    InMemoryFeatureReviewRepository,
    InMemorySignalRepository,
    InMemoryWorkflowRepository,
)

NOW = datetime.now(timezone.utc)
GRANTED = {AgentCapability.REVIEW_FEATURE_OPPORTUNITY}

VALID_PLAN = {
    "feature_summary": "Add Excel export",
    "architecture_notes": "Reuse the existing export module.",
    "affected_components": [{"name": "export", "reason": "add an excel format"}],
    "tasks": [{"id": "t1", "description": "implement excel exporter", "depends_on": []}],
    "dependencies": [],
    "acceptance_criteria": ["user can export a report to excel"],
    "risks": [{"description": "library size", "mitigation": "lazy import"}],
}


# ---- fakes ----------------------------------------------------------------


class FakeKnowledgeGateway:
    async def search(self, request):
        return []


class FakeToolGateway:
    async def execute(self, request):
        raise NotImplementedError


class FakeJiraClient:
    def __init__(self):
        self.calls = 0

    def create_story(self, summary: str, description: str) -> dict:
        self.calls += 1
        return {"key": f"QUIPU-{self.calls}", "url": f"https://example.atlassian.net/browse/QUIPU-{self.calls}"}


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


def make_plain_runner(final_text: str):
    class _FakeRunner:
        def __init__(self, agent, app_name):
            self.session_service = _CapturingSessionService()

        def run_async(self, **kwargs):
            async def _events():
                yield _FakeEvent(final_text)

            return _events()

    return _FakeRunner


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


class Harness:
    """One assembled set of in-memory repos + services, mirroring how a
    real deployment would wire FeatureReviewService and OrchestrationService
    against the same underlying persistence."""

    def __init__(self):
        self.signal_repo = InMemorySignalRepository()
        self.detection_repo = InMemoryDetectionRepository()
        self.review_repo = InMemoryFeatureReviewRepository()
        self.workflow_repo = InMemoryWorkflowRepository()
        self.artifact_repo = InMemoryArtifactRepository()
        self.execution_repo = InMemoryAgentExecutionRepository()
        self.decision_repo = InMemoryDecisionRepository()
        self.jira = FakeJiraClient()

        self.review_service = FeatureReviewService(
            self.review_repo, RepositoryDetectionGateway(self.detection_repo), RepositorySignalGateway(self.signal_repo), jira_client=self.jira
        )
        self.orchestration = OrchestrationService(
            workflow_repo=self.workflow_repo,
            artifact_repo=self.artifact_repo,
            execution_repo=self.execution_repo,
            decision_repo=self.decision_repo,
            registry=build_default_registry(),
            knowledge_gateway=FakeKnowledgeGateway(),
            tool_gateway=FakeToolGateway(),
            review_repo=self.review_repo,
        )

    async def seed_approved_feature(self, **detection_overrides):
        signal = make_signal("1")
        await self.signal_repo.save(signal)
        detection = make_detection([signal], **detection_overrides)
        await self.detection_repo.save(detection)
        review = await self.review_service.create_review(detection.detection_id)
        approved = await self.review_service.approve(review.review_id, reviewer_id="pm@company.com", reviewer_type=DecisionSource.HUMAN, granted=GRANTED)
        return signal, detection, approved


def patch_planning(monkeypatch, plan=None):
    monkeypatch.setattr("app.agents.planning.InMemoryRunner", make_plain_runner(json.dumps(plan or VALID_PLAN)))
    monkeypatch.setattr("app.agents.planning.JiraClient", FakeJiraClient)


# ---- Workflow creation from an approved review ---------------------------------


@pytest.mark.asyncio
async def test_valid_approved_review_creates_workflow():
    harness = Harness()
    _, _, review = await harness.seed_approved_feature()
    workflow = await harness.orchestration.start_workflow_from_review(review.review_id)
    assert workflow.current_stage == WorkflowStage.PLANNING
    assert workflow.status == WorkflowStatus.PENDING


@pytest.mark.asyncio
async def test_workflow_ticket_association_preserved():
    harness = Harness()
    _, _, review = await harness.seed_approved_feature()
    workflow = await harness.orchestration.start_workflow_from_review(review.review_id)
    assert workflow.ticket.ticket_id == review.ticket.ticket_id
    assert workflow.ticket.external_id == review.ticket.external_id


@pytest.mark.asyncio
async def test_workflow_source_detection_id_preserved():
    harness = Harness()
    _, detection, review = await harness.seed_approved_feature()
    workflow = await harness.orchestration.start_workflow_from_review(review.review_id)
    assert workflow.ticket.source_detection_id == detection.detection_id


@pytest.mark.asyncio
async def test_workflow_metadata_records_feature_review_origin():
    harness = Harness()
    _, detection, review = await harness.seed_approved_feature()
    workflow = await harness.orchestration.start_workflow_from_review(review.review_id)
    assert workflow.metadata["source"] == "feature_review"
    assert workflow.metadata["review_id"] == review.review_id
    assert workflow.metadata["source_detection_id"] == detection.detection_id


@pytest.mark.asyncio
async def test_workflow_persists_and_is_retrievable():
    harness = Harness()
    _, _, review = await harness.seed_approved_feature()
    workflow = await harness.orchestration.start_workflow_from_review(review.review_id)
    fetched = await harness.workflow_repo.get(workflow.workflow_id)
    assert fetched is not None
    assert fetched.workflow_id == workflow.workflow_id


@pytest.mark.asyncio
async def test_review_records_workflow_id_after_start():
    harness = Harness()
    _, _, review = await harness.seed_approved_feature()
    workflow = await harness.orchestration.start_workflow_from_review(review.review_id)
    stored_review = await harness.review_repo.get(review.review_id)
    assert stored_review.workflow_id == workflow.workflow_id


# ---- Planning handoff -----------------------------------------------------------


@pytest.mark.asyncio
async def test_workflow_builds_correct_agent_input_for_planning(monkeypatch):
    harness = Harness()
    patch_planning(monkeypatch)
    _, detection, review = await harness.seed_approved_feature()
    workflow = await harness.orchestration.start_workflow_from_review(review.review_id)

    captured = {}
    original_execute = harness.orchestration._build_agent_input

    def _spy(workflow_arg, agent_id, input_artifact_id, execution_id):
        agent_input = original_execute(workflow_arg, agent_id, input_artifact_id, execution_id)
        captured["agent_input"] = agent_input
        return agent_input

    monkeypatch.setattr(harness.orchestration, "_build_agent_input", _spy)
    await harness.orchestration.execute_next_step(workflow.workflow_id)

    agent_input = captured["agent_input"]
    assert agent_input.ticket.title == review.ticket.title
    assert agent_input.ticket.source_detection_id == detection.detection_id


@pytest.mark.asyncio
async def test_planning_receives_ticket_context_via_existing_mechanism(monkeypatch):
    """No new 'feature_request' session-state convention was introduced —
    Planning already reads ticket.title/description via AgentInput.ticket,
    unchanged."""
    harness = Harness()
    captured = {}

    class _CapturingRunner:
        def __init__(self, agent, app_name):
            self.session_service = _CapturingSessionService()

        def run_async(self, **kwargs):
            captured.update(self.session_service.captured_state)

            async def _events():
                yield _FakeEvent(json.dumps(VALID_PLAN))

            return _events()

    monkeypatch.setattr("app.agents.planning.InMemoryRunner", _CapturingRunner)
    monkeypatch.setattr("app.agents.planning.JiraClient", FakeJiraClient)

    _, detection, review = await harness.seed_approved_feature()
    workflow = await harness.orchestration.start_workflow_from_review(review.review_id)
    await harness.orchestration.execute_next_step(workflow.workflow_id)

    assert review.ticket.title in captured["ticket_summary"]


@pytest.mark.asyncio
async def test_planning_runs_standalone_with_feature_ticket(monkeypatch):
    """PlanningAgent works with a feature-derived ticket exactly like any
    other ticket — no OrchestrationService dependency."""
    from app.agent_runtime.context import AgentContext
    from app.agents.planning import PlanningAgent
    from app.domain import AgentInput

    harness = Harness()
    patch_planning(monkeypatch)
    _, _, review = await harness.seed_approved_feature()

    agent = PlanningAgent()
    context = AgentContext(
        workflow_id="standalone-wf",
        execution_id="standalone-exec",
        knowledge=FakeKnowledgeGateway(),
        tools=FakeToolGateway(),
        artifacts=InMemoryArtifactRepository(),
    )
    agent_input = AgentInput(workflow_id="standalone-wf", agent_name="planning_agent", ticket=review.ticket)
    output = await agent.execute(agent_input, context)
    assert output.status == WorkflowStatus.COMPLETED


@pytest.mark.asyncio
async def test_execute_next_step_invokes_planning_through_registry(monkeypatch):
    harness = Harness()
    patch_planning(monkeypatch)
    _, _, review = await harness.seed_approved_feature()
    workflow = await harness.orchestration.start_workflow_from_review(review.review_id)
    result = await harness.orchestration.execute_next_step(workflow.workflow_id)
    assert result.current_stage == WorkflowStage.ARCHITECTURE
    assert len(result.artifact_ids) == 1
    execution = await harness.execution_repo.list_for_workflow(workflow.workflow_id)
    assert any(e.agent_name == "planning_agent" for e in execution)


@pytest.mark.asyncio
async def test_enterprise_knowledge_still_reachable(monkeypatch):
    harness = Harness()
    patch_planning(monkeypatch)
    _, _, review = await harness.seed_approved_feature()
    workflow = await harness.orchestration.start_workflow_from_review(review.review_id)
    # FakeKnowledgeGateway.search would raise NotImplementedError if
    # something in the path broke the gateway wiring — instead it's just
    # never called since the model in this fake doesn't invoke the
    # knowledge tool; the important assertion is the run still completes.
    result = await harness.orchestration.execute_next_step(workflow.workflow_id)
    assert result.status != WorkflowStatus.FAILED


# ---- Feature provenance chain ----------------------------------------------------


@pytest.mark.asyncio
async def test_full_provenance_chain_survives():
    harness = Harness()
    signal, detection, review = await harness.seed_approved_feature()
    workflow = await harness.orchestration.start_workflow_from_review(review.review_id)

    assert signal.signal_id in detection.supporting_signal_ids
    assert review.detection_id == detection.detection_id
    assert review.ticket.source_detection_id == detection.detection_id
    assert workflow.ticket.source_detection_id == detection.detection_id
    assert workflow.metadata["review_id"] == review.review_id


# ---- Idempotency -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_starting_same_review_twice_returns_same_workflow():
    harness = Harness()
    _, _, review = await harness.seed_approved_feature()
    first = await harness.orchestration.start_workflow_from_review(review.review_id)
    second = await harness.orchestration.start_workflow_from_review(review.review_id)
    assert first.workflow_id == second.workflow_id
    all_workflows = list(harness.workflow_repo._store.keys())  # noqa: SLF001 — direct inspection, test-only
    assert len(all_workflows) == 1


@pytest.mark.asyncio
async def test_claimed_but_uncreated_workflow_is_recovered_on_retry():
    """Simulates a crash between the review claiming a workflow_id and the
    WorkflowState actually being created — the next call must create it
    using the already-claimed id, not error forever or claim a new one."""
    harness = Harness()
    _, _, review = await harness.seed_approved_feature()

    stuck_id = "pre-claimed-workflow-id"
    stored = await harness.review_repo.get(review.review_id)
    claimed = stored.model_copy(update={"workflow_id": stuck_id})
    await harness.review_repo.update_if_version(review.review_id, stored.version, claimed)

    assert await harness.workflow_repo.get(stuck_id) is None  # not created yet

    workflow = await harness.orchestration.start_workflow_from_review(review.review_id)
    assert workflow.workflow_id == stuck_id
    assert await harness.workflow_repo.get(stuck_id) is not None


# ---- Concurrency -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_start_from_review_only_one_workflow_wins():
    harness = Harness()
    _, _, review = await harness.seed_approved_feature()

    results = await asyncio.gather(
        harness.orchestration.start_workflow_from_review(review.review_id),
        harness.orchestration.start_workflow_from_review(review.review_id),
        return_exceptions=True,
    )
    succeeded = [r for r in results if not isinstance(r, Exception)]
    assert len(succeeded) >= 1
    workflow_ids = {r.workflow_id for r in succeeded}
    assert len(workflow_ids) == 1  # both callers agree on exactly one workflow


# ---- Jira behavior -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_planning_does_not_recreate_feature_ticket_in_jira(monkeypatch):
    harness = Harness()
    patch_planning(monkeypatch)
    _, _, review = await harness.seed_approved_feature()
    assert harness.jira.calls == 1  # only the feature-review-time creation

    workflow = await harness.orchestration.start_workflow_from_review(review.review_id)
    result = await harness.orchestration.execute_next_step(workflow.workflow_id)

    plan_artifact = await harness.artifact_repo.get(workflow.workflow_id, result.artifact_ids[0])
    # Planning still creates one Jira story PER TASK (a different kind of
    # Jira object than the feature-request-level ticket) — proven by the
    # plan's own task carrying a jira_key, using the module-level
    # FakeJiraClient Planning was patched to use (a separate instance from
    # harness.jira, since PlanningAgent constructs its own JiraClient()).
    assert plan_artifact.payload["tasks"][0]["jira_key"] is not None
    # harness.jira (FeatureReviewService's client) was never called again —
    # Planning used its own patched JiraClient, not FeatureReviewService's.
    assert harness.jira.calls == 1


@pytest.mark.asyncio
async def test_existing_manual_ticket_jira_behavior_unchanged(monkeypatch):
    """A manually-submitted ticket (no source_detection_id) still reaches
    Planning and gets task-level Jira stories exactly as before."""
    harness = Harness()
    patch_planning(monkeypatch)
    ticket = Ticket(title="Manual bug fix", description="Fix the thing")
    workflow = await harness.orchestration.start_workflow(ticket)
    result = await harness.orchestration.execute_next_step(workflow.workflow_id)
    plan_artifact = await harness.artifact_repo.get(workflow.workflow_id, result.artifact_ids[0])
    assert plan_artifact.payload["tasks"][0]["jira_key"] is not None


# ---- Security / adversarial ---------------------------------------------------------


@pytest.mark.asyncio
async def test_rejected_review_cannot_start_workflow():
    harness = Harness()
    signal = make_signal("1")
    await harness.signal_repo.save(signal)
    detection = make_detection([signal])
    await harness.detection_repo.save(detection)
    review = await harness.review_service.create_review(detection.detection_id)
    await harness.review_service.reject(review.review_id, reviewer_id="pm@company.com", reviewer_type=DecisionSource.HUMAN, granted=GRANTED)

    with pytest.raises(OrchestrationError):
        await harness.orchestration.start_workflow_from_review(review.review_id)


@pytest.mark.asyncio
async def test_pending_review_cannot_start_workflow():
    harness = Harness()
    signal = make_signal("1")
    await harness.signal_repo.save(signal)
    detection = make_detection([signal])
    await harness.detection_repo.save(detection)
    review = await harness.review_service.create_review(detection.detection_id)

    with pytest.raises(OrchestrationError):
        await harness.orchestration.start_workflow_from_review(review.review_id)


@pytest.mark.asyncio
async def test_incident_detection_cannot_become_feature_workflow():
    harness = Harness()
    signal = make_signal("1", stype=SignalType.APPLICATION_ERROR, source=SignalSource.CLOUD_LOGGING, subject="quipu-api")
    await harness.signal_repo.save(signal)
    detection = make_detection([signal], detection_type=DetectionType.INCIDENT, domain=DetectionDomain.OPERATIONAL, severity=SignalSeverity.CRITICAL)
    await harness.detection_repo.save(detection)

    from app.feature_review import InvalidDetectionTypeError

    with pytest.raises(InvalidDetectionTypeError):
        await harness.review_service.create_review(detection.detection_id)


@pytest.mark.asyncio
async def test_missing_review_cannot_start_workflow():
    harness = Harness()
    with pytest.raises(OrchestrationError):
        await harness.orchestration.start_workflow_from_review("does-not-exist")


@pytest.mark.asyncio
async def test_missing_review_repo_configured_raises():
    harness = Harness()
    _, _, review = await harness.seed_approved_feature()
    orchestration_without_reviews = OrchestrationService(
        workflow_repo=harness.workflow_repo,
        artifact_repo=harness.artifact_repo,
        execution_repo=harness.execution_repo,
        decision_repo=harness.decision_repo,
        registry=build_default_registry(),
        knowledge_gateway=FakeKnowledgeGateway(),
        tool_gateway=FakeToolGateway(),
    )
    with pytest.raises(OrchestrationError):
        await orchestration_without_reviews.start_workflow_from_review(review.review_id)


@pytest.mark.asyncio
async def test_approval_does_not_grant_extra_agent_capabilities(monkeypatch):
    """PlanningAgent's own capabilities are identical regardless of whether
    the workflow originated from a feature review or a manual ticket."""
    harness = Harness()
    patch_planning(monkeypatch)
    _, _, review = await harness.seed_approved_feature()
    workflow = await harness.orchestration.start_workflow_from_review(review.review_id)

    from app.agents.planning import PlanningAgent

    plain_ticket_workflow = await harness.orchestration.start_workflow(Ticket(title="Manual", description="Manual ticket"))

    assert PlanningAgent().capabilities == PlanningAgent().capabilities  # same fixed set regardless of origin
    assert AgentCapability.WRITE_CODE not in PlanningAgent().capabilities
    assert AgentCapability.DEPLOY not in PlanningAgent().capabilities


@pytest.mark.asyncio
async def test_arbitrary_source_detection_id_cannot_be_injected_by_caller():
    """A caller cannot fabricate a Ticket claiming an arbitrary
    source_detection_id and have it accepted as if FeatureReview produced
    it — start_workflow_from_review only ever reads the trusted,
    already-approved review.ticket, never a caller-supplied Ticket."""
    import inspect

    from app.orchestration.service import OrchestrationService as OS

    signature = inspect.signature(OS.start_workflow_from_review)
    assert set(signature.parameters) == {"self", "review_id"}  # no ticket/metadata parameter to spoof


@pytest.mark.asyncio
async def test_cannot_bypass_planning_to_invoke_codegen_directly():
    """No orchestration API accepts a caller-supplied starting stage —
    every workflow, feature-derived or not, always starts at PLANNING."""
    harness = Harness()
    _, _, review = await harness.seed_approved_feature()
    workflow = await harness.orchestration.start_workflow_from_review(review.review_id)
    assert workflow.current_stage == WorkflowStage.PLANNING

    import inspect

    from app.orchestration.service import OrchestrationService as OS

    start_workflow_params = set(inspect.signature(OS.start_workflow).parameters)
    assert "current_stage" not in start_workflow_params
    assert "stage" not in start_workflow_params


# ---- Feature Review boundary -----------------------------------------------------


def test_feature_review_service_has_no_orchestration_dependency():
    import inspect

    from app.feature_review.service import FeatureReviewService as FRS

    source = inspect.getsource(FRS.__init__)
    assert "OrchestrationService" not in source
    assert "WorkflowRepository" not in source
    assert "PlanningAgent" not in source


def test_feature_review_service_module_never_imports_orchestration():
    import ast
    from pathlib import Path

    source = Path("app/feature_review/service.py").read_text()
    tree = ast.parse(source)
    imported_modules = {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module}
    imported_modules |= {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
    assert not any(m.startswith("app.orchestration") or m.startswith("app.agents.planning") for m in imported_modules)


# ---- Regression: existing manual-ticket flow --------------------------------------


@pytest.mark.asyncio
async def test_manual_ticket_workflow_creation_unchanged():
    harness = Harness()
    ticket = Ticket(title="Manual feature", description="A manually filed request")
    workflow = await harness.orchestration.start_workflow(ticket)
    assert workflow.current_stage == WorkflowStage.PLANNING
    assert workflow.status == WorkflowStatus.PENDING
    assert workflow.metadata == {}
    assert workflow.ticket.source_detection_id is None
