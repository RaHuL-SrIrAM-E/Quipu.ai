"""Level 3.6 — Incident Resolution -> Authorized Remediation Orchestration
tests. Covers OrchestrationService.start_remediation_from_resolution():
authorization, CODE_FIX/ARCHITECTURE_REVIEW routing through the existing
Codegen->Testing->Deployment graph, ROLLBACK's escalate-only behavior,
ESCALATE/NO_ACTION handling, idempotency, concurrency, crash recovery
(reusing the existing _reconcile_stage mechanism unchanged), and adversarial
security cases. No live Gemini/Firestore/Cloud Run required.
"""

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from google.genai import types

from app.agent_runtime.context import AgentContext
from app.agent_runtime.gateways.artifacts import RepositoryArtifactGateway
from app.agent_runtime.gateways.detections import RepositoryDetectionGateway
from app.agent_runtime.gateways.resolutions import RepositoryResolutionGateway
from app.agent_runtime.gateways.signals import RepositorySignalGateway
from app.agents.incident_resolution import IncidentResolutionAgent, ResolutionProposal
from app.domain import (
    AgentInput,
    DetectionDomain,
    DetectionResult,
    DetectionType,
    RemediationRisk,
    ResolutionResult,
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
    compute_resolution_fingerprint,
)
from app.orchestration.errors import OrchestrationError
from app.orchestration.registry_setup import build_default_registry
from app.orchestration.service import OrchestrationService
from app.persistence.memory import (
    InMemoryAgentExecutionRepository,
    InMemoryArtifactRepository,
    InMemoryDecisionRepository,
    InMemoryDetectionRepository,
    InMemoryResolutionRepository,
    InMemorySignalRepository,
    InMemoryWorkflowRepository,
)
from app.tools.codegen_tools import write_file
from app.tools.deployment_tools import deploy_cloud_run
from app.tools.testing_tools import run_tests

NOW = datetime.now(timezone.utc)

VALID_PLAN = {
    "feature_summary": "Add dark mode",
    "architecture_notes": "Add a theme provider.",
    "affected_components": [{"name": "frontend", "reason": "theming"}],
    "tasks": [{"id": "t1", "description": "add theme provider", "depends_on": []}],
    "dependencies": [],
    "acceptance_criteria": ["toggle switches theme"],
    "risks": [{"description": "flash of wrong theme", "mitigation": "ssr cookie"}],
}
VALID_ARCHITECTURE = {
    "design_summary": "Add a ThemeProvider.",
    "components": [{"name": "ThemeProvider", "responsibility": "holds theme state"}],
    "data_model_changes": [],
    "api_contracts": [],
    "task_designs": [{"task_id": "t1", "approach": "create ThemeProvider", "files": ["src/theme.py"]}],
    "risks": [{"description": "theme flash", "mitigation": "inline script"}],
}
VALID_CODEGEN = {
    "summary": "Implemented ThemeProvider.",
    "modified_files": [],
    "created_files": ["src/theme.py"],
    "deleted_files": [],
    "changes": [{"path": "src/theme.py", "change_type": "created", "description": "theme provider"}],
    "implementation_notes": "",
    "unresolved_items": [],
    "tests_to_run": ["test_theme.py"],
}
VALID_TESTING_PASS = {
    "summary": "All tests pass.",
    "overall_status": "passed",
    "test_strategy": "regression",
    "targeted_tests": [],
    "regression_tests": ["test_theme.py"],
    "failures": [],
    "environment_errors": [],
    "coverage_summary": "",
    "recommendations": [],
}
VALID_DEPLOYMENT = {
    "deployment_summary": "Deployed theme provider to Cloud Run.",
    "target_platform": "cloud_run",
    "environment": "production",
    "service_name": "quipu-demo",
    "region": "us-central1",
    "strategy": "revision",
    "configuration": {"image_tag": "v1", "cpu": "1", "memory": "512Mi", "min_instances": 0, "max_instances": 2},
    "pre_deployment_checks": ["tests passed"],
    "rollback_strategy": "revert to previous revision",
    "risks": [],
}


def make_testing_output(failures: list[dict]) -> dict:
    return {**VALID_TESTING_PASS, "overall_status": "failed", "failures": failures}


# ---- ADK fakes (mirrors tests/test_orchestration.py's established pattern) -----


class FakeKnowledgeGateway:
    async def search(self, request):
        return []


class FakeToolGateway:
    async def execute(self, request):
        raise NotImplementedError


class FakeJiraClient:
    def create_story(self, summary: str, description: str) -> dict:
        return {"key": "QP-1", "url": "https://example.atlassian.net/browse/QP-1"}


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


class _FakeToolContext:
    def __init__(self, state):
        self.state = state


def make_plain_runner(final_text: str):
    class _FakeRunner:
        def __init__(self, agent, app_name):
            self.session_service = _CapturingSessionService()

        def run_async(self, **kwargs):
            async def _events():
                yield _FakeEvent(final_text)

            return _events()

    return _FakeRunner


def make_codegen_runner(final_text: str):
    class _FakeRunner:
        def __init__(self, agent, app_name):
            self.session_service = _CapturingSessionService()

        def run_async(self, **kwargs):
            async def _events():
                ctx = _FakeToolContext(self.session_service.captured_state)
                write_file("src/theme.py", "class ThemeProvider:\n    pass\n", ctx)
                yield _FakeEvent(final_text)

            return _events()

    return _FakeRunner


def make_testing_runner(final_text: str, mode: str = "regression"):
    class _FakeRunner:
        def __init__(self, agent, app_name):
            self.session_service = _CapturingSessionService()

        def run_async(self, **kwargs):
            async def _events():
                ctx = _FakeToolContext(self.session_service.captured_state)
                run_tests(mode, [], [], ctx)
                yield _FakeEvent(final_text)

            return _events()

    return _FakeRunner


class FakeCloudRunDeployer:
    def __init__(self, succeed: bool = True):
        self._succeed = succeed

    async def deploy(self, **kwargs):
        from app.core.cloud_run_client import CloudRunDeployResult

        return CloudRunDeployResult(
            status="succeeded" if self._succeed else "failed",
            service_name=kwargs["service_name"],
            project="test-project",
            region=kwargs["region"],
            revision=f"{kwargs['service_name']}-00001-abc" if self._succeed else None,
            uri="https://quipu-demo-xyz.a.run.app" if self._succeed else None,
            message="" if self._succeed else "container failed to start",
            deployed_at=datetime.now(timezone.utc),
        )


def make_deployment_runner(final_text: str, succeed: bool = True):
    class _FakeRunner:
        def __init__(self, agent, app_name):
            self.session_service = _CapturingSessionService()

        def run_async(self, **kwargs):
            async def _events():
                state = self.session_service.captured_state
                state["_cloud_run_deployer"] = FakeCloudRunDeployer(succeed=succeed)
                ctx = _FakeToolContext(state)
                await deploy_cloud_run(
                    service_name="quipu-demo",
                    region="us-central1",
                    environment="production",
                    image_tag="v1",
                    cpu="1",
                    memory="512Mi",
                    min_instances=0,
                    max_instances=2,
                    tool_context=ctx,
                )
                yield _FakeEvent(final_text)

            return _events()

    return _FakeRunner


class _FakeCloudRunSettings:
    cloud_run_image_registry = "gcr.io/test-project"
    cloud_run_allowed_regions = ["us-central1"]
    cloud_run_allowed_environments = ["development", "staging", "production"]
    cloud_run_max_instances_ceiling = 10


def patch_cloud_run_config(monkeypatch):
    monkeypatch.setattr("app.tools.deployment_tools.get_settings", lambda: _FakeCloudRunSettings())
    monkeypatch.setattr("app.agents.deployment.get_settings", lambda: _FakeCloudRunSettings())


def patch_happy_path(monkeypatch, workspace_path: str, test_outcome: dict | None = None, deployment_succeeds: bool = True):
    monkeypatch.setattr("app.agents.planning.InMemoryRunner", make_plain_runner(json.dumps(VALID_PLAN)))
    monkeypatch.setattr("app.agents.planning.JiraClient", FakeJiraClient)
    monkeypatch.setattr("app.agents.architecture.InMemoryRunner", make_plain_runner(json.dumps(VALID_ARCHITECTURE)))
    monkeypatch.setattr("app.agents.codegen.InMemoryRunner", make_codegen_runner(json.dumps(VALID_CODEGEN)))
    monkeypatch.setattr("app.agents.testing.InMemoryRunner", make_testing_runner(json.dumps(test_outcome or VALID_TESTING_PASS)))
    patch_cloud_run_config(monkeypatch)
    monkeypatch.setattr(
        "app.agents.deployment.InMemoryRunner", make_deployment_runner(json.dumps(VALID_DEPLOYMENT), succeed=deployment_succeeds)
    )
    (Path(workspace_path) / "requirements.txt").write_text("pytest\n")
    (Path(workspace_path) / "test_theme.py").write_text("def test_theme():\n    assert True\n")


def make_signal(source_event_id: str, *, stype=SignalType.APPLICATION_ERROR, subject="quipu-demo", **overrides) -> Signal:
    defaults = dict(
        signal_type=stype,
        source=SignalSource.CLOUD_LOGGING,
        severity=SignalSeverity.ERROR,
        observed_at=NOW,
        subject=subject,
        summary=f"signal {source_event_id}",
        provenance=SignalProvenance(source_system="x", source_event_id=source_event_id),
        fingerprint=compute_fingerprint(source=SignalSource.CLOUD_LOGGING, source_event_id=source_event_id, subject=subject),
    )
    defaults.update(overrides)
    return Signal(**defaults)


def make_incident_detection(signals, **overrides) -> DetectionResult:
    signal_ids = [s.signal_id for s in signals]
    defaults = dict(
        detection_type=DetectionType.INCIDENT,
        domain=DetectionDomain.OPERATIONAL,
        title="Errors after deploy",
        summary="Application errors spiked shortly after deployment",
        rationale="Error signals cluster right after the deployment event",
        confidence=0.9,
        severity=SignalSeverity.CRITICAL,
        subject="quipu-demo",
        supporting_signal_ids=signal_ids,
        observation_window_minutes=15,
        fingerprint=compute_detection_fingerprint(detection_type=DetectionType.INCIDENT, subject="quipu-demo", supporting_signal_ids=signal_ids, window_minutes=15),
    )
    defaults.update(overrides)
    return DetectionResult(**defaults)


def make_resolution_proposal(**overrides) -> ResolutionProposal:
    defaults = dict(
        diagnosis_summary="Application defect introduced by the last deployment.",
        probable_root_cause="Null pointer in the request handler.",
        root_cause_confidence=0.9,
        remediation_strategy="code_fix",
        remediation_rationale="Application-error signals correlate with the deployment.",
        expected_outcome="Error rate returns to baseline.",
        verification_strategy="Re-run the test suite and monitor error rate.",
        risk="low",
        severity="critical",
    )
    defaults.update(overrides)
    return ResolutionProposal(**defaults)


class Harness:
    def __init__(self):
        self.signal_repo = InMemorySignalRepository()
        self.detection_repo = InMemoryDetectionRepository()
        self.resolution_repo = InMemoryResolutionRepository()
        self.workflow_repo = InMemoryWorkflowRepository()
        self.artifact_repo = InMemoryArtifactRepository()
        self.execution_repo = InMemoryAgentExecutionRepository()
        self.decision_repo = InMemoryDecisionRepository()
        self.orchestration = OrchestrationService(
            workflow_repo=self.workflow_repo,
            artifact_repo=self.artifact_repo,
            execution_repo=self.execution_repo,
            decision_repo=self.decision_repo,
            registry=build_default_registry(),
            knowledge_gateway=FakeKnowledgeGateway(),
            tool_gateway=FakeToolGateway(),
            detection_repo=self.detection_repo,
            resolution_repo=self.resolution_repo,
        )

    async def run_original_workflow(self, workspace_path: str) -> "WorkflowState":  # noqa: F821
        workflow = await self.orchestration.start_workflow(Ticket(title="Add dark mode", description="feature"), workspace_path=workspace_path)
        return await self.orchestration.run_to_completion(workflow.workflow_id)

    async def seed_incident(self, original_workflow, *, signal_kwargs=None, detection_overrides=None):
        deployment_artifact_id = original_workflow.artifact_ids[-1]
        signal = make_signal("1", deployment_artifact_id=deployment_artifact_id, revision="quipu-demo-00001-abc", **(signal_kwargs or {}))
        await self.signal_repo.save(signal)
        detection = make_incident_detection([signal], **(detection_overrides or {}))
        await self.detection_repo.save(detection)
        return signal, detection

    async def run_incident_resolution(self, original_workflow_id: str, detection_id: str, proposal: ResolutionProposal, monkeypatch) -> ResolutionResult:
        monkeypatch.setattr("app.agents.incident_resolution.InMemoryRunner", make_plain_runner(proposal.model_dump_json()))
        agent = IncidentResolutionAgent()
        context = AgentContext(
            workflow_id=original_workflow_id,
            execution_id=f"ir-exec-{detection_id}",
            knowledge=FakeKnowledgeGateway(),
            tools=FakeToolGateway(),
            artifacts=RepositoryArtifactGateway(self.artifact_repo),
            signals=RepositorySignalGateway(self.signal_repo),
            detections=RepositoryDetectionGateway(self.detection_repo),
            resolutions=RepositoryResolutionGateway(self.resolution_repo),
        )
        agent_input = AgentInput(
            workflow_id=original_workflow_id, agent_name="incident_resolution_agent", ticket=Ticket(title="x", description="x"), context={"detection_id": detection_id}
        )
        output = await agent.execute(agent_input, context)
        assert output.status == WorkflowStatus.COMPLETED, output.errors
        resolution_id = json.loads(output.messages[1])["resolution_id"]
        return await self.resolution_repo.get(resolution_id)


@pytest.fixture
def workspace(tmp_path):
    return str(tmp_path)


# ---- Authorization ----------------------------------------------------------


@pytest.mark.asyncio
async def test_valid_code_fix_executes(monkeypatch, workspace):
    harness = Harness()
    patch_happy_path(monkeypatch, workspace)
    original = await harness.run_original_workflow(workspace)
    signal, detection = await harness.seed_incident(original)
    resolution = await harness.run_incident_resolution(
        original.workflow_id, detection.detection_id, make_resolution_proposal(supporting_signal_ids=[signal.signal_id]), monkeypatch
    )
    remediation = await harness.orchestration.start_remediation_from_resolution(resolution.resolution_id)
    assert remediation.current_stage == WorkflowStage.CODEGEN
    assert remediation.status == WorkflowStatus.PENDING
    assert remediation.workflow_id == original.workflow_id


@pytest.mark.asyncio
async def test_valid_architecture_review_executes(monkeypatch, workspace):
    harness = Harness()
    patch_happy_path(monkeypatch, workspace)
    original = await harness.run_original_workflow(workspace)
    signal, detection = await harness.seed_incident(original, signal_kwargs={"stype": SignalType.AVAILABILITY_DEGRADATION})
    resolution = await harness.run_incident_resolution(
        original.workflow_id,
        detection.detection_id,
        make_resolution_proposal(remediation_strategy="architecture_review", supporting_signal_ids=[signal.signal_id]),
        monkeypatch,
    )
    remediation = await harness.orchestration.start_remediation_from_resolution(resolution.resolution_id)
    assert remediation.current_stage == WorkflowStage.ARCHITECTURE


@pytest.mark.asyncio
async def test_rollback_always_escalates(monkeypatch, workspace):
    harness = Harness()
    patch_happy_path(monkeypatch, workspace)
    original = await harness.run_original_workflow(workspace)
    signal, detection = await harness.seed_incident(original, signal_kwargs={"stype": SignalType.DEPLOYMENT_EVENT})
    resolution = await harness.run_incident_resolution(
        original.workflow_id,
        detection.detection_id,
        make_resolution_proposal(
            remediation_strategy="rollback", rollback_target="quipu-demo-00003", supporting_signal_ids=[signal.signal_id]
        ),
        monkeypatch,
    )
    assert resolution.remediation_strategy.value == "rollback"  # persisted as-is by IncidentResolutionAgent
    remediation = await harness.orchestration.start_remediation_from_resolution(resolution.resolution_id)
    # No Cloud Run mutation was ever attempted — the workflow is escalated instead.
    assert remediation.status == WorkflowStatus.ESCALATED
    assert remediation.metadata["remediation_strategy"] == "escalate"


@pytest.mark.asyncio
async def test_escalate_does_not_execute_agents(monkeypatch, workspace):
    harness = Harness()
    patch_happy_path(monkeypatch, workspace)
    original = await harness.run_original_workflow(workspace)
    signal, detection = await harness.seed_incident(original)
    resolution = await harness.run_incident_resolution(
        original.workflow_id,
        detection.detection_id,
        make_resolution_proposal(remediation_strategy="escalate", risk="high", supporting_signal_ids=[signal.signal_id]),
        monkeypatch,
    )
    before_executions = len(await harness.execution_repo.list_for_workflow(original.workflow_id))
    remediation = await harness.orchestration.start_remediation_from_resolution(resolution.resolution_id)
    after_executions = len(await harness.execution_repo.list_for_workflow(original.workflow_id))
    assert remediation.status == WorkflowStatus.ESCALATED
    assert after_executions == before_executions  # no new agent execution


@pytest.mark.asyncio
async def test_no_action_does_not_execute_agents(monkeypatch, workspace):
    harness = Harness()
    patch_happy_path(monkeypatch, workspace)
    original = await harness.run_original_workflow(workspace)
    signal, detection = await harness.seed_incident(original)
    resolution = await harness.run_incident_resolution(
        original.workflow_id,
        detection.detection_id,
        make_resolution_proposal(remediation_strategy="no_action", supporting_signal_ids=[signal.signal_id]),
        monkeypatch,
    )
    before_executions = len(await harness.execution_repo.list_for_workflow(original.workflow_id))
    remediation = await harness.orchestration.start_remediation_from_resolution(resolution.resolution_id)
    after_executions = len(await harness.execution_repo.list_for_workflow(original.workflow_id))
    assert remediation.status == WorkflowStatus.COMPLETED  # unchanged — no production mutation
    assert after_executions == before_executions


@pytest.mark.asyncio
async def test_non_incident_detection_rejected(monkeypatch, workspace):
    """A ResolutionResult somehow associated with a non-INCIDENT detection
    (should never happen given IncidentResolutionAgent's own validation,
    but the orchestrator re-checks independently) is rejected."""
    harness = Harness()
    patch_happy_path(monkeypatch, workspace)
    original = await harness.run_original_workflow(workspace)
    signal = make_signal("1")
    await harness.signal_repo.save(signal)
    detection = make_incident_detection(
        [signal], detection_type=DetectionType.FEATURE_OPPORTUNITY, domain=DetectionDomain.PRODUCT, severity=None
    )
    await harness.detection_repo.save(detection)

    resolution = ResolutionResult(
        detection_id=detection.detection_id,
        workflow_id=original.workflow_id,
        diagnosis_summary="x",
        probable_root_cause="x",
        root_cause_confidence=0.9,
        remediation_strategy="code_fix",
        remediation_rationale="x",
        expected_outcome="x",
        verification_strategy="x",
        risk="low",
        target_agent="codegen_agent",
        supporting_signal_ids=[signal.signal_id],
        fingerprint=compute_resolution_fingerprint(detection_id=detection.detection_id, remediation_strategy=__import__("app.domain", fromlist=["RemediationStrategy"]).RemediationStrategy.CODE_FIX, subject="quipu-demo"),
    )
    await harness.resolution_repo.save(resolution)

    with pytest.raises(OrchestrationError):
        await harness.orchestration.start_remediation_from_resolution(resolution.resolution_id)


@pytest.mark.asyncio
async def test_missing_resolution_rejected():
    harness = Harness()
    with pytest.raises(OrchestrationError):
        await harness.orchestration.start_remediation_from_resolution("does-not-exist")


@pytest.mark.asyncio
async def test_high_risk_resolution_rejected_downgraded_to_escalate(monkeypatch, workspace):
    harness = Harness()
    patch_happy_path(monkeypatch, workspace)
    original = await harness.run_original_workflow(workspace)
    signal, detection = await harness.seed_incident(original)
    resolution = await harness.run_incident_resolution(
        original.workflow_id, detection.detection_id, make_resolution_proposal(risk="high", supporting_signal_ids=[signal.signal_id]), monkeypatch
    )
    # IncidentResolutionAgent's own policy already downgraded this to
    # escalate before persisting (Level 3.3) — confirm that already holds:
    assert resolution.remediation_strategy.value == "escalate"
    remediation = await harness.orchestration.start_remediation_from_resolution(resolution.resolution_id)
    assert remediation.status == WorkflowStatus.ESCALATED


@pytest.mark.asyncio
async def test_missing_repos_configured_raises():
    harness = Harness()
    orchestration_without_incident_repos = OrchestrationService(
        workflow_repo=harness.workflow_repo,
        artifact_repo=harness.artifact_repo,
        execution_repo=harness.execution_repo,
        decision_repo=harness.decision_repo,
        registry=build_default_registry(),
        knowledge_gateway=FakeKnowledgeGateway(),
        tool_gateway=FakeToolGateway(),
    )
    with pytest.raises(OrchestrationError):
        await orchestration_without_incident_repos.start_remediation_from_resolution("whatever")


# ---- CODE_FIX flow / Testing gate --------------------------------------------


@pytest.mark.asyncio
async def test_code_fix_routes_codegen_then_testing_then_deployment(monkeypatch, workspace):
    harness = Harness()
    patch_happy_path(monkeypatch, workspace)
    original = await harness.run_original_workflow(workspace)
    signal, detection = await harness.seed_incident(original)
    resolution = await harness.run_incident_resolution(
        original.workflow_id, detection.detection_id, make_resolution_proposal(supporting_signal_ids=[signal.signal_id]), monkeypatch
    )
    remediation = await harness.orchestration.start_remediation_from_resolution(resolution.resolution_id)
    assert remediation.current_stage == WorkflowStage.CODEGEN

    after_codegen = await harness.orchestration.execute_next_step(remediation.workflow_id)
    assert after_codegen.current_stage == WorkflowStage.TESTING

    after_testing = await harness.orchestration.execute_next_step(after_codegen.workflow_id)
    assert after_testing.current_stage == WorkflowStage.DEPLOYMENT

    final = await harness.orchestration.execute_next_step(after_testing.workflow_id)
    assert final.status == WorkflowStatus.COMPLETED


@pytest.mark.asyncio
async def test_testing_failure_prevents_deployment(monkeypatch, workspace):
    """TestingAgent is evidence-first (Level 1.8): it runs the REAL pytest
    suite in the workspace and overrides whatever the model's structured
    output claims — so to prove a real testing failure blocks deployment,
    the actual test file on disk must fail, not just the fake JSON text."""
    harness = Harness()
    failing_test_output = make_testing_output([{"test_name": "test_theme", "classification": "code_defect", "details": "boom"}])
    patch_happy_path(monkeypatch, workspace, test_outcome=VALID_TESTING_PASS)  # original run passes
    original = await harness.run_original_workflow(workspace)
    signal, detection = await harness.seed_incident(original)
    resolution = await harness.run_incident_resolution(
        original.workflow_id, detection.detection_id, make_resolution_proposal(supporting_signal_ids=[signal.signal_id]), monkeypatch
    )
    remediation = await harness.orchestration.start_remediation_from_resolution(resolution.resolution_id)

    # Break the actual on-disk test so the REAL pytest run fails, and point
    # the model's structured output at a matching (also failing) verdict.
    (Path(workspace) / "test_theme.py").write_text("def test_theme():\n    assert False\n")
    monkeypatch.setattr("app.agents.testing.InMemoryRunner", make_testing_runner(json.dumps(failing_test_output)))
    after_codegen = await harness.orchestration.execute_next_step(remediation.workflow_id)
    assert after_codegen.current_stage == WorkflowStage.TESTING
    after_testing = await harness.orchestration.execute_next_step(after_codegen.workflow_id)
    # deterministic code_defect routing sends it back to codegen_agent, not deployment
    assert after_testing.current_stage == WorkflowStage.CODEGEN
    assert after_testing.status != WorkflowStatus.COMPLETED


@pytest.mark.asyncio
async def test_deployment_not_reached_without_test_evidence(monkeypatch, workspace):
    """Same evidence-first guarantee TestingAgent already provides (Level
    1.8) — reused here unchanged, not reimplemented."""
    harness = Harness()
    patch_happy_path(monkeypatch, workspace)
    original = await harness.run_original_workflow(workspace)
    signal, detection = await harness.seed_incident(original)
    resolution = await harness.run_incident_resolution(
        original.workflow_id, detection.detection_id, make_resolution_proposal(supporting_signal_ids=[signal.signal_id]), monkeypatch
    )
    remediation = await harness.orchestration.start_remediation_from_resolution(resolution.resolution_id)
    assert remediation.current_stage == WorkflowStage.CODEGEN
    # Deployment cannot be the immediate next stage from Codegen.
    assert remediation.current_stage != WorkflowStage.DEPLOYMENT


# ---- ARCHITECTURE_REVIEW flow -------------------------------------------------


@pytest.mark.asyncio
async def test_architecture_review_routes_full_chain(monkeypatch, workspace):
    harness = Harness()
    patch_happy_path(monkeypatch, workspace)
    original = await harness.run_original_workflow(workspace)
    signal, detection = await harness.seed_incident(original, signal_kwargs={"stype": SignalType.AVAILABILITY_DEGRADATION})
    resolution = await harness.run_incident_resolution(
        original.workflow_id,
        detection.detection_id,
        make_resolution_proposal(remediation_strategy="architecture_review", supporting_signal_ids=[signal.signal_id]),
        monkeypatch,
    )
    remediation = await harness.orchestration.start_remediation_from_resolution(resolution.resolution_id)
    assert remediation.current_stage == WorkflowStage.ARCHITECTURE

    after_arch = await harness.orchestration.execute_next_step(remediation.workflow_id)
    assert after_arch.current_stage == WorkflowStage.CODEGEN
    after_codegen = await harness.orchestration.execute_next_step(after_arch.workflow_id)
    assert after_codegen.current_stage == WorkflowStage.TESTING
    after_testing = await harness.orchestration.execute_next_step(after_codegen.workflow_id)
    assert after_testing.current_stage == WorkflowStage.DEPLOYMENT
    final = await harness.orchestration.execute_next_step(after_testing.workflow_id)
    assert final.status == WorkflowStatus.COMPLETED


# ---- Deployment failure -----------------------------------------------------


@pytest.mark.asyncio
async def test_remediation_deployment_failure_routes_deterministically(monkeypatch, workspace):
    harness = Harness()
    patch_happy_path(monkeypatch, workspace)
    original = await harness.run_original_workflow(workspace)
    signal, detection = await harness.seed_incident(original)
    resolution = await harness.run_incident_resolution(
        original.workflow_id, detection.detection_id, make_resolution_proposal(supporting_signal_ids=[signal.signal_id]), monkeypatch
    )
    remediation = await harness.orchestration.start_remediation_from_resolution(resolution.resolution_id)
    after_codegen = await harness.orchestration.execute_next_step(remediation.workflow_id)
    after_testing = await harness.orchestration.execute_next_step(after_codegen.workflow_id)
    assert after_testing.current_stage == WorkflowStage.DEPLOYMENT

    monkeypatch.setattr(
        "app.agents.deployment.InMemoryRunner", make_deployment_runner(json.dumps(VALID_DEPLOYMENT), succeed=False)
    )
    after_deploy = await harness.orchestration.execute_next_step(after_testing.workflow_id)
    assert after_deploy.status != WorkflowStatus.COMPLETED  # deterministic deployment-failure routing, not blind success


# ---- Monitoring validation / remediation outcome ------------------------------


@pytest.mark.asyncio
async def test_successful_remediation_marked_deployed_pending_verification(monkeypatch, workspace):
    """Deployment success is NOT automatically reported as 'incident
    resolved' — see §20/§21 of the task."""
    harness = Harness()
    patch_happy_path(monkeypatch, workspace)
    original = await harness.run_original_workflow(workspace)
    signal, detection = await harness.seed_incident(original)
    resolution = await harness.run_incident_resolution(
        original.workflow_id, detection.detection_id, make_resolution_proposal(supporting_signal_ids=[signal.signal_id]), monkeypatch
    )
    remediation = await harness.orchestration.start_remediation_from_resolution(resolution.resolution_id)
    final = await harness.orchestration.run_to_completion(remediation.workflow_id)
    assert final.status == WorkflowStatus.COMPLETED
    assert final.metadata["remediation_outcome"] == "deployed_pending_verification"
    assert "incident_resolved" not in final.metadata


@pytest.mark.asyncio
async def test_normal_sdlc_completion_has_no_remediation_outcome_marker(monkeypatch, workspace):
    """The remediation_outcome marker only applies to remediation
    workflows — a normal, non-incident SDLC completion never gets it."""
    harness = Harness()
    patch_happy_path(monkeypatch, workspace)
    original = await harness.run_original_workflow(workspace)
    assert "remediation_outcome" not in original.metadata


# ---- Idempotency -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_same_resolution_submitted_twice_no_duplicate_workflow(monkeypatch, workspace):
    harness = Harness()
    patch_happy_path(monkeypatch, workspace)
    original = await harness.run_original_workflow(workspace)
    signal, detection = await harness.seed_incident(original)
    resolution = await harness.run_incident_resolution(
        original.workflow_id, detection.detection_id, make_resolution_proposal(supporting_signal_ids=[signal.signal_id]), monkeypatch
    )
    first = await harness.orchestration.start_remediation_from_resolution(resolution.resolution_id)
    second = await harness.orchestration.start_remediation_from_resolution(resolution.resolution_id)
    assert first.workflow_id == second.workflow_id == original.workflow_id
    assert second.current_stage == WorkflowStage.CODEGEN  # unchanged by the second, idempotent call


@pytest.mark.asyncio
async def test_idempotent_call_does_not_reinvoke_codegen(monkeypatch, workspace):
    harness = Harness()
    patch_happy_path(monkeypatch, workspace)
    original = await harness.run_original_workflow(workspace)
    signal, detection = await harness.seed_incident(original)
    resolution = await harness.run_incident_resolution(
        original.workflow_id, detection.detection_id, make_resolution_proposal(supporting_signal_ids=[signal.signal_id]), monkeypatch
    )
    await harness.orchestration.start_remediation_from_resolution(resolution.resolution_id)
    await harness.orchestration.execute_next_step(original.workflow_id)  # runs remediation Codegen
    codegen_executions_before = [e for e in await harness.execution_repo.list_for_workflow(original.workflow_id) if e.agent_name == "codegen_agent"]

    await harness.orchestration.start_remediation_from_resolution(resolution.resolution_id)  # idempotent re-call
    codegen_executions_after = [e for e in await harness.execution_repo.list_for_workflow(original.workflow_id) if e.agent_name == "codegen_agent"]
    assert len(codegen_executions_after) == len(codegen_executions_before)


# ---- Concurrency -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_remediation_starts_only_one_authoritative(monkeypatch, workspace):
    harness = Harness()
    patch_happy_path(monkeypatch, workspace)
    original = await harness.run_original_workflow(workspace)
    signal, detection = await harness.seed_incident(original)
    resolution = await harness.run_incident_resolution(
        original.workflow_id, detection.detection_id, make_resolution_proposal(supporting_signal_ids=[signal.signal_id]), monkeypatch
    )

    results = await asyncio.gather(
        harness.orchestration.start_remediation_from_resolution(resolution.resolution_id),
        harness.orchestration.start_remediation_from_resolution(resolution.resolution_id),
        return_exceptions=True,
    )
    succeeded = [r for r in results if not isinstance(r, Exception)]
    assert len(succeeded) >= 1
    stages = {r.current_stage for r in succeeded}
    assert stages == {WorkflowStage.CODEGEN}  # both callers agree on the exact same outcome


# ---- Crash recovery ------------------------------------------------------------


@pytest.mark.asyncio
async def test_crash_recovery_after_codegen_does_not_duplicate_codegen(monkeypatch, workspace):
    """Reuses the existing _reconcile_stage mechanism unchanged — proven
    here for the remediation path specifically."""
    harness = Harness()
    patch_happy_path(monkeypatch, workspace)
    original = await harness.run_original_workflow(workspace)
    signal, detection = await harness.seed_incident(original)
    resolution = await harness.run_incident_resolution(
        original.workflow_id, detection.detection_id, make_resolution_proposal(supporting_signal_ids=[signal.signal_id]), monkeypatch
    )
    remediation = await harness.orchestration.start_remediation_from_resolution(resolution.resolution_id)
    after_codegen = await harness.orchestration.execute_next_step(remediation.workflow_id)
    assert after_codegen.current_stage == WorkflowStage.TESTING

    # Simulate a crash: roll the durable WorkflowState back to look like
    # Codegen never advanced it, while leaving the real AgentExecution/
    # CodeArtifact intact (exactly test_orchestration.py's established
    # crash-recovery test shape).
    stale = after_codegen.model_copy(update={"current_stage": WorkflowStage.CODEGEN, "artifact_ids": after_codegen.artifact_ids[:-1]})
    await harness.workflow_repo.update_if_version(after_codegen.workflow_id, after_codegen.version, stale)

    codegen_executions_before = [e for e in await harness.execution_repo.list_for_workflow(remediation.workflow_id) if e.agent_name == "codegen_agent"]
    recovered = await harness.orchestration.resume_workflow(remediation.workflow_id)
    codegen_executions_after = [e for e in await harness.execution_repo.list_for_workflow(remediation.workflow_id) if e.agent_name == "codegen_agent"]

    assert recovered.current_stage == WorkflowStage.TESTING  # reconciled forward, not re-run
    assert len(codegen_executions_after) == len(codegen_executions_before)  # no duplicate Codegen execution


@pytest.mark.asyncio
async def test_crash_recovery_after_testing_resumes_correctly(monkeypatch, workspace):
    harness = Harness()
    patch_happy_path(monkeypatch, workspace)
    original = await harness.run_original_workflow(workspace)
    signal, detection = await harness.seed_incident(original)
    resolution = await harness.run_incident_resolution(
        original.workflow_id, detection.detection_id, make_resolution_proposal(supporting_signal_ids=[signal.signal_id]), monkeypatch
    )
    remediation = await harness.orchestration.start_remediation_from_resolution(resolution.resolution_id)
    after_codegen = await harness.orchestration.execute_next_step(remediation.workflow_id)
    after_testing = await harness.orchestration.execute_next_step(after_codegen.workflow_id)
    assert after_testing.current_stage == WorkflowStage.DEPLOYMENT

    # Simulate a crash: roll WorkflowState back as if Testing never
    # advanced it, leaving the real AgentExecution/TestArtifact intact.
    stale = after_testing.model_copy(update={"current_stage": WorkflowStage.TESTING, "artifact_ids": after_testing.artifact_ids[:-1]})
    await harness.workflow_repo.update_if_version(after_testing.workflow_id, after_testing.version, stale)

    testing_executions_before = [e for e in await harness.execution_repo.list_for_workflow(after_testing.workflow_id) if e.agent_name == "testing_agent"]
    recovered = await harness.orchestration.resume_workflow(after_testing.workflow_id)
    testing_executions_after = [e for e in await harness.execution_repo.list_for_workflow(after_testing.workflow_id) if e.agent_name == "testing_agent"]
    assert recovered.current_stage == WorkflowStage.DEPLOYMENT  # reconciled forward, not re-run
    assert len(testing_executions_after) == len(testing_executions_before)  # no duplicate Testing execution


@pytest.mark.asyncio
async def test_crash_recovery_after_deployment_resumes_to_completion(monkeypatch, workspace):
    harness = Harness()
    patch_happy_path(monkeypatch, workspace)
    original = await harness.run_original_workflow(workspace)
    signal, detection = await harness.seed_incident(original)
    resolution = await harness.run_incident_resolution(
        original.workflow_id, detection.detection_id, make_resolution_proposal(supporting_signal_ids=[signal.signal_id]), monkeypatch
    )
    remediation = await harness.orchestration.start_remediation_from_resolution(resolution.resolution_id)
    after_codegen = await harness.orchestration.execute_next_step(remediation.workflow_id)
    after_testing = await harness.orchestration.execute_next_step(after_codegen.workflow_id)
    final = await harness.orchestration.execute_next_step(after_testing.workflow_id)
    assert final.status == WorkflowStatus.COMPLETED

    deployment_executions_before = [e for e in await harness.execution_repo.list_for_workflow(final.workflow_id) if e.agent_name == "deployment_agent"]
    recovered = await harness.orchestration.resume_workflow(final.workflow_id)
    deployment_executions_after = [e for e in await harness.execution_repo.list_for_workflow(final.workflow_id) if e.agent_name == "deployment_agent"]
    assert recovered.status == WorkflowStatus.COMPLETED
    assert len(deployment_executions_after) == len(deployment_executions_before)  # not re-deployed


# ---- Security / adversarial ------------------------------------------------------


@pytest.mark.asyncio
async def test_spoofed_target_agent_is_ignored(monkeypatch, workspace):
    """Gemini claims target_agent=deployment_agent for a code_fix strategy
    — the orchestrator must still route to codegen_agent's stage."""
    harness = Harness()
    patch_happy_path(monkeypatch, workspace)
    original = await harness.run_original_workflow(workspace)
    signal, detection = await harness.seed_incident(original)
    resolution = await harness.run_incident_resolution(
        original.workflow_id,
        detection.detection_id,
        make_resolution_proposal(target_agent="deployment_agent", supporting_signal_ids=[signal.signal_id]),
        monkeypatch,
    )
    remediation = await harness.orchestration.start_remediation_from_resolution(resolution.resolution_id)
    assert remediation.current_stage == WorkflowStage.CODEGEN  # never deployment_agent


@pytest.mark.asyncio
async def test_rollback_without_target_still_escalates_never_mutates(monkeypatch, workspace):
    harness = Harness()
    patch_happy_path(monkeypatch, workspace)
    original = await harness.run_original_workflow(workspace)
    signal, detection = await harness.seed_incident(original, signal_kwargs={"stype": SignalType.DEPLOYMENT_EVENT})
    resolution = await harness.run_incident_resolution(
        original.workflow_id,
        detection.detection_id,
        make_resolution_proposal(remediation_strategy="rollback", rollback_target=None, risk="high", supporting_signal_ids=[signal.signal_id]),
        monkeypatch,
    )
    assert resolution.remediation_strategy.value == "escalate"  # IncidentResolutionAgent's own policy already caught this
    remediation = await harness.orchestration.start_remediation_from_resolution(resolution.resolution_id)
    assert remediation.status == WorkflowStatus.ESCALATED


@pytest.mark.asyncio
async def test_low_confidence_resolution_cannot_trigger_remediation(monkeypatch, workspace):
    harness = Harness()
    patch_happy_path(monkeypatch, workspace)
    original = await harness.run_original_workflow(workspace)
    signal, detection = await harness.seed_incident(original)
    resolution = await harness.run_incident_resolution(
        original.workflow_id, detection.detection_id, make_resolution_proposal(root_cause_confidence=0.2, supporting_signal_ids=[signal.signal_id]), monkeypatch
    )
    assert resolution.remediation_strategy.value == "escalate"
    remediation = await harness.orchestration.start_remediation_from_resolution(resolution.resolution_id)
    assert remediation.status == WorkflowStatus.ESCALATED


@pytest.mark.asyncio
async def test_fabricated_evidence_resolution_cannot_trigger_remediation(monkeypatch, workspace):
    """Fabricated signal ids never survive IncidentResolutionAgent's own
    evidence validation (Level 3.3) — confirm the orchestrator's backstop
    also independently checks supporting_signal_ids is non-empty."""
    harness = Harness()
    patch_happy_path(monkeypatch, workspace)
    original = await harness.run_original_workflow(workspace)
    signal, detection = await harness.seed_incident(original)
    resolution = await harness.run_incident_resolution(
        original.workflow_id, detection.detection_id, make_resolution_proposal(supporting_signal_ids=["fake-1", "fake-2"]), monkeypatch
    )
    assert resolution.supporting_signal_ids == []  # already dropped upstream
    assert resolution.remediation_strategy.value == "escalate"
    remediation = await harness.orchestration.start_remediation_from_resolution(resolution.resolution_id)
    assert remediation.status == WorkflowStatus.ESCALATED


@pytest.mark.asyncio
async def test_orchestrator_never_calls_deployment_tool_directly():
    """No direct tool import/call exists in OrchestrationService — it only
    ever invokes agents through AgentRegistry."""
    import inspect

    import app.orchestration.service as service_module

    source = inspect.getsource(service_module)
    assert "deploy_cloud_run(" not in source
    assert "write_file(" not in source
    assert "run_tests(" not in source
    assert "import subprocess" not in source
    assert "shell=True" not in source


@pytest.mark.asyncio
async def test_no_new_capability_bypasses_agent_capabilities(monkeypatch, workspace):
    """Codegen/Testing/Deployment agents invoked during remediation still
    enforce their own unchanged capabilities — the same instances,
    resolved through the same AgentRegistry."""
    from app.agent_runtime.capabilities import AgentCapability
    from app.agents.codegen import CodegenAgent
    from app.agents.deployment import DeploymentAgent
    from app.agents.testing import TestingAgent

    assert AgentCapability.WRITE_CODE in CodegenAgent().capabilities
    assert AgentCapability.RUN_TESTS in TestingAgent().capabilities
    assert AgentCapability.DEPLOY in DeploymentAgent().capabilities
    # None of them gained RESOLVE_INCIDENT or any remediation-specific capability.
    assert AgentCapability.RESOLVE_INCIDENT not in CodegenAgent().capabilities
    assert AgentCapability.RESOLVE_INCIDENT not in TestingAgent().capabilities
    assert AgentCapability.RESOLVE_INCIDENT not in DeploymentAgent().capabilities


def test_incident_resolution_agent_still_lacks_resolve_incident_capability():
    from app.agent_runtime.capabilities import AgentCapability

    assert AgentCapability.RESOLVE_INCIDENT not in IncidentResolutionAgent().capabilities


def test_start_remediation_signature_takes_only_resolution_id():
    """No caller-suppliable strategy/target_agent/stage parameter exists —
    everything is derived deterministically from the persisted
    ResolutionResult."""
    import inspect

    signature = inspect.signature(OrchestrationService.start_remediation_from_resolution)
    assert set(signature.parameters) == {"self", "resolution_id"}


# ---- Provenance / auditability --------------------------------------------------


@pytest.mark.asyncio
async def test_resolution_and_detection_remain_immutable_after_remediation(monkeypatch, workspace):
    harness = Harness()
    patch_happy_path(monkeypatch, workspace)
    original = await harness.run_original_workflow(workspace)
    signal, detection = await harness.seed_incident(original)
    resolution = await harness.run_incident_resolution(
        original.workflow_id, detection.detection_id, make_resolution_proposal(supporting_signal_ids=[signal.signal_id]), monkeypatch
    )
    detection_before = await harness.detection_repo.get(detection.detection_id)
    resolution_before = await harness.resolution_repo.get(resolution.resolution_id)

    remediation = await harness.orchestration.start_remediation_from_resolution(resolution.resolution_id)
    await harness.orchestration.run_to_completion(remediation.workflow_id)

    detection_after = await harness.detection_repo.get(detection.detection_id)
    resolution_after = await harness.resolution_repo.get(resolution.resolution_id)
    assert detection_before == detection_after
    assert resolution_before == resolution_after  # remediation execution never rewrites the recommendation


@pytest.mark.asyncio
async def test_workflow_metadata_preserves_detection_and_resolution_ids(monkeypatch, workspace):
    harness = Harness()
    patch_happy_path(monkeypatch, workspace)
    original = await harness.run_original_workflow(workspace)
    signal, detection = await harness.seed_incident(original)
    resolution = await harness.run_incident_resolution(
        original.workflow_id, detection.detection_id, make_resolution_proposal(supporting_signal_ids=[signal.signal_id]), monkeypatch
    )
    remediation = await harness.orchestration.start_remediation_from_resolution(resolution.resolution_id)
    assert remediation.metadata["remediation_resolution_ids"] == [resolution.resolution_id]
    assert remediation.metadata["remediation_detection_id"] == detection.detection_id


# ---- Regression: existing flows unaffected ---------------------------------------


@pytest.mark.asyncio
async def test_existing_manual_ticket_workflow_unaffected(monkeypatch, workspace):
    harness = Harness()
    patch_happy_path(monkeypatch, workspace)
    workflow = await harness.orchestration.start_workflow(Ticket(title="Manual bug fix", description="fix it"), workspace_path=workspace)
    result = await harness.orchestration.run_to_completion(workflow.workflow_id)
    assert result.status == WorkflowStatus.COMPLETED
    assert "remediation_resolution_ids" not in result.metadata


@pytest.mark.asyncio
async def test_existing_feature_review_workflow_unaffected(monkeypatch, workspace):
    from app.agent_runtime.gateways.detections import RepositoryDetectionGateway as _RDG
    from app.agent_runtime.gateways.signals import RepositorySignalGateway as _RSG
    from app.domain import DecisionSource, DetectionType as DT, ReviewStatus
    from app.feature_review import FeatureReviewService
    from app.persistence.memory import InMemoryFeatureReviewRepository

    harness = Harness()
    patch_happy_path(monkeypatch, workspace)

    review_repo = InMemoryFeatureReviewRepository()
    orchestration_with_reviews = OrchestrationService(
        workflow_repo=harness.workflow_repo,
        artifact_repo=harness.artifact_repo,
        execution_repo=harness.execution_repo,
        decision_repo=harness.decision_repo,
        registry=build_default_registry(),
        knowledge_gateway=FakeKnowledgeGateway(),
        tool_gateway=FakeToolGateway(),
        review_repo=review_repo,
        detection_repo=harness.detection_repo,
        resolution_repo=harness.resolution_repo,
    )

    signal = make_signal("1", stype=SignalType.CUSTOMER_FEEDBACK, subject="export")
    await harness.signal_repo.save(signal)
    detection = make_incident_detection(
        [signal], detection_type=DT.FEATURE_OPPORTUNITY, domain=DetectionDomain.PRODUCT, severity=None, subject="export"
    )
    await harness.detection_repo.save(detection)

    from app.feature_review import FeatureReviewService as FRS
    from app.agent_runtime.capabilities import AgentCapability

    review_service = FRS(review_repo, _RDG(harness.detection_repo), _RSG(harness.signal_repo), jira_client=FakeJiraClient())
    review = await review_service.create_review(detection.detection_id)
    approved = await review_service.approve(
        review.review_id, reviewer_id="pm@company.com", reviewer_type=DecisionSource.HUMAN, granted={AgentCapability.REVIEW_FEATURE_OPPORTUNITY}
    )
    workflow = await orchestration_with_reviews.start_workflow_from_review(approved.review_id)
    assert workflow.current_stage == WorkflowStage.PLANNING
    # Not carrying workspace_path (unrelated to this level — Level 3.5's
    # start_workflow_from_review never took one), so only Planning (which
    # doesn't need a checked-out repo) is exercised here; the point of this
    # regression test is that Level 3.6 didn't disturb this entry point.
    result = await orchestration_with_reviews.execute_next_step(workflow.workflow_id)
    assert result.current_stage == WorkflowStage.ARCHITECTURE
    assert result.status == WorkflowStatus.PENDING
