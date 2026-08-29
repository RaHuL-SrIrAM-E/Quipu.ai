"""Classified ingestion failures. The category determines ack policy (see
app/eventing/ingestion_service.py) — this is the whole dead-letter/poison-
message defense for this task: permanent failures are acknowledged (so a
single malformed message can never wedge a subscription in an infinite
redelivery loop) while transient failures are left unacknowledged so
Pub/Sub redelivers them once the underlying condition (e.g. Firestore
briefly unavailable) clears.

A real dead-letter topic (configured on the Pub/Sub subscription itself,
via `pubsub_dead_letter_topic` in app.config.Settings) is an orthogonal,
optional deployment-time policy for messages that exceed Pub/Sub's own
max-delivery-attempts — this module doesn't need to know whether one is
configured; it only needs to classify retryable vs. not.
"""

from enum import StrEnum


class IngestionFailureCategory(StrEnum):
    MALFORMED_ENVELOPE = "malformed_envelope"
    PAYLOAD_TOO_LARGE = "payload_too_large"
    UNSUPPORTED_SOURCE = "unsupported_source"
    UNSUPPORTED_EVENT_TYPE = "unsupported_event_type"
    NORMALIZATION_FAILURE = "normalization_failure"
    PERSISTENCE_FAILURE = "persistence_failure"
    TRANSIENT_FAILURE = "transient_failure"


# Permanent: never retryable. The message is acknowledged (dropped) per
# documented policy rather than redelivered forever.
_PERMANENT = frozenset(
    {
        IngestionFailureCategory.MALFORMED_ENVELOPE,
        IngestionFailureCategory.PAYLOAD_TOO_LARGE,
        IngestionFailureCategory.UNSUPPORTED_SOURCE,
        IngestionFailureCategory.UNSUPPORTED_EVENT_TYPE,
        IngestionFailureCategory.NORMALIZATION_FAILURE,
    }
)


class IngestionError(Exception):
    def __init__(self, category: IngestionFailureCategory, message: str):
        self.category = category
        self.retryable = category not in _PERMANENT
        super().__init__(message)
