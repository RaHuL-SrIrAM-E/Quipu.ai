"""Artifact — every major output an agent produces, independently identifiable and versioned.

WorkflowState never embeds artifacts directly; it holds artifact_ids and callers
resolve them through whatever store owns Artifact persistence.
"""

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from app.domain.enums import ArtifactType, WorkflowStatus


class Artifact(BaseModel):
    artifact_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    artifact_type: ArtifactType
    version: int = Field(default=1, ge=1)
    created_by: str
    created_at: datetime = Field(default_factory=datetime.utcnow)
    parent_artifact_ids: list[str] = Field(default_factory=list)
    status: WorkflowStatus = WorkflowStatus.COMPLETED
    payload: dict[str, Any] = Field(default_factory=dict)
