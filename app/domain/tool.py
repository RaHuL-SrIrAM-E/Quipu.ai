"""Scoped-tool contracts. These are request/execution-record shapes only —
actual tool implementations are a future integration.
"""

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from app.domain.enums import WorkflowStatus


class ToolRequest(BaseModel):
    request_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    tool_name: str
    operation: str
    parameters: dict[str, Any] = Field(default_factory=dict)
    workflow_id: str
    execution_id: str


class ToolExecution(BaseModel):
    """Record of one tool call made during an AgentExecution."""

    execution_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    tool_name: str
    operation: str
    workflow_id: str
    agent_execution_id: str
    status: WorkflowStatus = WorkflowStatus.PENDING
    started_at: datetime = Field(default_factory=datetime.utcnow)
    completed_at: datetime | None = None
    result: dict[str, Any] | None = None
    error: str | None = None
