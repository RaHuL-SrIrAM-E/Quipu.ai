"""RemediationVerification — the durable record of whether AI-generated
remediation actually fixed production, established by comparing
post-deployment production evidence against the original incident
condition. Framework-independent (no Google SDK imports here — see
app/verification/service.py for the deterministic comparison logic, and
app/agents/monitoring.py for the Google telemetry that ultimately produces
the Signals this compares).

This is the critical invariant Quipu enforces:

    Signal            = "what was observed"                      (app.domain.signal)
    DetectionResult   = "what Detecting believes it may represent" (app.domain.detection)
    ResolutionResult  = "what remediation Incident Resolution recommends" (app.domain.resolution)
    RemediationVerification = "whether POST-DEPLOYMENT production evidence
                        confirms the incident condition actually cleared" (this module)

**Deployment success != incident resolution.** A workflow reaching
WorkflowStatus.COMPLETED after remediation only means the code deployed —
see app.orchestration.service._execute_decision's "deployed_pending_
verification" marker. Only a RemediationVerification with outcome
VERIFIED_RESOLVED represents Quipu having actually checked.

Not persisted as an Artifact, for the same reason DetectionResult/
ResolutionResult weren't (see their own module docstrings): this isn't an
SDLC stage's output consumed by the next stage in the Plan->Architecture->
Code->Test->Deploy lineage — it's an operational evidence-comparison
record, one level further removed from the deployment itself. Same
treatment: its own narrow domain model + repository
(app.persistence.repositories.remediation_verification).
"""

import hashlib
import uuid
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, field_validator


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _require_aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("timestamp must be timezone-aware (UTC)")
    return value


class VerificationOutcome(StrEnum):
    """A closed set — deliberately distinguishes "checked and healthy" from
    "checked but can't tell" (§3 of the task: never conflate the two)."""

    VERIFIED_RESOLVED = "verified_resolved"  # sufficient post-deployment evidence, all evaluable conditions healthy
    STILL_DEGRADED = "still_degraded"  # sufficient evidence, the original incident condition is still present
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"  # missing/too little monitoring data to conclude safely — NEVER treated as success
    ESCALATED = "escalated"  # the verification process itself hit a safety/infrastructure condition needing human attention


class VerificationStatus(StrEnum):
    """The verification RECORD's own lifecycle — NOT the outcome. Mirrors
    the Signal/DetectionResult convention of keeping pipeline-state
    separate from interpretation (see app.domain.signal.SignalStatus's
    docstring for the same distinction one layer down)."""

    IN_PROGRESS = "in_progress"  # evidence collection/comparison underway — the idempotency claim, no outcome yet
    COMPLETED = "completed"  # outcome is final for this verification_id


def compute_verification_key(*, resolution_id: str, deployment_artifact_id: str, revision: str | None, window_minutes: int) -> str:
    """The idempotency key (§12 of the task): same resolution_id +
    deployment_artifact_id + revision + verification window always
    resolves to the same verification_id, so a repeated verification
    request for the same deployment never creates a duplicate record —
    reused directly as RemediationVerification.verification_id (see
    app/verification/service.py), the same "deterministic id doubles as
    the create()-level race guard" pattern already used for other
    at-most-once claims in this codebase (e.g.
    OrchestrationService.start_workflow_from_review's version-checked
    claim). Deliberately its own function — not a reuse of
    compute_fingerprint/compute_detection_fingerprint/
    compute_resolution_fingerprint, each of which is a different layer's
    own identity concept (see this module's docstring)."""
    basis = "|".join([resolution_id, deployment_artifact_id, revision or "", str(window_minutes)])
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()


class RemediationVerification(BaseModel):
    verification_id: str = Field(default_factory=lambda: str(uuid.uuid4()))

    # Lineage — never rewritten, never reinterpreted in place.
    resolution_id: str
    workflow_id: str
    deployment_artifact_id: str
    revision: str | None = None

    verification_started_at: datetime = Field(default_factory=_utc_now)
    verification_completed_at: datetime | None = None

    # Baseline evidence — REFERENCES only (§4/§13 of the task): the
    # original DetectionResult this remediation targeted, and the Signal
    # ids that supported it. No raw Cloud Monitoring/Logging payload is
    # ever copied in here — see baseline_summary for the compact,
    # non-raw description.
    baseline_detection_id: str
    baseline_signal_ids: list[str] = Field(default_factory=list)
    baseline_summary: str  # compact, e.g. "2 signal(s): metric_anomaly(critical), log_error(error)" — never raw payload

    # Post-deployment evidence — REFERENCES only, same rule.
    post_deployment_signal_ids: list[str] = Field(default_factory=list)
    # Subset of post_deployment_signal_ids that directly informed the
    # outcome (evidence-first, same naming convention as
    # DetectionResult.supporting_signal_ids/ResolutionResult.
    # supporting_signal_ids — every id here is verified to be part of what
    # was actually retrieved, never fabricated).
    supporting_signal_ids: list[str] = Field(default_factory=list)
    # Compact per-condition-type breakdown, e.g. {"metric_anomaly":
    # "healthy", "latency_anomaly": "no_evidence"} — never raw payload.
    evidence_summary: dict[str, str] = Field(default_factory=dict)

    status: VerificationStatus = VerificationStatus.IN_PROGRESS
    outcome: VerificationOutcome | None = None  # None while status == IN_PROGRESS
    reason: str = ""
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)

    idempotency_key: str
    created_by: str = "remediation_verification_service"

    # Optimistic concurrency (same pattern as WorkflowState/FeatureReview):
    # the IN_PROGRESS -> COMPLETED transition is the only update, and it
    # must be version-checked — see
    # app.persistence.repositories.remediation_verification.
    version: int = Field(default=1, ge=1)

    _validate_started_at = field_validator("verification_started_at")(_require_aware)

    @field_validator("verification_completed_at")
    @classmethod
    def _validate_completed_at(cls, value: datetime | None) -> datetime | None:
        return _require_aware(value) if value is not None else value

    @field_validator("resolution_id", "workflow_id", "deployment_artifact_id", "baseline_detection_id", "baseline_summary", "idempotency_key")
    @classmethod
    def _not_empty(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("must not be empty")
        return value.strip()
