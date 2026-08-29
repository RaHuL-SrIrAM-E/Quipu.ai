"""Thin Cloud Monitoring API v3 client wrapper — the ONLY place in the
repository allowed to import google.cloud.monitoring_v3. Same pattern as
app/core/cloud_run_client.py: a small dataclass result, structured errors
translated at the boundary, Application Default Credentials only.

Exposes exactly the three narrow, Cloud-Run-shaped queries MonitoringAgent
needs — not a generic "run any Monitoring filter" surface. All filter/
aggregation construction happens in this module; a caller supplies typed
arguments (service_name, region, window), never a raw filter string.

Metric names verified against the installed google-cloud-monitoring SDK and
Google's own Cloud Run metrics reference
(https://cloud.google.com/monitoring/api/metrics_gcp#gcp-run):

  run.googleapis.com/request_count          DELTA, INT64  — request volume,
                                              labelled by response_code_class
  run.googleapis.com/request_latencies      DELTA, DISTRIBUTION — request
                                              latency in milliseconds
  run.googleapis.com/container/instance_count  GAUGE, INT64 — active/idle
                                              instance counts, labelled by state

query_request_count_by_response_class and query_latency_p99 both use an
alignment_period equal to the full requested window, so each returned
MetricPoint already represents the aggregate for that whole window — the
agent does not need to do its own point-by-point aggregation.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from google.api_core import exceptions as google_exceptions
from google.cloud import monitoring_v3

from app.config import get_settings
from app.core.google_api_errors import GoogleApiConfigError, GoogleApiMalformedResponseError, translate_google_api_error

_METRIC_REQUEST_COUNT = "run.googleapis.com/request_count"
_METRIC_REQUEST_LATENCIES = "run.googleapis.com/request_latencies"
_METRIC_INSTANCE_COUNT = "run.googleapis.com/container/instance_count"


@dataclass
class MetricPoint:
    label: str  # e.g. a response_code_class value ("2xx"/"5xx"), "p99_latency_ms", or an instance state
    value: float
    window_start: datetime
    window_end: datetime


def _extract_value(point: "monitoring_v3.Point") -> float:
    which = point.value._pb.WhichOneof("value")
    if which is None:
        raise GoogleApiMalformedResponseError("Cloud Monitoring point had no value set")
    return float(getattr(point.value, which))


def _cloud_run_filter(*, metric_type: str, service_name: str | None, region: str) -> str:
    clauses = [f'metric.type = "{metric_type}"', 'resource.type = "cloud_run_revision"', f'resource.label.location = "{region}"']
    if service_name is not None:
        clauses.append(f'resource.label.service_name = "{service_name}"')
    return " AND ".join(clauses)


class CloudMonitoringClient:
    """`client` is injectable for tests — pass a fake with a
    `list_time_series(request=...)` async method; production code leaves it
    unset and a real MetricServiceAsyncClient is created lazily on first
    use, never at construction time."""

    def __init__(self, client: "monitoring_v3.MetricServiceAsyncClient | None" = None):
        settings = get_settings()
        if not settings.gcp_project_id:
            raise GoogleApiConfigError("GCP_PROJECT_ID is not set")
        self.project_id = settings.gcp_project_id
        self._settings = settings
        self._client = client

    def _get_client(self) -> "monitoring_v3.MetricServiceAsyncClient":
        if self._client is None:
            self._client = monitoring_v3.MetricServiceAsyncClient()
        return self._client

    def _window(self, window_minutes: int) -> "monitoring_v3.TimeInterval":
        end = datetime.now(timezone.utc)
        start = end - timedelta(minutes=window_minutes)
        interval = monitoring_v3.TimeInterval()
        interval.end_time = end
        interval.start_time = start
        return interval

    async def _list_time_series(self, *, metric_type: str, service_name: str | None, region: str, window_minutes: int, aligner, group_by: list[str]):
        interval = self._window(window_minutes)
        aggregation = monitoring_v3.Aggregation(
            alignment_period={"seconds": window_minutes * 60},
            per_series_aligner=aligner,
            cross_series_reducer=monitoring_v3.Aggregation.Reducer.REDUCE_SUM,
            group_by_fields=group_by,
        )
        request = monitoring_v3.ListTimeSeriesRequest(
            name=f"projects/{self.project_id}",
            filter=_cloud_run_filter(metric_type=metric_type, service_name=service_name, region=region),
            interval=interval,
            aggregation=aggregation,
            view=monitoring_v3.ListTimeSeriesRequest.TimeSeriesView.FULL,
        )
        client = self._get_client()
        try:
            response = await client.list_time_series(request=request, timeout=self._settings.monitoring_api_timeout_seconds)
            series = [ts async for ts in response]
        except google_exceptions.GoogleAPICallError as exc:
            raise translate_google_api_error(exc, context="Cloud Monitoring list_time_series") from exc
        except TimeoutError as exc:
            raise translate_google_api_error(exc, context="Cloud Monitoring list_time_series") from exc
        return series, interval

    async def query_request_count_by_response_class(
        self, *, service_name: str | None, region: str, window_minutes: int
    ) -> list[MetricPoint]:
        """One MetricPoint per observed response_code_class ("2xx", "4xx",
        "5xx", ...) with the total request count in the window. Empty list
        means no requests were observed — not an error."""
        series, interval = await self._list_time_series(
            metric_type=_METRIC_REQUEST_COUNT,
            service_name=service_name,
            region=region,
            window_minutes=window_minutes,
            aligner=monitoring_v3.Aggregation.Aligner.ALIGN_SUM,
            group_by=["metric.label.response_code_class"],
        )

        points: list[MetricPoint] = []
        for ts in series:
            label = dict(ts.metric.labels).get("response_code_class", "unknown")
            if not ts.points:
                continue
            try:
                value = _extract_value(ts.points[0])
            except GoogleApiMalformedResponseError:
                continue
            points.append(
                MetricPoint(
                    label=label,
                    value=value,
                    window_start=interval.start_time,
                    window_end=interval.end_time,
                )
            )
        return points

    async def query_latency_p99(self, *, service_name: str | None, region: str, window_minutes: int) -> MetricPoint | None:
        """A single MetricPoint (label="p99_latency_ms") for the p99 request
        latency across the window, or None if no requests were observed."""
        series, interval = await self._list_time_series(
            metric_type=_METRIC_REQUEST_LATENCIES,
            service_name=service_name,
            region=region,
            window_minutes=window_minutes,
            aligner=monitoring_v3.Aggregation.Aligner.ALIGN_PERCENTILE_99,
            group_by=[],
        )
        for ts in series:
            if not ts.points:
                continue
            try:
                value = _extract_value(ts.points[0])
            except GoogleApiMalformedResponseError:
                continue
            return MetricPoint(label="p99_latency_ms", value=value, window_start=interval.start_time, window_end=interval.end_time)
        return None

    async def query_instance_count_by_state(self, *, service_name: str | None, region: str, window_minutes: int) -> list[MetricPoint]:
        """One MetricPoint per instance state ("active", "idle") with the
        mean instance count over the window."""
        series, interval = await self._list_time_series(
            metric_type=_METRIC_INSTANCE_COUNT,
            service_name=service_name,
            region=region,
            window_minutes=window_minutes,
            aligner=monitoring_v3.Aggregation.Aligner.ALIGN_MEAN,
            group_by=["metric.label.state"],
        )
        points: list[MetricPoint] = []
        for ts in series:
            label = dict(ts.metric.labels).get("state", "unknown")
            if not ts.points:
                continue
            try:
                value = _extract_value(ts.points[0])
            except GoogleApiMalformedResponseError:
                continue
            points.append(MetricPoint(label=label, value=value, window_start=interval.start_time, window_end=interval.end_time))
        return points
