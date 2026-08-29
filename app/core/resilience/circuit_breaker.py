"""A small, process-local, async-safe circuit breaker — see
docs/architecture/resilience.md "Circuit breaker".

This is an application-process resilience mechanism, not a distributed
one: each Cloud Run instance (or local process) that constructs a
CircuitBreaker has its own independent state. Two instances of the same
Cloud Run service will not share OPEN/CLOSED state, and this is a
deliberate, documented limitation — a distributed breaker (backed by
Firestore/Redis/etc.) is explicitly out of scope, per this task's own
instruction not to build one.

Only failures the caller classifies as countable trip the breaker — a
permanent failure (auth error, validation error, 4xx) must never open the
circuit, since retrying a *different* correct request right after would
otherwise be blocked for no reason. See `is_countable_failure`.
"""

import asyncio
import time
from enum import Enum
from typing import Awaitable, Callable, TypeVar

from app.core.observability import get_logger

logger = get_logger("quipu.core.resilience.circuit_breaker")

T = TypeVar("T")


class CircuitState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitOpenError(Exception):
    def __init__(self, name: str):
        self.name = name
        super().__init__(f"circuit '{name}' is open — failing fast")


class CircuitBreaker:
    def __init__(
        self,
        name: str,
        *,
        failure_threshold: int = 5,
        recovery_timeout_seconds: float = 30.0,
        is_countable_failure: Callable[[Exception], bool] = lambda exc: True,
    ):
        self._name = name
        self._failure_threshold = failure_threshold
        self._recovery_timeout_seconds = recovery_timeout_seconds
        self._is_countable_failure = is_countable_failure

        self._state = CircuitState.CLOSED
        self._consecutive_failures = 0
        self._opened_at: float | None = None
        self._half_open_probe_in_flight = False
        self._lock = asyncio.Lock()

    @property
    def name(self) -> str:
        return self._name

    @property
    def state(self) -> CircuitState:
        return self._state

    @property
    def consecutive_failures(self) -> int:
        return self._consecutive_failures

    async def call(self, fn: Callable[[], Awaitable[T]]) -> T:
        """Fails fast with CircuitOpenError while OPEN (and the recovery
        window hasn't elapsed), or while a HALF_OPEN probe is already in
        flight (bounded to exactly one concurrent probe). Otherwise calls
        `fn()` and updates state based on the outcome."""
        async with self._lock:
            if self._state == CircuitState.OPEN:
                elapsed = time.monotonic() - (self._opened_at or 0.0)
                if elapsed < self._recovery_timeout_seconds:
                    raise CircuitOpenError(self._name)
                if self._half_open_probe_in_flight:
                    raise CircuitOpenError(self._name)
                self._state = CircuitState.HALF_OPEN
                self._half_open_probe_in_flight = True
                logger.info("resilience.circuit_breaker.half_open name=%s", self._name)
            elif self._state == CircuitState.HALF_OPEN:
                raise CircuitOpenError(self._name)

        try:
            result = await fn()
        except asyncio.CancelledError:
            async with self._lock:
                if self._state == CircuitState.HALF_OPEN:
                    self._half_open_probe_in_flight = False
            raise
        except Exception as exc:
            async with self._lock:
                self._half_open_probe_in_flight = False
                if self._is_countable_failure(exc):
                    self._consecutive_failures += 1
                    was_half_open = self._state == CircuitState.HALF_OPEN
                    if was_half_open or self._consecutive_failures >= self._failure_threshold:
                        if self._state != CircuitState.OPEN:
                            logger.warning(
                                "resilience.circuit_breaker.open name=%s consecutive_failures=%d",
                                self._name,
                                self._consecutive_failures,
                            )
                        self._state = CircuitState.OPEN
                        self._opened_at = time.monotonic()
                # else: a permanent/non-countable failure — never affects breaker state
            raise
        else:
            async with self._lock:
                if self._consecutive_failures or self._state != CircuitState.CLOSED:
                    logger.info("resilience.circuit_breaker.closed name=%s", self._name)
                self._consecutive_failures = 0
                self._state = CircuitState.CLOSED
                self._opened_at = None
                self._half_open_probe_in_flight = False
            return result
