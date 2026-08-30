"""ArtifactRepository — Artifact persistence, independent of WorkflowState.

WorkflowState only ever holds artifact_ids; this is where the actual
(potentially large) Artifact.payload lives. Artifact has no workflow_id field
of its own (it's a Level 1.1 domain model, shared by whatever owns it), so
every method takes workflow_id explicitly — this also matches the
Firestore implementation's workflow-centric subcollection layout
(workflows/{workflow_id}/artifacts/{artifact_id}).

find_owning_workflow_id is the one exception: the sole caller that has an
artifact_id but genuinely does not know its owning workflow_id yet
(DetectionActionProcessor correlating an INCIDENT DetectionResult's
deployment_artifact_id back to the WorkflowState that produced it — see
app/detection/action_processor.py). It answers "which workflow_id owns
this artifact_id" from the Firestore collection-group document path
itself — the authoritative source of that relationship — rather than a
second, independently-maintained field that could drift from reality.
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

    async def find_owning_workflow_id(self, artifact_id: str) -> str | None:
        """Reverse lookup: given only an artifact_id, return the
        workflow_id that owns it, or None if no artifact with that id
        exists in any workflow."""
        ...
