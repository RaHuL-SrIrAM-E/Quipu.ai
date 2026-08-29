"""Incident Resolution agent — no legacy predecessor, same QuipuAgent +
internal-ADK-adapter shape as Planning/Architecture/Codegen/Testing/
Deployment/Detecting. Genuinely needs Gemini (like Detecting, unlike
Monitoring) for the diagnosis task, but adds one more layer of deterministic
safety on top of Detecting's evidence-first validation.

Core architectural principle (the whole reason this agent is scoped the way
it is): Incident Resolution is DETECT -> DIAGNOSE -> DECIDE, never EXECUTE.

    DetectionResult (INCIDENT)
          |
    IncidentResolutionAgent   <- THIS FILE
          |
    ResolutionResult          (diagnosis + recommended remediation)
          |
    future OrchestrationService routes to:
       codegen_agent / testing_agent / deployment_agent / architecture_agent / human

This agent never writes code, never runs tests, never deploys, never rolls
back, never touches Cloud Run, and never resolves the incident itself — it
only produces a plan. "Gemini proposes, application code authorizes" is
enforced identically to how app/orchestration/decisions.py turns a
ProposedDecision into a Decision only after transition-policy validation:
here, a ResolutionProposal becomes a ResolutionResult only after
_validate_evidence()/_apply_safety_policy() have both run. See
docs/architecture/incident_resolution_agent.md for the full design.
"""

import json
import uuid
from datetime import datetime, timezone

from pydantic import BaseModel, Field, ValidationError, field_validator

from google.adk.agents import LlmAgent
from google.adk.agents.readonly_context import ReadonlyContext
from google.adk.runners import InMemoryRunner
from google.genai import types

from app.agent_runtime.base import QuipuAgent
from app.agent_runtime.capabilities import AgentCapability
from app.agent_runtime.context import AgentContext
from app.agent_runtime.identity import AgentIdentity
from app.agents.detecting import signal_to_evidence_dict
from app.agents.planning import _non_empty, _tool_capability_gate, _track_usage_metrics
from app.config import get_settings
from app.core.observability import get_logger
from app.domain import (
    AgentError,
    AgentExecution,
    AgentInput,
    AgentMetrics,
    AgentOutput,
    Artifact,
    DetectionResult,
    DetectionType,
    ErrorCategory,
    RemediationRisk,
    RemediationStrategy,
    ResolutionResult,
    Signal,
    SignalSeverity,
    SignalType,
    WorkflowStatus,
    compute_resolution_fingerprint,
)
from app.tools.knowledge_tools import KNOWLEDGE_TOOLS

logger = get_logger("quipu.agent.incident_resolution")
settings = get_settings()

# Deterministic strategy -> target agent mapping (§12/§13 of the task): the
# model's own `target_agent` claim is NEVER trusted directly — the final
# target_agent on a ResolutionResult is always derived from this fixed map,
# keyed by the (already-validated, closed-enum) remediation_strategy. This
# closes the "target_agent = malicious_agent" attack surface structurally,
# not just by allow-list checking a string.
_STRATEGY_TARGET_AGENT: dict[RemediationStrategy, str | None] = {
    RemediationStrategy.CODE_FIX: "codegen_agent",
    RemediationStrategy.RETEST: "testing_agent",
    RemediationStrategy.ARCHITECTURE_REVIEW: "architecture_agent",
    RemediationStrategy.ROLLBACK: "deployment_agent",
    RemediationStrategy.ESCALATE: None,
    RemediationStrategy.NO_ACTION: None,
}

# CODE_FIX requires at least one verified supporting signal whose type
# directly indicates an application-level defect — never authorized purely
# on e.g. a deployment-event or availability signal alone (§31 adversarial
# test F: "CODE_FIX but no actual code-related evidence").
_CODE_DEFECT_SIGNAL_TYPES = {SignalType.APPLICATION_ERROR, SignalType.LOG_ERROR}

_MIN_SUPPORTING_SIGNALS = 1


class ResolutionInput(BaseModel):
    """What IncidentResolutionAgent needs: a reference to an already-
    validated DetectionResult, nothing else. Parsed from AgentInput.context,
    the same existing extension point Monitoring/DetectingInput use. Every
    other piece of context (signals, artifacts, knowledge) is resolved
    deterministically FROM the DetectionResult itself — the caller does not
    hand-assemble evidence (§3 of the task: the primary input must be a
    validated DetectionResult, not an arbitrary model-generated description).
    """

    detection_id: str

    _validate_detection_id = field_validator("detection_id")(_non_empty)


class ResolutionProposal(BaseModel):
    """What the internal LlmAgent is allowed to produce — Gemini's proposed
    diagnosis and remediation. `target_agent` IS included so the model can
    express its own view, but it is never trusted directly — see
    _STRATEGY_TARGET_AGENT and _apply_safety_policy(). Fields the agent
    computes deterministically (detection_id, fingerprint, resolved_at)
    are NOT part of this schema."""

    diagnosis_summary: str
    probable_root_cause: str
    root_cause_confidence: float = Field(ge=0.0, le=1.0)
    root_cause_candidates: list[str] = Field(default_factory=list)

    remediation_strategy: RemediationStrategy
    remediation_rationale: str
    expected_outcome: str
    verification_strategy: str

    risk: RemediationRisk
    severity: SignalSeverity | None = None
    escalation_recommended: bool = False

    target_agent: str | None = None  # the model's own claim — cross-checked, never trusted directly
    rollback_target: str | None = None

    supporting_signal_ids: list[str] = Field(default_factory=list)
    supporting_artifact_ids: list[str] = Field(default_factory=list)
    knowledge_references: list[str] = Field(default_factory=list)

    _validate_diagnosis_summary = field_validator("diagnosis_summary")(_non_empty)
    _validate_probable_root_cause = field_validator("probable_root_cause")(_non_empty)
    _validate_remediation_rationale = field_validator("remediation_rationale")(_non_empty)
    _validate_expected_outcome = field_validator("expected_outcome")(_non_empty)
    _validate_verification_strategy = field_validator("verification_strategy")(_non_empty)


def _artifact_to_evidence_dict(artifact: Artifact) -> dict:
    """Compact deployment-artifact evidence — never the full Artifact
    object (no arbitrary payload contents beyond the few fields relevant to
    diagnosis)."""
    payload = artifact.payload
    return {
        "artifact_id": artifact.artifact_id,
        "artifact_type": artifact.artifact_type.value,
        "status": payload.get("status"),
        "service_name": payload.get("service_name"),
        "environment": payload.get("environment"),
        "revision": payload.get("revision"),
        "deployment_summary": payload.get("deployment_summary"),
        "failure_classification": payload.get("failure_classification"),
    }


def _build_instruction(context: ReadonlyContext) -> str:
    detection = context.state.get("detection") or {}
    evidence_set = context.state.get("evidence_set") or []
    artifact_evidence = context.state.get("artifact_evidence") or []

    detection_json = json.dumps(detection, indent=2)
    evidence_json = json.dumps(evidence_set, indent=2)
    artifact_json = json.dumps(artifact_evidence, indent=2)

    knowledge_note = ""
    if context.state.get("_knowledge_gateway") is not None:
        knowledge_note = (
            "\n\nYou also have query_enterprise_knowledge — known failure "
            "modes, service architecture, approved configuration, runbooks, "
            "incident history. Enterprise Knowledge is CONTEXTUAL GROUNDING, "
            "not evidence: never put a document_id into supporting_signal_ids "
            "or supporting_artifact_ids. Use it only when it would sharpen "
            "the diagnosis; report any document_id you actually used in "
            "knowledge_references."
        )

    return f"""You are Quipu's Incident Resolution Agent — the
DIAGNOSE-and-DECIDE layer, never the EXECUTE layer. You recommend a
remediation plan; you never modify code, run tests, deploy, roll back, or
resolve anything yourself. Nothing you propose is executed directly — a
separate, deterministic safety policy validates and can override your
recommendation before anything is persisted.

The incident under investigation (an already-validated, INCIDENT-typed
DetectionResult — you cannot change this, only interpret it):
{detection_json}

Supporting Signals actually retrieved ({len(evidence_set)} signal(s)) — the
ONLY evidence available to you:
{evidence_json}

Related deployment artifact evidence ({len(artifact_evidence)} item(s)):
{artifact_json}
{knowledge_note}

CRITICAL: supporting_signal_ids and supporting_artifact_ids must each be a
subset of the ids shown above. Any id you did not see here will be rejected
and the corresponding evidence discarded — do not guess or invent one.

Reason about: temporal ordering (did a deployment precede the failure?),
root cause candidates and how confident you actually are in each, whether
this looks like an application defect, an architecture/design problem, a
test that's wrong rather than the code, or a deployment regression that
should be rolled back. Consider what verification would actually confirm
the fix worked.

remediation_strategy must be exactly one of: code_fix, retest,
architecture_review, rollback, escalate, no_action. There is no
"configuration_change" option — if the right fix is a configuration change
and you have no safe way to express that as one of the above, recommend
escalate. If evidence is thin, contradictory, or you are not genuinely
confident, recommend escalate rather than guessing — a wrong automatic fix
is worse than asking a human. If you recommend rollback, rollback_target
must name the specific revision/service to roll back to, or leave it empty
and expect this to be escalated instead.

confidence values reflect YOUR confidence in the diagnosis — never inflate
confidence to make a risky remediation look more automatic. severity and
risk are separate concepts from root_cause_confidence: report all three
honestly, even if that combination should mean escalation (that decision is
enforced deterministically, not something you need to compute).

You have no tools that can modify anything, run code, deploy, or roll
anything back. You only diagnose and recommend."""


_incident_resolution_llm_agent = LlmAgent(
    name="incident_resolution",
    description="Diagnoses a validated operational incident and recommends (never executes) a remediation plan.",
    model=settings.gemini_model,
    instruction=_build_instruction,
    output_schema=ResolutionProposal,
    output_key="incident_resolution",
    tools=KNOWLEDGE_TOOLS,
    before_tool_callback=_tool_capability_gate,
    after_model_callback=_track_usage_metrics,
)


class IncidentResolutionAgent(QuipuAgent):
    """Quipu-native Incident Resolution Agent. Consumes a validated,
    INCIDENT-typed DetectionResult (via DetectionGateway), resolves its
    supporting Signals (via SignalGateway) and any correlated deployment
    Artifacts (via ArtifactGateway), optionally consults Enterprise
    Knowledge, reasons via Gemini, applies a deterministic evidence and
    risk safety policy, and persists the resulting ResolutionResult through
    ResolutionGateway. Never mutates the upstream DetectionResult, never
    executes remediation, never calls another agent.
    """

    @property
    def identity(self) -> AgentIdentity:
        return AgentIdentity(
            agent_id="incident_resolution_agent",
            name="Incident Resolution Agent",
            version="1.0.0",
            description="Diagnoses validated incidents and recommends (never executes) a remediation plan.",
        )

    @property
    def capabilities(self) -> set[AgentCapability]:
        return {
            AgentCapability.READ_DETECTION,
            AgentCapability.READ_SIGNALS,
            AgentCapability.READ_ARTIFACT,
            AgentCapability.QUERY_KNOWLEDGE,
            AgentCapability.WRITE_RESOLUTION,
        }

    async def _perform(self, agent_input: AgentInput, context: AgentContext) -> AgentOutput:
        self.require_capability(AgentCapability.READ_DETECTION)

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
            resolution_input = ResolutionInput.model_validate(agent_input.context)
        except ValidationError as exc:
            return await _fail("RESOLUTION_INPUT_INVALID", str(exc), ErrorCategory.VALIDATION)

        if context.detections is None:
            return await _fail("RESOLUTION_DETECTION_GATEWAY_MISSING", "AgentContext.detections is not configured", ErrorCategory.INTERNAL)
        if context.signals is None:
            return await _fail("RESOLUTION_SIGNAL_GATEWAY_MISSING", "AgentContext.signals is not configured", ErrorCategory.INTERNAL)
        if context.resolutions is None:
            return await _fail("RESOLUTION_RESOLUTION_GATEWAY_MISSING", "AgentContext.resolutions is not configured", ErrorCategory.INTERNAL)

        # --- Consume the DetectionResult through the existing gateway (§3):
        # never an arbitrary model-generated incident description.
        detection = await context.detections.get(resolution_input.detection_id)
        if detection is None:
            return await _fail(
                "DETECTION_NOT_FOUND", f"no DetectionResult '{resolution_input.detection_id}' found", ErrorCategory.VALIDATION
            )
        if detection.detection_type != DetectionType.INCIDENT:
            return await _fail(
                "DETECTION_NOT_AN_INCIDENT",
                f"DetectionResult '{detection.detection_id}' has detection_type '{detection.detection_type}', expected 'incident' — "
                "product opportunities are never reinterpreted as incidents",
                ErrorCategory.VALIDATION,
            )

        # --- Resolve supporting Signals (§4) — real Signal objects, bounded,
        # never invented. A supporting_signal_id that no longer resolves is
        # dropped, not fabricated.
        max_evidence = settings.incident_resolution_max_evidence
        evidence: list[Signal] = []
        for signal_id in detection.supporting_signal_ids[:max_evidence]:
            signal = await context.signals.get(signal_id)
            if signal is not None:
                evidence.append(signal)

        if not evidence:
            # Evidence-first: the DetectionResult's own supporting evidence
            # has entirely disappeared or was never resolvable. Never guess
            # a diagnosis from nothing — escalate deterministically, no
            # Gemini call (mirrors DetectingAgent's zero-evidence path).
            return await self._finalize(
                self._deterministic_escalation(
                    detection, reason="none of the DetectionResult's supporting_signal_ids resolved to an actual Signal"
                ),
                detection=detection,
                agent_input=agent_input,
                context=context,
                execution=execution,
                metrics=metrics,
            )

        # --- Correlate with deployment artifacts (§5) — via the existing
        # ArtifactGateway, never a direct Cloud Run call. Only signals that
        # already carry a deployment_artifact_id are looked up.
        artifact_evidence: list[dict] = []
        seen_artifact_ids: set[str] = set()
        self.require_capability(AgentCapability.READ_ARTIFACT)
        for signal in evidence:
            if signal.deployment_artifact_id and signal.deployment_artifact_id not in seen_artifact_ids:
                artifact = await context.artifacts.get(agent_input.workflow_id, signal.deployment_artifact_id)
                if artifact is not None:
                    artifact_evidence.append(_artifact_to_evidence_dict(artifact))
                    seen_artifact_ids.add(signal.deployment_artifact_id)

        session_state: dict = {
            "detection": {
                "detection_id": detection.detection_id,
                "domain": detection.domain.value,
                "title": detection.title,
                "summary": detection.summary,
                "subject": detection.subject,
                "service_name": detection.service_name,
                "environment": detection.environment,
                "severity": detection.severity.value if detection.severity else None,
                "confidence": detection.confidence,
                "detected_at": detection.detected_at.isoformat(),
            },
            "evidence_set": [signal_to_evidence_dict(s) for s in evidence],
            "artifact_evidence": artifact_evidence,
            "workflow_id": agent_input.workflow_id,
            "_agent_name": self.identity.agent_id,
            "_capabilities": self.capabilities,
            "_metrics": metrics,
        }
        if AgentCapability.QUERY_KNOWLEDGE in self.capabilities:
            session_state["_knowledge_gateway"] = context.knowledge

        runner = InMemoryRunner(agent=_incident_resolution_llm_agent, app_name="quipu")
        session = await runner.session_service.create_session(app_name="quipu", user_id=agent_input.workflow_id, state=session_state)
        message = types.Content(role="user", parts=[types.Part(text="Begin incident resolution.")])

        final_text = ""
        try:
            async for event in runner.run_async(user_id=agent_input.workflow_id, session_id=session.id, new_message=message):
                if event.is_final_response() and event.content and event.content.parts:
                    final_text = event.content.parts[0].text
        except Exception as exc:  # Gemini/ADK/tool failure — never fabricate a remediation plan.
            logger.exception("incident resolution agent LLM execution failed")
            return await _fail("RESOLUTION_LLM_FAILURE", str(exc), ErrorCategory.LLM_FAILURE)

        if not final_text.strip():
            return await _fail("RESOLUTION_EMPTY_RESPONSE", "model returned an empty response", ErrorCategory.LLM_FAILURE)

        try:
            proposal = ResolutionProposal.model_validate_json(final_text)
        except ValidationError as exc:
            return await _fail("RESOLUTION_VALIDATION_FAILED", str(exc), ErrorCategory.VALIDATION)

        proposal = self._validate_evidence(proposal, evidence, artifact_evidence)
        proposal = self._apply_safety_policy(proposal, evidence)

        return await self._finalize(proposal, detection=detection, agent_input=agent_input, context=context, execution=execution, metrics=metrics)

    def _deterministic_escalation(self, detection: DetectionResult, *, reason: str) -> ResolutionProposal:
        return ResolutionProposal(
            diagnosis_summary=f"Unable to diagnose: {reason}.",
            probable_root_cause="unknown — insufficient evidence",
            root_cause_confidence=0.0,
            remediation_strategy=RemediationStrategy.ESCALATE,
            remediation_rationale=reason,
            expected_outcome="A human reviews the incident and determines appropriate action.",
            verification_strategy="N/A — no automated remediation was recommended.",
            risk=RemediationRisk.HIGH,
            severity=detection.severity,
            escalation_recommended=True,
            supporting_signal_ids=[],
            supporting_artifact_ids=[],
        )

    def _validate_evidence(self, proposal: ResolutionProposal, evidence: list[Signal], artifact_evidence: list[dict]) -> ResolutionProposal:
        """Evidence-first validation (§20 of the task) — mirrors
        DetectingAgent._validate_evidence, extended to also cover
        supporting_artifact_ids. Any id the model claimed that was not
        actually part of the retrieved evidence is silently dropped, never
        trusted."""
        valid_signal_ids = {s.signal_id for s in evidence}
        valid_artifact_ids = {a["artifact_id"] for a in artifact_evidence}

        verified_signal_ids = [sid for sid in proposal.supporting_signal_ids if sid in valid_signal_ids]
        verified_artifact_ids = [aid for aid in proposal.supporting_artifact_ids if aid in valid_artifact_ids]

        fabricated_signals = set(proposal.supporting_signal_ids) - valid_signal_ids
        fabricated_artifacts = set(proposal.supporting_artifact_ids) - valid_artifact_ids
        if fabricated_signals:
            logger.warning("incident resolution agent: fabricated signal id(s) %s dropped", sorted(fabricated_signals))
        if fabricated_artifacts:
            logger.warning("incident resolution agent: fabricated artifact id(s) %s dropped", sorted(fabricated_artifacts))

        return proposal.model_copy(update={"supporting_signal_ids": verified_signal_ids, "supporting_artifact_ids": verified_artifact_ids})

    def _apply_safety_policy(self, proposal: ResolutionProposal, evidence: list[Signal]) -> ResolutionProposal:
        """The deterministic safety layer (§13/§14/§21 of the task):
        'Gemini proposes, application code authorizes' — mirrors
        app.orchestration.decisions turning a ProposedDecision into a
        Decision only after transition-policy validation. Every rule here
        can only ever downgrade a proposal toward ESCALATE/NO_ACTION —
        never upgrade it or invent a more permissive outcome than what the
        model itself proposed."""
        strategy = proposal.remediation_strategy

        def _escalate(reason: str) -> ResolutionProposal:
            return proposal.model_copy(
                update={
                    "remediation_strategy": RemediationStrategy.ESCALATE,
                    "escalation_recommended": True,
                    "remediation_rationale": f"Downgraded to escalate: {reason}",
                }
            )

        # Rule 1 — evidence-first floor: no real evidence survived
        # validation, no automatic remediation regardless of what the model
        # claimed.
        if strategy not in (RemediationStrategy.ESCALATE, RemediationStrategy.NO_ACTION) and len(proposal.supporting_signal_ids) < _MIN_SUPPORTING_SIGNALS:
            return _escalate("no verified supporting evidence remained after validation").model_copy(update={"root_cause_confidence": 0.0})

        # Rule 2 — high remediation risk is never auto-authorized (§14):
        # "Do not allow the LLM to bypass deterministic high-risk rules."
        if proposal.risk == RemediationRisk.HIGH and strategy not in (RemediationStrategy.ESCALATE, RemediationStrategy.NO_ACTION):
            return _escalate("remediation risk is HIGH")

        # Rule 3 — low root-cause confidence never authorizes automatic
        # remediation (§21: severity=HIGH, confidence=0.62, risk=HIGH should
        # escalate, not auto-remediate).
        if strategy not in (RemediationStrategy.ESCALATE, RemediationStrategy.NO_ACTION) and proposal.root_cause_confidence < settings.incident_resolution_min_confidence_for_auto_remediation:
            return _escalate(
                f"root_cause_confidence {proposal.root_cause_confidence:.2f} is below the "
                f"{settings.incident_resolution_min_confidence_for_auto_remediation:.2f} auto-remediation threshold"
            )

        # Rule 4 — ROLLBACK without a concrete rollback_target cannot be
        # authorized (§31 adversarial E: "no rollback target -> escalate").
        if strategy == RemediationStrategy.ROLLBACK and not (proposal.rollback_target and proposal.rollback_target.strip()):
            return _escalate("rollback was recommended without a concrete rollback_target")

        # Rule 5 — CODE_FIX requires at least one verified signal whose type
        # directly indicates an application-level defect (§31 adversarial F).
        if strategy == RemediationStrategy.CODE_FIX:
            verified_ids = set(proposal.supporting_signal_ids)
            has_code_evidence = any(s.signal_id in verified_ids and s.signal_type in _CODE_DEFECT_SIGNAL_TYPES for s in evidence)
            if not has_code_evidence:
                return _escalate("code_fix was recommended without any verified application-error/log-error evidence")

        # Rule 6 — target_agent is NEVER taken from the model directly;
        # always derived from the (now safety-checked) strategy. This runs
        # last so every escalation above already has target_agent=None via
        # the map lookup below in _finalize().
        return proposal

    async def _finalize(
        self,
        proposal: ResolutionProposal,
        *,
        detection: DetectionResult,
        agent_input: AgentInput,
        context: AgentContext,
        execution: AgentExecution,
        metrics: AgentMetrics,
    ) -> AgentOutput:
        target_agent = _STRATEGY_TARGET_AGENT[proposal.remediation_strategy]

        fingerprint = compute_resolution_fingerprint(
            detection_id=detection.detection_id, remediation_strategy=proposal.remediation_strategy, subject=detection.subject
        )

        self.require_capability(AgentCapability.WRITE_RESOLUTION)
        existing = await context.resolutions.find_by_fingerprint(fingerprint)
        if existing is not None:
            resolution = existing
        else:
            resolution = ResolutionResult(
                resolution_id=str(uuid.uuid4()),
                detection_id=detection.detection_id,
                diagnosis_summary=proposal.diagnosis_summary,
                probable_root_cause=proposal.probable_root_cause,
                root_cause_confidence=proposal.root_cause_confidence,
                root_cause_candidates=proposal.root_cause_candidates,
                remediation_strategy=proposal.remediation_strategy,
                remediation_rationale=proposal.remediation_rationale,
                expected_outcome=proposal.expected_outcome,
                verification_strategy=proposal.verification_strategy,
                risk=proposal.risk,
                severity=proposal.severity,
                escalation_recommended=proposal.escalation_recommended or proposal.remediation_strategy == RemediationStrategy.ESCALATE,
                target_agent=target_agent,
                rollback_target=proposal.rollback_target if proposal.remediation_strategy == RemediationStrategy.ROLLBACK else None,
                supporting_signal_ids=proposal.supporting_signal_ids,
                supporting_artifact_ids=proposal.supporting_artifact_ids,
                knowledge_references=proposal.knowledge_references,
                fingerprint=fingerprint,
            )
            try:
                resolution = await context.resolutions.save(resolution)
            except Exception as exc:
                logger.exception("resolution persistence failed")
                error = AgentError(code="RESOLUTION_PERSISTENCE_FAILED", message=str(exc), category=ErrorCategory.INTERNAL, recoverable=True, retryable=True)
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
            messages=[resolution.diagnosis_summary, resolution.model_dump_json()],
            metrics=metrics,
        )
