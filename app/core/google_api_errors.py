"""Shared translated-error hierarchy for Google API client boundaries.

Same convention already established independently by
app/knowledge/backends/google_search.py (Agent Search) and
app/persistence/firestore/errors.py (Firestore) — this module exists so the
two NEW Google clients this level adds (CloudMonitoringClient,
CloudLoggingClient) share one translation function instead of each
reimplementing the same google.api_core.exceptions -> category mapping.
The two pre-existing call sites are left untouched (no regression risk) —
this is not a forced refactor of working code, just the shared home for
the pattern used by anything added from here on.
"""

from google.api_core import exceptions as google_exceptions


class GoogleApiConfigError(Exception):
    """Missing/invalid configuration — not a network or API error."""


class GoogleApiError(Exception):
    """Base for all translated Google API errors. Callers should never need
    to catch google.api_core.exceptions directly — see translate_google_api_error()."""


class GoogleApiAuthError(GoogleApiError):
    pass


class GoogleApiPermissionError(GoogleApiError):
    pass


class GoogleApiInvalidRequestError(GoogleApiError):
    pass


class GoogleApiServiceUnavailableError(GoogleApiError):
    pass


class GoogleApiTimeoutError(GoogleApiError):
    pass


class GoogleApiMalformedResponseError(GoogleApiError):
    pass


def translate_google_api_error(exc: Exception, *, context: str = "") -> GoogleApiError:
    prefix = f"{context}: " if context else ""
    if isinstance(exc, google_exceptions.Unauthenticated):
        return GoogleApiAuthError(f"{prefix}{exc}")
    if isinstance(exc, (google_exceptions.PermissionDenied, google_exceptions.Forbidden)):
        return GoogleApiPermissionError(f"{prefix}{exc}")
    if isinstance(exc, google_exceptions.InvalidArgument):
        return GoogleApiInvalidRequestError(f"{prefix}{exc}")
    if isinstance(exc, (google_exceptions.DeadlineExceeded, TimeoutError)):
        return GoogleApiTimeoutError(f"{prefix}{exc}")
    if isinstance(exc, (google_exceptions.ServiceUnavailable, google_exceptions.BadGateway, google_exceptions.GatewayTimeout)):
        return GoogleApiServiceUnavailableError(f"{prefix}{exc}")
    return GoogleApiError(f"{prefix}unexpected Google API error: {exc}")
