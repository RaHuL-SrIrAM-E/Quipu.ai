"""AgentIdentity — stable, immutable identity for logging, routing and auditing."""

from pydantic import BaseModel, ConfigDict


class AgentIdentity(BaseModel):
    model_config = ConfigDict(frozen=True)

    agent_id: str
    name: str
    version: str
    description: str
