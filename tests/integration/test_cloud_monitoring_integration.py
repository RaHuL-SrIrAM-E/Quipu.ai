"""Real Cloud Monitoring + Cloud Logging integration test — NOT part of the
normal test run.

Skipped unless QUIPU_RUN_CLOUD_MONITORING_INTEGRATION_TESTS=true is set, and
even then requires real GCP credentials (Application Default Credentials)
plus a configured GCP_PROJECT_ID with a Cloud Run service actually running
in it (CLOUD_MONITORING_INTEGRATION_TEST_SERVICE /
CLOUD_MONITORING_INTEGRATION_TEST_REGION). `pytest tests/` never triggers
this file's network call — no personal/CI credentials are ever required for
the normal suite. Read-only: queries real telemetry, writes nothing to
Cloud Monitoring/Logging (Signals ARE persisted, to whatever SignalRepository
is configured — safe, since Signal persistence is idempotent/upsert).
"""

import os

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("QUIPU_RUN_CLOUD_MONITORING_INTEGRATION_TESTS") != "true",
    reason="set QUIPU_RUN_CLOUD_MONITORING_INTEGRATION_TESTS=true (plus a real GCP_PROJECT_ID and a running Cloud Run service) to run",
)


@pytest.mark.asyncio
async def test_real_cloud_monitoring_query_request_count():
    from app.core.cloud_monitoring_client import CloudMonitoringClient

    service_name = os.environ.get("CLOUD_MONITORING_INTEGRATION_TEST_SERVICE")
    region = os.environ.get("CLOUD_MONITORING_INTEGRATION_TEST_REGION", "us-central1")
    assert service_name, "set CLOUD_MONITORING_INTEGRATION_TEST_SERVICE to a real Cloud Run service name"

    client = CloudMonitoringClient()
    points = await client.query_request_count_by_response_class(service_name=service_name, region=region, window_minutes=60)
    assert isinstance(points, list)  # may be empty if the service received no traffic — still a valid, real response


@pytest.mark.asyncio
async def test_real_cloud_logging_query_service_logs():
    from app.core.cloud_logging_client import CloudLoggingClient

    service_name = os.environ.get("CLOUD_MONITORING_INTEGRATION_TEST_SERVICE")
    region = os.environ.get("CLOUD_MONITORING_INTEGRATION_TEST_REGION", "us-central1")
    assert service_name, "set CLOUD_MONITORING_INTEGRATION_TEST_SERVICE to a real Cloud Run service name"

    client = CloudLoggingClient()
    results = await client.query_service_logs(service_name=service_name, region=region, window_minutes=60, min_severity="ERROR", limit=10)
    assert isinstance(results, list)


@pytest.mark.asyncio
async def test_real_monitoring_agent_end_to_end():
    from app.agent_runtime.context import AgentContext
    from app.agent_runtime.gateways.signals import RepositorySignalGateway
    from app.agents.monitoring import MonitoringAgent
    from app.domain import AgentInput, Ticket
    from app.persistence.memory import InMemoryArtifactRepository, InMemorySignalRepository

    service_name = os.environ.get("CLOUD_MONITORING_INTEGRATION_TEST_SERVICE")
    region = os.environ.get("CLOUD_MONITORING_INTEGRATION_TEST_REGION", "us-central1")
    environment = os.environ.get("CLOUD_MONITORING_INTEGRATION_TEST_ENVIRONMENT", "production")
    assert service_name, "set CLOUD_MONITORING_INTEGRATION_TEST_SERVICE to a real Cloud Run service name"

    agent = MonitoringAgent()
    context = AgentContext(
        workflow_id="integration-test",
        execution_id="integration-test-exec",
        knowledge=None,
        tools=None,
        artifacts=InMemoryArtifactRepository(),
        signals=RepositorySignalGateway(InMemorySignalRepository()),
    )
    agent_input = AgentInput(
        workflow_id="integration-test",
        agent_name="monitoring_agent",
        ticket=Ticket(title="integration test", description="throwaway"),
        context={"service_name": service_name, "region": region, "environment": environment, "window_minutes": 60},
    )
    output = await agent.execute(agent_input, context)
    assert output.status.value == "completed"
