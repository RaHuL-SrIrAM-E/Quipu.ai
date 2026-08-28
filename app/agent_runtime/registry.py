"""AgentRegistry — in-memory registration and lookup. No database or service discovery."""

from app.agent_runtime.base import QuipuAgent
from app.agent_runtime.capabilities import AgentCapability


class DuplicateAgentError(RuntimeError):
    def __init__(self, agent_id: str):
        super().__init__(f"agent '{agent_id}' is already registered")


class AgentNotFoundError(RuntimeError):
    def __init__(self, agent_id: str):
        super().__init__(f"no agent registered with id '{agent_id}'")


class AgentRegistry:
    def __init__(self) -> None:
        self._agents: dict[str, QuipuAgent] = {}

    def register(self, agent: QuipuAgent) -> None:
        agent_id = agent.identity.agent_id
        if agent_id in self._agents:
            raise DuplicateAgentError(agent_id)
        self._agents[agent_id] = agent

    def get(self, agent_id: str) -> QuipuAgent:
        try:
            return self._agents[agent_id]
        except KeyError:
            raise AgentNotFoundError(agent_id) from None

    def list_agents(self) -> list[QuipuAgent]:
        return list(self._agents.values())

    def find_by_capability(self, capability: AgentCapability) -> list[QuipuAgent]:
        return [agent for agent in self._agents.values() if capability in agent.capabilities]
