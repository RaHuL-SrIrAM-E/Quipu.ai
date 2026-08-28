"""Decision evidence and the deterministic part of decision-making.

Reuses app.domain.Decision/DecisionAction/DecisionSource directly — no
competing decision model. ProposedDecision is the ADK-facing structured
output shape (no `source`: the model never gets to claim it's the
orchestrator; app.orchestration.service sets that after validating).
"""

from pydantic import BaseModel, Field

from app.agents.deployment import DeploymentFailureClassification
from app.agents.testing import FailureClassification
from app.domain import Decision, DecisionAction, DecisionSource, WorkflowStage

# Unambiguous per the task's own examples — when every failure in a Testing
# run shares one classification, the routing is mechanical and asking Gemini
# to "reason" about it would just be theater. Anything not covered here
# (mixed classifications, DEPENDENCY_FAILURE, an empty/ambiguous case) is
# left for the orchestration LlmAgent to actually reason about.
_DETERMINISTIC_ROUTING: dict[FailureClassification, tuple[DecisionAction, str | None]] = {
    FailureClassification.CODE_DEFECT: (DecisionAction.RETRY, "codegen_agent"),
    FailureClassification.ARCHITECTURE_DEFECT: (DecisionAction.REPLAN, "architecture_agent"),
    FailureClassification.TEST_DEFECT: (DecisionAction.RETRY, "testing_agent"),
    FailureClassification.ENVIRONMENT_FAILURE: (DecisionAction.RETRY, "testing_agent"),
    FailureClassification.UNKNOWN: (DecisionAction.ESCALATE, None),
}


# Deployment produces exactly one classification per attempt (not a list of
# per-test failures like Testing), so routing is a straight lookup rather
# than needing an ambiguous-case fallback to the LLM. BUILD_FAILURE and
# HEALTH_CHECK_FAILURE route back to Codegen — those indicate the code
# itself doesn't run correctly in the deployment environment, not a
# deployment-configuration problem. PERMISSION_FAILURE and UNKNOWN escalate
# — neither is something an automatic retry can fix.
_DEPLOYMENT_DETERMINISTIC_ROUTING: dict[DeploymentFailureClassification, tuple[DecisionAction, str | None]] = {
    DeploymentFailureClassification.CONFIGURATION_FAILURE: (DecisionAction.RETRY, "deployment_agent"),
    DeploymentFailureClassification.PERMISSION_FAILURE: (DecisionAction.ESCALATE, None),
    DeploymentFailureClassification.BUILD_FAILURE: (DecisionAction.RETRY, "codegen_agent"),
    DeploymentFailureClassification.PLATFORM_FAILURE: (DecisionAction.RETRY, "deployment_agent"),
    DeploymentFailureClassification.HEALTH_CHECK_FAILURE: (DecisionAction.RETRY, "codegen_agent"),
    DeploymentFailureClassification.NETWORK_FAILURE: (DecisionAction.RETRY, "deployment_agent"),
    DeploymentFailureClassification.UNKNOWN: (DecisionAction.ESCALATE, None),
}


def deployment_deterministic_action(
    classification: DeploymentFailureClassification | None,
) -> tuple[DecisionAction, str | None]:
    """Always returns a routing — deployment failures are never ambiguous
    the way a mixed Testing failure set can be, so this never falls back to
    the orchestration LlmAgent. A missing/unrecognized classification is
    treated the same as UNKNOWN: escalate rather than guess."""
    if classification is None:
        return DecisionAction.ESCALATE, None
    return _DEPLOYMENT_DETERMINISTIC_ROUTING.get(classification, (DecisionAction.ESCALATE, None))


class ProposedDecision(BaseModel):
    """What the orchestration LlmAgent is allowed to produce — a recommendation,
    not an authorized action. app.orchestration.service turns this into a
    real Decision (adding id/source/timestamp) only after policy validation."""

    action: DecisionAction
    target_agent: str | None = None
    reason: str
    confidence: float = Field(ge=0.0, le=1.0)


class WorkflowEvidence(BaseModel):
    """Structured facts handed to the orchestration LlmAgent — never the
    other way around. The model reasons about this; it cannot invent facts
    outside it."""

    workflow_id: str
    current_stage: WorkflowStage
    test_status: str | None = None
    failure_classifications: list[str] = Field(default_factory=list)
    retry_count: int = 0
    max_retries: int = 0
    summary: str = ""


def deterministic_action(classifications: list[FailureClassification]) -> tuple[DecisionAction, str | None] | None:
    """Returns (action, target_agent) if every failure shares one
    unambiguous classification with a known routing; None if the model
    needs to actually reason about it (mixed classifications, or one this
    table doesn't cover)."""
    if not classifications:
        return None
    unique = set(classifications)
    if len(unique) != 1:
        return None
    return _DETERMINISTIC_ROUTING.get(next(iter(unique)))


def build_decision(proposed: ProposedDecision, *, source: DecisionSource) -> Decision:
    """The one place a ProposedDecision becomes an authoritative Decision —
    only ever called after transition-policy validation has already run."""
    return Decision(
        action=proposed.action,
        target_agent=proposed.target_agent,
        reason=proposed.reason,
        confidence=proposed.confidence,
        source=source,
    )
