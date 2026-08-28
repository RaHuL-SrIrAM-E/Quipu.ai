"""QuipuAgent — the base agent abstraction.

execute() is concrete and owns the lifecycle transitions (CREATED ->
INITIALIZING -> RUNNING -> COMPLETED/FAILED); concrete agents implement
_perform() with their actual reasoning. This keeps lifecycle bookkeeping out
of every agent implementation while still exposing execute() as the single
entry point the orchestrator calls, as specified.

Agents never call other agents directly — routing belongs to the orchestrator,
which this class has no knowledge of.
"""

from abc import ABC, abstractmethod

from app.agent_runtime.capabilities import AgentCapability, check_capability
from app.agent_runtime.context import AgentContext
from app.agent_runtime.identity import AgentIdentity
from app.agent_runtime.status import AgentStatus
from app.domain import AgentInput, AgentOutput


class QuipuAgent(ABC):
    def __init__(self) -> None:
        self._status = AgentStatus.CREATED

    @property
    @abstractmethod
    def identity(self) -> AgentIdentity: ...

    @property
    @abstractmethod
    def capabilities(self) -> set[AgentCapability]: ...

    @property
    def status(self) -> AgentStatus:
        return self._status

    def require_capability(self, capability: AgentCapability) -> None:
        check_capability(self.identity.agent_id, self.capabilities, capability)

    @abstractmethod
    async def _perform(self, agent_input: AgentInput, context: AgentContext) -> AgentOutput:
        """Agent-specific reasoning. Implement this, not execute()."""

    async def execute(self, agent_input: AgentInput, context: AgentContext) -> AgentOutput:
        self._status = AgentStatus.INITIALIZING
        self._status = AgentStatus.RUNNING
        try:
            output = await self._perform(agent_input, context)
        except Exception:
            self._status = AgentStatus.FAILED
            raise
        self._status = AgentStatus.COMPLETED
        return output
