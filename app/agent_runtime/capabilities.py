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
