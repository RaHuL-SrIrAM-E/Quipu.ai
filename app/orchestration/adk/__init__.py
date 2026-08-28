"""Google ADK adapter boundary for the orchestration layer.

Everything that imports google.adk lives in this subpackage.
app.orchestration.service imports from here but the reverse never happens —
this keeps ADK isolated from the framework-independent orchestration logic
(transitions, decisions, persistence), consistent with how app.knowledge and
app.persistence isolate their own Google SDK dependencies.
"""

from app.orchestration.adk.adapter import QuipuAgentAdkAdapter
from app.orchestration.adk.decision_agent import decision_agent, propose_decision
from app.orchestration.adk.loop import build_recovery_loop_agent
from app.orchestration.adk.sequential import build_happy_path_sequential_agent

__all__ = [
    "QuipuAgentAdkAdapter",
    "build_happy_path_sequential_agent",
    "build_recovery_loop_agent",
    "decision_agent",
    "propose_decision",
]
