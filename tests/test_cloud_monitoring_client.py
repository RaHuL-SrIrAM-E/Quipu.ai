"""CloudMonitoringClient tests — real google.cloud.monitoring_v3 message
types (TimeSeries/Point/TypedValue), fake async client (no live Google
credentials/network required). Verifies real filter/aggregation
construction and real response normalization against actual SDK shapes."""

from datetime import datetime, timedelta, timezone

import pytest
from google.api_core import exceptions as google_exceptions
from google.cloud import monitoring_v3

from app.core.cloud_monitoring_client import CloudMonitoringClient, MetricPoint
from app.core.google_api_errors import (
    GoogleApiAuthError,
    GoogleApiConfigError,
    GoogleApiPermissionError,
    GoogleApiServiceUnavailableError,
    GoogleApiTimeoutError,
)


@pytest.fixture(autouse=True)
def _gcp_project_id(monkeypatch):
    """Scoped, auto-reverting GCP_PROJECT_ID — never leaks into other test
    modules the way a module-level os.environ.setdefault would (that
    previously broke tests/test_firestore_persistence.py's
    no-project-id-configured case by leaking a stale cached Settings)."""
    from app.config import get_settings

    monkeypatch.setenv("GCP_PROJECT_ID", "test-project")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def make_time_series(*, label_key: str | None, label_value: str | None, value: int | float, value_kind: str = "int64_value") -> "monitoring_v3.TimeSeries":
    ts = monitoring_v3.TimeSeries()
    if label_key is not None:
        ts.metric.labels[label_key] = label_value
    point = monitoring_v3.Point()
    setattr(point.value, value_kind, value)
    point.interval.end_time = datetime.now(timezone.utc)
    ts.points.append(point)
    return ts


class _AsyncIterable:
    def __init__(self, items):
        self._items = items

    def __aiter__(self):
        async def gen():
            for item in self._items:
                yield item

        return gen()


class FakeMetricServiceClient:
    def __init__(self, series=None, exc=None):
        self._series = series or []
        self._exc = exc
        self.last_request = None

    async def list_time_series(self, request, timeout):
        self.last_request = request
        if self._exc is not None:
            raise self._exc
        return _AsyncIterable(self._series)


def make_client(series=None, exc=None, fake=None) -> CloudMonitoringClient:
    fake_client = fake or FakeMetricServiceClient(series=series, exc=exc)
    client = CloudMonitoringClient(client=fake_client)
    return client, fake_client


# ---- config -----------------------------------------------------------------


def test_missing_project_id_raises_config_error(monkeypatch):
    from app import config as config_module

    config_module.get_settings.cache_clear()
    monkeypatch.delenv("GCP_PROJECT_ID", raising=False)
    with pytest.raises(GoogleApiConfigError):
        CloudMonitoringClient()
    monkeypatch.setenv("GCP_PROJECT_ID", "test-project")
    config_module.get_settings.cache_clear()


# ---- request_count / filter construction -----------------------------------


@pytest.mark.asyncio
async def test_query_request_count_valid_response_normalizes():
    series = [
        make_time_series(label_key="response_code_class", label_value="2xx", value=950),
        make_time_series(label_key="response_code_class", label_value="5xx", value=50),
    ]
    client, fake = make_client(series=series)
    points = await client.query_request_count_by_response_class(service_name="quipu-api", region="us-central1", window_minutes=15)
    assert {p.label: p.value for p in points} == {"2xx": 950, "5xx": 50}


@pytest.mark.asyncio
async def test_query_request_count_filter_includes_service_and_region():
    client, fake = make_client(series=[])
    await client.query_request_count_by_response_class(service_name="quipu-api", region="us-central1", window_minutes=15)
    assert 'resource.label.service_name = "quipu-api"' in fake.last_request.filter
    assert 'resource.label.location = "us-central1"' in fake.last_request.filter
    assert 'metric.type = "run.googleapis.com/request_count"' in fake.last_request.filter


@pytest.mark.asyncio
async def test_query_request_count_environment_wide_omits_service_filter():
    client, fake = make_client(series=[])
    await client.query_request_count_by_response_class(service_name=None, region="us-central1", window_minutes=15)
    assert "service_name" not in fake.last_request.filter


@pytest.mark.asyncio
async def test_time_window_matches_requested_minutes():
    client, fake = make_client(series=[])
    before = datetime.now(timezone.utc)
    await client.query_request_count_by_response_class(service_name="quipu-api", region="us-central1", window_minutes=15)
    after = datetime.now(timezone.utc)

    interval = fake.last_request.interval
    assert before - timedelta(seconds=1) <= interval.end_time <= after + timedelta(seconds=1)
    delta = interval.end_time - interval.start_time
    assert timedelta(minutes=14, seconds=55) <= delta <= timedelta(minutes=15, seconds=5)


@pytest.mark.asyncio
async def test_alignment_period_equals_window():
    client, fake = make_client(series=[])
    await client.query_request_count_by_response_class(service_name="quipu-api", region="us-central1", window_minutes=30)
    assert fake.last_request.aggregation.alignment_period.seconds == 30 * 60


@pytest.mark.asyncio
async def test_empty_result_returns_empty_list():
    client, fake = make_client(series=[])
    points = await client.query_request_count_by_response_class(service_name="quipu-api", region="us-central1", window_minutes=15)
    assert points == []


@pytest.mark.asyncio
async def test_time_series_with_no_points_is_skipped():
    ts = monitoring_v3.TimeSeries()
    ts.metric.labels["response_code_class"] = "2xx"
    client, fake = make_client(series=[ts])
    points = await client.query_request_count_by_response_class(service_name="quipu-api", region="us-central1", window_minutes=15)
    assert points == []


# ---- latency p99 --------------------------------------------------------------


@pytest.mark.asyncio
async def test_latency_p99_normalizes_double_value():
    series = [make_time_series(label_key=None, label_value=None, value=842.5, value_kind="double_value")]
    client, fake = make_client(series=series)
    point = await client.query_latency_p99(service_name="quipu-api", region="us-central1", window_minutes=15)
    assert point is not None
    assert point.label == "p99_latency_ms"
    assert point.value == 842.5


@pytest.mark.asyncio
async def test_latency_p99_uses_percentile_aligner():
    client, fake = make_client(series=[])
    await client.query_latency_p99(service_name="quipu-api", region="us-central1", window_minutes=15)
    assert fake.last_request.aggregation.per_series_aligner == monitoring_v3.Aggregation.Aligner.ALIGN_PERCENTILE_99
    assert 'metric.type = "run.googleapis.com/request_latencies"' in fake.last_request.filter


@pytest.mark.asyncio
async def test_latency_p99_empty_result_returns_none():
    client, fake = make_client(series=[])
    point = await client.query_latency_p99(service_name="quipu-api", region="us-central1", window_minutes=15)
    assert point is None


# ---- instance count -----------------------------------------------------------


@pytest.mark.asyncio
async def test_instance_count_by_state_normalizes():
    series = [
        make_time_series(label_key="state", label_value="active", value=2),
        make_time_series(label_key="state", label_value="idle", value=1),
    ]
    client, fake = make_client(series=series)
    points = await client.query_instance_count_by_state(service_name="quipu-api", region="us-central1", window_minutes=15)
    assert {p.label: p.value for p in points} == {"active": 2, "idle": 1}


# ---- error translation ------------------------------------------------------


@pytest.mark.asyncio
async def test_auth_failure_translated():
    client, fake = make_client(exc=google_exceptions.Unauthenticated("bad creds"))
    with pytest.raises(GoogleApiAuthError):
        await client.query_request_count_by_response_class(service_name="quipu-api", region="us-central1", window_minutes=15)


@pytest.mark.asyncio
async def test_permission_failure_translated():
    client, fake = make_client(exc=google_exceptions.PermissionDenied("no access"))
    with pytest.raises(GoogleApiPermissionError):
        await client.query_request_count_by_response_class(service_name="quipu-api", region="us-central1", window_minutes=15)


@pytest.mark.asyncio
async def test_timeout_translated():
    client, fake = make_client(exc=google_exceptions.DeadlineExceeded("too slow"))
    with pytest.raises(GoogleApiTimeoutError):
        await client.query_request_count_by_response_class(service_name="quipu-api", region="us-central1", window_minutes=15)


@pytest.mark.asyncio
async def test_service_unavailable_translated():
    client, fake = make_client(exc=google_exceptions.ServiceUnavailable("down"))
    with pytest.raises(GoogleApiServiceUnavailableError):
        await client.query_request_count_by_response_class(service_name="quipu-api", region="us-central1", window_minutes=15)


@pytest.mark.asyncio
async def test_malformed_response_point_with_no_value_set_skipped_not_crashed():
    ts = monitoring_v3.TimeSeries()
    ts.metric.labels["response_code_class"] = "2xx"
    point = monitoring_v3.Point()
    point.interval.end_time = datetime.now(timezone.utc)
    ts.points.append(point)  # value left entirely unset
    client, fake = make_client(series=[ts])
    points = await client.query_request_count_by_response_class(service_name="quipu-api", region="us-central1", window_minutes=15)
    assert points == []
