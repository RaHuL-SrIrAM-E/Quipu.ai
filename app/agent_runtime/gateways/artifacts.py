"""ArtifactGateway — the agent-facing artifact persistence surface.

Level 1.5 note: this Protocol's signature was evolved to take workflow_id
(save(workflow_id, artifact) / get(workflow_id, artifact_id)), aligning it
with app.persistence.repositories.artifact.ArtifactRepository — Artifact has
no workflow_id field of its own, and the two Protocols existed independently
until an agent (Planning) actually needed to persist one through both layers.
RepositoryArtifactGateway below is the small, direct bridge: it just
delegates to whatever ArtifactRepository it's given (in-memory or Firestore),
so agents never import a persistence backend directly.
"""

from typing import Protocol, runtime_checkable

from app.domain import Artifact
from app.persistence.repositories.artifact import ArtifactRepository


@runtime_checkable
class ArtifactGateway(Protocol):
    async def get(self, workflow_id: str, artifact_id: str) -> Artifact | None: ...
    async def save(self, workflow_id: str, artifact: Artifact) -> Artifact: ...
    async def find_owning_workflow_id(self, artifact_id: str) -> str | None: ...


class RepositoryArtifactGateway:
    """Delegates directly to an ArtifactRepository (in-memory or Firestore)."""

    def __init__(self, repository: ArtifactRepository):
        self._repository = repository

    async def get(self, workflow_id: str, artifact_id: str) -> Artifact | None:
        return await self._repository.get(workflow_id, artifact_id)

    async def save(self, workflow_id: str, artifact: Artifact) -> Artifact:
        return await self._repository.save(workflow_id, artifact)

    async def find_owning_workflow_id(self, artifact_id: str) -> str | None:
        return await self._repository.find_owning_workflow_id(artifact_id)
