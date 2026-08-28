"""Quipu's orchestration layer — the control plane that coordinates the
agent fleet. Separates AGENT EXECUTION (owned by app.agent_runtime /
app.agents.*) from WORKFLOW ORCHESTRATION (owned here): workflow
progression, agent selection, artifact handoffs, decisions, retry/replan
routing, workflow state, and recovery.

Framework-independent at its core (app.orchestration.service,
.transitions, .decisions, .errors) — Google ADK is isolated to
app.orchestration.adk. See docs/architecture/orchestration.md.
"""

from app.orchestration.decisions import ProposedDecision, WorkflowEvidence
from app.orchestration.errors import InvalidTransitionError, OrchestrationError, RetryLimitExceededError, UnknownAgentError
from app.orchestration.registry_setup import build_default_registry
from app.orchestration.service import OrchestrationService

__all__ = [
    "InvalidTransitionError",
    "OrchestrationError",
    "OrchestrationService",
    "ProposedDecision",
    "RetryLimitExceededError",
    "UnknownAgentError",
    "WorkflowEvidence",
    "build_default_registry",
]
