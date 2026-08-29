"""Signal — an observed piece of evidence from an external or internal
source. Framework-independent (no Google SDK imports here — see
app/signals/adapters.py for the provider-specific normalization that
produces these).

Signal is deliberately NOT a diagnosis, an incident, or a feature request:

    Signal        = "what was observed"                (this module)
    Candidate     = "what Quipu believes it may mean"   (future Detecting Agent)
    Ticket/Incident = "what the org decided to act on"  (existing Ticket model)

A Signal is treated as immutable evidence once persisted — see
docs/architecture/signal_platform.md "Signal lifecycle". `status` tracks
ingestion-pipeline progress only, never Detecting's interpretation.

Newer datetime convention than Artifact/Ticket (Level 1.1, which default to
naive `datetime.utcnow()`): every Signal timestamp is timezone-aware UTC,
per the Level 3 task's explicit instruction not to introduce more naive
datetime behavior. This module intentionally does not follow the older
models' convention.
"""

import hashlib
import uuid
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field, field_validator

from app.domain.enums import SignalSeverity, SignalSource, SignalStatus, SignalType


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _require_aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("timestamp must be timezone-aware (UTC)")
    return value


def compute_fingerprint(*, source: SignalSource, source_event_id: str | None, subject: str, window: str | None = None) -> str:
    """Deterministic dedup identity for a Signal. Same (source,
    source_event_id, subject, window) always produces the same fingerprint
    — callers (future ingestion pipeline) use this to check
    SignalRepository.find_by_fingerprint() before persisting, rather than
    this module enforcing dedup itself. `window` lets a caller fold repeated
    observations within a time bucket (e.g. "the same metric anomaly,
    reported every minute for an hour") into one fingerprint when that's the
    intended dedup granularity — omit it for sources with a stable
    source_event_id.

    Not a distributed dedup engine — just the contract boundary the task
    asked for. See docs/architecture/signal_platform.md "Deduplication".
    """
    basis = "|".join([source.value, source_event_id or "", subject, window or ""])
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()


class SignalProvenance(BaseModel):
    """Where a Signal's evidence can be traced back to. Intentionally small:
    enough to investigate the original event without turning Signal into a
    data dump. `source_uri`/`raw_reference` should point at the origin (a
    console URL, a log entry insertId, a feedback record id) — never embed
    raw secrets or full sensitive payloads here; see
    app/signals/sanitize.py, used by every adapter before evidence reaches
    this model."""

    source_system: str
    source_event_id: str | None = None
    source_uri: str | None = None
    trace_id: str | None = None
    collected_at: datetime = Field(default_factory=_utc_now)

    _validate_collected_at = field_validator("collected_at")(_require_aware)

    @field_validator("source_system")
    @classmethod
    def _not_empty(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("source_system must not be empty")
        return value.strip()


class Signal(BaseModel):
    signal_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    signal_type: SignalType
    source: SignalSource
    severity: SignalSeverity
    status: SignalStatus = SignalStatus.AVAILABLE

    # observed_at: when the underlying event happened, per the source.
    # ingested_at: when Quipu itself received/normalized it. These can
    # differ (e.g. a batch feedback import observed hours earlier).
    observed_at: datetime
    ingested_at: datetime = Field(default_factory=_utc_now)

    subject: str  # the entity/service/feature this concerns, human-readable
    summary: str  # short factual description of what was observed — not a diagnosis

    # Deployment/service correlation (Level 3 §19) — optional, since product
    # signals rarely have a service_name/revision at all.
    service_name: str | None = None
    environment: str | None = None
    deployment_artifact_id: str | None = None  # correlates to a DeploymentArtifact (app.domain.Artifact)
    revision: str | None = None

    # Normalized, sanitized evidence — the adapter's translation of the
    # source-specific payload into Signal's common shape. `metadata` is for
    # supplementary context that doesn't fit the typed fields above. Both
    # are expected to already be sanitized by the adapter (see
    # app/signals/sanitize.py) before a Signal is constructed — this model
    # does not re-sanitize, since it doesn't know which fields are
    # sensitive for an arbitrary source.
    evidence: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)

    provenance: SignalProvenance
    fingerprint: str

    _validate_observed_at = field_validator("observed_at")(_require_aware)
    _validate_ingested_at = field_validator("ingested_at")(_require_aware)

    @field_validator("subject", "summary", "fingerprint")
    @classmethod
    def _not_empty(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("must not be empty")
        return value.strip()
