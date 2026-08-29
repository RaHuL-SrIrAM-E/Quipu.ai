"""ResolutionResult — Incident Resolution's diagnosis and recommended
remediation for one validated, operational DetectionResult. Framework-
independent (no Google SDK imports here — IncidentResolutionAgent's Gemini/
ADK integration lives in app/agents/incident_resolution.py).

Extends the same evidence/interpretation split Detecting established one
level up:

    Signal           = "what was observed"                       (app.domain.signal)
    DetectionResult  = "what Detecting believes a set of Signals
                        may represent"                            (app.domain.detection)
    ResolutionResult = "what Incident Resolution believes an
                        INCIDENT-typed DetectionResult's root
                        cause and appropriate remediation are"    (this module)
    Candidate/Ticket = future work / existing, not produced here

ResolutionResult is DETECT -> DIAGNOSE -> DECIDE, never EXECUTE: it
recommends a `remediation_strategy` and (deterministically, never from the
model directly) a `target_agent` that a future OrchestrationService could
route to — it never modifies code, deploys, rolls back, or resolves
anything itself. See app/agents/incident_resolution.py's module docstring.

Not persisted as an Artifact, for the identical reason DetectionResult
wasn't (see app.domain.detection's module docstring): this isn't an SDLC
stage's completed output consumed by the next agent in
Plan->Architecture->Code->Test->Deploy lineage — it's Resolution's own
interpretation, one level further removed from raw evidence than Detecting
already was. Same treatment: its own narrow domain model + repository.
"""

import hashlib
import uuid
from datetime import datetime, timezone

from pydantic import BaseModel, Field, field_validator

from app.domain.enums import RemediationRisk, RemediationStrategy, SignalSeverity


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _require_aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("timestamp must be timezone-aware (UTC)")
    return value


def compute_resolution_fingerprint(*, detection_id: str, remediation_strategy: RemediationStrategy, subject: str) -> str:
    """Resolution's own identity/dedup boundary (Level 3.3 §27) —
    deliberately NOT a reuse of app.domain.signal.compute_fingerprint or
    app.domain.detection.compute_detection_fingerprint: each layer
    (evidence, interpretation, diagnosis/remediation) has its own identity
    concept, and conflating them would blur exactly the distinctions this
    architecture protects. Same detection_id + concluded strategy + subject
    -> same fingerprint, so re-running Resolution over an unchanged
    DetectionResult doesn't create an uncontrolled duplicate plan.
    """
    basis = "|".join([detection_id, remediation_strategy.value, subject.strip().lower()])
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()


class ResolutionResult(BaseModel):
    resolution_id: str = Field(default_factory=lambda: str(uuid.uuid4()))

    # Lineage to the upstream DetectionResult — never rewritten, never
    # reinterpreted in place; see app.domain.detection.DetectionResult,
    # which this references by id only (§34: Incident Resolution must never
    # mutate the original DetectionResult).
    detection_id: str

    # Level 3.6 (Incident Resolution -> Authorized Remediation): the
    # ORIGINAL deploying workflow this resolution's evidence correlates to
    # — set by IncidentResolutionAgent from its own AgentInput.workflow_id
    # (the existing calling convention that already made deployment-
    # artifact correlation work in Level 3.3 — see
    # app.agents.incident_resolution._perform's artifact_evidence lookup).
    # None if the agent was invoked without a meaningful workflow context.
    # OrchestrationService.start_remediation_from_resolution() requires
    # this to be set: CODE_FIX/ARCHITECTURE_REVIEW remediation reopens
    # *this* workflow (its artifacts — Plan/Architecture — already live
    # there, since Artifact storage is workflow-scoped) rather than
    # creating a new one with no accessible input artifact. See
    # docs/architecture/incident_remediation.md "Workflow identity".
    workflow_id: str | None = None

    diagnosis_summary: str
    probable_root_cause: str
    root_cause_confidence: float = Field(ge=0.0, le=1.0)  # confidence in the DIAGNOSIS, separate from severity/risk
    root_cause_candidates: list[str] = Field(default_factory=list)  # alternate plausible causes, when the model isn't certain

    remediation_strategy: RemediationStrategy
    remediation_rationale: str
    expected_outcome: str
    verification_strategy: str

    risk: RemediationRisk  # risk of the RECOMMENDED remediation itself, not incident severity
    severity: SignalSeverity | None = None  # reuses Signal's severity vocabulary rather than inventing a second one
    escalation_recommended: bool = False

    # Deterministically derived from remediation_strategy by
    # IncidentResolutionAgent — never taken directly from the model's own
    # claim. None for ESCALATE/NO_ACTION (no agent to route to).
    target_agent: str | None = None
    rollback_target: str | None = None  # only meaningful when remediation_strategy == ROLLBACK

    # Evidence-first (§20): every id here is verified, before this object is
    # ever constructed, to correspond to a Signal/Artifact actually
    # retrieved — see app.agents.incident_resolution._validate_evidence.
    supporting_signal_ids: list[str] = Field(default_factory=list)
    supporting_artifact_ids: list[str] = Field(default_factory=list)

    # Contextual grounding, NOT evidence (same asymmetry as DetectionResult
    # — see app.domain.detection's docstring §14 reference) — best-effort,
    # not cross-checked against a retrieval log.
    knowledge_references: list[str] = Field(default_factory=list)

    resolved_at: datetime = Field(default_factory=_utc_now)
    created_by: str = "incident_resolution_agent"
    fingerprint: str

    _validate_resolved_at = field_validator("resolved_at")(_require_aware)

    @field_validator("detection_id", "diagnosis_summary", "probable_root_cause", "remediation_rationale", "expected_outcome", "verification_strategy", "fingerprint")
    @classmethod
    def _not_empty(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("must not be empty")
        return value.strip()
