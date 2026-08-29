"""Bounded timeout for external-boundary calls that have no native
per-call timeout parameter — see docs/architecture/resilience.md
"Timeout". Google Cloud SDK clients already pass their own `timeout=`
kwarg straight to the underlying gRPC/HTTP call
(app/core/cloud_monitoring_client.py, cloud_logging_client.py,
cloud_run_client.py, app/eventing/google_pubsub_client.py,
app/knowledge/backends/google_search.py) — this module is not for them.
The one real gap this closes: the ADK runner's `run_async()` event loop
(every LlmAgent invocation, app/agents/*.py) has no bound at all today —
a hung Gemini call would hang the entire agent execution indefinitely.
"""

import asyncio
from typing import Coroutine, TypeVar

from app.core.observability import get_logger

logger = get_logger("quipu.core.resilience.timeout")

T = TypeVar("T")


class OperationTimeoutError(TimeoutError):
    """Raised instead of a bare TimeoutError so logs/callers can see which
    named operation actually hung."""

    def __init__(self, operation: str, seconds: float):
        self.operation = operation
        self.seconds = seconds
        super().__init__(f"'{operation}' did not complete within {seconds}s")


async def with_timeout(coro: Coroutine[None, None, T], seconds: float, *, operation: str) -> T:
    """Awaits `coro`, bounded by `seconds`. Cancels the underlying
    coroutine on timeout (asyncio.wait_for's own behavior) — no orphaned
    background work. Raises OperationTimeoutError, a TimeoutError
    subclass, so an existing `except Exception` at the call site (every
    agent's own LLM-failure handling, e.g. `except Exception as exc:
    return await _fail("PLANNING_LLM_FAILURE", ...)`) already catches it
    correctly with no change to that handling logic."""
    try:
        return await asyncio.wait_for(coro, timeout=seconds)
    except TimeoutError as exc:
        logger.warning("resilience.timeout operation=%s seconds=%.1f", operation, seconds)
        raise OperationTimeoutError(operation, seconds) from exc
