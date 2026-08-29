"""Builds the AgentRegistry the orchestrator resolves agents through.

The five SDLC agents (Planning/Architecture/Codegen/Testing/Deployment) plus
MonitoringAgent (Level 3.1) and DetectingAgent (Level 3.2) are registered
here. Neither Monitoring nor Detecting is part of STAGE_ORDER/
STAGE_TO_AGENT_ID in app.orchestration.transitions — they are not SDLC
stages; they're standalone continuous-intelligence agents invoked directly
(or by a future scheduler), not through
OrchestrationService.execute_next_step(). Registering them here still makes
them resolvable via registry.get("monitoring_agent")/
registry.get("detecting_agent"), consistent with how every other
Quipu-native agent is discovered.
"""

from app.agent_runtime.registry import AgentRegistry
from app.agents.architecture import ArchitectureAgent
from app.agents.codegen import CodegenAgent
from app.agents.deployment import DeploymentAgent
from app.agents.detecting import DetectingAgent
from app.agents.monitoring import MonitoringAgent
from app.agents.planning import PlanningAgent
from app.agents.testing import TestingAgent


def build_default_registry() -> AgentRegistry:
    registry = AgentRegistry()
    registry.register(PlanningAgent())
    registry.register(ArchitectureAgent())
    registry.register(CodegenAgent())
    registry.register(TestingAgent())
    registry.register(DeploymentAgent())
    registry.register(MonitoringAgent())
    registry.register(DetectingAgent())
    return registry
