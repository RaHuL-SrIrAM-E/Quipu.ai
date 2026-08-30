"""Structured orchestration errors. Framework-independent — no ADK, no
Firestore — the orchestration service translates persistence/ADK failures
into these at its own boundary, the same pattern used throughout app.knowledge
and app.persistence.
"""


class OrchestrationError(Exception):
    """Base class for all orchestration-layer errors."""


class UnknownAgentError(OrchestrationError):
    def __init__(self, agent_id: str):
        self.agent_id = agent_id
        super().__init__(f"no agent registered with id '{agent_id}'")


class InvalidTransitionError(OrchestrationError):
    """Raised when a requested/proposed workflow transition isn't allowed —
    either the stage/action combination doesn't exist, or policy (retry
    limits, required artifacts) blocks it right now."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


class RetryLimitExceededError(InvalidTransitionError):
    def __init__(self, stage: str, limit: int):
        self.stage = stage
        self.limit = limit
        super().__init__(f"retry limit ({limit}) exceeded for stage '{stage}'")


class WorkspaceProvisioningError(OrchestrationError):
    """Raised by OrchestrationService._ensure_workspace when a stage that
    needs a checked-out repository has no repo_url it can resolve (no
    Ticket.metadata override and no Settings.default_repo_url), or the
    actual `git clone` fails. execute_next_step catches this and fails the
    workflow deterministically via _fail_workflow — Planning/Architecture/
    Codegen/Testing must never start without a real, resolvable workspace."""
