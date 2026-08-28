"""RetrievalBackend — abstraction over the underlying retrieval engine.

KnowledgeService depends on this, not a concrete search engine, so a future
Google Agent Search adapter can be swapped in without changing KnowledgeService
or any agent-facing code. No real search engine here — see
app/knowledge/backends/in_memory.py for the deterministic fake used by tests.
"""

from typing import Protocol, runtime_checkable

from app.domain import KnowledgeRequest
from app.knowledge.models.retrieval import RetrievalResult
from app.knowledge.policies import RetrievalPolicy


@runtime_checkable
class RetrievalBackend(Protocol):
    async def search(self, request: KnowledgeRequest, policy: RetrievalPolicy) -> list[RetrievalResult]: ...
