"""Domain-level tests for DetectionResult (Level 3.2). No persistence, no
agent — just the model and its fingerprint helper."""

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from app.domain import DetectionDomain, DetectionResult, DetectionType, SignalSeverity, compute_detection_fingerprint


def make_detection(**overrides) -> DetectionResult:
    defaults = dict(
        detection_type=DetectionType.INCIDENT,
        domain=DetectionDomain.OPERATIONAL,
        title="Probable incident",
        summary="Error rate spiked after deployment",
        rationale="Errors and latency rose together shortly after rev-42 deployed.",
        confidence=0.9,
        severity=SignalSeverity.CRITICAL,
        subject="quipu-api",
        supporting_signal_ids=["sig-1", "sig-2"],
        observation_window_minutes=15,
        fingerprint=compute_detection_fingerprint(
            detection_type=DetectionType.INCIDENT, subject="quipu-api", supporting_signal_ids=["sig-1", "sig-2"], window_minutes=15
        ),
    )
    defaults.update(overrides)
    return DetectionResult(**defaults)


def test_valid_operational_detection():
    detection = make_detection()
    assert detection.detection_type == DetectionType.INCIDENT
    assert detection.domain == DetectionDomain.OPERATIONAL


def test_valid_product_detection():
    detection = make_detection(
        detection_type=DetectionType.FEATURE_OPPORTUNITY,
        domain=DetectionDomain.PRODUCT,
        severity=None,
        subject="export",
        supporting_signal_ids=["sig-a", "sig-b", "sig-c"],
        fingerprint=compute_detection_fingerprint(
            detection_type=DetectionType.FEATURE_OPPORTUNITY, subject="export", supporting_signal_ids=["sig-a", "sig-b", "sig-c"], window_minutes=15
        ),
    )
    assert detection.detection_type == DetectionType.FEATURE_OPPORTUNITY
    assert detection.severity is None


def test_valid_no_action_detection():
    detection = make_detection(detection_type=DetectionType.NO_ACTION, confidence=0.0, supporting_signal_ids=[], severity=None)
    assert detection.detection_type == DetectionType.NO_ACTION
    assert detection.supporting_signal_ids == []


def test_invalid_detection_type_rejected():
    with pytest.raises(ValidationError):
        make_detection(detection_type="not_a_real_type")


def test_invalid_domain_rejected():
    with pytest.raises(ValidationError):
        make_detection(domain="not_a_real_domain")


def test_confidence_bounds_enforced():
    with pytest.raises(ValidationError):
        make_detection(confidence=1.5)
    with pytest.raises(ValidationError):
        make_detection(confidence=-0.1)


def test_confidence_boundary_values_accepted():
    assert make_detection(confidence=0.0).confidence == 0.0
    assert make_detection(confidence=1.0).confidence == 1.0


def test_supporting_signal_ids_preserved():
    detection = make_detection(supporting_signal_ids=["sig-1", "sig-2", "sig-3"])
    assert detection.supporting_signal_ids == ["sig-1", "sig-2", "sig-3"]


def test_supporting_signal_ids_can_be_empty_for_no_action():
    detection = make_detection(detection_type=DetectionType.NO_ACTION, supporting_signal_ids=[])
    assert detection.supporting_signal_ids == []


def test_naive_detected_at_rejected():
    with pytest.raises(ValidationError):
        make_detection(detected_at=datetime.now())  # noqa: DTZ005 — deliberately naive


def test_detected_at_defaults_to_aware_utc():
    detection = make_detection()
    assert detection.detected_at.tzinfo is not None


def test_empty_title_rejected():
    with pytest.raises(ValidationError):
        make_detection(title="  ")


def test_empty_rationale_rejected():
    with pytest.raises(ValidationError):
        make_detection(rationale="")


def test_empty_fingerprint_rejected():
    with pytest.raises(ValidationError):
        make_detection(fingerprint="")


def test_observation_window_must_be_positive():
    with pytest.raises(ValidationError):
        make_detection(observation_window_minutes=0)


def test_knowledge_references_default_empty():
    detection = make_detection()
    assert detection.knowledge_references == []


def test_knowledge_references_can_be_set():
    detection = make_detection(knowledge_references=["doc-1", "doc-2"])
    assert detection.knowledge_references == ["doc-1", "doc-2"]


def test_provenance_via_created_by_defaults_to_detecting_agent():
    detection = make_detection()
    assert detection.created_by == "detecting_agent"


def test_severity_reuses_signal_severity_enum():
    """No second severity vocabulary was introduced."""
    detection = make_detection(severity=SignalSeverity.WARNING)
    assert isinstance(detection.severity, SignalSeverity)


# ---- Fingerprint / dedup contract ---------------------------------------


def test_fingerprint_deterministic_for_same_inputs():
    a = compute_detection_fingerprint(detection_type=DetectionType.INCIDENT, subject="quipu-api", supporting_signal_ids=["sig-1", "sig-2"], window_minutes=15)
    b = compute_detection_fingerprint(detection_type=DetectionType.INCIDENT, subject="quipu-api", supporting_signal_ids=["sig-2", "sig-1"], window_minutes=15)
    assert a == b  # order-independent — sorted internally


def test_fingerprint_differs_for_different_signal_sets():
    a = compute_detection_fingerprint(detection_type=DetectionType.INCIDENT, subject="quipu-api", supporting_signal_ids=["sig-1"], window_minutes=15)
    b = compute_detection_fingerprint(detection_type=DetectionType.INCIDENT, subject="quipu-api", supporting_signal_ids=["sig-1", "sig-2"], window_minutes=15)
    assert a != b


def test_fingerprint_differs_for_different_detection_type():
    a = compute_detection_fingerprint(detection_type=DetectionType.INCIDENT, subject="quipu-api", supporting_signal_ids=["sig-1"], window_minutes=15)
    b = compute_detection_fingerprint(detection_type=DetectionType.FEATURE_OPPORTUNITY, subject="quipu-api", supporting_signal_ids=["sig-1"], window_minutes=15)
    assert a != b


def test_fingerprint_differs_for_different_subject():
    a = compute_detection_fingerprint(detection_type=DetectionType.INCIDENT, subject="quipu-api", supporting_signal_ids=["sig-1"], window_minutes=15)
    b = compute_detection_fingerprint(detection_type=DetectionType.INCIDENT, subject="quipu-worker", supporting_signal_ids=["sig-1"], window_minutes=15)
    assert a != b


def test_fingerprint_is_a_hex_sha256_digest():
    fingerprint = compute_detection_fingerprint(detection_type=DetectionType.INCIDENT, subject="quipu-api", supporting_signal_ids=["sig-1"], window_minutes=15)
    assert len(fingerprint) == 64
    int(fingerprint, 16)


def test_fingerprint_is_a_distinct_function_from_signal_fingerprint():
    """Level 3.2 §21/§7: detection identity must NOT reuse or mutate
    Signal's own compute_fingerprint."""
    from app.domain.signal import compute_fingerprint as signal_fingerprint

    assert compute_detection_fingerprint is not signal_fingerprint


# ---- Detection is distinct from Signal/Artifact/Ticket ---------------------


def test_detection_result_is_distinct_model():
    from app.domain import Artifact, Signal, Ticket

    assert DetectionResult is not Signal
    assert DetectionResult is not Artifact
    assert DetectionResult is not Ticket
    assert "artifact_type" not in DetectionResult.model_fields
    assert "ticket_id" not in DetectionResult.model_fields


def test_detection_result_has_no_signal_mutation_surface():
    """DetectionResult never embeds a Signal object directly — only
    signal_id string references (supporting_signal_ids: list[str]) and the
    reused SignalSeverity enum (a value type, not the Signal model itself)."""
    from app.domain import Signal

    for field in DetectionResult.model_fields.values():
        assert field.annotation is not Signal
        assert field.annotation != list[Signal]
