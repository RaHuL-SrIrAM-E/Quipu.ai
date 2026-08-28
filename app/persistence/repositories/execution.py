"""AgentExecutionRepository — independently queryable execution records, for
future observability/auditing ("which agent ran, when, with what result").

AgentExecution already carries workflow_id, so create()/update() don't need
it passed separately; get()/list_for_workflow() do, since you don't have the
object yet.
"""

from typing import Protocol, runtime_checkable

from app.domain import AgentExecution


@runtime_checkable
class AgentExecutionRepository(Protocol):
    async def create(self, execution: AgentExecution) -> AgentExecution:
        """Raises DuplicateEntityError if execution_id already exists."""
        ...

    async def get(self, workflow_id: str, execution_id: str) -> AgentExecution | None: ...

    async def list_for_workflow(self, workflow_id: str) -> list[AgentExecution]: ...

    async def update(self, execution: AgentExecution) -> AgentExecution:
        """Raises EntityNotFoundError if missing."""
        ...
