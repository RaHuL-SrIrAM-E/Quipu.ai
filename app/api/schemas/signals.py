"""Signal-facing response schemas. Signal.evidence/metadata are already
sanitized at ingestion time (app.signals.sanitize.sanitize_metadata — see
docs/architecture/signal_platform.md) before ever being persisted, but
this API still applies its own additional narrowing on top (§4/Invariant
9 of the task: raw telemetry/customer payloads are never exposed by
default): the list view carries no evidence/metadata at all, and the
detail view exposes the already-sanitized `evidence` dict but never the
free-form `metadata` bucket (which can carry things like an anonymized
customer_ref that still shouldn't be default-visible over HTTP).
"""

from datetime import datetime
from typing import Any

from pydantic import BaseModel

from app.domain import Signal


class SignalSummary(BaseModel):
    signal_id: str
    signal_type: str
    source: str
    severity: str
    status: str
    observed_at: datetime
    subject: str
    summary: str
    service_name: str | None
    environment: str | None
    revision: str | None

    @classmethod
    def from_domain(cls, signal: Signal) -> "SignalSummary":
        return cls(
            signal_id=signal.signal_id,
            signal_type=signal.signal_type.value,
            source=signal.source.value,
            severity=signal.severity.value,
            status=signal.status.value,
            observed_at=signal.observed_at,
            subject=signal.subject,
            summary=signal.summary,
            service_name=signal.service_name,
            environment=signal.environment,
            revision=signal.revision,
        )


class SignalDetail(SignalSummary):
    ingested_at: datetime
    deployment_artifact_id: str | None
    # Already-sanitized at ingestion (app.signals.sanitize) — never the raw
    # source payload. The free-form `metadata` bucket is intentionally
    # excluded even here; see module docstring.
    evidence: dict[str, Any]
    source_system: str
    source_uri: str | None
    trace_id: str | None

    @classmethod
    def from_domain(cls, signal: Signal) -> "SignalDetail":
        base = SignalSummary.from_domain(signal)
        return cls(
            **base.model_dump(),
            ingested_at=signal.ingested_at,
            deployment_artifact_id=signal.deployment_artifact_id,
            evidence=signal.evidence,
            source_system=signal.provenance.source_system,
            source_uri=signal.provenance.source_uri,
            trace_id=signal.provenance.trace_id,
        )
