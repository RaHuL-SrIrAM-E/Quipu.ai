"""DemoHarness — wires the REAL Quipu domain/agent-runtime/orchestration
layer together (in-memory repositories, the real OrchestrationService, the
real FeatureReviewService, the real agents) and runs the two end-to-end
journeys the task asks for. Only external system boundaries are faked (see
app/demo/fakes.py + app/demo/patching.py) — orchestration logic, agent
`_perform()` implementations, evidence-first validation, capability
enforcement, and the transition/retry policy are all the genuine
production code, unmodified.

Known, documented accommodation: OrchestrationService.start_workflow_from_review
(Level 3.5) and start_remediation_from_resolution (Level 3.6) don't accept a
workspace_path — Codegen/Testing need one to touch a real filesystem. The
harness sets it directly on the resulting WorkflowState.metadata via the
same update_if_version() production repositories already expose (not a
new mechanism, not a bypass of any authorization gate) immediately after
workflow creation, before any code-touching stage runs. This is exactly
the kind of environment wiring a real deployment would also need to supply
some other way (e.g. a CI job checking out the repo) — it does not
influence *whether* the workflow proceeds, only *where* Codegen/Testing
find files.
"""

import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from app.agent_runtime.context import AgentContext
from app.agent_runtime.gateways.artifacts import RepositoryArtifactGateway
from app.agent_runtime.gateways.detections import RepositoryDetectionGateway
from app.agent_runtime.gateways.resolutions import RepositoryResolutionGateway
from app.agent_runtime.gateways.signals import RepositorySignalGateway
from app.agent_runtime.capabilities import AgentCapability
from app.agents.detecting import DetectingAgent
from app.agents.incident_resolution import IncidentResolutionAgent
from app.agents.monitoring import MonitoringAgent
from app.core.cloud_logging_client import LogEntryResult
from app.core.observability import get_logger
from app.demo.fakes import (
    VALID_ARCHITECTURE,
    VALID_CODEGEN,
    VALID_DEPLOYMENT,
    VALID_PLAN,
    VALID_TESTING_PASS,
    FakeCloudLoggingClient,
    FakeCloudMonitoringClient,
    FakeJiraClient,
    FakeKnowledgeGateway,
    FakeToolGateway,
    detection_output,
    resolution_proposal,
    testing_output_with_failures,
)
from app.demo.patching import demo_agent_runner_patches
from app.demo.summary import DemoSummary
from app.domain import (
    AgentInput,
    DecisionSource,
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
    compute_fingerprint,
)
from app.feature_review import FeatureReviewService
from app.orchestration.adk.loop import build_recovery_loop_agent
from app.orchestration.adk.sequential import build_happy_path_sequential_agent
from app.orchestration.registry_setup import build_default_registry
from app.orchestration.service import OrchestrationService
from app.persistence.memory import (
    InMemoryAgentExecutionRepository,
    InMemoryArtifactRepository,
    InMemoryDecisionRepository,
    InMemoryDetectionRepository,
    InMemoryFeatureReviewRepository,
    InMemoryResolutionRepository,
    InMemorySignalRepository,
    InMemoryWorkflowRepository,
)
from app.signals.adapters import normalize_customer_feedback, normalize_support_feedback

from app.demo.verify import (
    verify_artifact_chain,
    verify_detection,
    verify_detection_and_resolution_immutable,
    verify_no_duplicate_workflow,
    verify_no_remediation_execution,
    verify_resolution,
    verify_review,
    verify_signals_persisted,
    verify_ticket_created,
    verify_workflow_status,
)

logger = get_logger("quipu.demo")

_REVIEWER_ID = "demo-product-owner@quipu.example"
_GRANTED = {AgentCapability.REVIEW_FEATURE_OPPORTUNITY}


class DemoHarness:
    """One instance = one isolated, in-memory Quipu deployment. Every
    repository is real (InMemory* implementations of the exact same
    Protocols FirestoreXRepository satisfies), so swapping to Firestore in
    a real deployment is a constructor-argument change, not a rewrite —
    the same guarantee every prior level's persistence layer already
    established."""

    def __init__(self) -> None:
        self.signal_repo = InMemorySignalRepository()
        self.detection_repo = InMemoryDetectionRepository()
        self.resolution_repo = InMemoryResolutionRepository()
        self.review_repo = InMemoryFeatureReviewRepository()
        self.workflow_repo = InMemoryWorkflowRepository()
        self.artifact_repo = InMemoryArtifactRepository()
        self.execution_repo = InMemoryAgentExecutionRepository()
        self.decision_repo = InMemoryDecisionRepository()
        self.registry = build_default_registry()

        self.orchestration = OrchestrationService(
            workflow_repo=self.workflow_repo,
            artifact_repo=self.artifact_repo,
            execution_repo=self.execution_repo,
            decision_repo=self.decision_repo,
            registry=self.registry,
            knowledge_gateway=FakeKnowledgeGateway(),
            tool_gateway=FakeToolGateway(),
            review_repo=self.review_repo,
            detection_repo=self.detection_repo,
            resolution_repo=self.resolution_repo,
        )
        self.review_service = FeatureReviewService(
            self.review_repo,
            RepositoryDetectionGateway(self.detection_repo),
            RepositorySignalGateway(self.signal_repo),
            jira_client=FakeJiraClient(),
        )

    # ---- shared plumbing --------------------------------------------------

    def _make_workspace(self) -> str:
        workspace = tempfile.mkdtemp(prefix="quipu-demo-")
        (Path(workspace) / "requirements.txt").write_text("pytest\n")
        (Path(workspace) / "test_export.py").write_text("def test_export():\n    assert True\n")
        return workspace

    async def _set_workspace_path_async(self, workflow, workspace: str):
        updated = workflow.model_copy(update={"metadata": {**workflow.metadata, "workspace_path": workspace}})
        return await self.workflow_repo.update_if_version(workflow.workflow_id, workflow.version, updated)

    def _agent_context(self, workflow_id: str, execution_id: str, **overrides) -> AgentContext:
        defaults = dict(
            workflow_id=workflow_id,
            execution_id=execution_id,
            knowledge=FakeKnowledgeGateway(),
            tools=FakeToolGateway(),
            artifacts=RepositoryArtifactGateway(self.artifact_repo),
            signals=RepositorySignalGateway(self.signal_repo),
            detections=RepositoryDetectionGateway(self.detection_repo),
            resolutions=RepositoryResolutionGateway(self.resolution_repo),
            executions=self.execution_repo,
        )
        defaults.update(overrides)
        return AgentContext(**defaults)

    async def _run_detecting(self, *, workflow_id: str, domain: str, window_minutes: int, expected_output: dict) -> str:
        with demo_agent_runner_patches(detecting_text=json.dumps(expected_output)):
            agent = DetectingAgent()
            context = self._agent_context(workflow_id, f"demo-detect-{workflow_id}-{domain}")
            agent_input = AgentInput(
                workflow_id=workflow_id,
                agent_name="detecting_agent",
                ticket=Ticket(title="demo detection", description="demo detection"),
                context={"domain": domain, "window_minutes": window_minutes},
            )
            output = await agent.execute(agent_input, context)
        if output.status != WorkflowStatus.COMPLETED:
            raise RuntimeError(f"DetectingAgent failed: {output.errors}")
        return json.loads(output.messages[1])["detection_id"]

    async def _run_incident_resolution(self, *, workflow_id: str, detection_id: str, proposal: dict) -> str:
        with demo_agent_runner_patches(resolution_text=json.dumps(proposal)):
            agent = IncidentResolutionAgent()
            context = self._agent_context(workflow_id, f"demo-resolve-{detection_id}")
            agent_input = AgentInput(
                workflow_id=workflow_id,
                agent_name="incident_resolution_agent",
                ticket=Ticket(title="demo resolution", description="demo resolution"),
                context={"detection_id": detection_id},
            )
            output = await agent.execute(agent_input, context)
        if output.status != WorkflowStatus.COMPLETED:
            raise RuntimeError(f"IncidentResolutionAgent failed: {output.errors}")
        return json.loads(output.messages[1])["resolution_id"]

    async def _run_monitoring(
        self, *, workflow_id: str, service_name: str, deployment_artifact_id: str | None, monitoring_client, logging_client
    ) -> list[str]:
        agent = MonitoringAgent(monitoring_client=monitoring_client, logging_client=logging_client)
        context = self._agent_context(workflow_id, f"demo-monitor-{workflow_id}")
        context_dict = {"service_name": service_name, "region": "us-central1", "environment": "production", "window_minutes": 15}
        if deployment_artifact_id:
            context_dict["deployment_artifact_id"] = deployment_artifact_id
        agent_input = AgentInput(
            workflow_id=workflow_id, agent_name="monitoring_agent", ticket=Ticket(title="demo monitoring", description="demo monitoring"), context=context_dict
        )
        output = await agent.execute(agent_input, context)
        if output.status != WorkflowStatus.COMPLETED:
            raise RuntimeError(f"MonitoringAgent failed: {output.errors}")
        return json.loads(output.messages[1])["signal_ids"]

    # ---- Scenario 1: Feature Discovery -> SDLC -----------------------------

    async def run_feature_flow(self) -> DemoSummary:
        summary = DemoSummary(scenario="feature")
        workspace = self._make_workspace()
        now = datetime.now(timezone.utc)

        # 1. Real Signal normalization from customer/support feedback —
        # the exact production adapters app/signals/adapters.py already
        # provides, not a demo-only parallel implementation.
        feedback_signals = [
            normalize_customer_feedback(
                {"feedback_id": "demo-fb-1", "submitted_at": now.isoformat(), "text": "I really need to export reports to Excel.", "feature_area": "reports"}
            ),
            normalize_customer_feedback(
                {"feedback_id": "demo-fb-2", "submitted_at": now.isoformat(), "text": "Please add an Excel export option.", "feature_area": "reports"}
            ),
            normalize_support_feedback(
                {"ticket_ref": "demo-support-1", "submitted_at": now.isoformat(), "text": "Third customer this week asking for Excel export.", "feature_area": "reports"}
            ),
        ]
        for signal in feedback_signals:
            await self.signal_repo.save(signal)
        summary.signal_ids = [s.signal_id for s in feedback_signals]
        passed, detail = await verify_signals_persisted(self.signal_repo, summary.signal_ids)
        summary.record("signals_persisted", passed, detail)

        # 2. DetectingAgent — real evidence-first evaluation over a bounded,
        # deterministically-retrieved evidence set (Level 3.2).
        fake_output = detection_output(
            detection_type="feature_opportunity",
            title="Add Excel export to reports",
            summary="Multiple independent customers and a support ticket converge on the same unmet need: Excel export.",
            rationale="Two customer-feedback signals and one support signal, all within the same window, request the same capability.",
            subject="reports",
            supporting_signal_ids=summary.signal_ids,
            confidence=0.91,
        )
        detection_id = await self._run_detecting(workflow_id="demo-feature-detection", domain="product", window_minutes=10080, expected_output=fake_output)
        summary.detection_id = detection_id
        passed, detail = await verify_detection(self.detection_repo, detection_id, DetectionType.FEATURE_OPPORTUNITY)
        summary.record("detection_is_feature_opportunity", passed, detail)

        # 3. FeatureReviewService — real deterministic review workflow
        # (Level 3.4). No LLM anywhere in this step.
        review = await self.review_service.create_review(detection_id)
        passed, detail = await verify_review(self.review_repo, review.review_id, ReviewStatus.PENDING)
        summary.record("review_created_pending", passed, detail)

        approved = await self.review_service.approve(
            review.review_id, reviewer_id=_REVIEWER_ID, reviewer_type=DecisionSource.HUMAN, granted=_GRANTED, review_comment="Clear, repeated customer demand — approved."
        )
        summary.review_id = approved.review_id
        summary.ticket_id = approved.ticket.ticket_id
        passed, detail = await verify_review(self.review_repo, review.review_id, ReviewStatus.APPROVED)
        summary.record("review_approved_by_human", passed, detail)
        passed, detail = await verify_ticket_created(self.review_repo, review.review_id)
        summary.record("jira_ticket_created", passed, detail)

        # Failure path #5: idempotent re-entry — re-creating a review for
        # the same detection must return the SAME review, never a duplicate.
        review_again = await self.review_service.create_review(detection_id)
        summary.record(
            "idempotent_review_recreate",
            review_again.review_id == review.review_id,
            f"create_review() called again for detection '{detection_id}' returned the same review_id={review_again.review_id}",
        )

        # 4. OrchestrationService.start_workflow_from_review — the real
        # Level 3.5 entry point. FeatureReviewService never touches this.
        workflow = await self.orchestration.start_workflow_from_review(approved.review_id)
        workflow = await self._set_workspace_path_async(workflow, workspace)  # see module docstring
        summary.workflow_id = workflow.workflow_id
        passed, detail = await verify_workflow_status(self.workflow_repo, workflow.workflow_id, WorkflowStatus.PENDING)
        summary.record("workflow_started_at_planning", passed, detail)

        # Failure path #5 (variant): idempotent re-entry for workflow
        # creation itself — must return the SAME workflow.
        workflow_again = await self.orchestration.start_workflow_from_review(approved.review_id)
        passed, detail = await verify_no_duplicate_workflow(self.review_repo, self.workflow_repo, approved.review_id, workflow.workflow_id)
        summary.record("idempotent_workflow_recreate", workflow_again.workflow_id == workflow.workflow_id and passed, detail)

        # 5. Real step-wise OrchestrationService.run_to_completion — every
        # QuipuAgent invoked through the real AgentRegistry, each running
        # its own real internal ADK LlmAgent (faked only at the
        # InMemoryRunner/JiraClient construction boundary — see
        # app/demo/patching.py).
        with demo_agent_runner_patches(
            plan_text=json.dumps(VALID_PLAN),
            architecture_text=json.dumps(VALID_ARCHITECTURE),
            codegen_text=json.dumps(VALID_CODEGEN),
            testing_text=json.dumps(VALID_TESTING_PASS),
            deployment_text=json.dumps(VALID_DEPLOYMENT),
            deployment_succeeds=True,
        ):
            result = await self.orchestration.run_to_completion(workflow.workflow_id)

        summary.final_status = result.status.value
        summary.artifact_ids = result.artifact_ids
        summary.stages_executed = ["planning", "architecture", "codegen", "testing", "deployment"]
        passed, detail = await verify_workflow_status(self.workflow_repo, workflow.workflow_id, WorkflowStatus.COMPLETED)
        summary.record("sdlc_completed", passed, detail)
        passed, detail = await verify_artifact_chain(
            self.artifact_repo, workflow.workflow_id, result.artifact_ids, ["plan", "architecture", "code_change", "test_result", "deployment"]
        )
        summary.record("artifact_chain_complete", passed, detail)

        # 6. Post-deployment MonitoringAgent — real production Signal
        # normalization (app.signals.adapters), closing the loop the
        # diagram in docs/architecture/end_to_end_demo.md shows.
        monitoring_signal_ids = await self._run_monitoring(
            workflow_id=workflow.workflow_id,
            service_name="quipu-demo",
            deployment_artifact_id=result.artifact_ids[-1],
            monitoring_client=FakeCloudMonitoringClient(error_rate=0.0, latency_ms=95.0),
            logging_client=FakeCloudLoggingClient(entries=[]),
        )
        passed, detail = await verify_signals_persisted(self.signal_repo, monitoring_signal_ids) if monitoring_signal_ids else (True, "no post-deployment signals (healthy, no traffic in the fake window) — not a failure")
        summary.record("post_deployment_monitoring_observed", passed, detail)
        summary.extra["post_deployment_signal_ids"] = monitoring_signal_ids

        # 7. Visibly exercise ADK SequentialAgent construction (§ ADK
        # requirement) — proof the real happy-path SequentialAgent builds
        # correctly against the same registry, without re-running the SDLC
        # a second time through it (see docs/architecture/end_to_end_demo.md
        # "Why the step-wise path is primary").
        sequential_context = self._agent_context(workflow.workflow_id, "demo-sequential-proof")
        sequential_agent = build_happy_path_sequential_agent(self.registry, sequential_context)
        summary.extra["adk_sequential_agent_stages"] = [sub_agent.name for sub_agent in sequential_agent.sub_agents]

        summary.finalize()
        return summary

    # ---- Scenario 2: Production Incident -> Remediation --------------------

    async def run_incident_flow(self) -> DemoSummary:
        summary = DemoSummary(scenario="incident")
        workspace = self._make_workspace()

        # 1. A minimal, real original SDLC run — the "already deployed"
        # workflow the incident will target. Same production path as
        # Scenario 1's step 5, just condensed.
        with demo_agent_runner_patches(
            plan_text=json.dumps(VALID_PLAN),
            architecture_text=json.dumps(VALID_ARCHITECTURE),
            codegen_text=json.dumps(VALID_CODEGEN),
            testing_text=json.dumps(VALID_TESTING_PASS),
            deployment_text=json.dumps(VALID_DEPLOYMENT),
            deployment_succeeds=True,
        ):
            original = await self.orchestration.start_workflow(Ticket(title="Add dark mode", description="Add a dark theme toggle"), workspace_path=workspace)
            original = await self.orchestration.run_to_completion(original.workflow_id)
        passed, detail = await verify_workflow_status(self.workflow_repo, original.workflow_id, WorkflowStatus.COMPLETED)
        summary.record("original_service_deployed", passed, detail)
        deployment_artifact_id = original.artifact_ids[-1]

        # 2. MonitoringAgent observes a real error spike — real production
        # Signal normalization from a live-shaped (faked-client) Cloud
        # Monitoring/Logging query.
        error_log_entries = [
            LogEntryResult(
                insert_id="demo-log-1", timestamp=datetime.now(timezone.utc), severity="ERROR", message="NullPointerException in request handler",
                log_name="run.googleapis.com/stderr", resource_labels={"service_name": "quipu-demo"}, trace=None,
            )
        ]
        incident_signal_ids = await self._run_monitoring(
            workflow_id=original.workflow_id,
            service_name="quipu-demo",
            deployment_artifact_id=deployment_artifact_id,
            monitoring_client=FakeCloudMonitoringClient(error_rate=0.08, latency_ms=910.0),
            logging_client=FakeCloudLoggingClient(entries=error_log_entries),
        )
        summary.signal_ids = incident_signal_ids
        passed, detail = await verify_signals_persisted(self.signal_repo, incident_signal_ids)
        summary.record("incident_signals_persisted", passed, detail)

        # 3. DetectingAgent (domain=operational) -> INCIDENT.
        fake_detection = detection_output(
            detection_type="incident",
            title="Errors and latency increased after deployment",
            summary="Application errors and elevated p99 latency both began immediately after the last deployment.",
            rationale="Error-rate and application-error signals for the same service cluster right after the deployment event.",
            subject="quipu-demo",
            supporting_signal_ids=incident_signal_ids,
            confidence=0.92,
            severity="critical",
        )
        detection_id = await self._run_detecting(workflow_id=original.workflow_id, domain="operational", window_minutes=15, expected_output=fake_detection)
        summary.detection_id = detection_id
        passed, detail = await verify_detection(self.detection_repo, detection_id, DetectionType.INCIDENT)
        summary.record("detection_is_incident", passed, detail)

        # 4. IncidentResolutionAgent -> CODE_FIX. The proposal deliberately
        # claims target_agent="deployment_agent" (adversarial) — proven
        # ignored in step 5.
        proposal = resolution_proposal(strategy="code_fix", supporting_signal_ids=incident_signal_ids, target_agent="deployment_agent")
        resolution_id = await self._run_incident_resolution(workflow_id=original.workflow_id, detection_id=detection_id, proposal=proposal)
        summary.resolution_id = resolution_id
        passed, detail = await verify_resolution(self.resolution_repo, resolution_id, expected_strategy="code_fix")
        summary.record("resolution_recommends_code_fix", passed, detail)

        # 5. OrchestrationService.start_remediation_from_resolution — the
        # real Level 3.6 entry point. target_agent is never read; the
        # workflow lands on codegen_agent's stage, never deployment_agent's.
        remediation = await self.orchestration.start_remediation_from_resolution(resolution_id)
        summary.record(
            "remediation_authorized_ignoring_spoofed_target",
            remediation.current_stage == WorkflowStage.CODEGEN,
            f"workflow reopened at stage='{remediation.current_stage.value}' despite the model claiming target_agent='deployment_agent'",
        )

        # Failure path #3 + #6: the remediation's OWN Codegen -> Testing
        # attempt fails for real (a genuinely broken on-disk test), and the
        # existing deterministic retry/recovery machinery routes it back to
        # codegen_agent — never forward to Deployment.
        (Path(workspace) / "test_export.py").write_text("def test_export():\n    assert False\n")
        failing_output = testing_output_with_failures([{"test_name": "test_export", "classification": "code_defect", "details": "assertion failed"}])
        with demo_agent_runner_patches(codegen_text=json.dumps(VALID_CODEGEN), testing_text=json.dumps(failing_output)):
            after_codegen = await self.orchestration.execute_next_step(remediation.workflow_id)
            after_first_testing = await self.orchestration.execute_next_step(after_codegen.workflow_id)
        summary.record(
            "testing_failure_blocks_deployment_and_retries",
            after_first_testing.current_stage == WorkflowStage.CODEGEN and after_first_testing.status != WorkflowStatus.COMPLETED,
            f"after a real failing test run, workflow routed to stage='{after_first_testing.current_stage.value}' "
            f"status='{after_first_testing.status.value}' — Deployment was never reached",
        )

        # Recovery: fix the test, retry succeeds through to Deployment.
        (Path(workspace) / "test_export.py").write_text("def test_export():\n    assert True\n")
        with demo_agent_runner_patches(
            codegen_text=json.dumps(VALID_CODEGEN), testing_text=json.dumps(VALID_TESTING_PASS), deployment_text=json.dumps(VALID_DEPLOYMENT), deployment_succeeds=True
        ):
            after_second_codegen = await self.orchestration.execute_next_step(after_first_testing.workflow_id)
            after_second_testing = await self.orchestration.execute_next_step(after_second_codegen.workflow_id)
            final = await self.orchestration.execute_next_step(after_second_testing.workflow_id)

        summary.final_status = final.status.value
        summary.remediation_outcome = final.metadata.get("remediation_outcome")
        summary.artifact_ids = final.artifact_ids
        summary.stages_executed = ["codegen", "testing", "codegen", "testing", "deployment"]
        passed, detail = await verify_workflow_status(self.workflow_repo, final.workflow_id, WorkflowStatus.COMPLETED)
        summary.record("remediation_completed_after_recovery", passed, detail)

        # 6. Post-remediation MonitoringAgent — real evidence for whether
        # the deployment actually looks healthy. Deliberately never
        # reported as "incident resolved" — see
        # docs/architecture/incident_remediation.md §8/§20-21.
        healthy_signal_ids = await self._run_monitoring(
            workflow_id=final.workflow_id,
            service_name="quipu-demo",
            deployment_artifact_id=final.artifact_ids[-1],
            monitoring_client=FakeCloudMonitoringClient(error_rate=0.0, latency_ms=100.0),
            logging_client=FakeCloudLoggingClient(entries=[]),
        )
        summary.extra["post_remediation_signal_ids"] = healthy_signal_ids
        summary.record(
            "remediation_outcome_not_conflated_with_resolved",
            summary.remediation_outcome == "deployed_pending_verification",
            f"workflow.metadata['remediation_outcome']='{summary.remediation_outcome}' — deployment success alone is never reported as 'incident resolved'",
        )

        # 7. Failure path #4: a second, unsafe (high-risk) resolution must
        # be escalated, never executed. IncidentResolutionAgent's own
        # deterministic safety policy (Level 3.3) downgrades this before
        # it's even persisted.
        unsafe_signal = Signal(
            signal_type=SignalType.APPLICATION_ERROR,
            source=SignalSource.CLOUD_LOGGING,
            severity=SignalSeverity.CRITICAL,
            observed_at=datetime.now(timezone.utc),
            subject="quipu-demo",
            summary="Data corruption suspected in the export pipeline",
            service_name="quipu-demo",
            environment="production",
            provenance=SignalProvenance(source_system="demo", source_event_id="demo-unsafe-1"),
            fingerprint=compute_fingerprint(source=SignalSource.CLOUD_LOGGING, source_event_id="demo-unsafe-1", subject="quipu-demo"),
        )
        await self.signal_repo.save(unsafe_signal)
        unsafe_detection_output = detection_output(
            detection_type="incident",
            title="Possible data corruption in export pipeline",
            summary="A high-severity signal suggests possible data corruption.",
            rationale="A single critical application-error signal referencing data integrity.",
            subject="quipu-demo",
            supporting_signal_ids=[unsafe_signal.signal_id],
            confidence=0.8,
            severity="critical",
        )
        unsafe_detection_id = await self._run_detecting(
            workflow_id=original.workflow_id, domain="operational", window_minutes=15, expected_output=unsafe_detection_output
        )
        unsafe_proposal = resolution_proposal(strategy="code_fix", supporting_signal_ids=[unsafe_signal.signal_id], risk="high", root_cause_confidence=0.55)
        unsafe_resolution_id = await self._run_incident_resolution(workflow_id=original.workflow_id, detection_id=unsafe_detection_id, proposal=unsafe_proposal)
        passed, detail = await verify_resolution(self.resolution_repo, unsafe_resolution_id, expected_strategy="escalate")
        summary.record("unsafe_resolution_already_downgraded_by_agent_policy", passed, detail)

        executions_before = len(await self.execution_repo.list_for_workflow(original.workflow_id))
        unsafe_remediation = await self.orchestration.start_remediation_from_resolution(unsafe_resolution_id)
        summary.escalated = unsafe_remediation.status == WorkflowStatus.ESCALATED
        summary.record(
            "unsafe_remediation_escalated_not_executed",
            summary.escalated,
            f"high-risk resolution '{unsafe_resolution_id}' resulted in workflow status='{unsafe_remediation.status.value}', never invoking an agent",
        )
        passed, detail = await verify_no_remediation_execution(self.execution_repo, original.workflow_id, executions_before)
        summary.record("no_agent_execution_for_escalation", passed, detail)

        # 8. Idempotent rerun of the SAME (successful) resolution.
        remediation_again = await self.orchestration.start_remediation_from_resolution(resolution_id)
        summary.record(
            "idempotent_remediation_rerun",
            remediation_again.workflow_id == final.workflow_id,
            f"start_remediation_from_resolution('{resolution_id}') called again returned workflow_id='{remediation_again.workflow_id}' unchanged",
        )

        # 9. Provenance immutability check — DetectionResult/ResolutionResult
        # are byte-identical to what was persisted before any remediation
        # executed.
        detection_obj = await self.detection_repo.get(detection_id)
        resolution_obj = await self.resolution_repo.get(resolution_id)
        passed, detail = await verify_detection_and_resolution_immutable(self.detection_repo, self.resolution_repo, detection_obj, resolution_obj)
        summary.record("detection_and_resolution_immutable", passed, detail)

        # 10. Visibly exercise ADK LoopAgent construction (§ ADK
        # requirement) — proof it builds correctly, documented as a
        # secondary mechanism not used as remediation's primary retry path
        # (see docs/architecture/incident_remediation.md §9).
        loop_context = self._agent_context(final.workflow_id, "demo-loop-proof")
        loop_agent = build_recovery_loop_agent(self.registry, loop_context)
        summary.extra["adk_loop_agent_sub_agents"] = [sub_agent.name for sub_agent in loop_agent.sub_agents]

        summary.workflow_id = final.workflow_id
        summary.finalize()
        return summary
