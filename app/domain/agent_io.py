"""Contracts an agent receives and returns, plus the execution/metrics/error records
built around one invocation. Agents never call each other directly — these are the
only shapes that cross the orchestrator/agent boundary.
"""

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from app.domain.artifact import Artifact
from app.domain.decision import Decision
from app.domain.enums import ErrorCategory, WorkflowStatus
from app.domain.knowledge import KnowledgeItem, KnowledgeQuery
from app.domain.ticket import Ticket
from app.domain.tool import ToolExecution


class AgentError(BaseModel):
    code: str
    message: str
    category: ErrorCategory
    recoverable: bool = False
    retryable: bool = False
    details: dict[str, Any] = Field(default_factory=dict)


class AgentMetrics(BaseModel):
    execution_id: str
    latency_ms: float | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    cost_usd: float | None = None
    tool_call_count: int = 0
    knowledge_query_count: int = 0
    retry_count: int = 0


class AgentInput(BaseModel):
    execution_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    workflow_id: str
    agent_name: str
    ticket: Ticket
    artifact_ids: list[str] = Field(default_factory=list)
    context: dict[str, Any] = Field(default_factory=dict)
    knowledge_context: list[KnowledgeItem] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class AgentOutput(BaseModel):
    execution_id: str
    status: WorkflowStatus
    artifacts: list[Artifact] = Field(default_factory=list)
    decision: Decision | None = None
    messages: list[str] = Field(default_factory=list)
    errors: list[AgentError] = Field(default_factory=list)
    metrics: AgentMetrics | None = None


class AgentExecution(BaseModel):
    """The actual execution of an agent, separate from what it returned (AgentOutput)."""

    execution_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    workflow_id: str
    agent_name: str
    started_at: datetime = Field(default_factory=datetime.utcnow)
    completed_at: datetime | None = None
    status: WorkflowStatus = WorkflowStatus.PENDING
    input_artifact_ids: list[str] = Field(default_factory=list)
    output_artifact_ids: list[str] = Field(default_factory=list)
    tool_calls: list[ToolExecution] = Field(default_factory=list)
    knowledge_queries: list[KnowledgeQuery] = Field(default_factory=list)
    error: AgentError | None = None
    retry_count: int = 0
