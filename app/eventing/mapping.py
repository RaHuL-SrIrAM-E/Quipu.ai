"""The Pub/Sub envelope -> existing adapter dispatch table. A fixed
allow-list keyed on (SignalSource, IngestionEventType) — never
payload-controlled dynamic dispatch (no arbitrary class/function loading
from message content). Every target is one of the existing
app.signals.adapters `normalize_*` functions; this module adds no new
normalization logic of its own, per the task's mandatory-reuse requirement.

`normalize_cloud_run_deployment` is deliberately NOT included: it takes a
live app.domain.Artifact (Quipu's own already-persisted deployment state),
not an external payload dict, so it has no Pub/Sub envelope shape to route
to — see docs/architecture/pubsub_signal_ingestion.md "What Pub/Sub does
NOT do". Cloud Run deployment signals continue to be produced the existing
way (directly from DeploymentAgent's artifact), not through this ingestion
path.
"""

from typing import Any, Callable

from app.domain import Signal, SignalSource
from app.eventing.envelope import IngestionEventType
from app.signals.adapters import (
    normalize_cloud_logging_entry,
    normalize_cloud_monitoring_alert,
    normalize_cloud_monitoring_metric_observation,
    normalize_customer_feedback,
    normalize_support_feedback,
    normalize_user_behavior,
)

_AdapterFn = Callable[[dict[str, Any]], Signal]

ADAPTER_MAPPING: dict[tuple[SignalSource, IngestionEventType], _AdapterFn] = {
    (SignalSource.CLOUD_MONITORING, IngestionEventType.ALERT): normalize_cloud_monitoring_alert,
    (SignalSource.CLOUD_MONITORING, IngestionEventType.METRIC_OBSERVATION): normalize_cloud_monitoring_metric_observation,
    (SignalSource.CLOUD_LOGGING, IngestionEventType.LOG_ENTRY): normalize_cloud_logging_entry,
    (SignalSource.CUSTOMER_FEEDBACK, IngestionEventType.FEEDBACK): normalize_customer_feedback,
    (SignalSource.SUPPORT_SYSTEM, IngestionEventType.FEEDBACK): normalize_support_feedback,
    (SignalSource.USER_BEHAVIOR, IngestionEventType.PATTERN): normalize_user_behavior,
}

SUPPORTED_SOURCES: frozenset[SignalSource] = frozenset(source for source, _ in ADAPTER_MAPPING)


def resolve_adapter(source: SignalSource, event_type: IngestionEventType) -> _AdapterFn | None:
    return ADAPTER_MAPPING.get((source, event_type))
