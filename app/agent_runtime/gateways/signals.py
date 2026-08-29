"""SignalGateway — the agent-facing Signal persistence surface. Same shape
as ArtifactGateway (app/agent_runtime/gateways/artifacts.py): a narrow
Protocol plus a thin RepositorySignalGateway that delegates directly to
whichever SignalRepository (in-memory or Firestore) it's given, so agents
never import a persistence backend directly.

Added in Level 3.1 for MonitoringAgent — the first agent that persists
Signals rather than Artifacts.
"""

from typing import Protocol, runtime_checkable

from app.domain import Signal
from app.persistence.repositories.signal import SignalQuery, SignalRepository


@runtime_checkable
class SignalGateway(Protocol):
    async def save(self, signal: Signal) -> Signal: ...
    async def get(self, signal_id: str) -> Signal | None: ...
    async def find_by_fingerprint(self, fingerprint: str) -> Signal | None: ...
    async def query(self, query: SignalQuery) -> list[Signal]: ...


class RepositorySignalGateway:
    """Delegates directly to a SignalRepository (in-memory or Firestore)."""

    def __init__(self, repository: SignalRepository):
        self._repository = repository

    async def save(self, signal: Signal) -> Signal:
        return await self._repository.save(signal)

    async def get(self, signal_id: str) -> Signal | None:
        return await self._repository.get(signal_id)

    async def find_by_fingerprint(self, fingerprint: str) -> Signal | None:
        return await self._repository.find_by_fingerprint(fingerprint)

    async def query(self, query: SignalQuery) -> list[Signal]:
        return await self._repository.query(query)
