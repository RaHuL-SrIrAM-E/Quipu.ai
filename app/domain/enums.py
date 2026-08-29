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

    # Level 2.0: distinct from FAILED — a workflow the orchestrator has
    # deliberately handed to a human (retry budget exhausted, unknown
    # failure, invalid/unsafe requested transition), not one that crashed.
    ESCALATED = "escalated"


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


class SignalType(StrEnum):
    """What kind of thing was observed. Operational values describe
    production/runtime evidence; product values describe customer/usage
    evidence. Deliberately not exhaustive — only categories the Level 3
    Signal architecture (and its future Monitoring/Detecting consumers)
    actually need are defined here."""

    # Operational
    METRIC_ANOMALY = "metric_anomaly"
    LOG_ERROR = "log_error"
    APPLICATION_ERROR = "application_error"
    DEPLOYMENT_EVENT = "deployment_event"
    AVAILABILITY_DEGRADATION = "availability_degradation"
    LATENCY_ANOMALY = "latency_anomaly"

    # Product
    CUSTOMER_FEEDBACK = "customer_feedback"
    SUPPORT_FEEDBACK = "support_feedback"
    FEATURE_REQUEST_PATTERN = "feature_request_pattern"
    USER_BEHAVIOR = "user_behavior"
    ADOPTION_ANOMALY = "adoption_anomaly"


class SignalSource(StrEnum):
    """Where a Signal's evidence originated. Identifies the origin system,
    not the specific adapter implementation — app/signals/adapters.py maps
    each of these to a normalization function."""

    CLOUD_MONITORING = "cloud_monitoring"
    CLOUD_LOGGING = "cloud_logging"
    CLOUD_RUN = "cloud_run"
    CUSTOMER_FEEDBACK = "customer_feedback"
    SUPPORT_SYSTEM = "support_system"
    PRODUCT_ANALYTICS = "product_analytics"
    USER_BEHAVIOR = "user_behavior"
    INTERNAL_SYSTEM = "internal_system"


class SignalSeverity(StrEnum):
    """How important the observed evidence is. This is NOT a diagnosis or a
    priority assignment — it reflects the source's own signal about
    importance (e.g. a Cloud Monitoring alert's severity, or how many times
    the same feedback was repeated), carried through unchanged."""

    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


class DetectionDomain(StrEnum):
    """Which kind of question Detecting was asked to answer — determines
    both the default Signal types retrieved as evidence and how the result
    should be read (an operational detection targets INCIDENT; a product
    detection targets FEATURE_OPPORTUNITY). Not itself part of the model's
    structured output — the caller (DetectingInput) fixes it before any
    retrieval or reasoning happens."""

    OPERATIONAL = "operational"
    PRODUCT = "product"


class DetectionType(StrEnum):
    """What Detecting concluded the evidence represents. A closed set
    deliberately smaller than the eventual Candidate vocabulary
    (IncidentCandidate/FeatureCandidate, both future work) — Detecting
    itself only needs to distinguish these three outcomes."""

    INCIDENT = "incident"
    FEATURE_OPPORTUNITY = "feature_opportunity"
    NO_ACTION = "no_action"  # evidence retrieved but not sufficient/coherent enough to act on


class SignalStatus(StrEnum):
    """Ingestion-pipeline state, NOT Detecting's interpretation of the
    signal. A Signal's evidence fields never change across these states —
    only this field does, as the (currently synchronous) ingestion pipeline
    progresses. Every adapter in this level produces AVAILABLE signals
    directly; OBSERVED/INGESTED exist for a future asynchronous ingestion
    pipeline (e.g. a Pub/Sub-delivered payload staged before validation
    completes) and are not reachable through any code in this level."""

    OBSERVED = "observed"
    INGESTED = "ingested"
    AVAILABLE = "available"


class RemediationStrategy(StrEnum):
    """What Incident Resolution recommends — a closed set that maps 1:1
    onto an existing, already-implemented Quipu agent (or explicit human
    escalation), never an open string the model could use to invent a
    target. See app.agents.incident_resolution._STRATEGY_TARGET_AGENT for
    the deterministic strategy -> agent mapping. Deliberately excludes a
    CONFIGURATION_CHANGE value: no safe, existing execution path for
    mutating configuration exists anywhere in Quipu today, so that outcome
    is represented as ESCALATE instead of inventing a new mutation
    capability just for this level."""

    CODE_FIX = "code_fix"  # -> codegen_agent
    RETEST = "retest"  # -> testing_agent (confirm via re-execution, or the test itself needs attention)
    ARCHITECTURE_REVIEW = "architecture_review"  # -> architecture_agent
    ROLLBACK = "rollback"  # -> deployment_agent (recommendation only — never executed here)
    ESCALATE = "escalate"  # -> human / future incident workflow
    NO_ACTION = "no_action"  # evidence doesn't support any remediation


class RemediationRisk(StrEnum):
    """How risky the RECOMMENDED remediation itself is — independent of
    incident severity and independent of root_cause_confidence (see
    docs/architecture/incident_resolution_agent.md §10/§21). No existing
    risk-level model was found to reuse (app.agents.planning.Risk is a
    {description, mitigation} pair, not a level) — this is a genuinely new,
    narrow enum."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class ReviewStatus(StrEnum):
    """The Feature Review state machine (Level 3.4) — deliberately just
    three states, two of them terminal. PENDING -> APPROVED and
    PENDING -> REJECTED are the only allowed transitions; there is no
    REJECTED -> APPROVED or APPROVED -> REJECTED reopening in this level
    (see docs/architecture/feature_review.md "Review state machine"). This
    is a human governance decision, never something Detecting or any other
    agent can set — see app.feature_review.service."""

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
