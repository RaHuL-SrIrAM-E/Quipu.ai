"""MonitoringAgent — no legacy predecessor, same QuipuAgent shape as every
other Quipu-native agent, but with a real architectural difference: it does
NOT run an internal Gemini/ADK LlmAgent. See §21 in
docs/architecture/monitoring_agent.md for why — in short, Google API
response -> Signal normalization is a mechanical, deterministic
transformation (Level 3.1's own instruction: "do not use Gemini merely for
mechanical API translation"), and whether an observation crosses a static,
configured threshold is an operational collection policy, not a reasoning
task. There is nothing left for an LLM to meaningfully do here. This agent
still follows the QuipuAgent contract exactly (identity, capabilities,
require_capability, AgentExecution/AgentMetrics bookkeeping) — only the
internal "how" differs from Planning/Architecture/Codegen/Testing/Deployment.

Core responsibility: observe, never diagnose.

    Cloud Run telemetry (Cloud Monitoring + Cloud Logging)
          |
    MonitoringAgent   <- THIS FILE
          |
    normalized Signal(s)   (app/signals/adapters.py, reused, not reinvented)
          |
    SignalRepository (via AgentContext.signals)

MonitoringAgent answers "what is happening in the running system," never
"why" — it creates no IncidentCandidate, no FeatureCandidate, modifies no
code, deploys/rolls back nothing. See docs/architecture/monitoring_agent.md
"Observation vs Detection".
"""

import uuid
from datetime import datetime, timezone
from enum import StrEnum

from pydantic import BaseModel, Field, ValidationError, field_validator

from app.agent_runtime.base import QuipuAgent
from app.agent_runtime.capabilities import AgentCapability
from app.agent_runtime.context import AgentContext
from app.agent_runtime.identity import AgentIdentity
from app.config import get_settings
from app.core.cloud_logging_client import CloudLoggingClient, LogEntryResult
from app.core.cloud_monitoring_client import CloudMonitoringClient, MetricPoint
from app.core.google_api_errors import GoogleApiConfigError, GoogleApiError
from app.core.observability import get_logger
from app.domain import (
    AgentError,
    AgentExecution,
    AgentInput,
    AgentMetrics,
    AgentOutput,
    ErrorCategory,
    Signal,
    SignalSeverity,
    WorkflowStatus,
)
from app.signals.adapters import normalize_cloud_logging_entry, normalize_cloud_monitoring_metric_observation

logger = get_logger("quipu.agent.monitoring")


class MonitoringCollectionStatus(StrEnum):
    """The outcome of one Monitoring collection cycle — not a diagnosis of
    the system being monitored, a report on whether Monitoring itself
    could reach its telemetry sources."""

    COMPLETE = "complete"  # every configured source was queried successfully
    PARTIAL = "partial"  # at least one source failed; some Signals may still have been created
    FAILED = "failed"  # every source failed; no evidence was collected


class MonitoringInput(BaseModel):
    """What MonitoringAgent needs to determine what to observe. Parsed from
    AgentInput.context (the existing extension point every agent already
    has — see app.domain.agent_io.AgentInput) rather than a new invocation
    contract.

    service_name=None means environment-wide observation ("monitor
    production") — CloudMonitoringClient/CloudLoggingClient both already
    support an optional service_name (see their module docstrings), so this
    single input model covers both the service-specific and
    environment-wide cases without a second agent.
    """

    service_name: str | None = None
    region: str
    environment: str
    window_minutes: int = Field(default=15, gt=0)
    deployment_artifact_id: str | None = None  # optional Cloud Run correlation (Level 2.1 DeploymentArtifact)
    revision: str | None = None  # optional — usually resolved from deployment_artifact_id instead, see _perform

    @field_validator("region", "environment")
    @classmethod
    def _not_empty(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("must not be empty")
        return value.strip()


class MonitoringOutput(BaseModel):
    """Grounded in actual collected evidence — never a fabricated summary.
    signal_ids/metrics_observed/logs_observed are populated ONLY from what
    CloudMonitoringClient/CloudLoggingClient actually returned; if a query
    returns no data, the corresponding list stays empty rather than this
    output claiming anything about system health."""

    observation_window_minutes: int
    service_name: str | None
    environment: str
    signal_ids: list[str] = Field(default_factory=list)
    metrics_observed: list[str] = Field(default_factory=list)  # e.g. "error_rate", "latency_p99"
    logs_observed_count: int = 0
    collection_status: MonitoringCollectionStatus
    summary: str
    collection_errors: list[str] = Field(default_factory=list)


def _classify_error_rate(error_fraction: float) -> SignalSeverity:
    """Static, configured thresholds — an operational collection policy,
    NOT AI/diagnostic reasoning. See docs/architecture/monitoring_agent.md
    §10 "Observation vs Anomaly": Monitoring reports what a value IS; only
    a human-configured threshold (never an LLM) decides it's worth a higher
    severity, and Detecting (future) is still the one that decides this
    means anything."""
    settings = get_settings()
    if error_fraction >= settings.monitoring_error_rate_critical_threshold:
        return SignalSeverity.CRITICAL
    if error_fraction >= settings.monitoring_error_rate_warning_threshold:
        return SignalSeverity.WARNING
    return SignalSeverity.INFO


class MonitoringAgent(QuipuAgent):
    """Quipu-native Monitoring Agent. Queries Cloud Monitoring/Cloud Logging
    for one Cloud Run service (or an entire environment), normalizes real
    telemetry into Signals via the existing app.signals normalization
    architecture, and persists them through SignalRepository. Produces no
    diagnosis, no incident, no code change, no deployment. Never calls
    another agent.
    """

    def __init__(self, monitoring_client: CloudMonitoringClient | None = None, logging_client: CloudLoggingClient | None = None):
        super().__init__()
        self._monitoring_client = monitoring_client
        self._logging_client = logging_client

    @property
    def identity(self) -> AgentIdentity:
        return AgentIdentity(
            agent_id="monitoring_agent",
            name="Monitoring Agent",
            version="1.0.0",
            description="Observes Cloud Run production telemetry (Cloud Monitoring + Cloud Logging) and normalizes it into Signals.",
        )

    @property
    def capabilities(self) -> set[AgentCapability]:
        return {AgentCapability.READ_MONITORING, AgentCapability.READ_ARTIFACT}

    def _get_monitoring_client(self) -> CloudMonitoringClient:
        if self._monitoring_client is None:
            self._monitoring_client = CloudMonitoringClient()
        return self._monitoring_client

    def _get_logging_client(self) -> CloudLoggingClient:
        if self._logging_client is None:
            self._logging_client = CloudLoggingClient()
        return self._logging_client

    async def _perform(self, agent_input: AgentInput, context: AgentContext) -> AgentOutput:
        # Capability check at agent entry (§20 layer 1). MonitoringAgent has
        # no ADK tool boundary (no internal LlmAgent — see module docstring),
        # so layer 2 doesn't apply here; layer 3 (the implementation
        # boundary) is enforced again just below, immediately before either
        # client is ever touched, so this remains safe even if a future
        # caller invoked _collect_* directly.
        self.require_capability(AgentCapability.READ_MONITORING)

        execution = AgentExecution(
            execution_id=agent_input.execution_id,
            workflow_id=agent_input.workflow_id,
            agent_name=self.identity.agent_id,
            status=WorkflowStatus.RUNNING,
        )
        if context.executions is not None:
            await context.executions.create(execution)

        metrics = AgentMetrics(execution_id=agent_input.execution_id)

        async def _fail(code: str, message: str, category: ErrorCategory, *, recoverable: bool = True) -> AgentOutput:
            error = AgentError(code=code, message=message, category=category, recoverable=recoverable, retryable=recoverable)
            execution.status = WorkflowStatus.FAILED
            execution.completed_at = datetime.now(timezone.utc)
            execution.error = error
            if context.executions is not None:
                await context.executions.update(execution)
            return AgentOutput(execution_id=agent_input.execution_id, status=WorkflowStatus.FAILED, errors=[error], metrics=metrics)

        try:
            monitoring_input = MonitoringInput.model_validate(agent_input.context)
        except ValidationError as exc:
            return await _fail("MONITORING_INPUT_INVALID", str(exc), ErrorCategory.VALIDATION)

        settings = get_settings()
        if monitoring_input.region not in settings.cloud_run_allowed_regions:
            return await _fail(
                "MONITORING_REGION_NOT_ALLOWED",
                f"'{monitoring_input.region}' is not in the configured allowed region list",
                ErrorCategory.VALIDATION,
            )
        if monitoring_input.environment not in settings.cloud_run_allowed_environments:
            return await _fail(
                "MONITORING_ENVIRONMENT_NOT_ALLOWED",
                f"'{monitoring_input.environment}' is not in the configured allowed environment list",
                ErrorCategory.VALIDATION,
            )
        if monitoring_input.window_minutes > settings.monitoring_max_window_minutes:
            return await _fail(
                "MONITORING_WINDOW_TOO_LARGE",
                f"window_minutes={monitoring_input.window_minutes} exceeds the configured ceiling of {settings.monitoring_max_window_minutes}",
                ErrorCategory.VALIDATION,
            )
        if context.signals is None:
            return await _fail("MONITORING_SIGNAL_GATEWAY_MISSING", "AgentContext.signals is not configured", ErrorCategory.INTERNAL)

        revision = monitoring_input.revision
        if monitoring_input.deployment_artifact_id is not None and revision is None:
            self.require_capability(AgentCapability.READ_ARTIFACT)
            deployment_artifact = await context.artifacts.get(agent_input.workflow_id, monitoring_input.deployment_artifact_id)
            if deployment_artifact is not None:
                revision = deployment_artifact.payload.get("revision")

        created_signals: list[Signal] = []
        metrics_observed: list[str] = []
        collection_errors: list[str] = []

        try:
            await self._collect_metrics(
                monitoring_input=monitoring_input,
                revision=revision,
                created_signals=created_signals,
                metrics_observed=metrics_observed,
                context=context,
                granted=self.capabilities,
            )
        except GoogleApiError as exc:
            logger.warning("Cloud Monitoring collection failed: %s", exc)
            collection_errors.append(f"cloud_monitoring: {exc}")
        except GoogleApiConfigError as exc:
            logger.warning("Cloud Monitoring not configured: %s", exc)
            collection_errors.append(f"cloud_monitoring: {exc}")

        logs_observed_count = 0
        if monitoring_input.service_name is not None:
            try:
                logs_observed_count = await self._collect_logs(
                    monitoring_input=monitoring_input,
                    revision=revision,
                    created_signals=created_signals,
                    context=context,
                    granted=self.capabilities,
                )
            except GoogleApiError as exc:
                logger.warning("Cloud Logging collection failed: %s", exc)
                collection_errors.append(f"cloud_logging: {exc}")
            except GoogleApiConfigError as exc:
                logger.warning("Cloud Logging not configured: %s", exc)
                collection_errors.append(f"cloud_logging: {exc}")

        if collection_errors and not created_signals and not metrics_observed:
            collection_status = MonitoringCollectionStatus.FAILED
        elif collection_errors:
            collection_status = MonitoringCollectionStatus.PARTIAL
        else:
            collection_status = MonitoringCollectionStatus.COMPLETE

        # Evidence-first (§17): the summary is built from what was actually
        # collected, never a fabricated claim about system state. No LLM
        # output exists anywhere in this method to accidentally trust.
        summary = (
            f"Observed {len(created_signals)} signal(s) for "
            f"{monitoring_input.service_name or ('environment ' + monitoring_input.environment)} "
            f"over the last {monitoring_input.window_minutes} minute(s)."
        )

        output = MonitoringOutput(
            observation_window_minutes=monitoring_input.window_minutes,
            service_name=monitoring_input.service_name,
            environment=monitoring_input.environment,
            signal_ids=[s.signal_id for s in created_signals],
            metrics_observed=metrics_observed,
            logs_observed_count=logs_observed_count,
            collection_status=collection_status,
            summary=summary,
            collection_errors=collection_errors,
        )

        execution.status = WorkflowStatus.COMPLETED
        execution.completed_at = datetime.now(timezone.utc)
        if context.executions is not None:
            await context.executions.update(execution)

        # No Artifact is produced — the durable product of a Monitoring
        # cycle is the persisted Signal(s) themselves (already saved via
        # context.signals above), not a new artifact type. MonitoringOutput
        # is returned as the caller-facing summary of this one invocation;
        # AgentOutput has no generic typed-payload field (only `artifacts`),
        # so it travels as a JSON message, same boundary every other agent
        # uses for its human-readable summary.
        return AgentOutput(
            execution_id=agent_input.execution_id,
            status=WorkflowStatus.COMPLETED,
            messages=[output.summary, output.model_dump_json()],
            metrics=metrics,
        )

    async def _collect_metrics(
        self,
        *,
        monitoring_input: MonitoringInput,
        revision: str | None,
        created_signals: list[Signal],
        metrics_observed: list[str],
        context: AgentContext,
        granted: set[AgentCapability],
    ) -> None:
        """The implementation-boundary capability check (§20 layer 3): takes
        `granted` as an explicit argument (not read from `self.capabilities`
        implicitly) so this method stays safe even if ever invoked with a
        narrower capability set than the agent's own, same principle as
        deploy_cloud_run/run_tests checking tool_context.state["_capabilities"]
        rather than trusting an ambient object attribute."""
        if AgentCapability.READ_MONITORING not in granted:
            raise GoogleApiError("READ_MONITORING capability not granted")

        client = self._get_monitoring_client()

        count_points: list[MetricPoint] = await client.query_request_count_by_response_class(
            service_name=monitoring_input.service_name, region=monitoring_input.region, window_minutes=monitoring_input.window_minutes
        )
        if count_points:
            metrics_observed.append("error_rate")
            total = sum(p.value for p in count_points)
            errors = sum(p.value for p in count_points if p.label.startswith("5"))
            error_fraction = (errors / total) if total > 0 else 0.0
            signal = await self._persist_metric_signal(
                observation_kind="error_rate",
                value=round(error_fraction, 4),
                unit="",
                severity=_classify_error_rate(error_fraction),
                monitoring_input=monitoring_input,
                revision=revision,
                window_start=count_points[0].window_start,
                window_end=count_points[0].window_end,
                evidence={"total_requests": total, "error_requests": errors, "by_response_class": {p.label: p.value for p in count_points}},
                context=context,
            )
            created_signals.append(signal)

        latency_point = await client.query_latency_p99(
            service_name=monitoring_input.service_name, region=monitoring_input.region, window_minutes=monitoring_input.window_minutes
        )
        if latency_point is not None:
            metrics_observed.append("latency_p99")
            signal = await self._persist_metric_signal(
                observation_kind="latency_p99",
                value=round(latency_point.value, 2),
                unit="ms",
                severity=SignalSeverity.INFO,  # thresholding latency is a future policy addition — see docs, not implemented here
                monitoring_input=monitoring_input,
                revision=revision,
                window_start=latency_point.window_start,
                window_end=latency_point.window_end,
                evidence={},
                context=context,
            )
            created_signals.append(signal)

        instance_points: list[MetricPoint] = await client.query_instance_count_by_state(
            service_name=monitoring_input.service_name, region=monitoring_input.region, window_minutes=monitoring_input.window_minutes
        )
        if instance_points:
            active = sum(p.value for p in instance_points if p.label == "active")
            if active == 0:
                metrics_observed.append("availability")
                signal = await self._persist_metric_signal(
                    observation_kind="availability",
                    value=0.0,
                    unit="active_instances",
                    severity=SignalSeverity.CRITICAL,
                    monitoring_input=monitoring_input,
                    revision=revision,
                    window_start=instance_points[0].window_start,
                    window_end=instance_points[0].window_end,
                    evidence={"by_state": {p.label: p.value for p in instance_points}},
                    context=context,
                )
                created_signals.append(signal)

    async def _persist_metric_signal(
        self,
        *,
        observation_kind: str,
        value: float,
        unit: str,
        severity: SignalSeverity,
        monitoring_input: MonitoringInput,
        revision: str | None,
        window_start: datetime,
        window_end: datetime,
        evidence: dict,
        context: AgentContext,
    ) -> Signal:
        payload = {
            "observation_kind": observation_kind,
            "service_name": monitoring_input.service_name or f"environment:{monitoring_input.environment}",
            "region": monitoring_input.region,
            "environment": monitoring_input.environment,
            "value": value,
            "unit": unit,
            "severity": severity.value,
            "window_start": window_start,
            "window_end": window_end,
            "revision": revision,
            "deployment_artifact_id": monitoring_input.deployment_artifact_id,
            "evidence": evidence,
        }
        signal = normalize_cloud_monitoring_metric_observation(payload)
        return await self._save_signal_deduplicated(signal, context)

    async def _collect_logs(
        self,
        *,
        monitoring_input: MonitoringInput,
        revision: str | None,
        created_signals: list[Signal],
        context: AgentContext,
        granted: set[AgentCapability],
    ) -> int:
        """The implementation-boundary capability check (§20 layer 3), same
        as _collect_metrics."""
        if AgentCapability.READ_MONITORING not in granted:
            raise GoogleApiError("READ_MONITORING capability not granted")

        settings = get_settings()
        client = self._get_logging_client()
        limit = min(settings.monitoring_log_query_limit, settings.monitoring_log_query_max_limit)

        entries: list[LogEntryResult] = await client.query_service_logs(
            service_name=monitoring_input.service_name,
            region=monitoring_input.region,
            window_minutes=monitoring_input.window_minutes,
            min_severity=settings.monitoring_min_log_severity,
            limit=limit,
        )

        for entry in entries:
            payload = CloudLoggingClient.to_signal_payload(entry)
            signal = normalize_cloud_logging_entry(payload)
            # Cloud Run's own monitored-resource labels carry service_name/
            # revision_name/location — never "environment" (that's a Quipu
            # concept, not a GCP resource label), so the adapter alone can't
            # fill it in. MonitoringAgent knows the scope it queried, so it
            # backfills environment (and revision/deployment_artifact_id
            # when the adapter didn't already have them) here.
            update = {"environment": monitoring_input.environment}
            if revision is not None and signal.revision is None:
                update["revision"] = revision
            if monitoring_input.deployment_artifact_id is not None:
                update["deployment_artifact_id"] = monitoring_input.deployment_artifact_id
            signal = signal.model_copy(update=update)
            persisted = await self._save_signal_deduplicated(signal, context)
            created_signals.append(persisted)

        return len(entries)

    async def _save_signal_deduplicated(self, signal: Signal, context: AgentContext) -> Signal:
        """Level 3's dedup contract (compute_fingerprint +
        find_by_fingerprint), reused rather than reimplemented: if an
        equivalent signal already exists, return it unchanged instead of
        writing a duplicate."""
        existing = await context.signals.find_by_fingerprint(signal.fingerprint)
        if existing is not None:
            return existing
        return await context.signals.save(signal)
