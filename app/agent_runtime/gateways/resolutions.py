"""ResolutionGateway — the agent-facing ResolutionResult persistence
surface. Same shape as DetectionGateway/SignalGateway: a narrow Protocol
plus a thin RepositoryResolutionGateway that delegates directly to
whichever ResolutionRepository (in-memory or Firestore) it's given.

Added in Level 3.3 for IncidentResolutionAgent — the first (and, in this
level, only) agent that persists ResolutionResults.
"""

from typing import Protocol, runtime_checkable

from app.domain import ResolutionResult
from app.persistence.repositories.resolution import ResolutionQuery, ResolutionRepository


@runtime_checkable
class ResolutionGateway(Protocol):
    async def save(self, resolution: ResolutionResult) -> ResolutionResult: ...
    async def get(self, resolution_id: str) -> ResolutionResult | None: ...
    async def find_by_fingerprint(self, fingerprint: str) -> ResolutionResult | None: ...
    async def query(self, query: ResolutionQuery) -> list[ResolutionResult]: ...


class RepositoryResolutionGateway:
    """Delegates directly to a ResolutionRepository (in-memory or Firestore)."""

    def __init__(self, repository: ResolutionRepository):
        self._repository = repository

    async def save(self, resolution: ResolutionResult) -> ResolutionResult:
        return await self._repository.save(resolution)

    async def get(self, resolution_id: str) -> ResolutionResult | None:
        return await self._repository.get(resolution_id)

    async def find_by_fingerprint(self, fingerprint: str) -> ResolutionResult | None:
        return await self._repository.find_by_fingerprint(fingerprint)

    async def query(self, query: ResolutionQuery) -> list[ResolutionResult]:
        return await self._repository.query(query)
