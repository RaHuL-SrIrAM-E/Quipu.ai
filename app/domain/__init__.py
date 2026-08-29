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
from app.domain.detection import DetectionResult, compute_detection_fingerprint
from app.domain.enums import (
    ArtifactType,
    DecisionAction,
    DecisionSource,
    DetectionDomain,
    DetectionType,
    ErrorCategory,
    KnowledgeType,
    RemediationRisk,
    RemediationStrategy,
    RetrievalStrategy,
    ReviewStatus,
    SignalSeverity,
    SignalSource,
    SignalStatus,
    SignalType,
    WorkflowStage,
    WorkflowStatus,
)
from app.domain.feature_review import FeatureReview
from app.domain.knowledge import KnowledgeItem, KnowledgeQuery, KnowledgeRequest
from app.domain.remediation_verification import RemediationVerification, VerificationOutcome, VerificationStatus, compute_verification_key
from app.domain.resolution import ResolutionResult, compute_resolution_fingerprint
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
    "DetectionDomain",
    "DetectionResult",
    "DetectionType",
    "ErrorCategory",
    "FeatureReview",
    "KnowledgeItem",
    "KnowledgeQuery",
    "KnowledgeRequest",
    "KnowledgeType",
    "RemediationRisk",
    "RemediationStrategy",
    "RemediationVerification",
    "ResolutionResult",
    "RetrievalStrategy",
    "ReviewStatus",
    "Signal",
    "SignalProvenance",
    "SignalSeverity",
    "SignalSource",
    "SignalStatus",
    "SignalType",
    "Ticket",
    "ToolExecution",
    "ToolRequest",
    "VerificationOutcome",
    "VerificationStatus",
    "WorkflowStage",
    "WorkflowState",
    "WorkflowStatus",
    "compute_detection_fingerprint",
    "compute_fingerprint",
    "compute_resolution_fingerprint",
    "compute_verification_key",
]
