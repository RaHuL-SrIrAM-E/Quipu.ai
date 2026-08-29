"""Thin Cloud Logging API v2 client wrapper — the ONLY place in the
repository allowed to import google.cloud.logging_v2. Same pattern as
app/core/cloud_run_client.py and app/core/cloud_monitoring_client.py: a
small dataclass result, structured errors translated at the boundary,
Application Default Credentials only.

Uses the low-level LoggingServiceV2AsyncClient (async, matching the rest of
Quipu's Google client code) rather than the higher-level synchronous
google.cloud.logging.Client — the high-level client has no async API.

Monitored resource type/labels verified against Google's Cloud Run logging
integration (https://cloud.google.com/run/docs/logging): resource.type
"cloud_run_revision", with labels service_name/revision_name/location.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from google.api_core import exceptions as google_exceptions
from google.cloud.logging_v2.services.logging_service_v2 import LoggingServiceV2AsyncClient
from google.cloud.logging_v2.types import ListLogEntriesRequest, LogEntry
from google.logging.type.log_severity_pb2 import LogSeverity

from app.config import get_settings
from app.core.google_api_errors import GoogleApiConfigError, GoogleApiMalformedResponseError, translate_google_api_error

_RESOURCE_TYPE = "cloud_run_revision"
_SEVERITY_NAME_TO_NUMBER = {name: number for name, number in LogSeverity.items() if number > 0}


@dataclass
class LogEntryResult:
    """A normalized log entry, shaped to feed directly into
    app.signals.adapters.normalize_cloud_logging_entry — see
    CloudLoggingClient.to_signal_payload()."""

    insert_id: str
    timestamp: datetime
    severity: str  # e.g. "ERROR", "WARNING" — the LogSeverity enum name
    message: str
    log_name: str
    resource_labels: dict[str, str]
    trace: str | None


def _severity_threshold(min_severity: str) -> int:
    try:
        return _SEVERITY_NAME_TO_NUMBER[min_severity.upper()]
    except KeyError as exc:
        raise GoogleApiConfigError(f"'{min_severity}' is not a valid Cloud Logging severity name") from exc


def _extract_message(entry: "LogEntry") -> str:
    if entry.text_payload:
        return entry.text_payload
    if entry.json_payload:
        return str(dict(entry.json_payload))
    return ""


class CloudLoggingClient:
    """`client` is injectable for tests — pass a fake with a
    `list_log_entries(request=...)` async method; production code leaves it
    unset and a real LoggingServiceV2AsyncClient is created lazily on first
    use, never at construction time."""

    def __init__(self, client: "LoggingServiceV2AsyncClient | None" = None):
        settings = get_settings()
        if not settings.gcp_project_id:
            raise GoogleApiConfigError("GCP_PROJECT_ID is not set")
        self.project_id = settings.gcp_project_id
        self._settings = settings
        self._client = client

    def _get_client(self) -> "LoggingServiceV2AsyncClient":
        if self._client is None:
            self._client = LoggingServiceV2AsyncClient()
        return self._client

    async def query_service_logs(
        self,
        *,
        service_name: str,
        region: str,
        window_minutes: int,
        min_severity: str = "ERROR",
        limit: int = 50,
    ) -> list[LogEntryResult]:
        """Structured, bounded log evidence for one Cloud Run service —
        never an unfiltered log dump. `limit` is capped by the caller
        (MonitoringAgent) against settings.monitoring_log_query_max_limit
        before this method is ever called."""
        min_severity_number = _severity_threshold(min_severity)
        since = datetime.now(timezone.utc) - timedelta(minutes=window_minutes)

        filter_clauses = [
            f'resource.type = "{_RESOURCE_TYPE}"',
            f'resource.labels.service_name = "{service_name}"',
            f'resource.labels.location = "{region}"',
            f"severity >= {min_severity_number}",
            f'timestamp >= "{since.isoformat()}"',
        ]

        request = ListLogEntriesRequest(
            resource_names=[f"projects/{self.project_id}"],
            filter=" AND ".join(filter_clauses),
            order_by="timestamp desc",
            page_size=limit,
        )

        client = self._get_client()
        try:
            response = await client.list_log_entries(request=request, timeout=self._settings.monitoring_api_timeout_seconds)
            entries = []
            async for entry in response:
                entries.append(entry)
                if len(entries) >= limit:
                    break
        except google_exceptions.GoogleAPICallError as exc:
            raise translate_google_api_error(exc, context="Cloud Logging list_log_entries") from exc
        except TimeoutError as exc:
            raise translate_google_api_error(exc, context="Cloud Logging list_log_entries") from exc

        results = []
        for entry in entries:
            try:
                severity_name = LogSeverity.Name(entry.severity) if entry.severity else "DEFAULT"
            except ValueError as exc:
                raise GoogleApiMalformedResponseError(f"unrecognized Cloud Logging severity {entry.severity}") from exc
            results.append(
                LogEntryResult(
                    insert_id=entry.insert_id,
                    timestamp=entry.timestamp if entry.timestamp else datetime.now(timezone.utc),
                    severity=severity_name,
                    message=_extract_message(entry),
                    log_name=entry.log_name,
                    resource_labels=dict(entry.resource.labels),
                    trace=entry.trace or None,
                )
            )
        return results

    @staticmethod
    def to_signal_payload(entry: LogEntryResult) -> dict:
        """Shapes a LogEntryResult into the exact dict
        app.signals.adapters.normalize_cloud_logging_entry expects —
        reusing the existing Signal normalization function rather than
        building a second one for logs."""
        return {
            "insertId": entry.insert_id,
            "timestamp": entry.timestamp,
            "severity": entry.severity,
            "textPayload": entry.message,
            "logName": entry.log_name,
            "trace": entry.trace,
            "resource": {"type": _RESOURCE_TYPE, "labels": entry.resource_labels},
        }
