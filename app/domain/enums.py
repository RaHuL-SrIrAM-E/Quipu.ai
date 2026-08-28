"""Finite-value vocabularies shared across the domain model. Framework-agnostic."""

from enum import StrEnum


class ArtifactType(StrEnum):
    TICKET = "ticket"
    PLAN = "plan"
    ARCHITECTURE = "architecture"
    CODE_CHANGE = "code_change"
    TEST_RESULT = "test_result"
    DEPLOYMENT = "deployment"
    MONITORING = "monitoring"
    INCIDENT = "incident"
    RESOLUTION = "resolution"


class WorkflowStatus(StrEnum):
    """Lifecycle status shared by WorkflowState, Artifact, AgentOutput and AgentExecution."""

    PENDING = "pending"
    RUNNING = "running"
    WAITING = "waiting"
    COMPLETED = "completed"
    FAILED = "failed"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"


class WorkflowStage(StrEnum):
    PLANNING = "planning"
    ARCHITECTURE = "architecture"
    CODEGEN = "codegen"
    TESTING = "testing"
    DEPLOYMENT = "deployment"
    MONITORING = "monitoring"
    DETECTION = "detection"
    INCIDENT_RESOLUTION = "incident_resolution"
    COMPLETED = "completed"


class DecisionAction(StrEnum):
    CONTINUE = "continue"
    RETRY = "retry"
    REPLAN = "replan"
    SKIP = "skip"
    WAIT = "wait"
    ESCALATE = "escalate"
    ROLLBACK = "rollback"
    COMPLETE = "complete"
    FAIL = "fail"


class DecisionSource(StrEnum):
    """Who made the decision. Small finite set, so typed rather than left as a free string."""

    ORCHESTRATOR = "orchestrator"
    AGENT = "agent"
    HUMAN = "human"
    SYSTEM = "system"


class ErrorCategory(StrEnum):
    VALIDATION = "validation"
    TIMEOUT = "timeout"
    TOOL_FAILURE = "tool_failure"
    KNOWLEDGE_FAILURE = "knowledge_failure"
    LLM_FAILURE = "llm_failure"
    PERMISSION_DENIED = "permission_denied"
    EXTERNAL_SERVICE = "external_service"
    INTERNAL = "internal"
    UNKNOWN = "unknown"


class KnowledgeType(StrEnum):
    ARCHITECTURE_PATTERN = "architecture_pattern"
    CODING_STANDARD = "coding_standard"
    SECURITY_POLICY = "security_policy"
    COMPLIANCE = "compliance"
    TESTING_STANDARD = "testing_standard"
    DEPLOYMENT_STANDARD = "deployment_standard"
    TECHNOLOGY_STANDARD = "technology_standard"
    HISTORICAL_PROJECT = "historical_project"
    TROUBLESHOOTING = "troubleshooting"
    OPERATIONS = "operations"
    INCIDENT = "incident"


class RetrievalStrategy(StrEnum):
    """How the Knowledge Service found a result. A contract for future retrieval
    implementations (app/knowledge) — not implemented here."""

    SEMANTIC = "semantic"
    KEYWORD = "keyword"
    HYBRID = "hybrid"
