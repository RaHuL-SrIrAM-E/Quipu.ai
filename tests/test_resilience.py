"""Tests for the resilience layer (app/core/resilience/). See
docs/architecture/resilience.md.
"""

import asyncio
import time

import pytest

from app.core.resilience.circuit_breaker import CircuitBreaker, CircuitOpenError, CircuitState
from app.core.resilience.retry import RetryExhaustedError, RetryPolicy, retry_async
from app.core.resilience.timeout import OperationTimeoutError, with_timeout


class _TransientError(Exception):
    pass


class _PermanentError(Exception):
    pass


def _retryable_only_transient(exc: Exception) -> bool:
    return isinstance(exc, _TransientError)


# ---------------------------------------------------------------------------
# retry_async
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_retry_succeeds_on_first_attempt():
    calls = 0

    async def _fn():
        nonlocal calls
        calls += 1
        return "ok"

    result = await retry_async(_fn, RetryPolicy(retryable=_retryable_only_transient), operation="test")
    assert result == "ok"
    assert calls == 1


@pytest.mark.asyncio
async def test_retry_transient_failure_then_success():
    calls = 0

    async def _fn():
        nonlocal calls
        calls += 1
        if calls < 3:
            raise _TransientError("temporary")
        return "recovered"

    policy = RetryPolicy(max_attempts=5, base_delay_seconds=0.001, jitter_seconds=0.0, retryable=_retryable_only_transient)
    result = await retry_async(_fn, policy, operation="test")
    assert result == "recovered"
    assert calls == 3


@pytest.mark.asyncio
async def test_retry_permanent_failure_raises_immediately_no_delay():
    calls = 0

    async def _fn():
        nonlocal calls
        calls += 1
        raise _PermanentError("bad request")

    policy = RetryPolicy(max_attempts=5, base_delay_seconds=5.0, retryable=_retryable_only_transient)
    started = time.monotonic()
    with pytest.raises(_PermanentError):
        await retry_async(_fn, policy, operation="test")
    assert calls == 1  # never retried
    assert time.monotonic() - started < 1.0  # no backoff delay was ever slept


@pytest.mark.asyncio
async def test_retry_exponential_backoff_and_jitter_bounds(monkeypatch):
    delays: list[float] = []

    async def _fake_sleep(seconds):
        delays.append(seconds)

    monkeypatch.setattr("app.core.resilience.retry.asyncio.sleep", _fake_sleep)

    calls = 0

    async def _fn():
        nonlocal calls
        calls += 1
        raise _TransientError("always fails")

    policy = RetryPolicy(max_attempts=4, base_delay_seconds=0.1, max_delay_seconds=10.0, jitter_seconds=0.05, retryable=_retryable_only_transient)

    with pytest.raises(RetryExhaustedError):
        await retry_async(_fn, policy, operation="test")

    assert calls == 4
    assert len(delays) == 3  # one sleep between each of the 4 attempts, none after the last
    # exponential doubling, each within [base*2^i, base*2^i + jitter]
    for i, delay in enumerate(delays):
        lower = 0.1 * (2**i)
        upper = lower + 0.05
        assert lower <= delay <= upper, f"delay {delay} out of bounds [{lower}, {upper}] at attempt {i}"


@pytest.mark.asyncio
async def test_retry_max_attempts_exhausted_raises_retry_exhausted_error():
    calls = 0

    async def _fn():
        nonlocal calls
        calls += 1
        raise _TransientError("still failing")

    policy = RetryPolicy(max_attempts=3, base_delay_seconds=0.001, jitter_seconds=0.0, retryable=_retryable_only_transient)
    with pytest.raises(RetryExhaustedError) as exc_info:
        await retry_async(_fn, policy, operation="probe")
    assert calls == 3
    assert exc_info.value.attempts == 3
    assert isinstance(exc_info.value.last_error, _TransientError)


@pytest.mark.asyncio
async def test_retry_never_swallows_cancellation():
    async def _fn():
        raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await retry_async(_fn, RetryPolicy(max_attempts=5, retryable=lambda exc: True), operation="test")


# ---------------------------------------------------------------------------
# with_timeout
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_with_timeout_returns_result_when_fast_enough():
    async def _fast():
        return "done"

    result = await with_timeout(_fast(), 1.0, operation="test")
    assert result == "done"


@pytest.mark.asyncio
async def test_with_timeout_raises_operation_timeout_error_and_cancels():
    cancelled = False

    async def _slow():
        nonlocal cancelled
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            cancelled = True
            raise

    with pytest.raises(OperationTimeoutError) as exc_info:
        await with_timeout(_slow(), 0.05, operation="slow_llm_call")
    assert exc_info.value.operation == "slow_llm_call"
    assert cancelled is True  # the underlying coroutine was actually cancelled, not orphaned


@pytest.mark.asyncio
async def test_operation_timeout_error_is_a_timeout_error_subclass():
    """So existing `except Exception`/`except TimeoutError` call sites
    (every agent's own LLM-failure handling) catch it with no code
    change."""
    assert issubclass(OperationTimeoutError, TimeoutError)


# ---------------------------------------------------------------------------
# CircuitBreaker
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_circuit_breaker_starts_closed_and_stays_closed_on_success():
    breaker = CircuitBreaker("test", failure_threshold=3, recovery_timeout_seconds=0.1)

    async def _ok():
        return "fine"

    assert breaker.state == CircuitState.CLOSED
    for _ in range(5):
        assert await breaker.call(_ok) == "fine"
    assert breaker.state == CircuitState.CLOSED


@pytest.mark.asyncio
async def test_circuit_breaker_opens_after_failure_threshold():
    breaker = CircuitBreaker("test", failure_threshold=3, recovery_timeout_seconds=10.0)

    async def _boom():
        raise _TransientError("down")

    for _ in range(3):
        with pytest.raises(_TransientError):
            await breaker.call(_boom)

    assert breaker.state == CircuitState.OPEN
    assert breaker.consecutive_failures == 3


@pytest.mark.asyncio
async def test_circuit_breaker_fails_fast_while_open():
    breaker = CircuitBreaker("test", failure_threshold=1, recovery_timeout_seconds=10.0)

    async def _boom():
        raise _TransientError("down")

    with pytest.raises(_TransientError):
        await breaker.call(_boom)
    assert breaker.state == CircuitState.OPEN

    calls = 0

    async def _would_succeed():
        nonlocal calls
        calls += 1
        return "ok"

    with pytest.raises(CircuitOpenError):
        await breaker.call(_would_succeed)
    assert calls == 0  # fn was never even invoked — fail fast


@pytest.mark.asyncio
async def test_circuit_breaker_permanent_failure_does_not_trip():
    breaker = CircuitBreaker(
        "test", failure_threshold=1, recovery_timeout_seconds=10.0, is_countable_failure=lambda exc: isinstance(exc, _TransientError)
    )

    async def _permanent():
        raise _PermanentError("bad auth")

    for _ in range(10):
        with pytest.raises(_PermanentError):
            await breaker.call(_permanent)

    assert breaker.state == CircuitState.CLOSED
    assert breaker.consecutive_failures == 0


@pytest.mark.asyncio
async def test_circuit_breaker_half_open_recovery_success_closes():
    breaker = CircuitBreaker("test", failure_threshold=1, recovery_timeout_seconds=0.05)

    async def _boom():
        raise _TransientError("down")

    with pytest.raises(_TransientError):
        await breaker.call(_boom)
    assert breaker.state == CircuitState.OPEN

    await asyncio.sleep(0.06)  # let the recovery window elapse

    async def _recovered():
        return "back"

    result = await breaker.call(_recovered)
    assert result == "back"
    assert breaker.state == CircuitState.CLOSED


@pytest.mark.asyncio
async def test_circuit_breaker_half_open_probe_failure_reopens():
    breaker = CircuitBreaker("test", failure_threshold=1, recovery_timeout_seconds=0.05)

    async def _boom():
        raise _TransientError("down")

    with pytest.raises(_TransientError):
        await breaker.call(_boom)
    await asyncio.sleep(0.06)

    with pytest.raises(_TransientError):
        await breaker.call(_boom)  # the half-open probe itself fails
    assert breaker.state == CircuitState.OPEN


@pytest.mark.asyncio
async def test_circuit_breaker_bounds_concurrent_half_open_probes():
    breaker = CircuitBreaker("test", failure_threshold=1, recovery_timeout_seconds=0.02)

    async def _boom():
        raise _TransientError("down")

    with pytest.raises(_TransientError):
        await breaker.call(_boom)
    await asyncio.sleep(0.03)

    release = asyncio.Event()
    in_flight = 0
    max_in_flight = 0

    async def _slow_probe():
        nonlocal in_flight, max_in_flight
        in_flight += 1
        max_in_flight = max(max_in_flight, in_flight)
        await release.wait()
        in_flight -= 1
        return "ok"

    async def _second_caller():
        await asyncio.sleep(0.005)  # let the first caller claim the probe first
        with pytest.raises(CircuitOpenError):
            await breaker.call(_slow_probe)

    first = asyncio.create_task(breaker.call(_slow_probe))
    second = asyncio.create_task(_second_caller())
    await second
    release.set()
    await first

    assert max_in_flight == 1  # only one probe was ever actually in flight


@pytest.mark.asyncio
async def test_circuit_breaker_concurrent_calls_are_safe_when_closed():
    breaker = CircuitBreaker("test", failure_threshold=100, recovery_timeout_seconds=10.0)

    async def _ok():
        await asyncio.sleep(0.001)
        return "ok"

    results = await asyncio.gather(*[breaker.call(_ok) for _ in range(20)])
    assert results == ["ok"] * 20
    assert breaker.state == CircuitState.CLOSED


@pytest.mark.asyncio
async def test_circuit_breaker_never_swallows_cancellation():
    async def _cancels():
        raise asyncio.CancelledError()

    breaker = CircuitBreaker("test", failure_threshold=1)
    with pytest.raises(asyncio.CancelledError):
        await breaker.call(_cancels)
    assert breaker.state == CircuitState.CLOSED  # cancellation never counts as a countable failure


# ---------------------------------------------------------------------------
# No interaction with existing orchestration retry budgets
# ---------------------------------------------------------------------------


def test_resilience_layer_is_never_applied_around_whole_agent_execution():
    """Structural guard: this module must never be imported from
    app/orchestration/decisions.py (the existing, sole owner of
    orchestration retry-budget policy) — resilience.retry/circuit_breaker
    operate at a single external call, never around an entire agent
    execution or the orchestration decision loop itself."""
    import ast
    import pathlib

    source = pathlib.Path("app/orchestration/decisions.py").read_text()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and "resilience" in node.module:
            pytest.fail("app/orchestration/decisions.py must not import the resilience layer")
