"""Quipu's framework-agnostic domain contracts.

No Google ADK, no LLM logic, no tool implementations here — just the typed
shapes that flow between the orchestrator, agents, the Knowledge Service and
scoped tools.
"""

from app.domain.agent_io import (
    AgentError,
    AgentExecution,
    AgentInput,
    AgentMetrics,
    AgentOutput,
)
from app.domain.artifact import Artifact
from app.domain.decision import Decision
from app.domain.enums import (
    ArtifactType,
    DecisionAction,
    DecisionSource,
    ErrorCategory,
    KnowledgeType,
    RetrievalStrategy,
    SignalSeverity,
    SignalSource,
    SignalStatus,
    SignalType,
    WorkflowStage,
    WorkflowStatus,
)
from app.domain.knowledge import KnowledgeItem, KnowledgeQuery, KnowledgeRequest
from app.domain.signal import Signal, SignalProvenance, compute_fingerprint
from app.domain.ticket import Ticket
from app.domain.tool import ToolExecution, ToolRequest
from app.domain.workflow import WorkflowState

__all__ = [
    "AgentError",
    "AgentExecution",
    "AgentInput",
    "AgentMetrics",
    "AgentOutput",
    "Artifact",
    "ArtifactType",
    "Decision",
    "DecisionAction",
    "DecisionSource",
    "ErrorCategory",
    "KnowledgeItem",
    "KnowledgeQuery",
    "KnowledgeRequest",
    "KnowledgeType",
    "RetrievalStrategy",
    "Signal",
    "SignalProvenance",
    "SignalSeverity",
    "SignalSource",
    "SignalStatus",
    "SignalType",
    "Ticket",
    "ToolExecution",
    "ToolRequest",
    "WorkflowStage",
    "WorkflowState",
    "WorkflowStatus",
    "compute_fingerprint",
]
