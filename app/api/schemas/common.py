"""Shared API response shapes — not domain models. Every route in
app/api/routes/ returns one of these (or a list of them), never a raw
Pydantic domain model — see docs/architecture/control_plane_api.md
"Response models".
"""

from pydantic import BaseModel


class ErrorResponse(BaseModel):
    """The one error shape every non-2xx response returns — see
    app/api/errors.py. Never includes a stack trace, a raw exception
    string from a dependency (Firestore/Gemini/Google SDK), or any secret;
    `detail` is always a short, safe, human-readable message."""

    error: str  # a stable machine-readable code, e.g. "not_found", "version_conflict"
    detail: str
    correlation_id: str | None = None
