"""ResolutionResult-facing response schemas. `target_agent` is exposed
read-only — the API has no endpoint that lets a caller set or influence
it (see app/api/routes/resolutions.py's remediate() — it never reads a
client-supplied target_agent, exactly like
OrchestrationService.start_remediation_from_resolution itself)."""

from datetime import datetime

from pydantic import BaseModel

from app.domain import ResolutionResult


class ResolutionSummary(BaseModel):
    resolution_id: str
    detection_id: str
    workflow_id: str | None
    diagnosis_summary: str
    probable_root_cause: str
    root_cause_confidence: float
    remediation_strategy: str
    remediation_rationale: str
    expected_outcome: str
    risk: str
    severity: str | None
    escalation_recommended: bool
    target_agent: str | None
    supporting_signal_ids: list[str]
    supporting_artifact_ids: list[str]
    resolved_at: datetime

    @classmethod
    def from_domain(cls, resolution: ResolutionResult) -> "ResolutionSummary":
        return cls(
            resolution_id=resolution.resolution_id,
            detection_id=resolution.detection_id,
            workflow_id=resolution.workflow_id,
            diagnosis_summary=resolution.diagnosis_summary,
            probable_root_cause=resolution.probable_root_cause,
            root_cause_confidence=resolution.root_cause_confidence,
            remediation_strategy=resolution.remediation_strategy.value,
            remediation_rationale=resolution.remediation_rationale,
            expected_outcome=resolution.expected_outcome,
            risk=resolution.risk.value,
            severity=resolution.severity.value if resolution.severity else None,
            escalation_recommended=resolution.escalation_recommended,
            target_agent=resolution.target_agent,
            supporting_signal_ids=list(resolution.supporting_signal_ids),
            supporting_artifact_ids=list(resolution.supporting_artifact_ids),
            resolved_at=resolution.resolved_at,
        )
