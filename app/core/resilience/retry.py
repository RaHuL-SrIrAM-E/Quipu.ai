"""Bounded async retry with exponential backoff + jitter — see
docs/architecture/resilience.md "Retry".

Deliberately requires the caller to supply an explicit `retryable`
predicate (no default that retries everything): a permanent failure
(auth, validation, 4xx) must never be retried, and this module has no
way to know which exceptions mean what for an arbitrary boundary — each
integration site classifies its own failures (see
app.core.jira_client.is_transient_jira_error for the Jira boundary).

This is infrastructure-level retry for a SINGLE external call. It is NOT
a replacement for, and must never multiply, the existing orchestration
retry budget (app/orchestration/decisions.py, Settings.max_*_retries) —
see docs/architecture/resilience.md "Interaction with orchestration
retries" for why this module is deliberately not applied around whole
agent executions.
"""

import asyncio
import random
from dataclasses import dataclass
from typing import Awaitable, Callable, TypeVar

from app.core.observability import get_logger

logger = get_logger("quipu.core.resilience.retry")

T = TypeVar("T")


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 3
    base_delay_seconds: float = 0.5
    max_delay_seconds: float = 8.0
    jitter_seconds: float = 0.25
    retryable: Callable[[Exception], bool] = lambda exc: False  # never retry unless the caller says so


class RetryExhaustedError(Exception):
    def __init__(self, operation: str, attempts: int, last_error: Exception):
        self.operation = operation
        self.attempts = attempts
        self.last_error = last_error
        super().__init__(f"'{operation}' failed after {attempts} attempt(s): {last_error}")


async def retry_async(fn: Callable[[], Awaitable[T]], policy: RetryPolicy, *, operation: str, correlation_id: str | None = None) -> T:
    """Calls `fn()` up to `policy.max_attempts` times. A permanent failure
    (per `policy.retryable`) propagates immediately, on the first
    attempt, unchanged — this function adds no delay or wrapping around a
    non-retryable exception. `asyncio.CancelledError` is never treated as
    a failure to retry — it always propagates immediately, so a caller
    (or the ASGI server) cancelling this coroutine is respected instantly
    rather than being retried."""
    delay = policy.base_delay_seconds
    last_error: Exception | None = None

    for attempt in range(1, policy.max_attempts + 1):
        try:
            return await fn()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            last_error = exc
            if not policy.retryable(exc):
                logger.warning(
                    "resilience.retry.permanent operation=%s attempt=%d error=%s correlation_id=%s",
                    operation,
                    attempt,
                    type(exc).__name__,
                    correlation_id,
                )
                raise
            if attempt >= policy.max_attempts:
                logger.warning(
                    "resilience.retry.exhausted operation=%s attempts=%d error=%s correlation_id=%s",
                    operation,
                    attempt,
                    type(exc).__name__,
                    correlation_id,
                )
                raise RetryExhaustedError(operation, attempt, exc) from exc

            sleep_for = min(delay, policy.max_delay_seconds) + random.uniform(0, policy.jitter_seconds)
            logger.info(
                "resilience.retry.transient operation=%s attempt=%d next_delay_seconds=%.2f error=%s correlation_id=%s",
                operation,
                attempt,
                sleep_for,
                type(exc).__name__,
                correlation_id,
            )
            await asyncio.sleep(sleep_for)
            delay *= 2

    # Unreachable (the loop above always returns or raises), but keeps
    # type-checkers happy without a bare `assert False`.
    raise RetryExhaustedError(operation, policy.max_attempts, last_error or Exception("no attempts were made"))
