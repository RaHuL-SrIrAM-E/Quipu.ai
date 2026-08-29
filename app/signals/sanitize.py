"""Sanitization boundary every adapter runs source payloads through before
building a Signal. Framework-independent — no Google SDK imports.

Not a full DLP system (Level 3 §17 explicitly scopes that out) — just the
minimum floor: never let an obviously secret-shaped key or an oversized raw
blob reach persisted evidence/metadata. Source adapters remain responsible
for not putting raw customer PII into a Signal in the first place; this is
a defense-in-depth backstop, not the primary control.
"""

import re
from typing import Any

_SENSITIVE_KEY_RE = re.compile(
    r"(password|passwd|secret|token|api[_-]?key|apikey|authorization|auth[_-]?header|"
    r"credential|private[_-]?key|access[_-]?key|ssn|social[_-]?security|credit[_-]?card|cvv)",
    re.IGNORECASE,
)

REDACTED = "[REDACTED]"

# A single evidence/metadata string value longer than this is truncated —
# Signal is evidence with provenance pointing back to the source, not a
# place to dump entire raw payloads (log bodies, full feedback transcripts).
_MAX_VALUE_LENGTH = 2000


def _sanitize_value(value: Any) -> Any:
    if isinstance(value, str) and len(value) > _MAX_VALUE_LENGTH:
        return value[:_MAX_VALUE_LENGTH] + "...[truncated]"
    if isinstance(value, dict):
        return sanitize_metadata(value)
    if isinstance(value, list):
        return [_sanitize_value(item) for item in value]
    return value


def sanitize_metadata(data: dict[str, Any]) -> dict[str, Any]:
    """Redacts values whose key looks secret-shaped and truncates
    oversized string values, recursively. Every adapter in
    app/signals/adapters.py calls this before handing evidence/metadata to
    the Signal constructor."""
    sanitized: dict[str, Any] = {}
    for key, value in data.items():
        if _SENSITIVE_KEY_RE.search(key):
            sanitized[key] = REDACTED
        else:
            sanitized[key] = _sanitize_value(value)
    return sanitized
