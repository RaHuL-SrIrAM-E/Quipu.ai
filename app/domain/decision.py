"""Decision — an explicit, structured orchestration decision. Actions are a closed set."""

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from app.domain.enums import DecisionAction, DecisionSource


class Decision(BaseModel):
    decision_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    action: DecisionAction
    target_agent: str | None = None
    reason: str
    confidence: float = Field(ge=0.0, le=1.0)
    source: DecisionSource
    conditions: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=datetime.utcnow)
