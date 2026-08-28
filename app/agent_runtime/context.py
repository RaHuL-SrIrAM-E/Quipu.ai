"""AgentContext — the runtime environment injected into an agent's execution.

A plain dataclass, not a domain model: it carries live gateway objects and a
logger, none of which are meant to be serialized. Gateways are typed as
Protocols so test doubles can be injected without any real backend.
"""

from dataclasses import dataclass, field
from logging import Logger
from typing import Any

from app.agent_runtime.gateways.artifacts import ArtifactGateway
from app.agent_runtime.gateways.knowledge import KnowledgeGateway
from app.agent_runtime.gateways.tools import ToolGateway
from app.core.observability import get_logger


@dataclass
class AgentContext:
    workflow_id: str
    execution_id: str
    knowledge: KnowledgeGateway
    tools: ToolGateway
    artifacts: ArtifactGateway
    logger: Logger = field(default_factory=lambda: get_logger("quipu.agent_runtime"))
    metadata: dict[str, Any] = field(default_factory=dict)
