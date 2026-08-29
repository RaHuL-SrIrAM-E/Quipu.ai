"""AgentContext — the runtime environment injected into an agent's execution.

A plain dataclass, not a domain model: it carries live gateway objects and a
logger, none of which are meant to be serialized. Gateways are typed as
Protocols so test doubles can be injected without any real backend.
"""

from dataclasses import dataclass, field
from logging import Logger
from typing import Any

from app.agent_runtime.gateways.artifacts import ArtifactGateway
from app.agent_runtime.gateways.detections import DetectionGateway
from app.agent_runtime.gateways.knowledge import KnowledgeGateway
from app.agent_runtime.gateways.resolutions import ResolutionGateway
from app.agent_runtime.gateways.signals import SignalGateway
from app.agent_runtime.gateways.tools import ToolGateway
from app.core.observability import get_logger
from app.persistence.repositories.execution import AgentExecutionRepository


@dataclass
class AgentContext:
    workflow_id: str
    execution_id: str
    knowledge: KnowledgeGateway
    tools: ToolGateway
    artifacts: ArtifactGateway
    logger: Logger = field(default_factory=lambda: get_logger("quipu.agent_runtime"))
    metadata: dict[str, Any] = field(default_factory=dict)

    # Optional (Level 1.4/1.5 bridge): lets an agent record its own
    # AgentExecution — started/completed/status/output_artifact_ids — through
    # the existing persistence repository, instead of a bespoke lifecycle
    # side-channel. None when the caller hasn't wired persistence up (e.g. a
    # quick local/dev invocation); agents must treat that as "don't persist,"
    # not as an error.
    executions: AgentExecutionRepository | None = None

    # Optional (Level 3.1 bridge, same shape as `executions` above): lets an
    # agent persist Signals through SignalRepository. Only MonitoringAgent
    # uses this today — every other existing agent leaves it None and is
    # unaffected, since it defaults to None like `executions` did.
    signals: SignalGateway | None = None

    # Optional (Level 3.2 bridge, same shape as `signals` above): lets an
    # agent persist DetectionResults through DetectionRepository. Only
    # DetectingAgent uses this today.
    detections: DetectionGateway | None = None

    # Optional (Level 3.3 bridge, same shape as `detections` above): lets an
    # agent persist ResolutionResults through ResolutionRepository. Only
    # IncidentResolutionAgent uses this today.
    resolutions: ResolutionGateway | None = None
