"""Domain-level tests for ResolutionResult (Level 3.3). No persistence, no
agent — just the model and its fingerprint helper."""

from datetime import datetime

import pytest
from pydantic import ValidationError

from app.domain import RemediationRisk, RemediationStrategy, ResolutionResult, SignalSeverity, compute_resolution_fingerprint


def make_resolution(**overrides) -> ResolutionResult:
    defaults = dict(
        detection_id="det-1",
        diagnosis_summary="Application defect causing errors.",
        probable_root_cause="Null pointer in order handler after rev-42",
        root_cause_confidence=0.85,
        remediation_strategy=RemediationStrategy.CODE_FIX,
        remediation_rationale="Errors correlate with the code path changed in rev-42.",
        expected_outcome="Error rate returns to baseline.",
        verification_strategy="Re-run the test suite and monitor error rate for 30 minutes post-deploy.",
        risk=RemediationRisk.LOW,
        severity=SignalSeverity.CRITICAL,
        target_agent="codegen_agent",
        supporting_signal_ids=["sig-1", "sig-2"],
        fingerprint=compute_resolution_fingerprint(detection_id="det-1", remediation_strategy=RemediationStrategy.CODE_FIX, subject="quipu-api"),
    )
    defaults.update(overrides)
    return ResolutionResult(**defaults)


def test_valid_resolution_result():
    resolution = make_resolution()
    assert resolution.remediation_strategy == RemediationStrategy.CODE_FIX
    assert resolution.target_agent == "codegen_agent"


def test_valid_escalation_resolution():
    resolution = make_resolution(
        remediation_strategy=RemediationStrategy.ESCALATE,
        target_agent=None,
        escalation_recommended=True,
        risk=RemediationRisk.HIGH,
        root_cause_confidence=0.3,
        supporting_signal_ids=[],
        fingerprint=compute_resolution_fingerprint(detection_id="det-1", remediation_strategy=RemediationStrategy.ESCALATE, subject="quipu-api"),
    )
    assert resolution.remediation_strategy == RemediationStrategy.ESCALATE
    assert resolution.target_agent is None


def test_valid_rollback_resolution_with_target():
    resolution = make_resolution(
        remediation_strategy=RemediationStrategy.ROLLBACK,
        target_agent="deployment_agent",
        rollback_target="quipu-api-00006",
        fingerprint=compute_resolution_fingerprint(detection_id="det-1", remediation_strategy=RemediationStrategy.ROLLBACK, subject="quipu-api"),
    )
    assert resolution.rollback_target == "quipu-api-00006"


def test_invalid_remediation_strategy_rejected():
    with pytest.raises(ValidationError):
        make_resolution(remediation_strategy="execute_shell")


def test_invalid_risk_rejected():
    with pytest.raises(ValidationError):
        make_resolution(risk="extreme")


def test_configuration_change_is_not_a_valid_strategy():
    """No safe execution path exists for configuration mutation — the
    enum deliberately has no such value."""
    with pytest.raises(ValidationError):
        make_resolution(remediation_strategy="configuration_change")


def test_root_cause_confidence_bounds_enforced():
    with pytest.raises(ValidationError):
        make_resolution(root_cause_confidence=1.5)
    with pytest.raises(ValidationError):
        make_resolution(root_cause_confidence=-0.1)


def test_root_cause_confidence_boundary_values_accepted():
    assert make_resolution(root_cause_confidence=0.0).root_cause_confidence == 0.0
    assert make_resolution(root_cause_confidence=1.0).root_cause_confidence == 1.0


def test_risk_is_separate_from_severity_and_confidence():
    """A HIGH severity, low-confidence, HIGH-risk combination is
    representable — the model does not conflate these three fields."""
    resolution = make_resolution(severity=SignalSeverity.CRITICAL, root_cause_confidence=0.4, risk=RemediationRisk.HIGH)
    assert resolution.severity == SignalSeverity.CRITICAL
    assert resolution.root_cause_confidence == 0.4
    assert resolution.risk == RemediationRisk.HIGH


def test_lineage_to_detection_id_preserved():
    resolution = make_resolution(detection_id="det-42")
    assert resolution.detection_id == "det-42"


def test_empty_detection_id_rejected():
    with pytest.raises(ValidationError):
        make_resolution(detection_id="")


def test_supporting_signal_and_artifact_ids_preserved():
    resolution = make_resolution(supporting_signal_ids=["sig-1"], supporting_artifact_ids=["art-1"])
    assert resolution.supporting_signal_ids == ["sig-1"]
    assert resolution.supporting_artifact_ids == ["art-1"]


def test_supporting_ids_default_empty():
    resolution = make_resolution(supporting_signal_ids=[], supporting_artifact_ids=[])
    assert resolution.supporting_signal_ids == []
    assert resolution.supporting_artifact_ids == []


def test_naive_resolved_at_rejected():
    with pytest.raises(ValidationError):
        make_resolution(resolved_at=datetime.now())  # noqa: DTZ005 — deliberately naive


def test_resolved_at_defaults_to_aware_utc():
    resolution = make_resolution()
    assert resolution.resolved_at.tzinfo is not None


def test_empty_diagnosis_summary_rejected():
    with pytest.raises(ValidationError):
        make_resolution(diagnosis_summary="  ")


def test_empty_verification_strategy_rejected():
    with pytest.raises(ValidationError):
        make_resolution(verification_strategy="")


def test_empty_fingerprint_rejected():
    with pytest.raises(ValidationError):
        make_resolution(fingerprint="")


def test_root_cause_candidates_optional():
    resolution = make_resolution(root_cause_candidates=["cause A", "cause B"])
    assert resolution.root_cause_candidates == ["cause A", "cause B"]


def test_knowledge_references_default_empty():
    assert make_resolution().knowledge_references == []


def test_severity_reuses_signal_severity_enum():
    resolution = make_resolution(severity=SignalSeverity.WARNING)
    assert isinstance(resolution.severity, SignalSeverity)


def test_created_by_defaults_to_incident_resolution_agent():
    assert make_resolution().created_by == "incident_resolution_agent"


# ---- Fingerprint / dedup contract ---------------------------------------


def test_fingerprint_deterministic_for_same_inputs():
    a = compute_resolution_fingerprint(detection_id="det-1", remediation_strategy=RemediationStrategy.CODE_FIX, subject="quipu-api")
    b = compute_resolution_fingerprint(detection_id="det-1", remediation_strategy=RemediationStrategy.CODE_FIX, subject="quipu-api")
    assert a == b


def test_fingerprint_differs_for_different_detection_id():
    a = compute_resolution_fingerprint(detection_id="det-1", remediation_strategy=RemediationStrategy.CODE_FIX, subject="quipu-api")
    b = compute_resolution_fingerprint(detection_id="det-2", remediation_strategy=RemediationStrategy.CODE_FIX, subject="quipu-api")
    assert a != b


def test_fingerprint_differs_for_different_strategy():
    a = compute_resolution_fingerprint(detection_id="det-1", remediation_strategy=RemediationStrategy.CODE_FIX, subject="quipu-api")
    b = compute_resolution_fingerprint(detection_id="det-1", remediation_strategy=RemediationStrategy.ESCALATE, subject="quipu-api")
    assert a != b


def test_fingerprint_is_a_hex_sha256_digest():
    fingerprint = compute_resolution_fingerprint(detection_id="det-1", remediation_strategy=RemediationStrategy.CODE_FIX, subject="quipu-api")
    assert len(fingerprint) == 64
    int(fingerprint, 16)


def test_fingerprint_is_a_distinct_function_from_signal_and_detection_fingerprints():
    """Level 3.3 §27: resolution identity must not reuse Signal's or
    Detection's fingerprint functions."""
    from app.domain.detection import compute_detection_fingerprint
    from app.domain.signal import compute_fingerprint as signal_fingerprint

    assert compute_resolution_fingerprint is not signal_fingerprint
    assert compute_resolution_fingerprint is not compute_detection_fingerprint


# ---- Distinct from Signal/Detection/Artifact/Ticket -------------------------


def test_resolution_result_is_distinct_model():
    from app.domain import Artifact, DetectionResult, Signal, Ticket

    assert ResolutionResult is not Signal
    assert ResolutionResult is not DetectionResult
    assert ResolutionResult is not Artifact
    assert ResolutionResult is not Ticket
    assert "artifact_type" not in ResolutionResult.model_fields
    assert "ticket_id" not in ResolutionResult.model_fields


# ---- Level 3.6: workflow_id (Incident Resolution -> Remediation) ------------


def test_workflow_id_defaults_none():
    resolution = make_resolution()
    assert resolution.workflow_id is None


def test_workflow_id_can_be_set():
    resolution = make_resolution(workflow_id="wf-123")
    assert resolution.workflow_id == "wf-123"
