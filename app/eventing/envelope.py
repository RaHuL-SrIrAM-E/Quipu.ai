"""EventEnvelope — the small, explicit typed shape every Pub/Sub message
body must decode into before ingestion touches it. Deliberately not a
universal schema: `source`/`event_type` are closed enums (an allow-list,
not caller-controlled dispatch — see app/eventing/mapping.py), and
`payload` is left as the adapter-specific dict app.signals.adapters already
knows how to normalize, rather than inventing a second universal shape on
top of the six adapter-specific ones that already exist.

`event_id` is envelope-level correlation/audit metadata only — it is never
used as the Signal dedup key. Deduplication is entirely delegated to the
existing app.domain.signal.compute_fingerprint()/find_by_fingerprint()
mechanism, computed by the adapter from payload-internal, producer-assigned
fields (e.g. a Cloud Monitoring incident_id, a Cloud Logging insertId) —
see app/eventing/ingestion_service.py and
docs/architecture/pubsub_signal_ingestion.md "Deduplication / idempotency".
"""

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, field_validator

from app.domain import SignalSource


class IngestionEventType(StrEnum):
    """Which adapter shape a message's payload matches. A closed set — see
    app/eventing/mapping.py for the (source, event_type) -> adapter
    allow-list this is validated against."""

    ALERT = "alert"
    METRIC_OBSERVATION = "metric_observation"
    LOG_ENTRY = "log_entry"
    FEEDBACK = "feedback"
    PATTERN = "pattern"


class EventEnvelope(BaseModel):
    event_id: str
    source: SignalSource
    event_type: IngestionEventType
    occurred_at: datetime
    subject: str | None = None
    payload: dict[str, Any]
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("event_id")
    @classmethod
    def _event_id_not_empty(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("event_id must not be empty")
        return value.strip()

    @field_validator("occurred_at")
    @classmethod
    def _occurred_at_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("occurred_at must be timezone-aware (UTC)")
        return value

    @field_validator("payload")
    @classmethod
    def _payload_is_dict(cls, value: Any) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise ValueError("payload must be an object")
        return value
