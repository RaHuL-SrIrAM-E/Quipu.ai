"""Builds the AgentRegistry the orchestrator resolves agents through.

The five SDLC agents (Planning/Architecture/Codegen/Testing/Deployment)
plus MonitoringAgent (Level 3.1), DetectingAgent (Level 3.2), and
IncidentResolutionAgent (Level 3.3) are registered here. None of the three
continuous-intelligence agents is part of STAGE_ORDER/STAGE_TO_AGENT_ID in
app.orchestration.transitions — they are not SDLC stages; they're
standalone agents invoked directly (or by a future scheduler), not through
OrchestrationService.execute_next_step(). Registering them here still makes
them resolvable via registry.get("monitoring_agent")/
registry.get("detecting_agent")/registry.get("incident_resolution_agent"),
consistent with how every other Quipu-native agent is discovered.
IncidentResolutionAgent produces a ResolutionResult recommending a
target_agent among the SDLC agents already registered here
(codegen_agent/testing_agent/deployment_agent/architecture_agent) — routing
to that target is future orchestration work, not implemented in this level.
"""

from app.agent_runtime.registry import AgentRegistry
from app.agents.architecture import ArchitectureAgent
from app.agents.codegen import CodegenAgent
from app.agents.deployment import DeploymentAgent
from app.agents.detecting import DetectingAgent
from app.agents.incident_resolution import IncidentResolutionAgent
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
    registry.register(IncidentResolutionAgent())
    return registry
