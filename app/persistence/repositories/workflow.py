"""WorkflowRepository — framework-agnostic persistence interface for WorkflowState.

update_if_version is the safe, concurrency-aware update path: it fails with
VersionConflictError rather than silently overwriting a concurrent change —
this is the operation Quipu should use whenever more than one agent/event
could be touching the same workflow. Plain update() still exists for callers
that don't need that guarantee (e.g. immediately after create()).
"""

from typing import Protocol, runtime_checkable

from app.domain import WorkflowState, WorkflowStatus


@runtime_checkable
class WorkflowRepository(Protocol):
    async def create(self, workflow: WorkflowState) -> WorkflowState:
        """Raises DuplicateEntityError if workflow_id already exists."""
        ...

    async def get(self, workflow_id: str) -> WorkflowState | None: ...

    async def list_recent(self, *, status: WorkflowStatus | None = None, limit: int = 50) -> list[WorkflowState]:
        """Added for the Control Plane API's `GET /workflows` (§4 of that
        task) — the only listing WorkflowRepository needed until now, since
        every prior caller always already had a workflow_id in hand.
        WorkflowState carries no timestamp field of its own (Level 1.1
        scope), so ordering is implementation-defined (insertion order for
        the in-memory repo, `__name__` document order for Firestore) rather
        than a fabricated recency guarantee; always bounded by `limit`."""
        ...

    async def update(self, workflow: WorkflowState) -> WorkflowState:
        """Unconditional overwrite. Raises EntityNotFoundError if missing."""
        ...

    async def delete(self, workflow_id: str) -> None:
        """Raises EntityNotFoundError if missing."""
        ...

    async def update_if_version(
        self, workflow_id: str, expected_version: int, updated_workflow: WorkflowState
    ) -> WorkflowState:
        """Raises EntityNotFoundError if missing, VersionConflictError if the
        stored version != expected_version. On success, the stored version is
        expected_version + 1."""
        ...
