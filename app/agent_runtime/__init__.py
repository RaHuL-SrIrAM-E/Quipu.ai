"""Quipu's framework-agnostic agent runtime: what an agent is, how it executes,
what it's permitted to do, and how it reaches knowledge/tools/artifacts.

Depends on app.domain only. No Google ADK, no LLM calls, no real gateway
backends — those come with concrete agent implementations and ADK adapters later.
"""

from app.agent_runtime.base import QuipuAgent
from app.agent_runtime.capabilities import AgentCapability, CapabilityError, check_capability
from app.agent_runtime.context import AgentContext
from app.agent_runtime.gateways import ArtifactGateway, KnowledgeGateway, ToolGateway
from app.agent_runtime.identity import AgentIdentity
from app.agent_runtime.registry import AgentNotFoundError, AgentRegistry, DuplicateAgentError
from app.agent_runtime.status import AgentStatus

__all__ = [
    "AgentCapability",
    "AgentContext",
    "AgentIdentity",
    "AgentNotFoundError",
    "AgentRegistry",
    "AgentStatus",
    "ArtifactGateway",
    "CapabilityError",
    "DuplicateAgentError",
    "KnowledgeGateway",
    "QuipuAgent",
    "ToolGateway",
    "check_capability",
]
