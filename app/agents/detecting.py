"""Detecting agent — no legacy predecessor, same QuipuAgent + internal-ADK-
adapter shape as Planning/Architecture/Codegen/Testing/Deployment. Unlike
MonitoringAgent (Level 3.1, deliberately deterministic — see
docs/architecture/monitoring_agent.md §3), Detecting has a genuine
reasoning task: correlating a bounded set of already-collected Signals into
an interpretation. It uses a real Gemini/ADK LlmAgent for exactly that.

Core principle, extending the evidence-first philosophy established by
TestingAgent/DeploymentAgent one layer up: SIGNALS ARE EVIDENCE, THE MODEL
PROVIDES INTERPRETATION. Signal retrieval is deterministic Python
(app/persistence + SignalGateway), assembled into a bounded evidence set
BEFORE Gemini is ever invoked; Gemini reasons over that fixed set and
returns a DetectionResult whose `supporting_signal_ids` are verified
against the actual evidence set, never trusted blindly. See
docs/architecture/detecting_agent.md for the full design.
"""

import json
import uuid
from datetime import datetime, timedelta, timezone

from pydantic import BaseModel, Field, ValidationError, field_validator

from google.adk.agents import LlmAgent
from google.adk.agents.readonly_context import ReadonlyContext
from google.adk.runners import InMemoryRunner
from google.genai import types

from app.agent_runtime.base import QuipuAgent
from app.agent_runtime.capabilities import AgentCapability, CapabilityError
from app.agent_runtime.context import AgentContext
from app.agent_runtime.identity import AgentIdentity
from app.agents.planning import _non_empty, _tool_capability_gate, _track_usage_metrics
from app.config import get_settings
from app.core.observability import get_logger
from app.domain import (
    AgentError,
    AgentExecution,
    AgentInput,
    AgentMetrics,
    AgentOutput,
    DetectionDomain,
    DetectionResult,
    DetectionType,
    ErrorCategory,
    Signal,
    SignalSeverity,
    SignalType,
    WorkflowStatus,
    compute_detection_fingerprint,
)
from app.persistence.repositories.signal import SignalQuery
from app.tools.knowledge_tools import KNOWLEDGE_TOOLS

logger = get_logger("quipu.agent.detecting")
settings = get_settings()

# Default Signal types retrieved per domain when DetectingInput.signal_types
# is empty — deliberately fixed here, not user-supplied, so a caller can't
# accidentally (or a compromised caller can't deliberately) point Detecting
# at an unrelated signal population. A caller MAY narrow this via
# signal_types, never widen past what each domain's SignalType values are.
_OPERATIONAL_SIGNAL_TYPES = [
    SignalType.METRIC_ANOMALY,
    SignalType.LATENCY_ANOMALY,
    SignalType.AVAILABILITY_DEGRADATION,
    SignalType.LOG_ERROR,
    SignalType.APPLICATION_ERROR,
    SignalType.DEPLOYMENT_EVENT,
]
_PRODUCT_SIGNAL_TYPES = [
    SignalType.CUSTOMER_FEEDBACK,
    SignalType.SUPPORT_FEEDBACK,
    SignalType.FEATURE_REQUEST_PATTERN,
    SignalType.USER_BEHAVIOR,
    SignalType.ADOPTION_ANOMALY,
]
_DOMAIN_SIGNAL_TYPES = {DetectionDomain.OPERATIONAL: _OPERATIONAL_SIGNAL_TYPES, DetectionDomain.PRODUCT: _PRODUCT_SIGNAL_TYPES}

# Evidence-first floor (§18 of the task): a detection_type other than
# NO_ACTION requires at least this many verified (non-fabricated)
# supporting signals. A single Signal is still evidence — Detecting's job
# is to say whether it's SUFFICIENT (via confidence/rationale), not to
# require an arbitrary minimum count beyond "at least one real thing
# actually happened." Documented explicitly rather than left implicit.
_MIN_SUPPORTING_SIGNALS = 1


class DetectingInput(BaseModel):
    """What DetectingAgent needs to determine which Signals to retrieve and
    reason over. Parsed from AgentInput.context — the same existing
    extension point MonitoringInput uses (app.domain.agent_io.AgentInput.
    context), not a new invocation contract. One model covers both
    detection paths (§4 of the task): `domain` selects operational vs.
    product framing and default signal_types; nothing here forces two
    agents to exist.
    """

    domain: DetectionDomain
    service_name: str | None = None
    environment: str | None = None
    signal_types: list[SignalType] = Field(default_factory=list)  # narrows the domain default; never widens it
    window_minutes: int = Field(default=60, gt=0)
    max_signals: int = Field(default=50, gt=0)

    @field_validator("environment")
    @classmethod
    def _strip_environment(cls, value: str | None) -> str | None:
        return value.strip() if value else value


class DetectionOutput(BaseModel):
    """What the internal LlmAgent is allowed to produce — Gemini's
    structured interpretation of the evidence set it was given. Fields the
    agent computes deterministically (domain, observation_window_minutes,
    fingerprint, detected_at) are NOT part of this schema — the model isn't
    asked to restate what it was already told."""

    detection_type: DetectionType
    title: str
    summary: str
    rationale: str  # concise decision rationale — instructed explicitly not to be a chain-of-thought dump
    confidence: float = Field(ge=0.0, le=1.0)
    severity: SignalSeverity | None = None
    subject: str
    supporting_signal_ids: list[str] = Field(default_factory=list)
    knowledge_references: list[str] = Field(default_factory=list)

    _validate_title = field_validator("title")(_non_empty)
    _validate_summary = field_validator("summary")(_non_empty)
    _validate_rationale = field_validator("rationale")(_non_empty)
    _validate_subject = field_validator("subject")(_non_empty)


def _signal_to_evidence_dict(signal: Signal) -> dict:
    """Compact, bounded per-signal representation handed to Gemini — typed
    fields plus the already-sanitized `evidence` dict (Level 3's
    sanitize_metadata already caps/redacts it at ingestion time), never the
    full Signal object (no internal `status`, no `metadata` free-form
    bucket) and never a second sanitization pass, since one already
    happened before this Signal was ever persisted."""
    return {
        "signal_id": signal.signal_id,
        "signal_type": signal.signal_type.value,
        "source": signal.source.value,
        "severity": signal.severity.value,
        "observed_at": signal.observed_at.isoformat(),
        "subject": signal.subject,
        "summary": signal.summary,
        "service_name": signal.service_name,
        "environment": signal.environment,
        "revision": signal.revision,
        "deployment_artifact_id": signal.deployment_artifact_id,
        "evidence": signal.evidence,
    }


def _build_instruction(context: ReadonlyContext) -> str:
    evidence_set = context.state.get("evidence_set") or []
    domain = context.state.get("detection_domain", "operational")
    evidence_json = json.dumps(evidence_set, indent=2)

    domain_note = (
        "You are analyzing OPERATIONAL signals — production telemetry (errors, "
        "latency, availability, deployments). Look for a probable production "
        "incident: does a deployment precede a failure spike? do error/latency "
        "signals for the same service/revision move together in time?"
        if domain == "operational"
        else "You are analyzing PRODUCT signals — customer feedback, support "
        "requests, usage patterns. Look for a genuine feature opportunity: do "
        "independent sources (multiple customers, support tickets, usage "
        "patterns) converge on the same unmet need over time, or is this a "
        "single, isolated data point?"
    )

    knowledge_note = ""
    if context.state.get("_knowledge_gateway") is not None:
        knowledge_note = (
            "\n\nYou also have query_enterprise_knowledge — known failure modes, "
            "service architecture, historical incident patterns, operational "
            "runbooks. Enterprise Knowledge is CONTEXTUAL GROUNDING, not "
            "evidence: never treat a knowledge result as if it were a production "
            "event, and never put a document_id into supporting_signal_ids. Use "
            "it only when it would materially sharpen the interpretation; report "
            "any document_id you actually used in knowledge_references."
        )

    return f"""You are Quipu's Detecting Agent — the interpretation layer
between raw evidence and organizational action.

{domain_note}

Below is the ONLY evidence available to you: a bounded, deterministically
retrieved set of Signals (already deduplicated, already sanitized) that
Monitoring or another source produced. You did not choose this set and
cannot expand it — there is no tool to fetch more signals.

Evidence set ({len(evidence_set)} signal(s)):
{evidence_json}
{knowledge_note}

CRITICAL: supporting_signal_ids must be a subset of the signal_id values
shown above. Any id you did not see in this evidence set will be rejected
and the whole detection discarded — do not guess, invent, or reuse an id
from a previous run. If the evidence is empty, ambiguous, unrelated, or too
thin to support a real conclusion, return detection_type="no_action" rather
than fabricating an incident or opportunity to seem useful.

Reason about: temporal relationships between signals, repetition/independent
sources (stronger evidence than one isolated signal), and whether the
evidence set as a whole is actually sufficient to support your conclusion.
confidence reflects YOUR confidence in this interpretation of the evidence
— it is not a measure of how good any single Signal is, and a high
confidence does not excuse thin or fabricated evidence.

You have no tools that can modify anything, deploy, write code, create a
ticket, or resolve an incident. You only interpret.

Return ONLY the structured DetectionOutput: detection_type
(incident | feature_opportunity | no_action), title, summary, rationale (a
concise, decision-relevant explanation — not private step-by-step
reasoning), confidence, severity (only if genuinely applicable), subject,
supporting_signal_ids, knowledge_references."""


_detecting_llm_agent = LlmAgent(
    name="detecting",
    description="Interprets a bounded set of Signals as a probable incident, a product opportunity, or no action.",
    model=settings.gemini_model,
    instruction=_build_instruction,
    output_schema=DetectionOutput,
    output_key="detecting",
    tools=KNOWLEDGE_TOOLS,
    before_tool_callback=_tool_capability_gate,
    after_model_callback=_track_usage_metrics,
)


class DetectingAgent(QuipuAgent):
    """Quipu-native Detecting Agent. Retrieves a bounded set of Signals via
    SignalGateway, optionally consults Enterprise Knowledge, reasons about
    the evidence via Gemini, validates every claimed supporting_signal_id
    against what was actually retrieved, and persists the resulting
    DetectionResult through DetectionGateway. Never mutates a Signal, never
    creates an IncidentCandidate/FeatureCandidate/Ticket, never calls
    another agent.
    """

    @property
    def identity(self) -> AgentIdentity:
        return AgentIdentity(
            agent_id="detecting_agent",
            name="Detecting Agent",
            version="1.0.0",
            description="Interprets Signals as probable incidents or product opportunities.",
        )

    @property
    def capabilities(self) -> set[AgentCapability]:
        return {AgentCapability.READ_SIGNALS, AgentCapability.QUERY_KNOWLEDGE, AgentCapability.WRITE_DETECTION}

    async def _perform(self, agent_input: AgentInput, context: AgentContext) -> AgentOutput:
        self.require_capability(AgentCapability.READ_SIGNALS)

        execution = AgentExecution(
            execution_id=agent_input.execution_id,
            workflow_id=agent_input.workflow_id,
            agent_name=self.identity.agent_id,
            status=WorkflowStatus.RUNNING,
        )
        if context.executions is not None:
            await context.executions.create(execution)

        metrics = AgentMetrics(execution_id=agent_input.execution_id)

        async def _fail(code: str, message: str, category: ErrorCategory, *, recoverable: bool = True) -> AgentOutput:
            error = AgentError(code=code, message=message, category=category, recoverable=recoverable, retryable=recoverable)
            execution.status = WorkflowStatus.FAILED
            execution.completed_at = datetime.now(timezone.utc)
            execution.error = error
            if context.executions is not None:
                await context.executions.update(execution)
            return AgentOutput(execution_id=agent_input.execution_id, status=WorkflowStatus.FAILED, errors=[error], metrics=metrics)

        try:
            detecting_input = DetectingInput.model_validate(agent_input.context)
        except ValidationError as exc:
            return await _fail("DETECTING_INPUT_INVALID", str(exc), ErrorCategory.VALIDATION)

        if detecting_input.window_minutes > settings.detecting_max_window_minutes:
            return await _fail(
                "DETECTING_WINDOW_TOO_LARGE",
                f"window_minutes={detecting_input.window_minutes} exceeds the configured ceiling of {settings.detecting_max_window_minutes}",
                ErrorCategory.VALIDATION,
            )
        if detecting_input.environment is not None and detecting_input.environment not in get_settings().cloud_run_allowed_environments:
            return await _fail(
                "DETECTING_ENVIRONMENT_NOT_ALLOWED",
                f"'{detecting_input.environment}' is not in the configured allowed environment list",
                ErrorCategory.VALIDATION,
            )
        if context.signals is None:
            return await _fail("DETECTING_SIGNAL_GATEWAY_MISSING", "AgentContext.signals is not configured", ErrorCategory.INTERNAL)
        if context.detections is None:
            return await _fail("DETECTING_DETECTION_GATEWAY_MISSING", "AgentContext.detections is not configured", ErrorCategory.INTERNAL)

        max_signals = min(detecting_input.max_signals, settings.detecting_max_signals)
        evidence = await self._retrieve_evidence(detecting_input, max_signals, context.signals, granted=self.capabilities)

        if not evidence:
            # Evidence-first: no evidence retrieved means nothing to
            # interpret — never invoke Gemini just to have it invent a
            # conclusion from nothing (§17/§33 of the task).
            return await self._finalize(
                DetectionOutput(
                    detection_type=DetectionType.NO_ACTION,
                    title="No signals found",
                    summary=f"No Signals matched the given {detecting_input.domain.value} detection criteria in the last {detecting_input.window_minutes} minute(s).",
                    rationale="Retrieval returned zero Signals; there is no evidence to interpret.",
                    confidence=0.0,
                    subject=detecting_input.service_name or f"environment:{detecting_input.environment or 'unspecified'}",
                    supporting_signal_ids=[],
                ),
                evidence=[],
                detecting_input=detecting_input,
                agent_input=agent_input,
                context=context,
                execution=execution,
                metrics=metrics,
            )

        session_state: dict = {
            "evidence_set": [_signal_to_evidence_dict(s) for s in evidence],
            "detection_domain": detecting_input.domain.value,
            "workflow_id": agent_input.workflow_id,
            "_agent_name": self.identity.agent_id,
            "_capabilities": self.capabilities,
            "_metrics": metrics,
        }
        if AgentCapability.QUERY_KNOWLEDGE in self.capabilities:
            session_state["_knowledge_gateway"] = context.knowledge

        runner = InMemoryRunner(agent=_detecting_llm_agent, app_name="quipu")
        session = await runner.session_service.create_session(app_name="quipu", user_id=agent_input.workflow_id, state=session_state)
        message = types.Content(role="user", parts=[types.Part(text="Begin detection.")])

        final_text = ""
        try:
            async for event in runner.run_async(user_id=agent_input.workflow_id, session_id=session.id, new_message=message):
                if event.is_final_response() and event.content and event.content.parts:
                    final_text = event.content.parts[0].text
        except Exception as exc:  # Gemini/ADK/tool failure — never fabricate a detection.
            logger.exception("detecting agent LLM execution failed")
            return await _fail("DETECTING_LLM_FAILURE", str(exc), ErrorCategory.LLM_FAILURE)

        if not final_text.strip():
            return await _fail("DETECTING_EMPTY_RESPONSE", "model returned an empty response", ErrorCategory.LLM_FAILURE)

        try:
            detection_output = DetectionOutput.model_validate_json(final_text)
        except ValidationError as exc:
            return await _fail("DETECTING_VALIDATION_FAILED", str(exc), ErrorCategory.VALIDATION)

        detection_output = self._validate_evidence(detection_output, evidence)

        return await self._finalize(
            detection_output,
            evidence=evidence,
            detecting_input=detecting_input,
            agent_input=agent_input,
            context=context,
            execution=execution,
            metrics=metrics,
        )

    async def _retrieve_evidence(
        self, detecting_input: DetectingInput, max_signals: int, signal_gateway, *, granted: set[AgentCapability]
    ) -> list[Signal]:
        """The implementation-boundary capability check (§24/§25 layer 3):
        `granted` is an explicit argument, not read implicitly from
        `self.capabilities`, same defense-in-depth pattern MonitoringAgent's
        _collect_metrics/_collect_logs established. Deterministic, bounded
        pre-selection (§26 of the task) — never an unbounded query, never
        sent to Gemini un-filtered."""
        if AgentCapability.READ_SIGNALS not in granted:
            raise CapabilityError(self.identity.agent_id, AgentCapability.READ_SIGNALS)

        signal_types = detecting_input.signal_types or _DOMAIN_SIGNAL_TYPES[detecting_input.domain]
        since = datetime.now(timezone.utc) - timedelta(minutes=detecting_input.window_minutes)

        collected: dict[str, Signal] = {}
        for signal_type in signal_types:
            results = await signal_gateway.query(
                SignalQuery(
                    signal_type=signal_type,
                    service_name=detecting_input.service_name,
                    environment=detecting_input.environment,
                    since=since,
                    limit=max_signals,
                )
            )
            for signal in results:
                collected[signal.signal_id] = signal

        ordered = sorted(collected.values(), key=lambda s: s.observed_at, reverse=True)
        return ordered[:max_signals]

    def _validate_evidence(self, detection_output: DetectionOutput, evidence: list[Signal]) -> DetectionOutput:
        """Evidence-first validation (§10 of the task) — the single most
        important check in this agent. Any supporting_signal_id the model
        claimed that was NOT actually part of the retrieved evidence set is
        silently dropped (never trusted, never surfaced as if it were
        real). If that leaves fewer than _MIN_SUPPORTING_SIGNALS valid ids
        for a non-NO_ACTION detection, the whole result is downgraded to
        NO_ACTION with confidence forced to 0.0 — a fabricated-evidence
        detection is never persisted as an incident or opportunity,
        regardless of how confident the model claimed to be."""
        valid_ids = {s.signal_id for s in evidence}
        claimed_ids = detection_output.supporting_signal_ids
        verified_ids = [sid for sid in claimed_ids if sid in valid_ids]
        fabricated = set(claimed_ids) - valid_ids

        if fabricated:
            logger.warning("detecting agent: model referenced fabricated signal id(s) %s; dropped", sorted(fabricated))

        if detection_output.detection_type != DetectionType.NO_ACTION and len(verified_ids) < _MIN_SUPPORTING_SIGNALS:
            return detection_output.model_copy(
                update={
                    "detection_type": DetectionType.NO_ACTION,
                    "confidence": 0.0,
                    "supporting_signal_ids": [],
                    "rationale": (
                        "Downgraded to no_action: the model's claimed supporting evidence did not "
                        "correspond to any Signal actually retrieved."
                        if fabricated
                        else "Downgraded to no_action: fewer than the minimum required supporting signals were verified."
                    ),
                }
            )

        return detection_output.model_copy(update={"supporting_signal_ids": verified_ids})

    async def _finalize(
        self,
        detection_output: DetectionOutput,
        *,
        evidence: list[Signal],
        detecting_input: DetectingInput,
        agent_input: AgentInput,
        context: AgentContext,
        execution: AgentExecution,
        metrics: AgentMetrics,
    ) -> AgentOutput:
        fingerprint = compute_detection_fingerprint(
            detection_type=detection_output.detection_type,
            subject=detection_output.subject,
            supporting_signal_ids=detection_output.supporting_signal_ids,
            window_minutes=detecting_input.window_minutes,
        )

        self.require_capability(AgentCapability.WRITE_DETECTION)
        existing = await context.detections.find_by_fingerprint(fingerprint)
        if existing is not None:
            detection = existing
        else:
            detection = DetectionResult(
                detection_id=str(uuid.uuid4()),
                detection_type=detection_output.detection_type,
                domain=detecting_input.domain,
                title=detection_output.title,
                summary=detection_output.summary,
                rationale=detection_output.rationale,
                confidence=detection_output.confidence,
                severity=detection_output.severity,
                subject=detection_output.subject,
                service_name=detecting_input.service_name,
                environment=detecting_input.environment,
                supporting_signal_ids=detection_output.supporting_signal_ids,
                knowledge_references=detection_output.knowledge_references,
                observation_window_minutes=detecting_input.window_minutes,
                fingerprint=fingerprint,
            )
            try:
                detection = await context.detections.save(detection)
            except Exception as exc:
                logger.exception("detection persistence failed")
                error = AgentError(code="DETECTION_PERSISTENCE_FAILED", message=str(exc), category=ErrorCategory.INTERNAL, recoverable=True, retryable=True)
                execution.status = WorkflowStatus.FAILED
                execution.completed_at = datetime.now(timezone.utc)
                execution.error = error
                if context.executions is not None:
                    await context.executions.update(execution)
                return AgentOutput(execution_id=agent_input.execution_id, status=WorkflowStatus.FAILED, errors=[error], metrics=metrics)

        execution.status = WorkflowStatus.COMPLETED
        execution.completed_at = datetime.now(timezone.utc)
        if context.executions is not None:
            await context.executions.update(execution)

        return AgentOutput(
            execution_id=agent_input.execution_id,
            status=WorkflowStatus.COMPLETED,
            messages=[detection.summary, detection.model_dump_json()],
            metrics=metrics,
        )
