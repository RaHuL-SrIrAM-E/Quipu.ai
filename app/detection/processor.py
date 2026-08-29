"""DetectionProcessor — the framework-independent core of event-driven
detection processing (see docs/architecture/event_driven_detection.md).

Owns exactly one responsibility chain:

    SignalAvailableEvent -> resolve DetectionDomain -> evaluate the
    aggregation policy's minimum-evidence gate (app.detection.policy) ->
    construct DetectingInput -> invoke the EXISTING DetectingAgent through
    its existing QuipuAgent.execute() contract -> read back the
    DetectionResult it persisted (through the existing DetectionGateway)
    -> return a structured DetectionProcessingOutcome.

It does not retrieve/rank/interpret evidence itself (DetectingAgent's own
`_retrieve_evidence`/Gemini call remain the only place that happens), does
not fabricate a DetectionResult, and does not call FeatureReviewService or
IncidentResolutionAgent — persisting a DetectionResult is where this
processor's job ends. What happens with a FEATURE_OPPORTUNITY or INCIDENT
DetectionResult afterward (human review, remediation authorization) is
deliberately left to whatever already-existing flow consumes DetectionResult
by id (see docs/architecture/event_driven_detection.md "Product/incident
flows") — never invoked automatically from here.
"""

import asyncio
import json
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone

from app.agent_runtime.context import AgentContext
from app.agent_runtime.gateways.detections import DetectionGateway
from app.agent_runtime.gateways.knowledge import KnowledgeGateway
from app.agent_runtime.gateways.signals import SignalGateway
from app.agent_runtime.gateways.tools import ToolGateway
from app.agents.detecting import DetectingAgent
from app.config import get_settings
from app.core.observability import get_logger
from app.detection.policy import SIGNAL_TYPE_TO_DOMAIN, AggregationPolicy, count_related_signals
from app.domain import AgentInput, DetectionDomain, Ticket, WorkflowStatus
from app.eventing.trigger import SignalAvailableEvent
from app.persistence.repositories.execution import AgentExecutionRepository

logger = get_logger("quipu.detection.processor")


class DetectionProcessingError(Exception):
    """Detection processing failed independently of Signal persistence,
    which already succeeded before this ever runs. Always retryable —
    there is no permanent-failure category here the way there is for
    ingestion (app.eventing.errors): a DetectingAgent/Gemini failure today
    may succeed on retry tomorrow, and nothing about it invalidates the
    already-persisted Signal."""


@dataclass
class DetectionProcessingOutcome:
    signal_id: str
    invoked_detecting_agent: bool
    outcome: str  # "invoked" | "skipped_insufficient_evidence" | "skipped_unmapped_domain"
    domain: str | None = None
    evidence_count: int = 0
    detection_id: str | None = None
    detection_type: str | None = None
    duration_ms: float | None = None


class DetectionProcessor:
    def __init__(
        self,
        *,
        signal_gateway: SignalGateway,
        detection_gateway: DetectionGateway,
        knowledge_gateway: KnowledgeGateway | None = None,
        tool_gateway: ToolGateway | None = None,
        artifact_gateway=None,
        execution_repo: AgentExecutionRepository | None = None,
        policy: AggregationPolicy | None = None,
        detecting_agent: DetectingAgent | None = None,
    ):
        self._signals = signal_gateway
        self._detections = detection_gateway
        self._knowledge = knowledge_gateway
        self._tools = tool_gateway
        self._artifacts = artifact_gateway
        self._executions = execution_repo
        self._policy = policy or AggregationPolicy.from_settings()
        self._agent = detecting_agent or DetectingAgent()
        # Serializes concurrent processing for the same aggregation scope
        # (domain/service_name/environment) so two near-simultaneous
        # triggers can't both race past DetectingAgent's own
        # find_by_fingerprint-then-save dedup check (app.agents.detecting
        # _finalize) and create two DetectionResults with the same
        # fingerprint. Not a second dedup mechanism — compute_detection_
        # fingerprint/find_by_fingerprint remain the sole identity check;
        # this only prevents wasteful/racy concurrent DetectingAgent
        # invocations for the same scope.
        self._scope_locks: dict[tuple, asyncio.Lock] = defaultdict(asyncio.Lock)

    async def process_signal_available(self, event: SignalAvailableEvent) -> DetectionProcessingOutcome:
        started = datetime.now(timezone.utc)
        domain = SIGNAL_TYPE_TO_DOMAIN.get(event.signal_type)
        if domain is None:
            logger.info("detection.skipped signal_id=%s reason=unmapped_signal_type", event.signal_id)
            return DetectionProcessingOutcome(signal_id=event.signal_id, invoked_detecting_agent=False, outcome="skipped_unmapped_domain")

        scope_key = (domain, event.service_name, event.environment)
        async with self._scope_locks[scope_key]:
            return await self._process_in_scope(event, domain, started)

    async def _process_in_scope(self, event: SignalAvailableEvent, domain: DetectionDomain, started: datetime) -> DetectionProcessingOutcome:
        settings = get_settings()
        domain_policy = self._policy.for_domain(domain)
        max_signals = settings.detecting_max_signals

        related_count = await count_related_signals(
            self._signals,
            domain=domain,
            service_name=event.service_name,
            environment=event.environment,
            window_minutes=domain_policy.window_minutes,
            max_signals=max_signals,
        )

        if related_count < domain_policy.min_related_signals:
            logger.info(
                "detection.skipped signal_id=%s domain=%s evidence_count=%d min_required=%d reason=insufficient_evidence",
                event.signal_id,
                domain.value,
                related_count,
                domain_policy.min_related_signals,
            )
            return DetectionProcessingOutcome(
                signal_id=event.signal_id,
                invoked_detecting_agent=False,
                outcome="skipped_insufficient_evidence",
                domain=domain.value,
                evidence_count=related_count,
            )

        agent_input = AgentInput(
            workflow_id=f"detection-{uuid.uuid4()}",
            agent_name="detecting_agent",
            ticket=Ticket(
                title=f"Detection processing for signal {event.signal_id}",
                description=f"Triggered by Signal {event.signal_id} ({event.signal_type.value} from {event.source.value}).",
            ),
            context={
                "domain": domain.value,
                "service_name": event.service_name,
                "environment": event.environment,
                "window_minutes": domain_policy.window_minutes,
                "max_signals": max_signals,
            },
        )
        context = AgentContext(
            workflow_id=agent_input.workflow_id,
            execution_id=agent_input.execution_id,
            knowledge=self._knowledge,
            tools=self._tools,
            artifacts=self._artifacts,
            executions=self._executions,
            signals=self._signals,
            detections=self._detections,
        )

        try:
            output = await self._agent.execute(agent_input, context)
        except Exception as exc:
            logger.exception("detection.agent_execution_failed signal_id=%s domain=%s", event.signal_id, domain.value)
            raise DetectionProcessingError(f"DetectingAgent execution failed: {exc}") from exc

        duration_ms = (datetime.now(timezone.utc) - started).total_seconds() * 1000

        if output.status != WorkflowStatus.COMPLETED:
            codes = [error.code for error in output.errors]
            logger.warning(
                "detection.agent_failed signal_id=%s domain=%s evidence_count=%d errors=%s duration_ms=%.1f",
                event.signal_id,
                domain.value,
                related_count,
                codes,
                duration_ms,
            )
            raise DetectionProcessingError(f"DetectingAgent did not complete: {codes}")

        detection_id = None
        detection_type = None
        if len(output.messages) > 1:
            detection_id = json.loads(output.messages[1])["detection_id"]
            detection = await self._detections.get(detection_id)
            detection_type = detection.detection_type.value if detection else None

        logger.info(
            "detection.processed signal_id=%s domain=%s evidence_count=%d detection_id=%s detection_type=%s duration_ms=%.1f",
            event.signal_id,
            domain.value,
            related_count,
            detection_id,
            detection_type,
            duration_ms,
        )
        return DetectionProcessingOutcome(
            signal_id=event.signal_id,
            invoked_detecting_agent=True,
            outcome="invoked",
            domain=domain.value,
            evidence_count=related_count,
            detection_id=detection_id,
            detection_type=detection_type,
            duration_ms=duration_ms,
        )
