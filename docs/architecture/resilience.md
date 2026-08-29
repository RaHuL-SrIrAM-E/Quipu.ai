# Resilience Layer (NFR Hardening)

## Architecture

```
Agent / Service code
        │
        ▼
  app/core/resilience/
   ┌───────────┬──────────────────┬───────────┐
   │  retry.py │ circuit_breaker  │ timeout.py│
   └───────────┴──────────────────┴───────────┘
        │
        ▼
  Genuine external boundary
  (Gemini/ADK, Jira, Google Cloud SDK, ...)
```

```
Bounded. Explicit. Additive.
Never a second orchestration engine.
```

## 1. Why this layer exists

By the time this layer was added, Quipu already had substantial resilience
built into specific subsystems: bounded orchestration retries and retry
budgets in `OrchestrationService`, Firestore optimistic concurrency,
worker concurrency limits and graceful shutdown in the Pub/Sub worker,
Pub/Sub's own retry/DLQ semantics, idempotency fingerprints, correlation
IDs, structured logging, and capability enforcement. None of that is
duplicated here.

What was missing was a small set of *generic, reusable* primitives for the
one thing none of those mechanisms provide: bounded retry with backoff,
fail-fast circuit breaking, and enforced timeouts around calls to systems
Quipu does not control (Gemini, Jira). `app/core/resilience/` is that
layer, and nothing more.

## 2. Why it is not an agent, and not a second orchestration engine

There is no `ResilienceAgent`, no Gemini call anywhere in this package, and
no workflow state machine. `app/core/resilience/` is pure infrastructure —
three small, independent primitives (`retry_async`, `CircuitBreaker`,
`with_timeout`) that wrap a single async callable. They have no knowledge
of workflows, stages, agents, or the orchestration retry budget.

This distinction is enforced structurally, not just by convention:
`tests/test_resilience.py::test_resilience_layer_is_never_applied_around_whole_agent_execution`
AST-parses `app/orchestration/decisions.py` and asserts it never imports
anything from `app.core.resilience` — the module that decides *what an
agent should do next* must never be wrapped in infrastructure retry, only
the narrow external I/O call inside an agent's execution can be.

## 3. Retry (`app/core/resilience/retry.py`)

`RetryPolicy` is a frozen dataclass: `max_attempts`, `base_delay_seconds`,
`max_delay_seconds`, `jitter_seconds`, and a `retryable: Callable[[Exception],
bool]` classifier that **defaults to `lambda exc: False`** — a caller must
explicitly say which exceptions are safe to retry. There is no ambient
"retry everything" default anywhere in this layer, mirroring the rest of
Quipu's "deterministic strategy → target mapping" philosophy: nothing here
guesses on the caller's behalf.

`retry_async(fn, policy, *, operation, correlation_id=None)`:

- Never retries an exception `policy.retryable` rejects — permanent
  failures (4xx auth/validation errors) fail on the first attempt.
- Never swallows `asyncio.CancelledError` — cancellation always propagates
  immediately, no matter which attempt it happens on.
- Uses exponential backoff (`delay *= 2` each attempt, capped at
  `max_delay_seconds`) plus `random.uniform(0, jitter_seconds)` jitter, so
  concurrent retries don't synchronize into a thundering herd.
- On exhaustion, raises `RetryExhaustedError(operation, attempts,
  last_error)` — never the raw last exception directly — carrying the
  original exception as `.last_error` for callers that need to classify it
  further (see §6).
- Logs each retry attempt with the `operation` name and, where available,
  the correlation ID already threaded through Quipu's structured logging.

## 4. Circuit breaker (`app/core/resilience/circuit_breaker.py`)

`CircuitBreaker(name, *, failure_threshold, recovery_timeout_seconds,
is_countable_failure)` implements the standard CLOSED → OPEN → HALF_OPEN →
CLOSED state machine:

- **CLOSED**: calls pass through; a failure only counts toward the
  threshold if `is_countable_failure(exc)` returns `True`. Permanent
  failures (a classifier can return `False` for them) never trip the
  breaker — the breaker only reacts to failures that indicate the
  *external system* is unhealthy, not to caller error.
- **OPEN**: calls fail fast with `CircuitOpenError` without ever invoking
  the wrapped function, until `recovery_timeout_seconds` has elapsed.
- **HALF_OPEN**: exactly one probe call is admitted at a time (guarded by
  an internal flag under the breaker's `asyncio.Lock`); a successful probe
  closes the circuit, a failed probe reopens it immediately.
- State transitions are concurrency-safe: all state reads/writes happen
  under a single `asyncio.Lock`, verified by a concurrent-calls test.
- `asyncio.CancelledError` is never counted as a failure.
- Current state is a plain public attribute for observability/tests — no
  hidden state.

**Not distributed.** A `CircuitBreaker` instance's state lives entirely in
that process's memory. On Cloud Run, each instance has its own breaker; one
instance opening its Jira breaker does not affect any other instance's
calls to Jira. This is a deliberate scope decision (see the task's
explicit "do not implement a distributed circuit breaker" constraint), not
an oversight. If cross-instance breaker coordination is ever needed, it
would require a shared state store (e.g. Firestore) and was explicitly
out of scope for this hardening pass.

## 5. Timeout (`app/core/resilience/timeout.py`)

`with_timeout(coro, seconds, *, operation)` wraps `asyncio.wait_for` and
raises `OperationTimeoutError` — a subclass of the built-in `TimeoutError`
— instead of letting `asyncio.wait_for`'s bare `TimeoutError` propagate.
`OperationTimeoutError` carries `.operation` and `.seconds` for logging.

It is a subclass of `TimeoutError` (which is itself an `Exception`)
specifically so that every agent's existing `except Exception as exc:
return await _fail(...)` error-handling path already catches it with
**zero changes to that logic** — a timed-out Gemini call becomes an
ordinary `*_LLM_FAILURE` outcome, exactly like any other agent exception.

## 6. Where each mechanism is applied

Applied only at genuine external boundaries, per the task's priority
order:

1. **Gemini/ADK calls** (`with_timeout`, `settings.llm_call_timeout_seconds
   = 60.0`): every agent that runs an ADK `Runner` — `PlanningAgent`,
   `ArchitectureAgent`, `CodegenAgent`, `TestingAgent`, `DeploymentAgent`,
   `DetectingAgent`, `IncidentResolutionAgent`, and the orchestration
   `decision_agent` — wraps its `runner.run_async(...)` event-consumption
   loop in `with_timeout(...)`. This bounds how long a single LLM call can
   block a workflow step; on timeout the agent's existing exception
   handling converts it into a normal failure outcome, which then flows
   into the *existing, unchanged* orchestration retry budget. Infrastructure
   timeout does not add a retry of its own here — it only bounds the call;
   whether to retry the step at all remains `OrchestrationService`'s
   decision, exactly as before this hardening pass.

2. **Jira** (`RetryPolicy` + `CircuitBreaker`,
   `settings.jira_retry_max_attempts = 3`,
   `settings.jira_circuit_breaker_failure_threshold = 5`,
   `settings.jira_circuit_breaker_recovery_timeout_seconds = 30.0`):
   `FeatureReviewService._create_jira_story_resilient()` wraps the
   synchronous `JiraClient.create_story` call (via `asyncio.to_thread`) in
   `retry_async`, and wraps the whole retry sequence in a `CircuitBreaker`.
   `is_transient_jira_error` (`app/core/jira_client.py`) classifies which
   failures are retryable/countable: HTTP 5xx, connection errors, and
   timeouts are transient; HTTP 4xx (auth, validation) is permanent and
   never retried or counted toward the breaker. Because `retry_async`
   always raises `RetryExhaustedError` on exhaustion rather than the raw
   exception, `is_transient_jira_error` recursively unwraps
   `RetryExhaustedError.last_error` before classifying — otherwise the
   circuit breaker, which only observes one outcome per whole
   `retry_async()` call, would never see the underlying HTTP error and
   would never open.

3. **Google Cloud SDK calls** (Firestore, Pub/Sub, Cloud Build, etc.) —
   **deferred, not wrapped**. These calls already pass explicit
   `timeout=` kwargs directly to the underlying gRPC/HTTP client, and
   Google's `google-api-core` client libraries already ship a built-in
   default retry policy for idempotent operations. Given the task's
   explicit instruction to keep this layer small, wrapping these again at
   the application level would either duplicate that existing retry
   behavior or risk a retry-on-top-of-retry interaction with no clear
   incremental benefit. This is a deliberate scope decision, documented
   here rather than left as a silent gap.

4. **Agent Search** — deferred for the same reason as Cloud Search calls
   generally: no evidence of an unbounded call in the current codebase
   that lacks a timeout; revisit if a real production incident shows
   otherwise.

5. **Cloud Run deployment** — deferred; Quipu does not perform live GCP
   deployment in this phase (deployment is currently simulated/represented
   as an artifact, not an actual `gcloud run deploy` invocation), so there
   is no real external call yet to wrap.

## 7. Interaction with orchestration retries — the critical invariant

Infrastructure retry (`retry_async`) and orchestration retry
(`OrchestrationService`'s existing retry budget, tracked per workflow in
`workflow.metadata["retry_count:<stage>"]`) are two independent, bounded
layers that never multiply into an uncontrolled retry storm:

- Infrastructure retry only wraps a *single* external call (e.g. one Jira
  `POST`, or one Gemini `runner.run_async` invocation) and is capped at
  `jira_retry_max_attempts` (3) — it has no knowledge of workflow stages
  or orchestration state.
- Orchestration retry only decides whether to re-invoke an *entire agent
  step* after it fails, using its own independent budget, and has no
  knowledge of how many infrastructure-level retries happened inside that
  step.
- The worst case is therefore `orchestration_retry_budget ×
  infrastructure_retry_max_attempts` calls to a given external system for
  one workflow stage — a fixed, small, computable bound (e.g. 3
  orchestration retries × 3 Jira retries = at most 9 Jira calls), never
  unbounded.
- `tests/test_resilience.py` and `tests/test_resilience_integration.py`
  assert this boundary explicitly: the resilience layer is never imported
  by `app/orchestration/decisions.py`, and the Jira integration tests
  assert a fixed, small number of underlying calls even when both layers
  are exercised together.

## 8. Idempotency and graceful degradation

Nothing in this layer introduces new idempotency requirements beyond what
already existed: Jira story creation was already a single, non-idempotent
external call before this change, and remains one — retrying it a bounded
number of times on a *transient* failure (connection reset, 5xx) is safe
because a transient failure by definition means the request did not
succeed, and Quipu does not retry once any success response is observed.
Firestore-backed workflow state continues to use its existing optimistic
concurrency control for idempotent step re-execution; this layer does not
touch that mechanism.

When the Jira circuit breaker is OPEN, `FeatureReviewService.approve()`
fails fast with a clear error (surfaced as `TicketCreationFailedError`)
instead of hanging on a Jira outage — graceful degradation, not silent
data loss: the feature review itself remains in `pending`/`approved`
state and can be retried by the human reviewer once Jira recovers.

## 9. Configuration

All resilience parameters are `Settings` fields (`app/config.py`), with
matching entries in `.env.example`:

| Setting | Default | Purpose |
|---|---|---|
| `llm_call_timeout_seconds` | 60.0 | Bounds every ADK/Gemini call |
| `jira_retry_max_attempts` | 3 | Jira retry ceiling |
| `jira_retry_base_delay_seconds` | 0.5 | Jira retry backoff base |
| `jira_circuit_breaker_failure_threshold` | 5 | Jira breaker trip threshold |
| `jira_circuit_breaker_recovery_timeout_seconds` | 30.0 | Jira breaker OPEN duration |

## 10. Tests

`tests/test_resilience.py` (unit-level, 19 tests): successful first
attempt, transient-then-success, permanent failure not retried,
exponential backoff/jitter bounds, max attempts exhausted, cancellation
never swallowed, timeout success/failure/subclass check, circuit breaker
CLOSED/OPEN/HALF_OPEN transitions, fail-fast while OPEN (wrapped function
never invoked), permanent failures never trip the breaker, bounded
concurrent half-open probes, concurrent calls safe while CLOSED, plus the
structural AST guard described in §2.

`tests/test_resilience_integration.py` (3 tests): proves the layer is
actually wired into `FeatureReviewService`'s real Jira call path — transient
failure then success is retried, permanent failure (401) is never
retried, and repeated transient failures open the circuit breaker and
stop making further Jira calls.

## 11. Deferred NFRs

- Distributed circuit breaker state across Cloud Run instances — out of
  scope by explicit instruction; see §4.
- Google Cloud SDK / Agent Search / Cloud Run deployment call wrapping —
  deferred per §6, items 3–5, given existing SDK-level protection and the
  instruction to keep this layer small.
- Rate limiting — not added; no existing API boundary in this pass was
  identified as genuinely requiring it without expanding scope beyond
  this task.
