"""AgentCapability — the closed set of permissions an agent can be granted, and
the enforcement primitives that check them. Permissions are enforced here, in
application code — never left to the LLM to self-police.
"""

from enum import StrEnum


class AgentCapability(StrEnum):
    READ_TICKET = "read_ticket"
    READ_REPOSITORY = "read_repository"
    READ_PLAN = "read_plan"
    READ_ARCHITECTURE = "read_architecture"
    READ_CODE_CHANGE = "read_code_change"
    QUERY_KNOWLEDGE = "query_knowledge"
    CREATE_PLAN = "create_plan"
    CREATE_ARCHITECTURE = "create_architecture"
    WRITE_CODE = "write_code"
    CREATE_COMMIT = "create_commit"
    RUN_TESTS = "run_tests"
    BUILD = "build"
    DEPLOY = "deploy"
    HEALTH_CHECK = "health_check"
    READ_MONITORING = "read_monitoring"
    CREATE_INCIDENT = "create_incident"
    RESOLVE_INCIDENT = "resolve_incident"
    ROLLBACK = "rollback"

    # Level 1.5: generic artifact persistence (any agent producing/reading a
    # workflow artifact, not just plans) and external-tracker writes. WRITE_JIRA
    # is deliberately narrow — "create/update an issue in the external tracker" —
    # rather than a broad WRITE_ANYTHING, since Jira is the only external tool
    # an agent writes to today.
    READ_ARTIFACT = "read_artifact"
    WRITE_ARTIFACT = "write_artifact"
    WRITE_JIRA = "write_jira"

    # Level 3.2 (DetectingAgent): reading persisted Signal evidence through
    # SignalGateway is a distinct concern from READ_MONITORING (which gates
    # calling the *live* Cloud Monitoring/Logging APIs — MonitoringAgent's
    # concern). Detecting never touches those APIs directly, only the
    # already-collected, already-sanitized Signal record — hence a separate,
    # narrower capability rather than reusing READ_MONITORING. WRITE_DETECTION
    # mirrors WRITE_ARTIFACT's role for every other agent: permission to
    # persist the agent's OWN structured output/audit record (DetectionResult)
    # through DetectionGateway — not a real-world mutation capability like
    # DEPLOY/WRITE_CODE, so it stays part of Detecting's read-only posture.
    READ_SIGNALS = "read_signals"
    WRITE_DETECTION = "write_detection"

    # Level 3.3 (IncidentResolutionAgent): reading a persisted DetectionResult
    # through DetectionGateway is a distinct concern from WRITE_DETECTION
    # (which gates Detecting's own persistence of its interpretation) — a
    # read-only counterpart, same split as READ_ARTIFACT/WRITE_ARTIFACT.
    # WRITE_RESOLUTION mirrors WRITE_DETECTION's role: permission to persist
    # this agent's OWN structured output (ResolutionResult) through
    # ResolutionGateway — not a real-world mutation capability. Note this is
    # deliberately NOT the same as RESOLVE_INCIDENT (above, pre-existing):
    # RESOLVE_INCIDENT is reserved for whatever future component actually
    # closes out/executes remediation for an incident; IncidentResolutionAgent
    # only ever proposes a plan, so it must never hold RESOLVE_INCIDENT — see
    # docs/architecture/incident_resolution_agent.md.
    READ_DETECTION = "read_detection"
    WRITE_RESOLUTION = "write_resolution"

    # Level 3.4 (Feature Review — a deterministic service, NOT an agent; see
    # app/feature_review/service.py): permission to approve/reject a
    # FEATURE_OPPORTUNITY DetectionResult. Deliberately independent of
    # WRITE_CODE/DEPLOY/RESOLVE_INCIDENT — reviewing a product opportunity
    # has nothing to do with those. This capability alone is NOT sufficient
    # authorization to approve/reject: the caller's DecisionSource must also
    # be HUMAN (see FeatureReview.reviewer_type) — an agent holding this
    # capability could still never satisfy that second, independent check,
    # which is what actually prevents Detecting (or any agent) from
    # approving its own feature opportunity.
    REVIEW_FEATURE_OPPORTUNITY = "review_feature_opportunity"


class CapabilityError(RuntimeError):
    """Raised when an agent attempts to act outside its granted capabilities."""

    def __init__(self, agent_id: str, capability: AgentCapability):
        self.agent_id = agent_id
        self.capability = capability
        super().__init__(f"agent '{agent_id}' lacks required capability '{capability}'")


def check_capability(
    agent_id: str, granted: set[AgentCapability], required: AgentCapability
) -> None:
    if required not in granted:
        raise CapabilityError(agent_id, required)
