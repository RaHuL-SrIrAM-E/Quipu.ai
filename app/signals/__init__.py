"""Signal ingestion — source adapters and sanitization for Level 3's Signal
architecture. Depends on app.domain only; no Google SDK imports anywhere in
this package (adapters normalize already-received payloads/artifacts, they
do not call any provider API — see app/signals/adapters.py module docstring
for exactly what's real vs. a documented future integration).

Signal persistence lives in app.persistence (SignalRepository), matching
every other domain concept — this package is only the normalization layer
between a source-specific payload and a Signal instance.
"""

from app.signals.adapters import (
    CloudLoggingEntryAdapter,
    CloudMonitoringAlertAdapter,
    CloudMonitoringMetricAdapter,
    CloudRunDeploymentAdapter,
    CustomerFeedbackAdapter,
    SignalSourceAdapter,
    SupportFeedbackAdapter,
    UserBehaviorAdapter,
    normalize_cloud_logging_entry,
    normalize_cloud_monitoring_alert,
    normalize_cloud_monitoring_metric_observation,
    normalize_cloud_run_deployment,
    normalize_customer_feedback,
    normalize_support_feedback,
    normalize_user_behavior,
)
from app.signals.sanitize import sanitize_metadata

__all__ = [
    "CloudLoggingEntryAdapter",
    "CloudMonitoringAlertAdapter",
    "CloudMonitoringMetricAdapter",
    "CloudRunDeploymentAdapter",
    "CustomerFeedbackAdapter",
    "SignalSourceAdapter",
    "SupportFeedbackAdapter",
    "UserBehaviorAdapter",
    "normalize_cloud_logging_entry",
    "normalize_cloud_monitoring_alert",
    "normalize_cloud_monitoring_metric_observation",
    "normalize_cloud_run_deployment",
    "normalize_customer_feedback",
    "normalize_support_feedback",
    "normalize_user_behavior",
    "sanitize_metadata",
]
