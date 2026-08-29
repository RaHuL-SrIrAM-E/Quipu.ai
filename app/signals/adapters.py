"""Signal source adapters — translate source-specific payloads into the
common Signal contract (Level 3 §9/§10). No provider-transformation logic
lives in app.domain; it all lives here instead.

    source event (dict, or an existing Quipu domain object)
          |
       adapter  (this module)
          |
        Signal

Each adapter is a small, stateless `normalize_...(payload) -> Signal`
function, plus a thin class implementing SignalSourceAdapter so callers can
depend on the Protocol rather than a specific function. None of these call
a live Google API — see the module-level note on each operational adapter
for exactly what's real vs. a documented future integration:

- `CloudRunDeploymentAdapter` is a REAL, fully-functional adapter: it
  normalizes an actual `app.domain.Artifact` (ArtifactType.DEPLOYMENT)
  already produced by DeploymentAgent (app/agents/deployment.py) — no
  external call needed, the evidence already exists in Quipu's own
  persisted state.
- `CloudMonitoringAlertAdapter` / `CloudLoggingEntryAdapter` normalize an
  *already-delivered* payload matching Cloud Monitoring's notification-
  channel webhook schema and Cloud Logging's LogEntry JSON schema,
  respectively (both are real, documented Google payload shapes). Neither
  adapter calls the Cloud Monitoring or Cloud Logging API itself — the
  delivery mechanism (a Pub/Sub push subscription, or a webhook endpoint
  receiving Cloud Monitoring's notification channel POST) is explicitly a
  future integration; building it means an HTTP surface, which Level 3
  explicitly excludes. See docs/architecture/signal_platform.md
  "Source adapters".
- `CustomerFeedbackAdapter` / `SupportFeedbackAdapter` / `UserBehaviorAdapter`
  normalize an internal, Quipu-defined payload shape — there is no external
  product-analytics/support-system SDK integration in this level at all
  (Level 3 §27 explicitly excludes customer data connectors); these exist
  so the product-signal path (§20) has a concrete, testable normalization
  boundary ready for whatever ingestion mechanism a future level adds.

Level 3.1 (MonitoringAgent, app/agents/monitoring.py) adds one more:
`normalize_cloud_monitoring_metric_observation`. This is a genuinely new
normalization function, not a second Signal factory — it uses the exact
same `Signal` model, `sanitize_metadata`, and `compute_fingerprint` as
everything else in this module. It exists because MonitoringAgent's real,
live Cloud Monitoring metric queries (app/core/cloud_monitoring_client.py)
produce a fundamentally different evidence shape than
`normalize_cloud_monitoring_alert` was built for: that function normalizes
an already-delivered *alert notification* (an incident someone else
decided crossed a threshold); this one normalizes a *queried metric
observation* MonitoringAgent computed deterministically from a live
`list_time_series` call — see docs/architecture/monitoring_agent.md.
"""

from datetime import datetime, timezone
from typing import Any, Protocol, runtime_checkable

from app.domain import Artifact, Signal, SignalProvenance, SignalSeverity, SignalSource, SignalType, compute_fingerprint
from app.signals.sanitize import sanitize_metadata


@runtime_checkable
class SignalSourceAdapter(Protocol):
    """The contract every source adapter satisfies. Deliberately one
    method — this is the minimal boundary Level 3 asks for, not a generic
    event framework."""

    def normalize(self, raw_event: Any) -> Signal: ...


def _parse_timestamp(value: Any) -> datetime:
    """Accepts an aware/naive ISO string, a unix epoch (seconds), or a
    datetime — always returns timezone-aware UTC. Raises ValueError on
    anything else, so a malformed source payload fails loudly at the
    adapter boundary rather than producing a Signal with a wrong time."""
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    if isinstance(value, int | float):
        return datetime.fromtimestamp(value, tz=timezone.utc)
    if isinstance(value, str):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)
    raise ValueError(f"cannot parse timestamp from {value!r}")


def _require(payload: dict, *keys: str) -> None:
    missing = [key for key in keys if key not in payload or payload[key] in (None, "")]
    if missing:
        raise ValueError(f"malformed source payload: missing required field(s) {missing}")


# --------------------------------------------------------------------------
# Operational: Cloud Run deployment (real — normalizes Quipu's own artifact)
# --------------------------------------------------------------------------


def normalize_cloud_run_deployment(artifact: Artifact) -> Signal:
    """DeploymentAgent's own DeploymentArtifact, reinterpreted as evidence
    for the operations loop (Level 3 §19: a future Detecting Agent
    correlates this against error-rate/latency signals for the same
    service_name + revision)."""
    payload = artifact.payload
    status = payload.get("status", "unknown")
    severity = SignalSeverity.INFO if status == "succeeded" else SignalSeverity.ERROR
    service_name = payload.get("service_name")
    revision = payload.get("revision")

    evidence = sanitize_metadata(
        {
            "status": status,
            "revision": revision,
            "service_uri": payload.get("service_uri"),
            "strategy": payload.get("strategy"),
            "failure_classification": payload.get("failure_classification"),
            "failure_details": payload.get("failure_details"),
        }
    )

    observed_at = artifact.created_at if artifact.created_at.tzinfo else artifact.created_at.replace(tzinfo=timezone.utc)

    return Signal(
        signal_type=SignalType.DEPLOYMENT_EVENT,
        source=SignalSource.CLOUD_RUN,
        severity=severity,
        observed_at=observed_at,
        subject=service_name or artifact.artifact_id,
        summary=payload.get("deployment_summary") or f"Cloud Run deployment {status}",
        service_name=service_name,
        environment=payload.get("environment"),
        deployment_artifact_id=artifact.artifact_id,
        revision=revision,
        evidence=evidence,
        provenance=SignalProvenance(
            source_system="cloud_run",
            source_event_id=artifact.artifact_id,
            source_uri=payload.get("service_uri"),
            collected_at=observed_at,
        ),
        fingerprint=compute_fingerprint(
            source=SignalSource.CLOUD_RUN, source_event_id=artifact.artifact_id, subject=service_name or artifact.artifact_id
        ),
    )


class CloudRunDeploymentAdapter:
    def normalize(self, raw_event: Artifact) -> Signal:
        return normalize_cloud_run_deployment(raw_event)


# --------------------------------------------------------------------------
# Operational: Cloud Monitoring alert notification payload
# --------------------------------------------------------------------------


def normalize_cloud_monitoring_alert(payload: dict[str, Any]) -> Signal:
    """Expects Cloud Monitoring's notification-channel webhook shape:
    {"incident": {"incident_id", "resource", "state", "started_at"
    (unix seconds), "summary", "policy_name", "url", ...}}. See
    https://cloud.google.com/monitoring/support/notification-options#webhooks.
    """
    incident = payload.get("incident")
    if not isinstance(incident, dict):
        raise ValueError("malformed source payload: missing 'incident' object")
    _require(incident, "incident_id", "started_at", "summary")

    resource = incident.get("resource") or {}
    labels = resource.get("labels") or {}
    service_name = labels.get("service_name")
    condition = incident.get("condition_name", "")
    severity = SignalSeverity.CRITICAL if incident.get("state") == "open" else SignalSeverity.WARNING

    signal_type = SignalType.LATENCY_ANOMALY if "latency" in condition.lower() else SignalType.METRIC_ANOMALY

    evidence = sanitize_metadata(
        {
            "resource_type": resource.get("type"),
            "policy_name": incident.get("policy_name"),
            "condition_name": condition,
            "state": incident.get("state"),
        }
    )

    observed_at = _parse_timestamp(incident["started_at"])

    return Signal(
        signal_type=signal_type,
        source=SignalSource.CLOUD_MONITORING,
        severity=severity,
        observed_at=observed_at,
        subject=service_name or incident["incident_id"],
        summary=str(incident["summary"])[:500],
        service_name=service_name,
        environment=labels.get("environment"),
        revision=labels.get("revision_name"),
        evidence=evidence,
        provenance=SignalProvenance(
            source_system="cloud_monitoring",
            source_event_id=incident["incident_id"],
            source_uri=incident.get("url"),
            collected_at=observed_at,
        ),
        fingerprint=compute_fingerprint(
            source=SignalSource.CLOUD_MONITORING, source_event_id=incident["incident_id"], subject=service_name or incident["incident_id"]
        ),
    )


class CloudMonitoringAlertAdapter:
    def normalize(self, raw_event: dict[str, Any]) -> Signal:
        return normalize_cloud_monitoring_alert(raw_event)


# --------------------------------------------------------------------------
# Operational: Cloud Monitoring metric observation (MonitoringAgent, Level 3.1)
# --------------------------------------------------------------------------


def normalize_cloud_monitoring_metric_observation(payload: dict[str, Any]) -> Signal:
    """Normalizes one deterministic metric observation MonitoringAgent
    computed from a live CloudMonitoringClient query. Expects:
    {"observation_kind": "error_rate"|"latency_p99"|"availability",
    "service_name", "region", "environment", "value", "unit", "window_start",
    "window_end", "severity" (a SignalSeverity value MonitoringAgent already
    computed from its OWN static, configured threshold — never from an LLM),
    "revision"?, "deployment_artifact_id"?, "evidence"? (extra sanitized
    detail, e.g. per-response-class counts)}.

    The severity is NOT decided here — it arrives pre-computed from
    MonitoringAgent's deterministic threshold check (see
    docs/architecture/monitoring_agent.md §10 "Observation vs Anomaly").
    This adapter only maps observation_kind to the existing SignalType
    taxonomy and builds provenance/fingerprint, same as every other
    adapter in this module.
    """
    _require(payload, "observation_kind", "service_name", "region", "value", "window_start", "window_end", "severity")

    kind_to_type = {
        "error_rate": SignalType.METRIC_ANOMALY,
        "latency_p99": SignalType.LATENCY_ANOMALY,
        "availability": SignalType.AVAILABILITY_DEGRADATION,
    }
    observation_kind = payload["observation_kind"]
    if observation_kind not in kind_to_type:
        raise ValueError(f"malformed source payload: unknown observation_kind '{observation_kind}'")

    service_name = payload["service_name"]
    window_start = _parse_timestamp(payload["window_start"])
    window_end = _parse_timestamp(payload["window_end"])

    evidence = sanitize_metadata(
        {
            "observation_kind": observation_kind,
            "value": payload["value"],
            "unit": payload.get("unit"),
            "window_start": window_start.isoformat(),
            "window_end": window_end.isoformat(),
            **(payload.get("evidence") or {}),
        }
    )

    window_key = f"{window_start.isoformat()}/{window_end.isoformat()}"

    return Signal(
        signal_type=kind_to_type[observation_kind],
        source=SignalSource.CLOUD_MONITORING,
        severity=SignalSeverity(payload["severity"]),
        observed_at=window_end,
        subject=service_name,
        summary=f"{observation_kind} = {payload['value']}{payload.get('unit', '')} for {service_name} over {payload['window_start']}..{payload['window_end']}",
        service_name=service_name,
        environment=payload.get("environment"),
        deployment_artifact_id=payload.get("deployment_artifact_id"),
        revision=payload.get("revision"),
        evidence=evidence,
        provenance=SignalProvenance(
            source_system="cloud_monitoring",
            source_event_id=None,
            collected_at=window_end,
        ),
        fingerprint=compute_fingerprint(
            source=SignalSource.CLOUD_MONITORING, source_event_id=f"{observation_kind}:{service_name}", subject=service_name, window=window_key
        ),
    )


class CloudMonitoringMetricAdapter:
    def normalize(self, raw_event: dict[str, Any]) -> Signal:
        return normalize_cloud_monitoring_metric_observation(raw_event)


# --------------------------------------------------------------------------
# Operational: Cloud Logging LogEntry payload
# --------------------------------------------------------------------------


def normalize_cloud_logging_entry(payload: dict[str, Any]) -> Signal:
    """Expects a Cloud Logging LogEntry JSON shape: {"insertId", "timestamp",
    "severity", "resource": {"type", "labels": {...}}, "textPayload" or
    "jsonPayload", "trace"}. See
    https://cloud.google.com/logging/docs/reference/v2/rest/v2/LogEntry."""
    _require(payload, "insertId", "timestamp", "severity")

    resource = payload.get("resource") or {}
    labels = resource.get("labels") or {}
    service_name = labels.get("service_name")
    gcp_severity = str(payload["severity"]).upper()
    severity = SignalSeverity.CRITICAL if gcp_severity in ("CRITICAL", "ALERT", "EMERGENCY") else (
        SignalSeverity.ERROR if gcp_severity == "ERROR" else (SignalSeverity.WARNING if gcp_severity == "WARNING" else SignalSeverity.INFO)
    )

    message = payload.get("textPayload") or str(payload.get("jsonPayload") or "")

    evidence = sanitize_metadata({"severity": gcp_severity, "message": message, "log_name": payload.get("logName")})

    observed_at = _parse_timestamp(payload["timestamp"])

    return Signal(
        signal_type=SignalType.LOG_ERROR if severity in (SignalSeverity.ERROR, SignalSeverity.CRITICAL) else SignalType.APPLICATION_ERROR,
        source=SignalSource.CLOUD_LOGGING,
        severity=severity,
        observed_at=observed_at,
        subject=service_name or payload["insertId"],
        summary=message[:500] if message else f"Cloud Logging entry ({gcp_severity})",
        service_name=service_name,
        environment=labels.get("environment"),
        revision=labels.get("revision_name"),
        evidence=evidence,
        provenance=SignalProvenance(
            source_system="cloud_logging",
            source_event_id=payload["insertId"],
            trace_id=payload.get("trace"),
            collected_at=observed_at,
        ),
        fingerprint=compute_fingerprint(
            source=SignalSource.CLOUD_LOGGING, source_event_id=payload["insertId"], subject=service_name or payload["insertId"]
        ),
    )


class CloudLoggingEntryAdapter:
    def normalize(self, raw_event: dict[str, Any]) -> Signal:
        return normalize_cloud_logging_entry(raw_event)


# --------------------------------------------------------------------------
# Product: customer feedback / support feedback / user behavior
# --------------------------------------------------------------------------


def normalize_customer_feedback(payload: dict[str, Any]) -> Signal:
    """Internal, Quipu-defined shape — no external feedback-tool SDK exists
    in this level. Expects {"feedback_id", "submitted_at", "text",
    "feature_area"?, "customer_ref"? (already-anonymized, e.g. a hashed
    account id — never a raw email/name)}."""
    _require(payload, "feedback_id", "submitted_at", "text")

    evidence = sanitize_metadata({"text": payload["text"], "feature_area": payload.get("feature_area")})
    observed_at = _parse_timestamp(payload["submitted_at"])

    return Signal(
        signal_type=SignalType.CUSTOMER_FEEDBACK,
        source=SignalSource.CUSTOMER_FEEDBACK,
        severity=SignalSeverity.INFO,
        observed_at=observed_at,
        subject=payload.get("feature_area") or "general",
        summary=str(payload["text"])[:500],
        evidence=evidence,
        metadata=sanitize_metadata({"customer_ref": payload.get("customer_ref")}) if payload.get("customer_ref") else {},
        provenance=SignalProvenance(source_system="customer_feedback", source_event_id=payload["feedback_id"], collected_at=observed_at),
        fingerprint=compute_fingerprint(
            source=SignalSource.CUSTOMER_FEEDBACK, source_event_id=payload["feedback_id"], subject=payload.get("feature_area") or "general"
        ),
    )


class CustomerFeedbackAdapter:
    def normalize(self, raw_event: dict[str, Any]) -> Signal:
        return normalize_customer_feedback(raw_event)


def normalize_support_feedback(payload: dict[str, Any]) -> Signal:
    """Internal shape: {"ticket_ref", "submitted_at", "text", "feature_area"?}."""
    _require(payload, "ticket_ref", "submitted_at", "text")

    evidence = sanitize_metadata({"text": payload["text"], "feature_area": payload.get("feature_area")})
    observed_at = _parse_timestamp(payload["submitted_at"])

    return Signal(
        signal_type=SignalType.SUPPORT_FEEDBACK,
        source=SignalSource.SUPPORT_SYSTEM,
        severity=SignalSeverity.INFO,
        observed_at=observed_at,
        subject=payload.get("feature_area") or "general",
        summary=str(payload["text"])[:500],
        evidence=evidence,
        provenance=SignalProvenance(source_system="support_system", source_event_id=payload["ticket_ref"], collected_at=observed_at),
        fingerprint=compute_fingerprint(
            source=SignalSource.SUPPORT_SYSTEM, source_event_id=payload["ticket_ref"], subject=payload.get("feature_area") or "general"
        ),
    )


class SupportFeedbackAdapter:
    def normalize(self, raw_event: dict[str, Any]) -> Signal:
        return normalize_support_feedback(raw_event)


def normalize_user_behavior(payload: dict[str, Any]) -> Signal:
    """Internal, already-aggregated shape — Signal is not a raw event
    stream; product analytics is expected to hand Quipu an already-computed
    pattern, not individual clickstream events. Expects {"pattern_id",
    "observed_at", "pattern", "feature_area", "occurrence_count",
    "window"?}."""
    _require(payload, "pattern_id", "observed_at", "pattern", "feature_area", "occurrence_count")

    signal_type = SignalType.ADOPTION_ANOMALY if payload["pattern"] == "adoption_anomaly" else SignalType.USER_BEHAVIOR
    evidence = sanitize_metadata({"pattern": payload["pattern"], "occurrence_count": payload["occurrence_count"], "window": payload.get("window")})
    observed_at = _parse_timestamp(payload["observed_at"])

    return Signal(
        signal_type=signal_type,
        source=SignalSource.USER_BEHAVIOR,
        severity=SignalSeverity.INFO,
        observed_at=observed_at,
        subject=payload["feature_area"],
        summary=f"{payload['pattern']} observed {payload['occurrence_count']}x for {payload['feature_area']}",
        evidence=evidence,
        provenance=SignalProvenance(source_system="product_analytics", source_event_id=payload["pattern_id"], collected_at=observed_at),
        fingerprint=compute_fingerprint(
            source=SignalSource.USER_BEHAVIOR,
            source_event_id=payload["pattern_id"],
            subject=payload["feature_area"],
            window=payload.get("window"),
        ),
    )


class UserBehaviorAdapter:
    def normalize(self, raw_event: dict[str, Any]) -> Signal:
        return normalize_user_behavior(raw_event)
