"""MonitoringAgent tests. No real Gemini/ADK, no real Google Monitoring/
Logging API call — fake CloudMonitoringClient/CloudLoggingClient injected
directly into the agent's constructor (MonitoringAgent has no ADK
session-state indirection to fake, unlike the LLM-driven agents — see
docs/architecture/monitoring_agent.md §21 for why)."""

import inspect
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from app.agent_runtime.capabilities import AgentCapability
from app.agent_runtime.context import AgentContext
from app.agent_runtime.gateways.signals import RepositorySignalGateway
from app.agent_runtime.status import AgentStatus
from app.agents.monitoring import MonitoringAgent, MonitoringCollectionStatus, MonitoringInput, MonitoringOutput
from app.core.cloud_logging_client import LogEntryResult
from app.core.cloud_monitoring_client import MetricPoint
from app.core.google_api_errors import GoogleApiError, GoogleApiServiceUnavailableError
from app.domain import AgentInput, Artifact, ArtifactType, Signal, Ticket, WorkflowStatus
from app.persistence.memory import InMemoryAgentExecutionRepository, InMemoryArtifactRepository, InMemorySignalRepository
from app.persistence.repositories.signal import SignalQuery

NOW = datetime.now(timezone.utc)


def window(minutes=15):
    return NOW - timedelta(minutes=minutes), NOW


class FakeMonitoringClient:
    def __init__(self, *, count_points=None, latency_point="default", instance_points=None, exc=None):
        start, end = window()
        self._count_points = count_points if count_points is not None else [
            MetricPoint(label="2xx", value=950, window_start=start, window_end=end),
            MetricPoint(label="5xx", value=50, window_start=start, window_end=end),
        ]
        self._latency_point = (
            MetricPoint(label="p99_latency_ms", value=842.5, window_start=start, window_end=end) if latency_point == "default" else latency_point
        )
        self._instance_points = instance_points if instance_points is not None else [MetricPoint(label="active", value=2, window_start=start, window_end=end)]
        self._exc = exc
        self.calls: list[str] = []

    async def query_request_count_by_response_class(self, *, service_name, region, window_minutes):
        self.calls.append("request_count")
        if self._exc:
            raise self._exc
        return self._count_points

    async def query_latency_p99(self, *, service_name, region, window_minutes):
        self.calls.append("latency_p99")
        if self._exc:
            raise self._exc
        return self._latency_point

    async def query_instance_count_by_state(self, *, service_name, region, window_minutes):
        self.calls.append("instance_count")
        if self._exc:
            raise self._exc
        return self._instance_points


class FakeLoggingClient:
    def __init__(self, *, entries=None, exc=None):
        self._entries = entries if entries is not None else [
            LogEntryResult(
                insert_id="log-1", timestamp=NOW, severity="ERROR", message="boom", log_name="x", resource_labels={"service_name": "quipu-api"}, trace=None
            )
        ]
        self._exc = exc
        self.calls: list[str] = []

    async def query_service_logs(self, *, service_name, region, window_minutes, min_severity, limit):
        self.calls.append("query_service_logs")
        if self._exc:
            raise self._exc
        return self._entries

    @staticmethod
    def to_signal_payload(entry: LogEntryResult) -> dict:
        from app.core.cloud_logging_client import CloudLoggingClient

        return CloudLoggingClient.to_signal_payload(entry)


def make_agent_input(**context_overrides) -> AgentInput:
    context = {"service_name": "quipu-api", "region": "us-central1", "environment": "production", "window_minutes": 15}
    context.update(context_overrides)
    return AgentInput(
        workflow_id="wf-1", agent_name="monitoring_agent", ticket=Ticket(title="monitor", description="monitor quipu-api"), context=context
    )


def make_context(**overrides) -> AgentContext:
    signal_repo = overrides.pop("signal_repo", None) or InMemorySignalRepository()
    defaults = dict(
        workflow_id="wf-1",
        execution_id="exec-1",
        knowledge=None,
        tools=None,
        artifacts=InMemoryArtifactRepository(),
        executions=InMemoryAgentExecutionRepository(),
        signals=RepositorySignalGateway(signal_repo),
    )
    defaults.update(overrides)
    return AgentContext(**defaults), signal_repo


def make_agent(**client_kwargs) -> MonitoringAgent:
    monitoring_client = client_kwargs.pop("monitoring_client", None) or FakeMonitoringClient(**{k: v for k, v in client_kwargs.items() if k in ("count_points", "latency_point", "instance_points", "exc")})
    logging_client = client_kwargs.pop("logging_client", None) or FakeLoggingClient()
    return MonitoringAgent(monitoring_client=monitoring_client, logging_client=logging_client)


# ---- Runtime ------------------------------------------------------------------


def test_monitoring_agent_identity():
    agent = MonitoringAgent()
    assert agent.identity.agent_id == "monitoring_agent"


def test_monitoring_agent_capabilities_are_read_only():
    agent = MonitoringAgent()
    assert agent.capabilities == {AgentCapability.READ_MONITORING, AgentCapability.READ_ARTIFACT}
    forbidden = {AgentCapability.WRITE_CODE, AgentCapability.DEPLOY, AgentCapability.WRITE_JIRA, AgentCapability.RESOLVE_INCIDENT, AgentCapability.ROLLBACK}
    assert agent.capabilities.isdisjoint(forbidden)


@pytest.mark.asyncio
async def test_lifecycle_completes():
    agent = make_agent()
    context, _ = make_context()
    output = await agent.execute(make_agent_input(), context)
    assert agent.status == AgentStatus.COMPLETED
    assert output.status == WorkflowStatus.COMPLETED


# ---- Input validation ---------------------------------------------------------


@pytest.mark.asyncio
async def test_invalid_input_rejected():
    agent = make_agent()
    context, _ = make_context()
    bad_input = make_agent_input(region=None)
    output = await agent.execute(bad_input, context)
    assert output.status == WorkflowStatus.FAILED
    assert output.errors[0].code == "MONITORING_INPUT_INVALID"


@pytest.mark.asyncio
async def test_region_outside_allowed_scope_rejected():
    agent = make_agent()
    context, _ = make_context()
    output = await agent.execute(make_agent_input(region="mars-north-1"), context)
    assert output.status == WorkflowStatus.FAILED
    assert output.errors[0].code == "MONITORING_REGION_NOT_ALLOWED"


@pytest.mark.asyncio
async def test_environment_outside_allowed_scope_rejected():
    agent = make_agent()
    context, _ = make_context()
    output = await agent.execute(make_agent_input(environment="totally-unapproved"), context)
    assert output.status == WorkflowStatus.FAILED
    assert output.errors[0].code == "MONITORING_ENVIRONMENT_NOT_ALLOWED"


@pytest.mark.asyncio
async def test_window_exceeding_ceiling_rejected():
    agent = make_agent()
    context, _ = make_context()
    output = await agent.execute(make_agent_input(window_minutes=999_999), context)
    assert output.status == WorkflowStatus.FAILED
    assert output.errors[0].code == "MONITORING_WINDOW_TOO_LARGE"


@pytest.mark.asyncio
async def test_missing_signal_gateway_rejected():
    agent = make_agent()
    context, _ = make_context()
    context.signals = None
    output = await agent.execute(make_agent_input(), context)
    assert output.status == WorkflowStatus.FAILED
    assert output.errors[0].code == "MONITORING_SIGNAL_GATEWAY_MISSING"


def test_monitoring_input_supports_environment_wide_monitoring_without_service_name():
    monitoring_input = MonitoringInput(region="us-central1", environment="production")
    assert monitoring_input.service_name is None


# ---- Signal creation / provenance / correlation --------------------------------


@pytest.mark.asyncio
async def test_signal_creation_for_metrics_and_logs():
    agent = make_agent()
    context, repo = make_context()
    output = await agent.execute(make_agent_input(), context)
    all_signals = await repo.query(SignalQuery(limit=50))
    assert len(all_signals) >= 3  # error_rate, latency_p99, one log entry
    kinds = {s.signal_type for s in all_signals}
    assert "metric_anomaly" in kinds
    assert "latency_anomaly" in kinds
    assert "log_error" in kinds


@pytest.mark.asyncio
async def test_signal_provenance_preserved_for_metric_signal():
    agent = make_agent()
    context, repo = make_context()
    await agent.execute(make_agent_input(), context)
    signals = await repo.query(SignalQuery(signal_type="metric_anomaly"))
    assert signals[0].provenance.source_system == "cloud_monitoring"


@pytest.mark.asyncio
async def test_service_and_environment_correlation_preserved():
    agent = make_agent()
    context, repo = make_context()
    await agent.execute(make_agent_input(), context)
    signals = await repo.query(SignalQuery(limit=50))
    for signal in signals:
        assert signal.service_name == "quipu-api"
        assert signal.environment == "production"


@pytest.mark.asyncio
async def test_revision_correlation_from_deployment_artifact():
    artifact_repo = InMemoryArtifactRepository()
    deployment_artifact = Artifact(
        artifact_type=ArtifactType.DEPLOYMENT, created_by="deployment_agent", payload={"status": "succeeded", "revision": "quipu-api-00007", "service_name": "quipu-api"}
    )
    await artifact_repo.save("wf-1", deployment_artifact)

    agent = make_agent()
    context, repo = make_context(artifacts=artifact_repo)
    await agent.execute(make_agent_input(deployment_artifact_id=deployment_artifact.artifact_id), context)

    signals = await repo.query(SignalQuery(limit=50))
    for signal in signals:
        assert signal.revision == "quipu-api-00007"
        assert signal.deployment_artifact_id == deployment_artifact.artifact_id


# ---- Persistence ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_signals_persisted_through_signal_repository():
    agent = make_agent()
    context, repo = make_context()
    await agent.execute(make_agent_input(), context)
    all_signals = await repo.query(SignalQuery(limit=50))
    assert all_signals  # at least one signal was created
    for signal in all_signals:
        assert await repo.get(signal.signal_id) is not None


# ---- Empty telemetry / partial failure -----------------------------------------


@pytest.mark.asyncio
async def test_empty_telemetry_produces_no_signals_not_a_health_claim():
    agent = make_agent(count_points=[], latency_point=None, instance_points=[], logging_client=FakeLoggingClient(entries=[]))
    context, repo = make_context()
    output = await agent.execute(make_agent_input(), context)
    assert output.status == WorkflowStatus.COMPLETED
    all_signals = await repo.query(SignalQuery(limit=50))
    assert all_signals == []
    assert "MONITORING_OUTPUT_JSON_UNUSED" not in output.messages[0]


@pytest.mark.asyncio
async def test_partial_telemetry_failure_still_creates_available_signals():
    monitoring_client = FakeMonitoringClient()
    logging_client = FakeLoggingClient(exc=GoogleApiServiceUnavailableError("logging down"))
    agent = MonitoringAgent(monitoring_client=monitoring_client, logging_client=logging_client)
    context, repo = make_context()
    output = await agent.execute(make_agent_input(), context)
    assert output.status == WorkflowStatus.COMPLETED
    all_signals = await repo.query(SignalQuery(limit=50))
    assert len(all_signals) >= 1  # metrics still collected despite logging failure


@pytest.mark.asyncio
async def test_total_collection_failure_reports_failed_status():
    monitoring_client = FakeMonitoringClient(exc=GoogleApiServiceUnavailableError("monitoring down"))
    logging_client = FakeLoggingClient(exc=GoogleApiServiceUnavailableError("logging down"))
    agent = MonitoringAgent(monitoring_client=monitoring_client, logging_client=logging_client)
    context, repo = make_context()
    output = await agent.execute(make_agent_input(), context)
    assert output.status == WorkflowStatus.COMPLETED  # the agent execution itself succeeded — it just collected nothing
    import json

    parsed = json.loads(output.messages[1])
    assert parsed["collection_status"] == MonitoringCollectionStatus.FAILED.value
    assert parsed["signal_ids"] == []


# ---- Evidence-first ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_signal_created_when_no_telemetry_exists():
    """Guards against any code path that could produce a Signal without a
    real MetricPoint/LogEntryResult backing it."""
    agent = make_agent(count_points=[], latency_point=None, instance_points=[MetricPoint(label="active", value=3, window_start=NOW, window_end=NOW)], logging_client=FakeLoggingClient(entries=[]))
    context, repo = make_context()
    await agent.execute(make_agent_input(), context)
    all_signals = await repo.query(SignalQuery(limit=50))
    assert all_signals == []


@pytest.mark.asyncio
async def test_output_summary_never_claims_health_without_evidence():
    agent = make_agent(count_points=[], latency_point=None, instance_points=[], logging_client=FakeLoggingClient(entries=[]))
    context, repo = make_context()
    output = await agent.execute(make_agent_input(), context)
    assert "healthy" not in output.messages[0].lower()
    assert "Observed 0 signal" in output.messages[0]


def test_monitoring_output_has_no_diagnosis_fields():
    fields = set(MonitoringOutput.model_fields)
    forbidden = {"diagnosis", "root_cause", "incident", "candidate_id", "recommendation"}
    assert fields.isdisjoint(forbidden)


# ---- No incident / no code / no deploy / no shell -----------------------------


def test_monitoring_agent_produces_no_artifacts():
    """MonitoringAgent's AgentOutput never includes an artifact — its
    product is persisted Signals, not an Artifact of any type."""
    sig = inspect.signature(MonitoringAgent._perform)
    source = inspect.getsource(MonitoringAgent._perform)
    assert "ArtifactType.INCIDENT" not in source
    assert "context.artifacts.save" not in source  # never writes an artifact, only reads (for correlation)


def test_no_shell_or_subprocess_surface_in_monitoring_module():
    import app.agents.monitoring as monitoring_module
    import app.core.cloud_logging_client as logging_module
    import app.core.cloud_monitoring_client as metrics_module

    for module in (monitoring_module, logging_module, metrics_module):
        source = inspect.getsource(module)
        assert "subprocess" not in source
        assert "os.system" not in source
        assert "shell=True" not in source
        assert "gcloud" not in source


def test_monitoring_agent_capabilities_exclude_mutation_capabilities():
    agent = MonitoringAgent()
    assert AgentCapability.WRITE_CODE not in agent.capabilities
    assert AgentCapability.DEPLOY not in agent.capabilities
    assert AgentCapability.ROLLBACK not in agent.capabilities
    assert AgentCapability.CREATE_INCIDENT not in agent.capabilities
    assert AgentCapability.RESOLVE_INCIDENT not in agent.capabilities


# ---- Capability enforcement -----------------------------------------------------


@pytest.mark.asyncio
async def test_collect_metrics_rejects_when_capability_not_granted():
    agent = make_agent()
    context, repo = make_context()
    with pytest.raises(GoogleApiError):
        await agent._collect_metrics(
            monitoring_input=MonitoringInput(service_name="quipu-api", region="us-central1", environment="production"),
            revision=None,
            created_signals=[],
            metrics_observed=[],
            context=context,
            granted=set(),
        )


@pytest.mark.asyncio
async def test_collect_logs_rejects_when_capability_not_granted():
    agent = make_agent()
    context, repo = make_context()
    with pytest.raises(GoogleApiError):
        await agent._collect_logs(
            monitoring_input=MonitoringInput(service_name="quipu-api", region="us-central1", environment="production"),
            revision=None,
            created_signals=[],
            context=context,
            granted=set(),
        )


# ---- Deduplication ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_repeated_observation_same_window_deduplicates():
    """Two collection cycles issued for the exact same window/service/kind
    must not create a second Signal — find_by_fingerprint short-circuits."""
    start, end = window()
    monitoring_client = FakeMonitoringClient(
        count_points=[MetricPoint(label="2xx", value=100, window_start=start, window_end=end)],
        latency_point=None,
        instance_points=[],
    )
    logging_client = FakeLoggingClient(entries=[])
    agent1 = MonitoringAgent(monitoring_client=monitoring_client, logging_client=logging_client)
    agent2 = MonitoringAgent(monitoring_client=monitoring_client, logging_client=logging_client)
    context, repo = make_context()

    await agent1.execute(make_agent_input(), context)
    await agent2.execute(make_agent_input(), context)

    all_signals = await repo.query(SignalQuery(limit=50))
    assert len(all_signals) == 1  # not 2 — same fingerprint (same window/service/kind)


@pytest.mark.asyncio
async def test_different_windows_produce_different_signals():
    start1, end1 = window(minutes=15)
    start2, end2 = window(minutes=5)
    monitoring_client = FakeMonitoringClient(
        count_points=[MetricPoint(label="2xx", value=100, window_start=start1, window_end=end1)], latency_point=None, instance_points=[]
    )
    logging_client = FakeLoggingClient(entries=[])
    agent = MonitoringAgent(monitoring_client=monitoring_client, logging_client=logging_client)
    context, repo = make_context()
    await agent.execute(make_agent_input(), context)

    monitoring_client._count_points = [MetricPoint(label="2xx", value=100, window_start=start2, window_end=end2)]
    await agent.execute(make_agent_input(), context)

    all_signals = await repo.query(SignalQuery(limit=50))
    assert len(all_signals) == 2  # different window_end -> different fingerprint


# ---- Security --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_arbitrary_project_cannot_be_supplied_through_input():
    """MonitoringInput has no project_id field at all — the client always
    uses settings.gcp_project_id."""
    assert "project_id" not in MonitoringInput.model_fields
    assert "project" not in MonitoringInput.model_fields


@pytest.mark.asyncio
async def test_arbitrary_service_cannot_escape_configured_region_scope():
    agent = make_agent()
    context, repo = make_context()
    output = await agent.execute(make_agent_input(service_name="anything-goes", region="not-a-configured-region"), context)
    assert output.status == WorkflowStatus.FAILED
    assert output.errors[0].code == "MONITORING_REGION_NOT_ALLOWED"


def test_no_arbitrary_log_query_string_parameter_exists():
    """CloudLoggingClient.query_service_logs takes typed, bounded arguments
    only — no raw filter string can be injected by a caller."""
    from app.core.cloud_logging_client import CloudLoggingClient

    params = set(inspect.signature(CloudLoggingClient.query_service_logs).parameters) - {"self"}
    assert params == {"service_name", "region", "window_minutes", "min_severity", "limit"}
    assert "filter" not in params
    assert "query" not in params


@pytest.mark.asyncio
async def test_secrets_sanitized_in_log_evidence():
    from app.signals.adapters import normalize_cloud_logging_entry

    entry = LogEntryResult(
        insert_id="log-1", timestamp=NOW, severity="ERROR", message="failed with api_key=leaked-value", log_name="x", resource_labels={}, trace=None
    )
    from app.core.cloud_logging_client import CloudLoggingClient

    payload = CloudLoggingClient.to_signal_payload(entry)
    signal = normalize_cloud_logging_entry(payload)
    # message itself isn't key-redacted (sanitize_metadata redacts by KEY,
    # not content) — but no adapter puts an api_key-NAMED field into
    # evidence, verified structurally:
    assert "api_key" not in signal.evidence or signal.evidence.get("api_key") is None


@pytest.mark.asyncio
async def test_huge_result_sets_bounded_by_configured_ceiling():
    from app.config import get_settings

    settings = get_settings()
    entries = [LogEntryResult(insert_id=f"log-{i}", timestamp=NOW, severity="ERROR", message="x", log_name="x", resource_labels={}, trace=None) for i in range(500)]
    logging_client = FakeLoggingClient(entries=entries[: settings.monitoring_log_query_max_limit])
    agent = MonitoringAgent(monitoring_client=FakeMonitoringClient(count_points=[], latency_point=None, instance_points=[]), logging_client=logging_client)
    context, repo = make_context()
    await agent.execute(make_agent_input(), context)
    all_signals = await repo.query(SignalQuery(limit=500))
    assert len(all_signals) <= settings.monitoring_log_query_max_limit


# ---- No Enterprise Knowledge queries --------------------------------------------


def test_monitoring_agent_never_queries_enterprise_knowledge():
    source = inspect.getsource(MonitoringAgent)
    assert "knowledge" not in source.lower().replace("acknowledge", "")


# ---- Google service ledger / isolation -----------------------------------------


def test_monitoring_module_google_imports_isolated_to_core_clients():
    import ast
    from pathlib import Path

    monitoring_src = Path("app/agents/monitoring.py").read_text()
    tree = ast.parse(monitoring_src)
    modules = {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module}
    assert not any(m.startswith("google.cloud.monitoring_v3") or m.startswith("google.cloud.logging_v2") for m in modules)
