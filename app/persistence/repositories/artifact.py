"""ArtifactRepository — Artifact persistence, independent of WorkflowState.

WorkflowState only ever holds artifact_ids; this is where the actual
(potentially large) Artifact.payload lives. Artifact has no workflow_id field
of its own (it's a Level 1.1 domain model, shared by whatever owns it), so
every method takes workflow_id explicitly — this also matches the
Firestore implementation's workflow-centric subcollection layout
(workflows/{workflow_id}/artifacts/{artifact_id}), so the same Protocol
works for both backends without a collection-group query.
"""

from typing import Protocol, runtime_checkable

from app.domain import Artifact


@runtime_checkable
class ArtifactRepository(Protocol):
    async def save(self, workflow_id: str, artifact: Artifact) -> Artifact:
        """Create-or-replace — artifacts are treated as immutable audit
        records, so this is a simple upsert, not create-vs-update."""
        ...

    async def get(self, workflow_id: str, artifact_id: str) -> Artifact | None: ...

    async def list_for_workflow(self, workflow_id: str) -> list[Artifact]: ...
