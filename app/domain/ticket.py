"""Ticket — the enterprise request/issue a workflow exists to resolve."""

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator


class Ticket(BaseModel):
    ticket_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    title: str
    description: str
    source: str | None = None
    external_id: str | None = None
    priority: str | None = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    metadata: dict[str, Any] = Field(default_factory=dict)

    # Level 3.4 (Feature Review): provenance back to the DetectionResult a
    # Ticket originated from, when it was created from an approved feature
    # opportunity rather than filed directly. Optional and additive —
    # every existing caller that builds a Ticket without this field is
    # unaffected. See app.feature_review.service and
    # docs/architecture/feature_review.md "Signal provenance". Deliberately
    # just the id, never the full DetectionResult — Ticket stays a thin
    # request/issue record, not a copy of Detecting's evidence.
    source_detection_id: str | None = None

    @field_validator("title", "description")
    @classmethod
    def _not_empty(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("must not be empty")
        return value.strip()
