"""DetectionActionProcessor — the framework-independent core of
DetectionResult -> Action processing (the production implementation of
app.detection.action_trigger.ActionTrigger).

Owns exactly one responsibility chain:

    DetectionAvailableEvent -> branch on detection_type ->
        no_action            -> terminal no-op
        feature_opportunity  -> FeatureReviewService.create_review()
        incident             -> resolve the owning WorkflowState from the
                                 detection's supporting evidence, then
                                 invoke the EXISTING IncidentResolutionAgent
                                 through its existing QuipuAgent.execute()
                                 contract

It does NOT approve/reject a FeatureReview, create a Jira ticket, start or
reopen a WorkflowState, or authorize remediation — every one of those
remains exactly where it already lived (FeatureReviewService.approve(),
OrchestrationService.start_workflow_from_review()/
start_remediation_from_resolution()), reachable only through the existing
human-approval-gated API routes. This processor's job ends the moment a
FeatureReview or ResolutionResult exists — see
docs/architecture/event_driven_detection.md and the Detection -> Action
boundary design this implements.

Idempotency is entirely inherited, never reinvented: FeatureReviewService.
create_review() already dedups per detection_id
(FeatureReviewRepository.find_by_detection_id), and IncidentResolutionAgent
already dedups per (detection_id, remediation_strategy, subject) fingerprint
(ResolutionRepository.find_by_fingerprint) — reprocessing the same
DetectionAvailableEvent is always safe by construction of those two
existing components, not because this module adds a second dedup key.
"""

import json
import uuid
from dataclasses import dataclass

from app.agent_runtime.context import AgentContext
from app.agent_runtime.gateways.artifacts import ArtifactGateway
from app.agent_runtime.gateways.detections import DetectionGateway
from app.agent_runtime.gateways.knowledge import KnowledgeGateway
from app.agent_runtime.gateways.resolutions import ResolutionGateway
from app.agent_runtime.gateways.signals import SignalGateway
from app.agent_runtime.gateways.tools import ToolGateway
from app.agents.incident_resolution import IncidentResolutionAgent
from app.config import get_settings
from app.core.observability import get_logger
from app.detection.action_trigger import DetectionAvailableEvent
from app.domain import AgentInput, DetectionResult, DetectionType, Ticket, WorkflowStatus
from app.feature_review import FeatureReviewService
from app.persistence.repositories.execution import AgentExecutionRepository

logger = get_logger("quipu.detection.action_processor")


class ActionProcessingError(Exception):
    """Action processing failed independently of DetectionResult
    persistence, which already succeeded before this ever runs. Always
    retryable — same rationale as app.detection.processor.
    DetectionProcessingError: there is no permanent-failure category here,
    since nothing about a transient failure invalidates the already-
    persisted DetectionResult, and re-invoking this processor later with
    the same event is always safe (see module docstring)."""


@dataclass
class ActionProcessingOutcome:
    detection_id: str
    action: str  # "skipped_no_action" | "review_created" | "resolution_created"
    review_id: str | None = None
    resolution_id: str | None = None
    workflow_id: str | None = None


class DetectionActionProcessor:
    def __init__(
        self,
        *,
        review_service: FeatureReviewService,
        incident_agent: IncidentResolutionAgent,
        signal_gateway: SignalGateway,
        detection_gateway: DetectionGateway,
        artifact_gateway: ArtifactGateway,
        resolution_gateway: ResolutionGateway,
        knowledge_gateway: KnowledgeGateway | None = None,
        tool_gateway: ToolGateway | None = None,
        execution_repo: AgentExecutionRepository | None = None,
        max_evidence: int | None = None,
    ):
        self._review_service = review_service
        self._incident_agent = incident_agent
        self._signals = signal_gateway
        self._detections = detection_gateway
        self._artifacts = artifact_gateway
        self._resolutions = resolution_gateway
        self._knowledge = knowledge_gateway
        self._tools = tool_gateway
        self._executions = execution_repo
        self._max_evidence = max_evidence if max_evidence is not None else get_settings().incident_resolution_max_evidence

    async def on_detection_available(self, event: DetectionAvailableEvent) -> None:
        """Satisfies app.detection.action_trigger.ActionTrigger — discards
        the rich ActionProcessingOutcome, same relationship
        DetectionProcessorTrigger.on_signal_available has to
        DetectionProcessor.process_signal_available()."""
        await self.process_detection_available(event)

    async def process_detection_available(self, event: DetectionAvailableEvent) -> ActionProcessingOutcome:
        if event.detection_type == DetectionType.NO_ACTION:
            logger.info("action.skipped detection_id=%s reason=no_action", event.detection_id)
            return ActionProcessingOutcome(detection_id=event.detection_id, action="skipped_no_action")

        if event.detection_type == DetectionType.FEATURE_OPPORTUNITY:
            review = await self._review_service.create_review(event.detection_id)
            logger.info(
                "action.review_created detection_id=%s review_id=%s status=%s", event.detection_id, review.review_id, review.status.value
            )
            return ActionProcessingOutcome(detection_id=event.detection_id, action="review_created", review_id=review.review_id)

        if event.detection_type == DetectionType.INCIDENT:
            return await self._process_incident(event)

        raise ActionProcessingError(f"unhandled detection_type '{event.detection_type}' for detection '{event.detection_id}'")

    async def _process_incident(self, event: DetectionAvailableEvent) -> ActionProcessingOutcome:
        detection = await self._detections.get(event.detection_id)
        if detection is None:
            raise ActionProcessingError(f"DetectionResult '{event.detection_id}' not found for action processing")

        workflow_id = await self._resolve_owning_workflow_id(detection)
        if workflow_id is None:
            raise ActionProcessingError(
                f"could not resolve an owning WorkflowState for DetectionResult '{event.detection_id}': "
                "no supporting signal carried a deployment_artifact_id that resolved to a known artifact"
            )

        context = AgentContext(
            workflow_id=workflow_id,
            execution_id=str(uuid.uuid4()),
            knowledge=self._knowledge,
            tools=self._tools,
            artifacts=self._artifacts,
            executions=self._executions,
            signals=self._signals,
            detections=self._detections,
            resolutions=self._resolutions,
        )
        agent_input = AgentInput(
            workflow_id=workflow_id,
            agent_name="incident_resolution_agent",
            ticket=Ticket(
                title=f"Incident resolution for detection {event.detection_id}",
                description=f"Triggered by DetectionResult {event.detection_id} (incident, subject={detection.subject}).",
            ),
            context={"detection_id": event.detection_id},
        )

        try:
            output = await self._incident_agent.execute(agent_input, context)
        except Exception as exc:
            logger.exception("action.incident_resolution_failed detection_id=%s workflow_id=%s", event.detection_id, workflow_id)
            raise ActionProcessingError(f"IncidentResolutionAgent execution failed: {exc}") from exc

        if output.status != WorkflowStatus.COMPLETED:
            codes = [error.code for error in output.errors]
            logger.warning(
                "action.incident_resolution_incomplete detection_id=%s workflow_id=%s errors=%s", event.detection_id, workflow_id, codes
            )
            raise ActionProcessingError(f"IncidentResolutionAgent did not complete for detection '{event.detection_id}': {codes}")

        resolution_id = None
        if len(output.messages) > 1:
            resolution_id = json.loads(output.messages[1])["resolution_id"]

        logger.info(
            "action.resolution_created detection_id=%s resolution_id=%s workflow_id=%s", event.detection_id, resolution_id, workflow_id
        )
        return ActionProcessingOutcome(
            detection_id=event.detection_id, action="resolution_created", resolution_id=resolution_id, workflow_id=workflow_id
        )

    async def _resolve_owning_workflow_id(self, detection: DetectionResult) -> str | None:
        """Same selection semantics IncidentResolutionAgent itself already
        uses to correlate deployment artifacts (app.agents.incident_
        resolution: iterate supporting_signal_ids in order, bounded by the
        same incident_resolution_max_evidence setting, first non-null
        deployment_artifact_id wins) — just stopping at the first
        resolvable one, since only one owning workflow_id is needed here.
        Never infers a workflow_id from service_name/environment (not a
        stable identifier — see the investigation this implements), never
        fabricates one."""
        for signal_id in detection.supporting_signal_ids[: self._max_evidence]:
            signal = await self._signals.get(signal_id)
            if signal is None or not signal.deployment_artifact_id:
                continue
            workflow_id = await self._artifacts.find_owning_workflow_id(signal.deployment_artifact_id)
            if workflow_id is not None:
                return workflow_id
            logger.warning(
                "action.artifact_not_found detection_id=%s signal_id=%s deployment_artifact_id=%s",
                detection.detection_id,
                signal_id,
                signal.deployment_artifact_id,
            )
        return None
