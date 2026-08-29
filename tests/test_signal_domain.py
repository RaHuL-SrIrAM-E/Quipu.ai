"""Domain-level tests for Signal (Level 3). No persistence, no adapters —
just the model itself."""

from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from app.domain import Signal, SignalProvenance, SignalSeverity, SignalSource, SignalStatus, SignalType, compute_fingerprint


def make_provenance(**overrides) -> SignalProvenance:
    defaults = dict(source_system="cloud_monitoring", source_event_id="evt-1")
    defaults.update(overrides)
    return SignalProvenance(**defaults)


def make_signal(**overrides) -> Signal:
    defaults = dict(
        signal_type=SignalType.METRIC_ANOMALY,
        source=SignalSource.CLOUD_MONITORING,
        severity=SignalSeverity.WARNING,
        observed_at=datetime.now(timezone.utc),
        subject="quipu-api",
        summary="error rate increased",
        provenance=make_provenance(),
        fingerprint=compute_fingerprint(source=SignalSource.CLOUD_MONITORING, source_event_id="evt-1", subject="quipu-api"),
    )
    defaults.update(overrides)
    return Signal(**defaults)


def test_valid_signal_constructs():
    signal = make_signal()
    assert signal.status == SignalStatus.AVAILABLE
    assert signal.signal_id


def test_signal_defaults_to_available_status():
    assert make_signal().status == SignalStatus.AVAILABLE


def test_invalid_signal_type_rejected():
    with pytest.raises(ValidationError):
        make_signal(signal_type="not_a_real_type")


def test_invalid_source_rejected():
    with pytest.raises(ValidationError):
        make_signal(source="not_a_real_source")


def test_invalid_severity_rejected():
    with pytest.raises(ValidationError):
        make_signal(severity="not_a_real_severity")


def test_empty_subject_rejected():
    with pytest.raises(ValidationError):
        make_signal(subject="   ")


def test_empty_summary_rejected():
    with pytest.raises(ValidationError):
        make_signal(summary="")


def test_empty_fingerprint_rejected():
    with pytest.raises(ValidationError):
        make_signal(fingerprint="")


def test_naive_observed_at_rejected():
    with pytest.raises(ValidationError):
        make_signal(observed_at=datetime.now())  # noqa: DTZ005 — deliberately naive, testing rejection


def test_naive_ingested_at_rejected():
    with pytest.raises(ValidationError):
        make_signal(ingested_at=datetime.now())  # noqa: DTZ005


def test_ingested_at_defaults_to_aware_utc():
    signal = make_signal()
    assert signal.ingested_at.tzinfo is not None


def test_provenance_requires_non_empty_source_system():
    with pytest.raises(ValidationError):
        make_provenance(source_system="")


def test_provenance_naive_collected_at_rejected():
    with pytest.raises(ValidationError):
        make_provenance(collected_at=datetime.now())  # noqa: DTZ005


def test_provenance_optional_fields_default_none():
    provenance = make_provenance()
    assert provenance.source_uri is None
    assert provenance.trace_id is None


def test_evidence_and_metadata_default_to_empty_dict():
    signal = make_signal()
    assert signal.evidence == {}
    assert signal.metadata == {}


def test_deployment_correlation_fields_optional_and_settable():
    signal = make_signal(service_name="quipu-api", environment="production", deployment_artifact_id="artifact-1", revision="rev-1")
    assert signal.service_name == "quipu-api"
    assert signal.deployment_artifact_id == "artifact-1"


def test_deployment_correlation_fields_default_none():
    signal = make_signal()
    assert signal.service_name is None
    assert signal.deployment_artifact_id is None
    assert signal.revision is None


# ---- Fingerprint / dedup contract ---------------------------------------


def test_fingerprint_deterministic_for_same_inputs():
    a = compute_fingerprint(source=SignalSource.CLOUD_LOGGING, source_event_id="log-1", subject="quipu-api")
    b = compute_fingerprint(source=SignalSource.CLOUD_LOGGING, source_event_id="log-1", subject="quipu-api")
    assert a == b


def test_fingerprint_differs_for_different_source_event_id():
    a = compute_fingerprint(source=SignalSource.CLOUD_LOGGING, source_event_id="log-1", subject="quipu-api")
    b = compute_fingerprint(source=SignalSource.CLOUD_LOGGING, source_event_id="log-2", subject="quipu-api")
    assert a != b


def test_fingerprint_differs_for_different_source():
    a = compute_fingerprint(source=SignalSource.CLOUD_LOGGING, source_event_id="evt-1", subject="quipu-api")
    b = compute_fingerprint(source=SignalSource.CLOUD_MONITORING, source_event_id="evt-1", subject="quipu-api")
    assert a != b


def test_fingerprint_window_distinguishes_repeated_observations():
    a = compute_fingerprint(source=SignalSource.CLOUD_MONITORING, source_event_id=None, subject="quipu-api", window="2026-01-01T00")
    b = compute_fingerprint(source=SignalSource.CLOUD_MONITORING, source_event_id=None, subject="quipu-api", window="2026-01-01T01")
    assert a != b


def test_fingerprint_is_a_hex_sha256_digest():
    fingerprint = compute_fingerprint(source=SignalSource.CLOUD_LOGGING, source_event_id="log-1", subject="quipu-api")
    assert len(fingerprint) == 64
    int(fingerprint, 16)  # raises ValueError if not valid hex


# ---- Signal is not a diagnosis / does not carry interpretation ----------


def test_signal_model_has_no_interpretation_fields():
    """Signal must not carry Detecting's future interpretation — no
    'diagnosis', 'root_cause', 'candidate_id', 'incident_id' field exists."""
    fields = set(Signal.model_fields)
    forbidden = {"diagnosis", "root_cause", "candidate_id", "incident_id", "candidate_type", "resolution"}
    assert fields.isdisjoint(forbidden)


def test_signal_is_distinct_model_from_artifact_and_ticket():
    from app.domain import Artifact, Ticket

    assert Signal is not Artifact
    assert Signal is not Ticket
    assert "artifact_type" not in Signal.model_fields
    assert "ticket_id" not in Signal.model_fields
