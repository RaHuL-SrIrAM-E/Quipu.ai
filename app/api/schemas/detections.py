"""DetectionResult-facing response schemas."""

from datetime import datetime

from pydantic import BaseModel

from app.domain import DetectionResult


class DetectionSummary(BaseModel):
    detection_id: str
    detection_type: str
    domain: str
    title: str
    summary: str
    rationale: str
    confidence: float
    severity: str | None
    subject: str
    service_name: str | None
    environment: str | None
    supporting_signal_ids: list[str]
    knowledge_references: list[str]
    observation_window_minutes: int
    detected_at: datetime

    @classmethod
    def from_domain(cls, detection: DetectionResult) -> "DetectionSummary":
        return cls(
            detection_id=detection.detection_id,
            detection_type=detection.detection_type.value,
            domain=detection.domain.value,
            title=detection.title,
            summary=detection.summary,
            rationale=detection.rationale,
            confidence=detection.confidence,
            severity=detection.severity.value if detection.severity else None,
            subject=detection.subject,
            service_name=detection.service_name,
            environment=detection.environment,
            supporting_signal_ids=list(detection.supporting_signal_ids),
            knowledge_references=list(detection.knowledge_references),
            observation_window_minutes=detection.observation_window_minutes,
            detected_at=detection.detected_at,
        )
