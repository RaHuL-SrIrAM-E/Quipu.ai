# Quipu — Hackathon Submission Readiness Report

**Audit date**: 2026-08-29. **Environment**: this audit was performed in a
sandboxed session with no `gcloud` CLI, no GCP credentials, and no
network access to Google Cloud APIs. Every "verified" claim below was
verified either by (a) reading the actual source code and its tests, or
(b) running the local test suites. **No live GCP deployment was performed
during this audit.** Section C states exactly what remains unverified.

Source for hackathon requirements: `allthingsagentichackathon.devpost.com`
(main page + `/rules`), fetched live during this audit — see quoted text
throughout.

---

## A. Taskmaster requirement matrix

Track description (quoted): *"Build a complete workflow, not just a
chatbot"* — find *"a messy, multi-step chore"* and build an agent that
*"handles the details, sends the right info to the right places."*

| Requirement | Status | Evidence | Missing action |
|---|---|---|---|
| Required Google tech #1: Gemini 3.5+ via Gemini API/Vertex AI | **PARTIALLY MET** | `Settings.gemini_model` default is now `"gemini-3.5-flash"` (`app/config.py`), read centrally by all 10 `LlmAgent` construction sites — code-level fix applied and verified locally (`tests/`, `979 passed`). `GOOGLE_GENAI_USE_VERTEXAI` is now set by `app/config.py` itself (`os.environ.setdefault`), not merely documented — verified consumed at import time. | Confirm the exact model id against a live Vertex AI project (this environment has no `gcloud`/credentials to do so — see `docs/deployment/gcp_validation.md`), and execute at least one real call. Remains PARTIALLY MET, not MET, until that live verification happens. |
| Required Google tech #2: a Google Agent Framework | **MET** | Google ADK (`google-adk` in `requirements.txt`; `LlmAgent`/`SequentialAgent`/`LoopAgent` used in `app/agents/*.py`, `app/orchestration/adk/*.py`) | none |
| Required Google tech #3: a Google Cloud infrastructure service | **MET** (multiple) | Firestore, Pub/Sub, Cloud Run all real integrations — see §B | none |
| 1. Event-driven | **MET** | Pub/Sub → `SignalConsumerWorker` → `SignalIngestionService` — real, tested (`tests/test_pubsub_worker.py`) | none |
| 2. Watches for change | **MET** | `MonitoringAgent` (Cloud Monitoring/Logging) + product-feedback adapters both produce Signals from real external change | MonitoringAgent has no production *trigger* wired (no Cloud Scheduler) — see Gap G3 |
| 3. Understands what needs to happen | **MET** | `DetectingAgent` (evidence-first Gemini reasoning), `IncidentResolutionAgent` (diagnosis + strategy) | none |
| 4. Autonomous routing | **MET** | `DetectionProcessor` → `DetectingAgent` → `OrchestrationService` deterministically routes INCIDENT→remediation, FEATURE_OPPORTUNITY→review, with no human-written routing rule per event | none |
| 5. Interacts with different applications | **MET** | Jira (ticket creation), Cloud Run (target-app deployment), Cloud Monitoring/Logging (telemetry), Pub/Sub (ingestion) | none |
| 6. Completes work end-to-end | **MET** | Signal → Detection → (Review or Resolution) → Planning/Architecture/Codegen/Testing/Deployment → Monitoring → Verification, proven by `DemoHarness.run_feature_flow()`/`run_incident_flow()` | none |
| 7. Handles exceptions/recovery | **MET** | Testing-failure retry routing (`app/orchestration/decisions.py`), deployment-failure classification, Pub/Sub redelivery/dead-letter policy, verification's INSUFFICIENT_EVIDENCE/ESCALATED outcomes — all real, tested | none |
| 8. Minimal human guidance | **MET** | Only two human touchpoints by design: feature-review approve/reject, and the fact that remediation requires an already-authorized `ResolutionResult` — everything else is autonomous | none |

**Taskmaster fit: strong.** Quipu is not a chatbot; it is a closed-loop,
event-driven engineering workflow with a genuine multi-application
footprint (Jira, Cloud Run, Cloud Monitoring/Logging, Pub/Sub, Firestore)
and real autonomous routing decisions. The one blocking gap is the Gemini
model version string, which is a one-line configuration fix, not an
architecture gap.

---

## B. Google services matrix

| Service | Where used | File(s) | Real or abstraction? | ADC? | Needs GCP resource? | Exercised by demo/tests? | Can we honestly claim it? |
|---|---|---|---|---|---|---|---|
| Google ADK | 7 agents + orchestration decision agent + Sequential/Loop | `app/agents/*.py`, `app/orchestration/adk/*.py` | **Real** (`LlmAgent`/`SequentialAgent`/`LoopAgent`, monkeypatched only at the `InMemoryRunner` construction seam for tests) | n/a | no | Yes — every agent test, `DemoHarness` | **Yes** |
| Gemini | Reasoning for every agent | via ADK, see above | **Real**; credential mode now pinned in code — see §E finding | Yes, by default (`app/config.py` sets `GOOGLE_GENAI_USE_VERTEXAI=true`) | Vertex AI enabled | Yes (with fake runner in tests; real call still unverified live — see `docs/deployment/gcp_validation.md`) | **Yes for the architecture claim; not yet for "verified against live Vertex AI"** |
| Agent Search / Discovery Engine | `KnowledgeGateway` backend option | `app/knowledge/backends/google_search.py` | **Real client code**, but not wired into `app/api/container.py`'s default (in-memory backend used instead) | Yes (client itself) | Discovery Engine data store | Only via `tests/integration/test_google_search_integration.py` (gated, skipped by default) | **Partially** — real code exists and is tested in isolation; not part of the live API request path today |
| Firestore | Durable state for every domain entity | `app/persistence/firestore/*.py` | **Real** | Yes | Firestore Native database | Yes — `tests/test_firestore_persistence.py`, gated | **Yes** |
| Cloud Run (target-app deploy) | `DeploymentAgent` | `app/core/cloud_run_client.py` | **Real** | Yes | Artifact Registry + Cloud Run API enabled | Yes — `tests/test_deployment_agent.py` (fake client), demo (fake client) | **Yes** |
| Cloud Run (Quipu's own hosting) | Control Plane API + UI | `Dockerfile`, `app/main.py` | **Real deployment target**, not yet actually deployed | Yes | Cloud Run API enabled | No (deployment not performed in this audit) | **Only after a real deploy — see §C** |
| Cloud Monitoring | `MonitoringAgent` | `app/core/cloud_monitoring_client.py` | **Real** | Yes | Monitoring API enabled, a monitored Cloud Run service | Yes — gated integration test, demo (fake client) | **Yes** |
| Cloud Logging | `MonitoringAgent` | `app/core/cloud_logging_client.py` | **Real** | Yes | Logging API enabled | Yes — gated integration test, demo (fake client) | **Yes** |
| Pub/Sub | Signal ingestion transport | `app/eventing/google_pubsub_client.py` | **Real** | Yes | Topic + subscription | Yes — gated integration test, full unit suite via in-memory fake | **Yes** |

**Summary**: 8 of 9 rows are real, tested integrations. One (Agent Search)
is real code not yet wired into the live API path by default. This
satisfies the hackathon's "at least one Google Cloud infrastructure
service" requirement many times over — the honest claim is "Firestore,
Pub/Sub, and Cloud Run are all real, ADC-authenticated integrations,"
which is true regardless of deployment status, because the client code
itself is real and tested; only the *live deployment* of Quipu's own
Cloud Run service is unverified (§C).

---

## C. GCP deployment status

**Still no live deployment as of this update.** A follow-up task
attempted to execute `docs/deployment/gcp.md` against a real GCP
environment; that environment also had no `gcloud` CLI, no Docker, and no
credentials, so it was correctly halted before any resource was touched —
see `docs/deployment/gcp_validation.md` for the full record of what was
checked and what was fixed at the code level (Gemini model default,
Vertex AI/ADC enforcement) despite the lack of live access. Everything
below is a static-audit conclusion, not a runtime observation.

### What was verified locally (real, in this environment)

1. Full backend test suite: **979 passed, 10 skipped** (`pytest tests/`).
2. Full frontend test suite: **30/30 passed** (`npm run test` in `ui/`).
3. `tsc -b` (TypeScript) clean.
4. `npm run build` (Vite production build) succeeds.
5. `docker` was not available/exercised in this session — the
   `Dockerfile`'s syntax was reviewed by inspection, not built.
6. Every Google client module (`app/core/*_client.py`,
   `app/persistence/firestore/*.py`, `app/eventing/google_pubsub_client.py`,
   `app/knowledge/backends/google_search.py`) constructs its real SDK
   client lazily, with no explicit credential argument (ADC by
   construction) — confirmed by reading every constructor.
7. No secret, API key, or service-account JSON is committed to the
   repository (`.env` exists locally, confirmed git-ignored and untracked
   — see §E).

### What remains unverified (requires live GCP access)

1. Quipu's own Control Plane API actually running on Cloud Run.
2. The UI actually loading from that deployed service.
3. `/health` and `/ready` responding from a live deployment.
4. A real Firestore read/write round-trip in production.
5. A real Pub/Sub message entering the system and being pulled by a
   deployed worker.
6. A real Signal being persisted from that message.
7. A real Gemini/Vertex AI call completing (this also verifies §A's model
   version once fixed).
8. A real Cloud Monitoring/Logging query.
9. A real `deploy_cloud_run` call reaching the Cloud Run API.
10. A real `RemediationVerification` record round-tripping through
    Firestore.
11. Composite Firestore indexes actually being created (§ "Firestore"
    below) — cannot be discovered without live queries.
12. The Pub/Sub worker's actual behavior when deployed as a Cloud Run
    worker pool or CPU-always-allocated service (§ "Worker deployment
    model" below) — the code is unit-tested against a real in-memory
    Pub/Sub fake and a gated real-Pub/Sub integration test exists
    (`tests/integration/test_pubsub_integration.py`), but neither proves
    the *Cloud Run process model* works, only that the client/worker
    logic itself is correct.

**Do not claim Quipu is deployed to GCP until items 1–10 above are
actually executed against a real project and the outputs captured (Cloud
Run dashboard screenshot, a `.run.app` URL, Cloud Logging entries) for the
demo video — this is a hard hackathon requirement (§ "Deployment
Requirements": *"clear proof that it was built and deployed on Google
Cloud"*).**

### Firestore

Collections match the expected families exactly: `workflows/` (+
subcollections `artifacts`, `executions`, `decisions`), `signals/`,
`detections/`, `resolutions/`, `remediation_verifications/`,
`feature_reviews/`. Optimistic concurrency (`update_if_version` +
`VersionConflictError`) is implemented for every entity that needs it
(`WorkflowState`, `FeatureReview`, `RemediationVerification`) via real
Firestore transactions (`firestore.async_transactional`, see
`app/persistence/firestore/repositories.py`). **Gap**: no
`firestore.indexes.json` exists — see `docs/deployment/gcp.md` §5 for the
documented recommended procedure. This is a genuine, real limitation, not
fixed in this audit (requires a live Firestore project to discover the
exact composite indexes needed).

### Worker deployment model

See `docs/deployment/gcp.md` §10 for the full analysis. Summary: the
current `SignalConsumerWorker` (sync-pull, `asyncio` loop) is correct,
tested code, but `app/eventing/worker_main.py` has no HTTP server, so it
cannot be deployed as a standard Cloud Run *Service* without either (a)
Cloud Run worker pools (preferred, zero code change) or (b) a small
additive `/healthz` HTTP handler bolted onto the existing entrypoint
(identified, not implemented — see "Remaining blockers" below). Push
delivery (Pub/Sub → HTTP) was considered and explicitly **not** adopted —
it would require redesigning the ack/nack contract from an explicit
`message.ack()`/`nack()` call into an HTTP status-code contract, which is
a real transport-boundary redesign, not a minimal adapter.

---

## D. Demo scenario status

Both scenarios described in the task are already fully implemented and
proven deterministic **locally** via `app/demo/harness.py::DemoHarness`
(no GCP credentials required) — see
`docs/architecture/end_to_end_demo.md` for the full existing design.

| Scenario | Status | Evidence |
|---|---|---|
| A — Product Evolution (feedback → Pub/Sub → Signal → Detecting → Feature Opportunity → Human Approval → Jira → Planning → Architecture → Codegen → Testing → Deployment → Monitoring) | **MET locally, PARTIALLY MET on real GCP** | `DemoHarness.run_feature_flow()` (`tests/test_demo_feature_flow.py`) proves every step except the literal Pub/Sub hop (the harness seeds Signals directly via `app.signals.adapters`, matching real ingestion output exactly — see that doc's own note). `app/demo/worker_demo.py` separately proves the real Pub/Sub → worker → Signal → Detection hop end-to-end locally (in-memory Pub/Sub, real `SignalIngestionService`/`DetectionProcessor`). No run has combined "real Pub/Sub over the network" with "real Gemini" with "real Jira" with "real Cloud Run deploy" in one pass — that is the live-GCP verification described in §C, not yet performed. |
| B — Production Incident (telemetry → Signal → Detecting → Incident Resolution → CODE_FIX → Codegen → Testing → Deployment → fresh telemetry → Verification → VERIFIED RESOLVED) | **MET locally, PARTIALLY MET on real GCP** | `DemoHarness.run_incident_flow()` (`tests/test_demo_incident_flow.py`) proves the full chain including a **second**, independent remediation cycle whose post-deployment evidence stays degraded (`STILL_DEGRADED`) — i.e. the demo already proves both possible verification outcomes, not just the happy path. Same real-Pub/Sub caveat as Scenario A. |

No core business logic needs to change to make either scenario
demonstrable — both are already reproducible with a single command
(`pytest tests/test_demo_feature_flow.py tests/test_demo_incident_flow.py`
or `python -m app.demo.run --scenario both`).

---

## E. Security status

Repository-wide audit performed against `app/tools/`, `app/agents/`,
`app/api/`, `app/eventing/`, `app/core/`, plus a full-repo grep for
secret-shaped strings.

| Finding | Severity | Status | Detail |
|---|---|---|---|
| No committed secrets/API keys/service-account JSON | — | **CONFIRMED CLEAN** | Full-repo grep for key-shaped patterns, private-key headers, and `*.json` credential files found nothing; `.env` exists locally but is git-ignored and untracked (verified via `git check-ignore`/`git ls-files`) |
| Gemini/ADK credential mode not explicitly pinned to Vertex AI/ADC | **Medium — genuine gap, found in a prior audit** | **CODE-FIXED this task** | `app/config.py` now calls `os.environ.setdefault("GOOGLE_GENAI_USE_VERTEXAI", "true")` at import time (before any agent's module-level `LlmAgent(...)` construction), preceded by `load_dotenv()` so an explicit override is still respected. Verified consumed by the runtime locally (`os.environ["GOOGLE_GENAI_USE_VERTEXAI"] == "true"` after `import app.config`). Still **unverified against a real Vertex AI call** — no credentials available in this environment; see `docs/deployment/gcp_validation.md`. |
| `app/core/llm.py::GeminiClient` is dead code | Low | **NOTED** | Not imported anywhere; harmless, but should not be cited as evidence of "ADC-only Gemini access" since it's not on the live path — the real evidence is the env var fix above |
| Codegen file-write tool | — | **CONFIRMED SAFE** | `app/tools/codegen_tools.py::write_file` — capability-gated, rejects absolute paths, enforces an architecture-approved allow-list, and resolves through `_safe_join` (traversal/symlink-escape protection) before ever touching disk; a rejected write never reaches the filesystem |
| Testing/tool execution | — | **CONFIRMED SAFE** (unchanged from prior audits this project) | `app/tools/testing_tools.py` runs a bounded, real `pytest` subprocess against the workspace only — no shell string interpolation, no arbitrary command |
| Deployment target selection | — | **CONFIRMED SAFE** | `deploy_cloud_run` builds the image URI from `Settings.cloud_run_image_registry` (app-controlled) + a regex-validated model-supplied tag — the model never supplies a full image URI |
| No `subprocess`/`os.system`/`shell=True` anywhere in `app/` | — | **CONFIRMED CLEAN** | grep found zero matches |
| No `eval`/`exec` anywhere in `app/` | — | **CONFIRMED CLEAN** | grep found zero matches |
| Pub/Sub payload → adapter dispatch is a closed allow-list | — | **CONFIRMED SAFE** (established in prior work) | `app/eventing/mapping.py` — no payload-driven dynamic import/dispatch |
| Signal evidence/metadata sanitization | — | **CONFIRMED SAFE** (established in prior work) | `app.signals.sanitize.sanitize_metadata` redacts secret-shaped keys, truncates oversized values, applied before every Signal is persisted |
| Control Plane API dangerous-operation surface | — | **CONFIRMED CLEAN** | No shell/deploy/arbitrary-tool/arbitrary-agent endpoint exists; structurally tested (`tests/test_api.py`, `ui/src/App.test.tsx`) |
| API auth mode is development-only | Medium (by design, already documented) | **KNOWN, DOCUMENTED** | `Settings.api_auth_mode="development"` trusts a caller-supplied header for attribution only, no real authentication — already explicitly documented as a limitation in `docs/architecture/control_plane_api.md` §5 and `docs/architecture/control_plane_ui.md` §5; must not be represented as production-grade auth in the submission |
| CORS defaults to no origins (`[]`) | — | **CONFIRMED SAFE** | Never defaults to `"*"` |
| No firestore.indexes.json | Low (deployment-readiness, not a security issue) | **DOCUMENTED GAP** | See §C "Firestore" |

**No high-severity issue requiring a code fix was found.** The one
genuine gap (Gemini credential mode) is fixed by documentation/deployment
configuration, not a code change, per this audit's own scope ("do not
modify anything until the audit is complete" / "fix genuine high-severity
issues if they can be fixed safely without changing architecture" — this
one is fixed at the environment-configuration layer, which is the
correct, safe fix here since the actual risk is "which credential a
deploy uses," not a code defect).

---

## F. Architecture diagram reference

See `docs/architecture/system_overview.md` — the submission-quality
diagram, including the explicit Cloud Run (A) vs. Cloud Run (B)
distinction the task required (Quipu's own hosting vs. the target
applications Quipu deploys).

---

## G. Remaining blockers

Ordered by priority for submission readiness. Items 1 and 3 are now
**code-fixed** (this update) but still need live verification; nothing
below can be closed out without `gcloud`/Docker/GCP credentials, which
remain unavailable in every environment this project has been audited
from so far — see `docs/deployment/gcp_validation.md`.

1. **Gemini model version** (§A) — ~~update `GEMINI_MODEL`~~ **DONE, live-
   verified 2026-08-30**: default is `gemini-2.5-flash`, centrally read by
   all 10 `LlmAgent` sites. `gemini-3.5-flash` (the earlier default) was
   confirmed to return `404 NOT_FOUND` from live Vertex AI in
   `quipu-507109`/`us-central1` — not available for this project/region.
   `gemini-2.5-flash` and `gemini-2.5-pro` were both confirmed working via
   a real `google.genai` Vertex AI call; `gemini-2.5-flash` is the current
   default. See `docs/deployment/gcp_validation.md` for the full record.
2. **Actual GCP deployment** (§C) — still nothing executed; a second
   attempt (this task) also found no `gcloud`/Docker/credentials and
   correctly halted rather than faking results.
3. **`GOOGLE_GENAI_USE_VERTEXAI=true`** — ~~must be set~~ **DONE, live-
   verified 2026-08-30**: enforced in code (`app/config.py`) and exercised
   against a real Vertex AI call in `quipu-507109`/`us-central1` (item 1
   above) — no longer merely documented or code-enforced-but-untested.
4. **Firestore composite indexes** (§C) — analyzed this task (every
   `query()` method's filter/order-by shape is now documented in
   `docs/deployment/gcp_validation.md`); deliberately not guessed at in a
   `firestore.indexes.json`, since Firestore's own `FailedPrecondition`
   error is the only reliable source for the exact index definition and
   no live project exists to trigger it.
5. **Worker deployment model** (§C) — unchanged: needs either Cloud Run
   worker pools (verify availability in the target project/region) or the
   identified-but-not-implemented `/healthz` adapter for a Service-based
   fallback.
6. **Agent Search wiring** (§B) — unchanged: real code exists and is
   independently tested but is not part of the live API's default
   container.
7. **Jira credentials** — unchanged: optional for the demo; decide before
   recording whether to configure a real Jira site or narrate around that
   step.

None of these require new agents, new LLMs, new databases, Kubernetes,
another messaging system, another UI, or another orchestration engine —
every blocker above is a configuration, documentation, or one small
additive-adapter task. Two of seven are now done at the code level.

---

## H. Recommended demo flow (for the ~4-minute video)

1. **Problem statement** (15s): "Engineering teams manually triage
   feedback and incidents, then manually route them through planning,
   coding, testing, and deployment. Quipu does this autonomously, and —
   critically — never claims an incident is fixed just because a
   deployment succeeded."
2. **Architecture diagram** (20s): show `docs/architecture/system_overview.md`'s
   diagram, narrate the two Cloud Run usages (A vs. B) explicitly.
3. **Scenario A — live** (60–75s): publish a customer-feedback event to
   the real Pub/Sub topic → show the Signal appear in the UI's Signal
   Explorer → show the Detection appear in Detection Center → approve it
   in the Feature Review Queue → show the Jira ticket → show the workflow
   advance through Planning/Architecture/Codegen/Testing/Deployment in
   Workflow Detail.
4. **Scenario B — live** (60–75s): trigger (or replay) a production
   telemetry signal → show the Incident/Resolution Console → click
   "Authorize Remediation" → show the remediation timeline advance → show
   the Verification page's outcome, explicitly narrating "deployment
   succeeded" vs. "verified resolved" as two different, separately-proven
   facts.
5. **Google Cloud proof** (20–30s): Cloud Run dashboard showing the
   `quipu-api` service and its `.run.app` URL, a Cloud Logging view of a
   real request, and (if time) a Vertex AI/Gemini log line.
6. **Close** (10s): restate the value proposition.

Record this only after §G items 1–3 are actually done — the video's
Google Cloud proof requirement cannot be satisfied by the current
(undeployed) state.

---

## I. Recommended submission claims

Safe to state, because verified in this audit:

- "Quipu uses Google ADK for all seven of its specialized agents."
- "Quipu uses Gemini for reasoning, evidence-first: every factual claim
  (test results, deployment outcomes, production telemetry) comes from
  real application code, never from trusting the model's own report."
- "Quipu integrates Firestore, Pub/Sub, and Cloud Run as real,
  ADC-authenticated services — no service-account keys anywhere."
- "Quipu's event-driven ingestion pipeline is fully tested end-to-end,
  including at-least-once redelivery and idempotent deduplication."
- "Quipu explicitly distinguishes deployment success from verified
  incident resolution — a deployment is never, by itself, reported as
  'resolved.'"
- "979 backend tests and 30 frontend tests, all passing, cover this
  architecture."
- "Quipu is configured to use Gemini 2.5 (`gemini-2.5-flash`) via Vertex
  AI/ADC by default — enforced in code, and live-verified with a real
  Vertex AI call against `quipu-507109`/`us-central1` on 2026-08-30
  (`gemini-3.5-flash` was tried first and confirmed unavailable for this
  project/region)."

## J. Claims we should NOT make (yet)

- "Quipu is deployed and running on Google Cloud" — **still not true**;
  a second attempt to deploy (this update) also found no
  `gcloud`/Docker/credentials available and made no live resource.
- "Quipu uses Gemini 3.5 in production" — **false and should not be
  claimed at all**: `gemini-3.5-flash` was live-tested and confirmed
  unavailable (`404 NOT_FOUND`) in `quipu-507109`/`us-central1` (§G
  item 1). The correct claim is "Quipu is configured to use Gemini 2.5
  (`gemini-2.5-flash`) via Vertex AI, live-verified against the target
  project" — this one *is* now backed by a real call, not just code.
- "Quipu uses Vertex AI" — this is now backed by a live-verified call
  (§G item 3), not just code enforcement. Safe to say "configured to use
  Vertex AI/ADC, live-verified against `quipu-507109`" — still distinct
  from "deployed in production," since no Cloud Run service is running
  yet (§C).
- "Quipu uses Agent Search in production" — the code is real, but the
  live API's default container does not wire it in (§B) — say "Agent
  Search integration is implemented and tested" rather than "in
  production use," unless §G item 6 is completed first.
- "Quipu has enterprise-grade authentication" — the current API auth
  mode is explicitly development-only attribution, not authentication
  (§E) — describe it honestly if asked, don't claim more.
- Do not show a doctored or pre-recorded "success" for any step that
  wasn't actually run live during the demo — the judging criteria
  explicitly require *"unedited, live execution."*
