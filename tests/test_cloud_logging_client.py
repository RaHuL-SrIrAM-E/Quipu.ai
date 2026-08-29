"""CloudLoggingClient tests — real google.cloud.logging_v2 LogEntry
messages, fake async client (no live Google credentials/network required)."""

from datetime import datetime, timezone

import pytest
from google.api_core import exceptions as google_exceptions
from google.cloud.logging_v2.types import LogEntry

from app.core.cloud_logging_client import CloudLoggingClient, LogEntryResult
from app.core.google_api_errors import (
    GoogleApiAuthError,
    GoogleApiConfigError,
    GoogleApiPermissionError,
    GoogleApiServiceUnavailableError,
    GoogleApiTimeoutError,
)


@pytest.fixture(autouse=True)
def _gcp_project_id(monkeypatch):
    """Scoped, auto-reverting GCP_PROJECT_ID — see test_cloud_monitoring_client.py's
    identical fixture for why this must not be a module-level os.environ mutation."""
    from app.config import get_settings

    monkeypatch.setenv("GCP_PROJECT_ID", "test-project")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def make_entry(*, insert_id="log-1", severity=500, text="boom", service_name="quipu-api", trace=None) -> "LogEntry":
    entry = LogEntry(insert_id=insert_id, log_name="projects/p/logs/run.googleapis.com%2Fstderr", severity=severity, text_payload=text)
    entry.timestamp = datetime.now(timezone.utc)
    entry.resource.type = "cloud_run_revision"
    entry.resource.labels["service_name"] = service_name
    if trace:
        entry.trace = trace
    return entry


class _AsyncIterable:
    def __init__(self, items):
        self._items = items

    def __aiter__(self):
        async def gen():
            for item in self._items:
                yield item

        return gen()


class FakeLoggingServiceClient:
    def __init__(self, entries=None, exc=None):
        self._entries = entries or []
        self._exc = exc
        self.last_request = None

    async def list_log_entries(self, request, timeout):
        self.last_request = request
        if self._exc is not None:
            raise self._exc
        return _AsyncIterable(self._entries)


def make_client(entries=None, exc=None):
    fake = FakeLoggingServiceClient(entries=entries, exc=exc)
    return CloudLoggingClient(client=fake), fake


# ---- config -----------------------------------------------------------------


def test_missing_project_id_raises_config_error(monkeypatch):
    from app import config as config_module

    config_module.get_settings.cache_clear()
    monkeypatch.delenv("GCP_PROJECT_ID", raising=False)
    with pytest.raises(GoogleApiConfigError):
        CloudLoggingClient()
    monkeypatch.setenv("GCP_PROJECT_ID", "test-project")
    config_module.get_settings.cache_clear()


def test_invalid_severity_name_raises_config_error():
    client, fake = make_client(entries=[])
    import asyncio

    with pytest.raises(GoogleApiConfigError):
        asyncio.run(client.query_service_logs(service_name="quipu-api", region="us-central1", window_minutes=15, min_severity="NOT_A_SEVERITY", limit=10))


# ---- valid query / normalization --------------------------------------------


@pytest.mark.asyncio
async def test_valid_query_normalizes_entries():
    client, fake = make_client(entries=[make_entry()])
    results = await client.query_service_logs(service_name="quipu-api", region="us-central1", window_minutes=15, min_severity="ERROR", limit=10)
    assert len(results) == 1
    assert results[0].severity == "ERROR"
    assert results[0].message == "boom"
    assert results[0].resource_labels["service_name"] == "quipu-api"


@pytest.mark.asyncio
async def test_service_filter_applied():
    client, fake = make_client(entries=[])
    await client.query_service_logs(service_name="quipu-api", region="us-central1", window_minutes=15, min_severity="ERROR", limit=10)
    assert 'resource.labels.service_name = "quipu-api"' in fake.last_request.filter
    assert 'resource.type = "cloud_run_revision"' in fake.last_request.filter


@pytest.mark.asyncio
async def test_time_window_filter_applied():
    client, fake = make_client(entries=[])
    await client.query_service_logs(service_name="quipu-api", region="us-central1", window_minutes=15, min_severity="ERROR", limit=10)
    assert "timestamp >=" in fake.last_request.filter


@pytest.mark.asyncio
async def test_severity_filter_applied():
    client, fake = make_client(entries=[])
    await client.query_service_logs(service_name="quipu-api", region="us-central1", window_minutes=15, min_severity="CRITICAL", limit=10)
    assert "severity >= 600" in fake.last_request.filter


@pytest.mark.asyncio
async def test_page_size_matches_limit():
    client, fake = make_client(entries=[])
    await client.query_service_logs(service_name="quipu-api", region="us-central1", window_minutes=15, min_severity="ERROR", limit=25)
    assert fake.last_request.page_size == 25


@pytest.mark.asyncio
async def test_results_bounded_by_limit_even_if_more_returned():
    entries = [make_entry(insert_id=f"log-{i}") for i in range(10)]
    client, fake = make_client(entries=entries)
    results = await client.query_service_logs(service_name="quipu-api", region="us-central1", window_minutes=15, min_severity="ERROR", limit=3)
    assert len(results) == 3


@pytest.mark.asyncio
async def test_empty_result_returns_empty_list():
    client, fake = make_client(entries=[])
    results = await client.query_service_logs(service_name="quipu-api", region="us-central1", window_minutes=15, min_severity="ERROR", limit=10)
    assert results == []


@pytest.mark.asyncio
async def test_trace_preserved_when_present():
    client, fake = make_client(entries=[make_entry(trace="projects/p/traces/t-1")])
    results = await client.query_service_logs(service_name="quipu-api", region="us-central1", window_minutes=15, min_severity="ERROR", limit=10)
    assert results[0].trace == "projects/p/traces/t-1"


@pytest.mark.asyncio
async def test_json_payload_used_when_text_payload_absent():
    entry = LogEntry(insert_id="log-2", log_name="x", severity=500, json_payload={"error": "boom"})
    entry.timestamp = datetime.now(timezone.utc)
    entry.resource.type = "cloud_run_revision"
    entry.resource.labels["service_name"] = "quipu-api"
    client, fake = make_client(entries=[entry])
    results = await client.query_service_logs(service_name="quipu-api", region="us-central1", window_minutes=15, min_severity="ERROR", limit=10)
    assert "error" in results[0].message


# ---- error translation ------------------------------------------------------


@pytest.mark.asyncio
async def test_auth_failure_translated():
    client, fake = make_client(exc=google_exceptions.Unauthenticated("bad creds"))
    with pytest.raises(GoogleApiAuthError):
        await client.query_service_logs(service_name="quipu-api", region="us-central1", window_minutes=15, min_severity="ERROR", limit=10)


@pytest.mark.asyncio
async def test_permission_failure_translated():
    client, fake = make_client(exc=google_exceptions.PermissionDenied("no access"))
    with pytest.raises(GoogleApiPermissionError):
        await client.query_service_logs(service_name="quipu-api", region="us-central1", window_minutes=15, min_severity="ERROR", limit=10)


@pytest.mark.asyncio
async def test_timeout_translated():
    client, fake = make_client(exc=google_exceptions.DeadlineExceeded("too slow"))
    with pytest.raises(GoogleApiTimeoutError):
        await client.query_service_logs(service_name="quipu-api", region="us-central1", window_minutes=15, min_severity="ERROR", limit=10)


@pytest.mark.asyncio
async def test_service_unavailable_translated():
    client, fake = make_client(exc=google_exceptions.ServiceUnavailable("down"))
    with pytest.raises(GoogleApiServiceUnavailableError):
        await client.query_service_logs(service_name="quipu-api", region="us-central1", window_minutes=15, min_severity="ERROR", limit=10)


# ---- reuse of existing Signal normalization ---------------------------------


def test_to_signal_payload_matches_normalize_cloud_logging_entry_shape():
    from app.signals.adapters import normalize_cloud_logging_entry

    entry = LogEntryResult(
        insert_id="log-1",
        timestamp=datetime.now(timezone.utc),
        severity="ERROR",
        message="boom",
        log_name="x",
        resource_labels={"service_name": "quipu-api"},
        trace="t-1",
    )
    payload = CloudLoggingClient.to_signal_payload(entry)
    signal = normalize_cloud_logging_entry(payload)
    assert signal.service_name == "quipu-api"
    assert signal.provenance.trace_id == "t-1"
