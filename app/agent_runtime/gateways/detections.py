"""DetectionGateway — the agent-facing DetectionResult persistence surface.
Same shape as SignalGateway (app/agent_runtime/gateways/signals.py): a
narrow Protocol plus a thin RepositoryDetectionGateway that delegates
directly to whichever DetectionRepository (in-memory or Firestore) it's
given, so agents never import a persistence backend directly.

Added in Level 3.2 for DetectingAgent — the first (and, in this level,
only) agent that persists DetectionResults.
"""

from typing import Protocol, runtime_checkable

from app.domain import DetectionResult
from app.persistence.repositories.detection import DetectionQuery, DetectionRepository


@runtime_checkable
class DetectionGateway(Protocol):
    async def save(self, detection: DetectionResult) -> DetectionResult: ...
    async def get(self, detection_id: str) -> DetectionResult | None: ...
    async def find_by_fingerprint(self, fingerprint: str) -> DetectionResult | None: ...
    async def query(self, query: DetectionQuery) -> list[DetectionResult]: ...


class RepositoryDetectionGateway:
    """Delegates directly to a DetectionRepository (in-memory or Firestore)."""

    def __init__(self, repository: DetectionRepository):
        self._repository = repository

    async def save(self, detection: DetectionResult) -> DetectionResult:
        return await self._repository.save(detection)

    async def get(self, detection_id: str) -> DetectionResult | None:
        return await self._repository.get(detection_id)

    async def find_by_fingerprint(self, fingerprint: str) -> DetectionResult | None:
        return await self._repository.find_by_fingerprint(fingerprint)

    async def query(self, query: DetectionQuery) -> list[DetectionResult]:
        return await self._repository.query(query)
