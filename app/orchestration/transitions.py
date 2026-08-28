"""Application-level transition policy — the orchestrator's own control
graph. Gemini proposes a Decision; this module is what actually decides
whether that Decision is allowed to execute. Nothing here trusts the model
to have obeyed the graph on its own.

Deployment is deliberately absent from STAGE_ORDER (Level 2.0 scope: the
happy path stops after Testing until a Deployment Agent exists).
"""

from app.domain import ArtifactType, DecisionAction, WorkflowStage
from app.orchestration.errors import InvalidTransitionError

STAGE_ORDER: list[WorkflowStage] = [
    WorkflowStage.PLANNING,
    WorkflowStage.ARCHITECTURE,
    WorkflowStage.CODEGEN,
    WorkflowStage.TESTING,
]

STAGE_TO_AGENT_ID: dict[WorkflowStage, str] = {
    WorkflowStage.PLANNING: "planning_agent",
    WorkflowStage.ARCHITECTURE: "architecture_agent",
    WorkflowStage.CODEGEN: "codegen_agent",
    WorkflowStage.TESTING: "testing_agent",
}

STAGE_TO_ARTIFACT_TYPE: dict[WorkflowStage, ArtifactType] = {
    WorkflowStage.PLANNING: ArtifactType.PLAN,
    WorkflowStage.ARCHITECTURE: ArtifactType.ARCHITECTURE,
    WorkflowStage.CODEGEN: ArtifactType.CODE_CHANGE,
    WorkflowStage.TESTING: ArtifactType.TEST_RESULT,
}

# The only backward/lateral jumps the orchestrator will ever authorize from
# TESTING, regardless of what a proposed Decision asks for. Testing -> Planning
# is deliberately not in this set — see docs/architecture/orchestration.md
# "Transition policy".
_ALLOWED_RETRY_TARGETS: dict[WorkflowStage, set[str]] = {
    WorkflowStage.TESTING: {"codegen_agent", "architecture_agent", "testing_agent"},
    WorkflowStage.CODEGEN: {"codegen_agent"},
    WorkflowStage.ARCHITECTURE: {"architecture_agent"},
    WorkflowStage.PLANNING: {"planning_agent"},
}


def next_stage(current: WorkflowStage) -> WorkflowStage | None:
    try:
        index = STAGE_ORDER.index(current)
    except ValueError:
        return None
    if index + 1 < len(STAGE_ORDER):
        return STAGE_ORDER[index + 1]
    return None  # TESTING is currently the last stage — no Deployment yet


def can_transition(current_stage: WorkflowStage, action: DecisionAction, target_agent: str | None) -> None:
    """Raises InvalidTransitionError if the action isn't structurally valid
    from current_stage. Does not check retry budgets — see
    app.orchestration.service for that (it needs workflow history, not just
    the static graph)."""

    if action == DecisionAction.CONTINUE:
        # Always structurally valid: next_stage() returning None just means
        # this is the last implemented stage (TESTING today — no Deployment
        # yet), and CONTINUE there means "the workflow is done," not
        # "invalid." See _execute_decision for how that's actually applied.
        return

    if action in (DecisionAction.RETRY, DecisionAction.REPLAN):
        if target_agent is None:
            raise InvalidTransitionError(f"{action} requires a target_agent")
        allowed = _ALLOWED_RETRY_TARGETS.get(current_stage, set())
        if target_agent not in allowed:
            raise InvalidTransitionError(
                f"{action} to '{target_agent}' is not an allowed transition from stage '{current_stage}' "
                f"(allowed: {sorted(allowed)})"
            )
        return

    if action in (DecisionAction.ESCALATE, DecisionAction.COMPLETE, DecisionAction.FAIL):
        return  # always structurally valid from any stage

    if action in (DecisionAction.SKIP, DecisionAction.WAIT, DecisionAction.ROLLBACK):
        raise InvalidTransitionError(f"action '{action}' is not supported by the Level 2.0 orchestrator")

    raise InvalidTransitionError(f"unrecognized action '{action}'")
