"""A small, explicit, process-local resilience layer applied ONLY at
genuine external boundaries (Gemini/ADK, Jira today — see
docs/architecture/resilience.md for the full rationale and the
boundaries deliberately left alone).

This package does not replace anything that already exists:
orchestration retry budgets (app/orchestration/decisions.py,
Settings.max_*_retries), Firestore optimistic concurrency
(update_if_version), Pub/Sub retry/DLQ semantics (app/eventing/errors.py),
worker concurrency/shutdown (app/eventing/worker.py), or capability
enforcement (app/agent_runtime/capabilities.py) are all untouched and
remain authoritative for their own concerns. This layer is strictly
additive infrastructure hardening around calls that had no bounded
protection at all before.
"""

from app.core.resilience.circuit_breaker import CircuitBreaker, CircuitOpenError, CircuitState
from app.core.resilience.retry import RetryPolicy, retry_async
from app.core.resilience.timeout import OperationTimeoutError, with_timeout

__all__ = [
    "CircuitBreaker",
    "CircuitOpenError",
    "CircuitState",
    "OperationTimeoutError",
    "RetryPolicy",
    "retry_async",
    "with_timeout",
]
