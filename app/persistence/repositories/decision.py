"""DecisionRepository — Decisions are audit records and must remain
independently queryable, even though Decision itself has no workflow_id
field (it's a Level 1.1 domain model owned by whoever recorded it)."""

from typing import Protocol, runtime_checkable

from app.domain import Decision


@runtime_checkable
class DecisionRepository(Protocol):
    async def save(self, workflow_id: str, decision: Decision) -> Decision:
        """Decisions are immutable once made — create-or-replace by decision_id."""
        ...

    async def get(self, workflow_id: str, decision_id: str) -> Decision | None: ...

    async def list_for_workflow(self, workflow_id: str) -> list[Decision]: ...
