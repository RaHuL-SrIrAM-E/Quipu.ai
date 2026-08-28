"""WorkflowState — the current state of one Quipu workflow.

Deliberately thin: it references artifacts, executions and incidents by id
rather than embedding them, so workflow state stays separate from the
(potentially large) content those ids resolve to.
"""

import uuid
from typing import Any

from pydantic import BaseModel, Field

from app.domain.enums import WorkflowStage, WorkflowStatus
from app.domain.ticket import Ticket


class WorkflowState(BaseModel):
    workflow_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    ticket: Ticket
    status: WorkflowStatus = WorkflowStatus.PENDING
    current_stage: WorkflowStage
    artifact_ids: list[str] = Field(default_factory=list)
    active_decision_id: str | None = None
    execution_ids: list[str] = Field(default_factory=list)
    active_incident_ids: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)

    # Optimistic concurrency (Level 1.4): every persisted update must state
    # which version it read, so a stale write fails instead of silently
    # clobbering a concurrent one. See app.persistence.repositories.workflow.
    version: int = Field(default=1, ge=1)
