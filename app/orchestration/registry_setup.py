"""Builds the AgentRegistry the orchestrator resolves agents through.

Only the four currently-implemented Quipu-native agents are registered.
Adding Deployment/Monitoring/Detecting/Incident-Resolution later is just
another registry.register(...) call — nothing in the orchestrator hardcodes
these four beyond STAGE_TO_AGENT_ID in app.orchestration.transitions.
"""

from app.agent_runtime.registry import AgentRegistry
from app.agents.architecture import ArchitectureAgent
from app.agents.codegen import CodegenAgent
from app.agents.planning import PlanningAgent
from app.agents.testing import TestingAgent


def build_default_registry() -> AgentRegistry:
    registry = AgentRegistry()
    registry.register(PlanningAgent())
    registry.register(ArchitectureAgent())
    registry.register(CodegenAgent())
    registry.register(TestingAgent())
    return registry
