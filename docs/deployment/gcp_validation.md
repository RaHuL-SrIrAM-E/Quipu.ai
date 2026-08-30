# GCP Deployment Validation

**Timestamp**: 2026-08-29 (this task's session). **Result: no live GCP
deployment was performed or verified.** This document records exactly
what was checked, what was fixed at the code level, and what remains
blocked — per this task's explicit instruction not to fake deployment
results.

## Environment check (performed first, per task instructions)

```
$ which gcloud   -> not found
$ which docker   -> not found
$ ls ~/.config/gcloud -> no such directory
$ env | grep -i 'GOOGLE\|GCP\|PROJECT' -> no ambient GCP env vars
```

This execution environment has **no `gcloud` CLI, no Docker daemon, no
Application Default Credentials, and no configured GCP project**. Per
this task's own instruction ("If GCP credentials/project are unavailable,
STOP before making destructive changes and report exactly what is
missing"), no attempt was made to enable APIs, create resources, deploy
services, or run any smoke test against real Google Cloud infrastructure.
Everything in §§3–17 of the driving task is therefore **BLOCKED** on
tooling/credential availability, not on Quipu's own architecture or code.

## What WAS done in this task (code-level, verified locally)

### 1. Gemini model

- `Settings.gemini_model` default changed: `gemini-2.5-pro` →
  **`gemini-3.5-flash`** (`app/config.py`).
- Verified every `LlmAgent` construction site reads this one centralized
  setting — no per-agent hardcoding: `app/agents/{planning×2,
  architecture×2, codegen, testing, deployment, detecting,
  incident_resolution}.py`, `app/orchestration/adk/decision_agent.py`
  (10 construction sites, all `model=settings.gemini_model`).
  `MonitoringAgent` correctly has no `LlmAgent` at all (deterministic by
  design) and was untouched.
- `.env.example` updated to match.
- **Not verified**: that `gemini-3.5-flash` is the exact, currently valid
  Vertex AI model identifier for this GCP project/region — that requires
  a live API call this environment cannot make. The developer's own
  local `.env` (git-ignored, not part of this audit's evidence) was
  observed to already contain a different, presumably real, value
  (`gemini-3.6-flash`) which the `Settings` object correctly picked up
  over the code default during a local smoke check — confirming the
  override mechanism works, but not confirming which exact string is
  correct for a fresh deployment. **Action required**: confirm the exact
  model id against the Vertex AI Model Garden for the target project
  before the live demo.

### 2. Vertex AI / ADC mode

- `app/config.py` now calls `os.environ.setdefault("GOOGLE_GENAI_USE_VERTEXAI", "true")`
  at import time (before any agent module's module-level `LlmAgent(...)`
  construction runs), preceded by `load_dotenv()` so a `.env` override is
  respected. This is a **consumed-by-runtime** fix, not documentation
  only — verified locally:

  ```
  $ python -c "import os, app.config; print(os.environ['GOOGLE_GENAI_USE_VERTEXAI'])"
  true
  ```

- `.env.example` updated with the same variable, documented.
- `docs/deployment/gcp.md` §4 updated to describe the fix.
- **Not verified**: an actual Vertex AI call succeeding under this
  configuration — requires live credentials (§17 below).

### 3. Firestore composite indexes — analysis only (no live project)

Every `FirestoreXRepository.query()` method was inspected
(`app/persistence/firestore/repositories.py`) and its exact `where()`/
`order_by()` chain extracted:

| Repository | Equality filters | Range filter | order_by |
|---|---|---|---|
| Signal | signal_type, source, service_name, environment, severity, status | observed_at | observed_at |
| Detection | detection_type, domain, service_name, environment | detected_at | detected_at |
| Resolution | detection_id, remediation_strategy, risk | resolved_at | resolved_at |
| FeatureReview | status | created_at | created_at |
| RemediationVerification | outcome, status | verification_started_at | verification_started_at |

Every one of these combines at least one equality filter with an
`order_by` on a different field whenever that equality filter is used
without the matching range filter — the textbook case Firestore requires
a composite index for. **Because every filter is optional** (all
`None`-skippable, ANDed only when the caller supplies them — see each
`*Query` model's own docstring), the *exact set* of composite indexes
actually needed depends on which filter combinations real traffic (the
UI, the demo harness) actually exercises — not every mathematically
possible combination. Enumerating and hand-writing all of them
speculatively would mean guessing at combinations that may never be
queried (wasted index-maintenance cost) while still risking missing the
ones that matter — exactly what this task's "do not blindly create
indexes" instruction warns against.

**Decision: no `firestore.indexes.json` was authored in this task.**
Firestore's own `FailedPrecondition` error, returned the first time an
uncovered combination actually runs, includes a console link with the
*exact* index definition needed — this is authoritative in a way static
analysis cannot be. The documented procedure (`docs/deployment/gcp.md`
§5) remains: deploy, exercise the UI's real query paths once end-to-end,
follow every `FailedPrecondition` link, then export
(`gcloud firestore indexes composite list --format=json`) and check the
result into the repo. **Not performed** — no live Firestore available.

## What remains BLOCKED (tooling/credentials unavailable)

Every one of the following (task §§3–17) requires `gcloud`/Docker/live
GCP credentials, none of which exist in this environment:

| Task item | Status | Reason |
|---|---|---|
| §3 Real GCP deployment | **BLOCKED** | no `gcloud`, no credentials |
| §4 Enable required APIs | **BLOCKED** | same |
| §5 Firestore live smoke test | **BLOCKED** | same (analysis done, §above) |
| §6 Pub/Sub topic/subscription creation | **BLOCKED** | same |
| §7 Worker deployment model decision | **DOCUMENTED, not executed** | analysis already in `docs/deployment/gcp.md` §10 from the prior audit; unchanged this task, still requires a live project to validate which of worker-pools vs. Service-with-health-check is actually available |
| §8 Service account creation/IAM | **BLOCKED** | same |
| §9 Control Plane Cloud Run deploy | **BLOCKED** | same (Dockerfile reviewed, not built — no Docker daemon) |
| §10 Real Gemini/ADK call in GCP | **BLOCKED** | same |
| §11 Agent Search live retrieval | **BLOCKED** | same |
| §12 Cloud Monitoring/Logging live query | **BLOCKED** | same |
| §13 DeploymentAgent live Cloud Run test | **BLOCKED** | same |
| §14 Jira live test | **NOT ATTEMPTED** | no credentials provided; kept at the existing fake/demo boundary (see below) |
| §15 Live incident end-to-end | **BLOCKED** | depends on all of the above |
| §16 Live product feature end-to-end | **BLOCKED** | depends on all of the above |

## Jira status (§14)

No Jira credentials were provided or discovered in this environment
(`.env`'s `jira_*` fields were not inspected for values, consistent with
never printing credential contents — see the security check below). No
change was made to Jira configuration or code.
`app/core/jira_client.py`/`FeatureReviewService` remain exactly as
before: a real client is constructed lazily only inside `approve()`, and
`app/demo/fakes.py::FakeJiraClient` remains the deterministic demo/test
boundary. **Recommendation carried over unchanged from the prior audit**:
decide before recording the demo video whether a real Jira site will be
configured; if not, narrate around that one step or accept the demo will
show a `TicketCreationFailedError` (HTTP 503) at that point.

## Security check before deployment (§17) — re-run this task

```
$ grep -rniE "api[_-]?key\s*=\s*['\"]|secret\s*=\s*['\"]|AIza[0-9A-Za-z_-]{20,}|-----BEGIN.*PRIVATE KEY-----" app ui/src
  -> no matches (excluding sanitize.py's own detection regex and docs)
$ find . -iname "*service*account*.json" -o -iname "*credentials*.json"
  -> no matches
$ git ls-files .env
  -> not tracked (confirmed git-ignored)
```

- `API_SERVE_UI`: defaults `False` — production-safe (must be explicitly
  set `true` in the deploy command, as documented).
- CORS: defaults to `[]` (no origins) — production-safe.
- Reviewer auth: `Settings.api_auth_mode="development"` remains
  attribution-only, honestly documented as such in
  `docs/architecture/control_plane_api.md` §5 and
  `docs/architecture/control_plane_ui.md` §5 — unchanged, not a new
  finding.
- Vertex AI mode: now enforced by code (§2 above) — was documentation-only
  before this task.
- ADC: every Google client (`app/core/*_client.py`,
  `app/persistence/firestore/*.py`, `app/eventing/google_pubsub_client.py`)
  still constructs its client with zero explicit credentials — unchanged,
  re-confirmed by inspection.

No new secret, hardcoded project ID, or debug flag was introduced.

## Record (per task template — mostly N/A given no deployment)

| Field | Value |
|---|---|
| Project | *(none — no GCP project available in this environment)* |
| Deployed services | none |
| Cloud Run URLs | none |
| Pub/Sub resources | none created |
| Firestore database | none created |
| Service accounts | none created (design documented in `docs/deployment/gcp.md` §8, unchanged) |
| Gemini model | `gemini-3.5-flash` (code default, unverified against live Vertex AI) |
| Vertex AI mode | enforced in code (`GOOGLE_GENAI_USE_VERTEXAI=true` via `app/config.py`), unverified live |
| Agent Search | not wired into the live API container by default (unchanged from prior audit); no datastore created |
| Monitoring status | not exercised live |
| Logging status | not exercised live |
| Deployment-agent test status | not exercised live |
| Live smoke tests | none run |
| Timestamp | 2026-08-29 |

## Known limitations (unchanged or newly confirmed)

- No live GCP deployment exists for Quipu as of this document.
- The exact Gemini 3.5+ model identifier has not been confirmed against
  a live Vertex AI project.
- Firestore composite indexes are undiscoverable without a live project
  (analysis above explains why guessing was deliberately avoided).
- The worker's Cloud Run deployment model (worker pools vs. Service +
  health-check adapter) is still a plan, not a validated choice — depends
  on product availability in the actual target project/region.
- Agent Search remains real-but-unwired in the default API container.
- Jira remains at the fake/demo boundary unless a deployer supplies real
  credentials.

**Bottom line: this task fixed the two things it could fix without GCP
access (Gemini model default, Vertex AI/ADC enforcement), verified both
are actually consumed by the runtime, and left every deployment-dependent
item honestly marked BLOCKED rather than fabricated.**

## 2026-08-30 follow-up: live corrections against quipu-507109

`gcloud` and live GCP credentials (owner) became available. This pass
applied the corrections identified by a live validation against the
project, and made no other changes. See `docs/deployment/gcp.md` for the
full current-state description; this section records only what changed
and what was decided not to change.

**Code changes**:
- `Settings.gemini_model` default: `gemini-3.5-flash` → `gemini-2.5-flash`
  (`app/config.py`), live-verified via a real `google.genai` Vertex AI
  call against `quipu-507109`/`us-central1` — the old default 404s, the
  new one works (`gemini-2.5-pro` also works, not used as the default).
  `.env.example` and `scripts/smoke_test_gemini.py` updated to match; no
  other hardcoded model id exists in the repo (repo-wide grep confirmed).
- `Dockerfile.worker` added (repo root) — independent build for
  `python -m app.eventing.worker_main`, same `requirements.txt`, no UI
  stage. The existing root `Dockerfile` (API+UI) is unchanged.

**Live GCP changes** (`quipu-507109`):
- `quipu-signals-sub`: ack deadline 10s → 60s; dead-letter policy wired
  to `quipu-signals-dlq` with `maxDeliveryAttempts=5`.
- IAM: Pub/Sub service agent
  (`service-608549741775@gcp-sa-pubsub.iam.gserviceaccount.com`) granted
  `roles/pubsub.publisher` on `quipu-signals-dlq` and
  `roles/pubsub.subscriber` on `quipu-signals-sub` — required for DLQ
  forwarding, verified via `get-iam-policy` on both resources.
- No other live resources were created, modified, or deleted. No Cloud
  Run service or worker-pool was deployed.

**IAM audited, not changed**: `roles/iam.serviceAccountUser`,
`roles/logging.logWriter`, `roles/discoveryengine.viewer` were reviewed
against actual code (`CloudRunDeployer`, `cloud_logging_client.py`,
`app/api/container.py`) and found not required by the live default
request path — none were granted to `quipu-api-sa`/`quipu-worker-sa`.
`docs/deployment/gcp.md` §8 now documents the reasoning per role instead
of listing them as required. `quipu-api-sa`/`quipu-worker-sa` role sets
were not otherwise touched.

**Firestore indexes**: repository query shapes reviewed
(`app/persistence/firestore/repositories.py`) and the fixed field lists
per repository documented in `docs/deployment/gcp.md` §5 — but which
*combinations* need a composite index still cannot be determined without
live query traffic (every filter is optional). No `firestore.indexes.json`
was authored; still BLOCKED on live traffic, as before.

**CloudRunDeployer / service account**: confirmed by reading
`app/core/cloud_run_client.py` and a repo-wide grep for
`service_account`/impersonation that `deploy_cloud_run` never attaches a
service account to the Cloud Run services it deploys — no code change
made, since the architecture does not require one today.

**Tests**: full backend (`pytest`) and frontend (`vitest run`) suites run
after the code changes above — see the test run this pass recorded
separately for pass/fail counts.

**Remaining BLOCKED items** (unchanged from the section above, still
true): no Cloud Run service/worker-pool deployed yet, worker image not
yet built/pushed, Firestore composite indexes not yet discoverable,
Discovery Engine/Agent Search not enabled or wired, Jira not configured.
This task did not deploy anything, per its own instruction.
