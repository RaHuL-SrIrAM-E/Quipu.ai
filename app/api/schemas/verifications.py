"""RemediationVerification-facing response schemas — the UI-critical
distinction (Invariant 8/task §4 "Verifications"): a workflow reaching
COMPLETED after remediation is DEPLOYED, never VERIFIED RESOLVED on its
own. `outcome` here is the only field a UI should ever read to claim
resolution; `status` is the record's own IN_PROGRESS/COMPLETED lifecycle,
not the outcome — see docs/architecture/remediation_verification.md §6.
"""

from datetime import datetime

from pydantic import BaseModel

from app.domain import RemediationVerification


class VerificationSummary(BaseModel):
    verification_id: str
    resolution_id: str
    workflow_id: str
    deployment_artifact_id: str
    revision: str | None
    status: str
    outcome: str | None
    reason: str
    confidence: float | None
    baseline_detection_id: str
    baseline_signal_ids: list[str]
    baseline_summary: str
    post_deployment_signal_ids: list[str]
    supporting_signal_ids: list[str]
    evidence_summary: dict[str, str]
    verification_started_at: datetime
    verification_completed_at: datetime | None

    @classmethod
    def from_domain(cls, verification: RemediationVerification) -> "VerificationSummary":
        return cls(
            verification_id=verification.verification_id,
            resolution_id=verification.resolution_id,
            workflow_id=verification.workflow_id,
            deployment_artifact_id=verification.deployment_artifact_id,
            revision=verification.revision,
            status=verification.status.value,
            outcome=verification.outcome.value if verification.outcome else None,
            reason=verification.reason,
            confidence=verification.confidence,
            baseline_detection_id=verification.baseline_detection_id,
            baseline_signal_ids=list(verification.baseline_signal_ids),
            baseline_summary=verification.baseline_summary,
            post_deployment_signal_ids=list(verification.post_deployment_signal_ids),
            supporting_signal_ids=list(verification.supporting_signal_ids),
            evidence_summary=dict(verification.evidence_summary),
            verification_started_at=verification.verification_started_at,
            verification_completed_at=verification.verification_completed_at,
        )
