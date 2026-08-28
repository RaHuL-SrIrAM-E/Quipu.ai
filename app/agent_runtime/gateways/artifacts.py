"""ArtifactGateway — abstraction only. No persistence backend here."""

from typing import Protocol, runtime_checkable

from app.domain import Artifact


@runtime_checkable
class ArtifactGateway(Protocol):
    async def get(self, artifact_id: str) -> Artifact | None: ...
    async def save(self, artifact: Artifact) -> None: ...
