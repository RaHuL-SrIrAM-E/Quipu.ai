# Quipu Control Plane API

## Diagram

```
Operator / future UI
        ↓
   HTTP / REST (app/api/, FastAPI)
        ↓
   ┌─────────────┬──────────────┐
   │ Query routes │ Command routes│
   └─────────────┴──────────────┘
        ↓                  ↓
  Repositories       OrchestrationService
  (read-only)        FeatureReviewService
        ↓                  ↓
        └────────┬─────────┘
                  ↓
        Firestore / in-memory
```

## 1. Purpose

`app/api/` is a **thin control plane** over the existing Quipu backend —
it is the stable external boundary the future operator UI (and any human
interacting with Quipu directly) will use, but it is not, and must never
become, a second orchestration engine. Every route is either a **query**
(reads through an existing repository) or a **command** (delegates to an
existing service method: `OrchestrationService.execute_next_step`,
`OrchestrationService.start_remediation_from_resolution`,
`FeatureReviewService.approve`/`reject`). No route contains business
logic, transition policy, authorization logic, or agent-selection logic
of its own (Invariant 1).

## 2. Architecture

```
app/api/
  app.py            FastAPI application factory (create_app), CORS,
                     correlation-id middleware, exception handler wiring
  container.py       ApiContainer — constructs the same repository/service
                     objects DemoHarness/worker_main already build
                     (in-memory by default, Firestore when GCP_PROJECT_ID
                     is set)
  dependencies.py    FastAPI dependency accessors (get_container)
  auth.py             The API-level authorization boundary (see §5)
  pagination.py       bounded_limit — every collection endpoint's ceiling
  errors.py            exception -> HTTP response mapping, in one place
  schemas/            typed response/request models — never a raw domain
                       model or repository object crosses the HTTP boundary
  routes/              one router per resource group
```

`app/main.py` re-exports `app.api.app:app` as the ASGI entrypoint
`uvicorn`/Cloud Run serve; the root `main.py` dev script is unchanged.

## 3. Endpoint groups

| Group | Routes |
|---|---|
| Health | `GET /health`, `GET /ready` |
| Workflows | `GET /workflows`, `GET /workflows/{id}`, `GET /workflows/{id}/artifacts`, `GET /workflows/{id}/executions`, `GET /workflows/{id}/decisions`, `POST /workflows/{id}/step`, `POST /workflows/{id}/run` |
| Signals | `GET /signals`, `GET /signals/{id}` |
| Detections | `GET /detections`, `GET /detections/{id}` |
| Resolutions | `GET /resolutions`, `GET /resolutions/{id}`, `POST /resolutions/{id}/remediate` |
| Verifications | `GET /verifications`, `GET /verifications/{id}` |
| Feature Reviews | `GET /feature-reviews`, `GET /feature-reviews/{id}`, `POST /feature-reviews/{id}/approve`, `POST /feature-reviews/{id}/reject` |
| Demo (disabled by default) | `POST /demo/scenarios/{scenario}` — see §13 |

## 4. Query vs. command semantics

**Queries** (all `GET` routes) read directly from a repository (or two —
e.g. `list_workflow_artifacts` first confirms the workflow exists via
`WorkflowRepository.get`, then reads `ArtifactRepository.list_for_workflow`)
and map the result through a typed response schema. No query route ever
mutates state.

**Commands** (`POST` routes) delegate to exactly one existing service
method:

| Route | Delegates to |
|---|---|
| `POST /workflows/{id}/step` | `OrchestrationService.execute_next_step` |
| `POST /workflows/{id}/run` | `OrchestrationService.run_to_completion` |
| `POST /resolutions/{id}/remediate` | `OrchestrationService.start_remediation_from_resolution` |
| `POST /feature-reviews/{id}/approve` | `FeatureReviewService.approve` |
| `POST /feature-reviews/{id}/reject` | `FeatureReviewService.reject` |

None of these accept a request body field that influences *what*
happens beyond an optional `review_comment` string — no target agent,
strategy, stage, or workflow id override is ever accepted from the client
(Invariants 5/6). `POST /resolutions/{id}/remediate` accepts **no request
body at all**: everything `start_remediation_from_resolution` needs, it
already re-derives deterministically from the persisted `ResolutionResult`
(see `app/orchestration/service.py`'s own docstring on why
`resolution.target_agent` is never trusted there either).

`POST /workflows/{id}/run` is **not** a second orchestration engine — it
repeatedly calls the exact same `execute_next_step` mechanism the `/step`
route uses (via the pre-existing `OrchestrationService.run_to_completion`),
bounded by `Settings.workflow_run_max_iterations` (default 20), until the
workflow reaches a terminal status, becomes blocked waiting on a human, or
the iteration cap is hit. It is safe to call repeatedly: a workflow already
in a terminal state is returned unchanged by `execute_next_step` itself, so
re-invoking `/run` never duplicates completed work. The response
(`WorkflowRunResult`) is a summary derived entirely from durable state
(new artifact ids, decision count delta, `retry_count:*` metadata) —
never raw LLM output. See `docs/architecture/resilience.md` for how this
interacts with the orchestration retry budget.

## 5. Authorization boundary

Read honestly: this is the **smallest** boundary that satisfies the task's
requirement that an agent can never self-approve a feature review, not a
production-grade identity system.

- `Settings.api_auth_mode` gates everything. `"development"` (the
  default) is the only mode that authenticates anyone — via
  `app/api/auth.py::require_reviewer_identity`, which trusts an
  `X-Quipu-Reviewer-Id` request header **for attribution only**. It never
  reads a client-supplied privilege field (no `{"is_admin": true}` or
  equivalent is accepted anywhere in this API); the capability set granted
  (`{AgentCapability.REVIEW_FEATURE_OPPORTUNITY}`) is fixed server-side
  and identical for every authenticated caller in this mode.
- Any other `api_auth_mode` value refuses every endpoint that requires
  identity with `401` — this is the explicit seam a production deployment
  replaces with real token verification (a Cloud Run/IAM-fronted identity
  token, or an OIDC provider) behind the exact same
  `require_reviewer_identity` dependency, without touching any route.
- `reviewer_type` is **never** read from the request body — the approve/
  reject routes fix it to `DecisionSource.HUMAN` unconditionally, because
  the endpoint itself represents "a human is acting through the control
  plane." `FeatureReviewService.approve`/`reject` independently re-checks
  this (`UnauthorizedReviewerError` if it's ever anything else) — the API
  can't weaken that check even if it wanted to, since the service call
  itself enforces it.
- `AgentCapability` (`app.agent_runtime.capabilities`) is reused as the
  *vocabulary* FeatureReviewService already requires, not stretched into a
  general HTTP permission system — see `app/api/auth.py`'s own docstring
  for the explicit statement of this distinction.

This is a real limitation, not glossed over: `api_auth_mode="development"`
has no cryptographic verification of the caller's claimed identity at all.
It must not be used for a real deployment with real reviewers.

## 6. Error handling

`app/api/errors.py::register_exception_handlers` maps every existing
application error type to a fixed, safe HTTP response — routes never
construct an `HTTPException` from a caught exception's message themselves.

| Exception | Status | `error` code |
|---|---|---|
| `EntityNotFoundError` | 404 | `not_found` |
| `DuplicateEntityError` | 409 | `conflict` |
| `VersionConflictError` | 409 | `version_conflict` |
| `CapabilityError` | 403 | `forbidden` |
| `UnauthorizedReviewerError` | 403 | `forbidden` |
| `ReviewNotFoundError` / `DetectionNotFoundError` | 404 | `not_found` |
| `InvalidDetectionTypeError` / `InsufficientEvidenceError` / other `FeatureReviewError` | 422 | `business_rule_violation` |
| `InvalidReviewTransitionError` | 409 | `invalid_transition` |
| `TicketCreationFailedError` | 503 | `dependency_unavailable` |
| `VerificationError` | 422 | `business_rule_violation` |
| `UnknownAgentError` | 404 | `not_found` |
| `InvalidTransitionError` (orchestration) | 409 | `invalid_transition` |
| `OrchestrationError` (base) | 422 | `business_rule_violation` |
| `RequestValidationError` (bad query/body) | 422 | `validation_error` |
| anything else | 500 | `internal_error` |

Every response body is `{"error", "detail", "correlation_id"}`
(`app/api/schemas/common.py::ErrorResponse`) — `detail` is always a short,
safe, pre-written or `str(exc)` message from the application's own
exception hierarchy, never a raw Firestore/Gemini/Google SDK exception or
a stack trace. The catch-all `Exception` handler logs the full traceback
server-side (with the correlation id) and returns a fixed
`"an internal error occurred"` message — verified directly by
`test_internal_exceptions_dont_leak`.

## 7. Pagination / limits

No cursor pagination: none of the underlying repositories
(`SignalRepository`, `DetectionRepository`, `ResolutionRepository`,
`RemediationVerificationRepository`, `FeatureReviewRepository`,
`WorkflowRepository.list_recent` — added in this task) expose a cursor
today. `app/api/pagination.py::bounded_limit` is a shared FastAPI
dependency every collection route uses: an unspecified `limit` defaults to
`Settings.api_default_page_size` (50); any requested `limit` is clamped to
`Settings.api_max_page_size` (200) — a caller can never force an unbounded
scan. `WorkflowRepository` needed one genuinely new method for this task,
`list_recent(status=None, limit=50)` — the only repository change this
task required (§2 of the task's own instructions permits this: "a genuine
API integration issue requiring minimal additive change"); every other
endpoint reuses an existing query method unchanged.

## 8. Security

- `Settings.api_cors_allow_origins` defaults to **empty** — no CORS grant
  at all until a deployer explicitly lists the UI's real origin(s); never
  defaults to `"*"`.
- No endpoint exists for arbitrary shell execution, arbitrary file writes,
  arbitrary deployment, arbitrary rollback, arbitrary tool invocation,
  arbitrary agent execution, arbitrary target-agent selection, or sending
  an arbitrary prompt to Gemini — verified structurally
  (`test_no_arbitrary_tool_execution_endpoint`,
  `test_no_shell_endpoint`, `test_no_arbitrary_deployment_endpoint`,
  `test_no_arbitrary_target_agent_selection`).
- `SignalDetail` exposes the already-sanitized `evidence` dict (redacted/
  truncated at ingestion by `app.signals.sanitize.sanitize_metadata`) but
  never the free-form `metadata` bucket, and `SignalSummary` (list view)
  exposes neither — see `app/api/schemas/signals.py`.
- `WorkflowDetail` exposes an explicit allow-list of `metadata` keys
  (`remediation_outcome`, `remediation_strategy`,
  `latest_verification_id`, `source_detection_id`, `review_id`) rather
  than the raw metadata dict, which can carry a local filesystem
  `workspace_path` never meant for a client.

## 9. Observability

Two layers, both reusing `app.core.observability.get_logger` — no new
metrics subsystem:

- A correlation-id middleware (`app/api/app.py`) assigns/propagates
  `X-Request-ID` on every request/response and logs one
  `api.request` line per call (`correlation_id`, `method`, `path`,
  `status`, `duration_ms`).
- Each route logs one `api.query`/`api.command` line with the
  operation name, relevant id (`workflow_id`/`detection_id`/
  `resolution_id`/`verification_id`/`review_id` where applicable), result
  count or status, and `duration_ms`. Never logs a request body, a raw
  signal payload, or an authentication header value.

## 10. Cloud Run deployment

A minimal `Dockerfile` (repository root) builds a single-process
`uvicorn app.main:app` image; Cloud Run supplies `$PORT`, which the image
binds to. Startup command:

```
uvicorn app.main:app --host 0.0.0.0 --port $PORT
```

(exactly what `CMD` in the `Dockerfile` runs). Configuration comes
entirely from `Settings`/environment variables (`.env.example`) — no
Google SDK client is constructed inside `app/api/` itself; `ApiContainer`
picks Firestore-backed repositories automatically once `GCP_PROJECT_ID` is
set, reusing the exact same `FirestoreXRepository` classes and
`get_firestore_client()` (ADC-only) every other Quipu component already
uses. This image serves **only** the HTTP control plane — the Pub/Sub
Signal Consumer Worker (`app/eventing/worker_main.py`) is a separate
process/deployment, not started by this container.

## 11. Relationship to the UI

The UI contract this API makes possible, without any UI code being built
in this task:

- **Workflow view**: `GET /workflows`, `GET /workflows/{id}`.
- **Agent execution view**: `GET /workflows/{id}/executions`,
  `GET /workflows/{id}/decisions`.
- **Signal explorer**: `GET /signals` (filterable), `GET /signals/{id}`.
- **Detection detail**: `GET /detections`, `GET /detections/{id}`.
- **Incident/resolution view**: `GET /resolutions`,
  `GET /resolutions/{id}`.
- **Remediation/verification timeline**: `workflow.remediation_outcome`
  (via `GET /workflows/{id}`) alongside `GET /verifications` /
  `GET /verifications/{id}` — the UI reads these as two DISTINCT fields so
  it can render "deployed" and "verified resolved" as different states,
  never collapsed into one (Invariant 8).
- **Feature review queue**: `GET /feature-reviews`,
  `POST /feature-reviews/{id}/approve`/`reject`.

FastAPI's generated OpenAPI schema (`/docs`, `/openapi.json`) is the
concrete contract a UI developer builds against.

## 12. Dangerous operations intentionally excluded

No endpoint exists for: arbitrary shell commands, arbitrary file writes,
arbitrary deployment, arbitrary rollback, arbitrary tool invocation,
arbitrary agent execution, arbitrary target-agent selection, or an
arbitrary Gemini prompt against privileged context. **This API is not the
orchestration engine** — it is a control surface over
`OrchestrationService`/`FeatureReviewService`, which remain the sole
authorities over agent selection, transition policy, retry budgets, and
remediation authorization (Invariants 2–7). A `STILL_DEGRADED`
verification, an escalated resolution, or a failed workflow step is
surfaced through the query endpoints above for a human to act on — this
API never automatically retries, escalates, or remediates anything on its
own.

> **Update**: a UI now consumes this API — see
> `docs/architecture/control_plane_ui.md` (`ui/`). The only backend change
> that task required was an explicit, opt-in static-file mount in
> `app/api/app.py` (`Settings.api_serve_ui`, default `False`) for serving
> the built UI from the same Cloud Run service; every route/schema/service
> documented above is unchanged.

## 13. Demo scenario seeding (disabled by default)

`POST /demo/scenarios/{scenario}` is demo-only infrastructure, not part of
the production control surface described above. It does not weaken §12:
`scenario` is a `str` `Enum` with exactly two members (`feature`,
`incident`) — there is no way to pass an arbitrary scenario, workflow, or
ticket through this route. The handler does not invoke agents directly or
run shell commands; it constructs a `DemoHarness` (the same harness already
used by tests) with the live `ApiContainer`'s own repositories injected, so
seeded data becomes visible through the ordinary query endpoints above
(`GET /workflows`, `GET /signals`, etc.) exactly as if it had been produced
by a real signal.

The route is **absent from the running app entirely** unless
`Settings.demo_endpoints_enabled=True` — `app/api/app.py` only imports and
registers `app/api/routes/demo.py`'s router inside that conditional, so a
disabled deployment returns a genuine 404 rather than reaching a
disabled-check a request could ever exploit. The flag defaults to `False`
and is expected to stay `False` in any real deployment; see
`docs/architecture/end_to_end_demo.md` "Seeding a live API" for the
intended usage.

Repeat calls for the same scenario are idempotent: the container caches
the seeded result (`ApiContainer.demo_scenario_results`) and returns it
unchanged (`already_seeded=True`) instead of re-running the harness and
duplicating data.
