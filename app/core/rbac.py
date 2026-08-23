"""Agent-level RBAC: each agent stage declares the permissions it needs,
and is only allowed to act within that scope regardless of what it decides to do internally.
"""

from dataclasses import dataclass, field
from enum import StrEnum


class Permission(StrEnum):
    READ_CODEBASE = "read_codebase"
    WRITE_CODE = "write_code"
    READ_KNOWLEDGE_BASE = "read_knowledge_base"
    RUN_TESTS = "run_tests"
    DEPLOY = "deploy"
    MODIFY_INFRA = "modify_infra"
    PAGE_ONCALL = "page_oncall"
    READ_MONITORING = "read_monitoring"


class PermissionDeniedError(Exception):
    pass


@dataclass
class AgentRole:
    name: str
    permissions: frozenset[Permission] = field(default_factory=frozenset)

    def requires(self, permission: Permission) -> None:
        if permission not in self.permissions:
            raise PermissionDeniedError(f"role '{self.name}' lacks permission '{permission}'")


# Default role grants per pipeline stage. Tune per deployment/org policy.
STAGE_ROLES: dict[str, AgentRole] = {
    "feature_detection": AgentRole("feature_detection", frozenset({Permission.READ_CODEBASE, Permission.READ_KNOWLEDGE_BASE})),
    "planning": AgentRole("planning", frozenset({Permission.READ_CODEBASE, Permission.READ_KNOWLEDGE_BASE})),
    "architecture": AgentRole("architecture", frozenset({Permission.READ_CODEBASE, Permission.READ_KNOWLEDGE_BASE})),
    "coding": AgentRole("coding", frozenset({Permission.READ_CODEBASE, Permission.WRITE_CODE, Permission.READ_KNOWLEDGE_BASE})),
    "testing": AgentRole("testing", frozenset({Permission.READ_CODEBASE, Permission.RUN_TESTS})),
    "devops": AgentRole("devops", frozenset({Permission.READ_CODEBASE, Permission.DEPLOY, Permission.MODIFY_INFRA})),
    "monitoring": AgentRole("monitoring", frozenset({Permission.READ_MONITORING})),
    "incident_management": AgentRole(
        "incident_management",
        frozenset({Permission.READ_MONITORING, Permission.PAGE_ONCALL, Permission.READ_CODEBASE}),
    ),
}
