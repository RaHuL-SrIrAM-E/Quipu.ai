# Deploying Quipu to Google Cloud

**Status of this document**: as of 2026-08-30, project `quipu-507109`
(region `us-central1`) has live-verified provisioning for the following —
confirmed by direct `gcloud`/API inspection, not merely planned:

- Artifact Registry Docker repo `quipu` (`us-central1`).
- Firestore `(default)` database, `FIRESTORE_NATIVE`, `us-central1`.
- Pub/Sub topics `quipu-signals` and `quipu-signals-dlq`; subscription
  `quipu-signals-sub` (60s ack deadline, dead-letter policy pointed at
  `quipu-signals-dlq` with `maxDeliveryAttempts=5`; the Pub/Sub service
  agent holds `roles/pubsub.publisher` on the DLQ topic and
  `roles/pubsub.subscriber` on the source subscription — required for
  DLQ forwarding to actually work).
- Service accounts `quipu-api-sa` and `quipu-worker-sa`, IAM bindings
  reduced to what current code actually needs — see §8.
- The live-verified Gemini model default (`gemini-2.5-flash` — see §4).

Not yet done: no Cloud Run service or worker-pool has been deployed, no
Docker image has been built/pushed. Firestore composite indexes remain
undiscovered (see §5 — this can only be done against live query
traffic). Treat §§9–10 below as a validated plan for the *first* deploy,
not yet a verified runbook.

## 1. Prerequisites

- A GCP project with billing enabled.
- `gcloud` CLI installed and authenticated (`gcloud auth login`).
- Docker (for local image builds) or Cloud Build (`gcloud builds submit`).
- Node.js 22+ locally only if you want to build the UI outside Docker.
- A Jira Cloud site + API token, if you want `PlanningAgent`'s real ticket
  creation to work (optional — everything else functions without it; see
  §11 "What still works without it").

## 2. GCP project setup

```bash
export PROJECT_ID=<your-project-id>
export REGION=us-central1
gcloud config set project "$PROJECT_ID"
```

## 3. APIs to enable

```bash
gcloud services enable \
  run.googleapis.com \
  firestore.googleapis.com \
  pubsub.googleapis.com \
  aiplatform.googleapis.com \
  monitoring.googleapis.com \
  logging.googleapis.com \
  artifactregistry.googleapis.com
```

`aiplatform.googleapis.com` is required because Quipu's ADK agents call
Gemini through **Vertex AI**, not the standalone Gemini Developer API —
see §4's `GOOGLE_GENAI_USE_VERTEXAI` note, which is the single most
important environment variable in this whole deployment.

`discoveryengine.googleapis.com` is deliberately **not** in this list —
it is optional and not required for the live default API path. Agent
Search (`app/knowledge/backends/google_search.py`) is real and tested but
`app/api/container.py` does not wire it into the default
`OrchestrationService` (see §7); enable this API only when that wiring is
done and a real Discovery Engine data store is created. Confirmed as of
2026-08-30: this API is not enabled on `quipu-507109`, and nothing in the
live default request path needs it.

## 4. Gemini / ADK credential mode — read this first

Every Quipu agent (`PlanningAgent`, `ArchitectureAgent`, `CodegenAgent`,
`TestingAgent`, `DeploymentAgent`, `DetectingAgent`,
`IncidentResolutionAgent`) constructs an ADK `LlmAgent(model=settings.gemini_model,
...)` with a bare model-name string — it never explicitly passes
`vertexai=True` or a project/location. Google's `google-genai` SDK (which
ADK uses internally) resolves credentials in this order:

1. If `GOOGLE_GENAI_USE_VERTEXAI=true` is set → Vertex AI, authenticated
   via **Application Default Credentials** (the Cloud Run service's
   attached service account — no key file).
2. Otherwise, if `GOOGLE_API_KEY`/`GEMINI_API_KEY` is set → the **Gemini
   Developer API**, authenticated via that literal API key.

Quipu's own `Settings` class has no field for this — it's an SDK-level
environment variable, outside `app/config.py`'s pydantic model. **As of
this task, `app/config.py` sets it itself**
(`os.environ.setdefault("GOOGLE_GENAI_USE_VERTEXAI", "true")`, evaluated
at import time, before any agent module's module-level `LlmAgent(...)`
construction runs) — so every environment gets the safe default without
a deployer having to remember it, while still allowing an explicit
override (`setdefault` never clobbers an already-set value). You may
still set it explicitly for clarity:

```bash
GOOGLE_GENAI_USE_VERTEXAI=true
```

and must **not** set `GOOGLE_API_KEY`/`GEMINI_API_KEY` in that same
environment, or the SDK may prefer the API key path instead. This keeps
Quipu's "ADC only, no embedded API keys" security posture (already true
for Firestore/Cloud Run/Cloud Monitoring/Cloud Logging/Pub/Sub — see
`docs/architecture/*.md`) true for Gemini as well. This was a genuine gap
found during a prior audit (see
`docs/hackathon/submission_readiness.md` §E) — originally fixed by
documentation only; now also enforced at the code level (`app/config.py`)
so it is consumed by the runtime automatically rather than relying on a
deployer copying an env var by hand. **Still unverified against a real
Vertex AI call** — no GCP credentials were available to this task either;
see `docs/deployment/gcp_validation.md`.

`app/core/llm.py::GeminiClient` is a separate, **unused** wrapper (not
imported by any agent) that does explicitly pass `vertexai=True` — it is
not part of the live request path and should not be relied on as
evidence of Vertex AI usage; the actual agents' ADK `LlmAgent`s are what
need `GOOGLE_GENAI_USE_VERTEXAI=true` set at the process level.

**Model id, live-verified 2026-08-30**: `Settings.gemini_model`'s prior
default, `gemini-3.5-flash`, returns `404 NOT_FOUND` from Vertex AI
against `quipu-507109`/`us-central1` (confirmed via a real
`google.genai.Client(vertexai=True, ...).models.generate_content()` call,
not just a REST probe). `gemini-2.5-flash` and `gemini-2.5-pro` both
succeeded and are the current default (`gemini-2.5-flash`). Re-run this
check against the target project before ever bumping the default again —
model availability is project/region-specific and cannot be assumed from
documentation.

## 5. Firestore setup

```bash
gcloud firestore databases create --location="$REGION" --type=firestore-native
```

Quipu uses a single Firestore database, workflow-centric layout for
`workflows/{id}/{artifacts,executions,decisions}`, plus top-level
collections for entities that outlive any one workflow: `signals/`,
`detections/`, `resolutions/`, `remediation_verifications/`,
`feature_reviews/` — see `docs/architecture/persistence.md` and each
level's own doc for the exact rationale.

**Composite indexes**: no `firestore.indexes.json` exists in this
repository yet — this is a genuine, documented gap (see submission
readiness §G). Several repository `query()` methods combine a range
filter (`since`/`until`) with multiple equality filters
(`app/persistence/firestore/repositories.py`'s own comments flag this on
`FirestoreSignalRepository`/`FirestoreDetectionRepository` etc.).
Firestore will refuse an uncovered combination the first time it runs
with a `FailedPrecondition` error that includes a console link to create
the exact index needed. **Recommended procedure**: after deploying,
exercise each list/filter endpoint once (via the UI or `curl`), follow
every `FailedPrecondition` link that appears, then export the resulting
indexes with `gcloud firestore indexes composite list --format=json` and
check that output into the repo as `firestore.indexes.json` for future
`gcloud firestore indexes composite create` reproducibility.

**What can and can't be determined statically (checked 2026-08-30,
`app/persistence/firestore/repositories.py`)**: every `query()` method
combines optional equality `.where()` filters with an optional
`since`/`until` range filter, always finishing with `.order_by()` on the
same timestamp field. The *fields involved* in each repository are fixed
and listed below, but every filter is independently optional (`None`
skips it), so which *combination* actually triggers Firestore's composite
index requirement depends entirely on which filters real traffic
supplies together — that part cannot be determined without running the
actual queries.

| Repository | Equality filter fields | Range + order field |
|---|---|---|
| `FirestoreSignalRepository` | `signal_type, source, service_name, environment, severity, status` | `observed_at` |
| `FirestoreDetectionRepository` | `detection_type, domain, service_name, environment` | `detected_at` |
| `FirestoreResolutionRepository` | `detection_id, remediation_strategy, risk` | `resolved_at` |
| `FirestoreFeatureReviewRepository` | `status` | `created_at` |
| `FirestoreRemediationVerificationRepository` | `outcome, status` | `verification_started_at` |

Deliberately **no `firestore.indexes.json` was authored** from this
table — enumerating every mathematically possible subset would create
indexes for combinations that may never be queried (wasted
index-maintenance cost) while still risking missing the one combination
that matters, which is exactly what Firestore's own `FailedPrecondition`
error tells you for free, with an exact console link. Not done in this
pass (still no live Firestore query traffic to observe).

## 6. Pub/Sub setup

```bash
gcloud pubsub topics create quipu-signals
gcloud pubsub subscriptions create quipu-signals-sub \
  --topic=quipu-signals \
  --ack-deadline=60
```

A dead-letter topic is optional (`Settings.pubsub_dead_letter_topic`) —
create one only if you want Pub/Sub's own max-delivery-attempts policy on
top of the application-level permanent/transient classification
(`app/eventing/errors.py`) that already exists:

```bash
gcloud pubsub topics create quipu-signals-dlq
gcloud pubsub subscriptions update quipu-signals-sub \
  --dead-letter-topic=quipu-signals-dlq \
  --max-delivery-attempts=5
```

**DLQ forwarding also requires IAM** — Pub/Sub's own service agent
(`service-<PROJECT_NUMBER>@gcp-sa-pubsub.iam.gserviceaccount.com`, get
the project number via `gcloud projects describe`) must be able to
publish to the DLQ topic and pull from the source subscription. This step
was missing from this document until the 2026-08-30 validation and is
**not optional** — without it Pub/Sub silently cannot deliver to the DLQ:

```bash
PROJECT_NUMBER=$(gcloud projects describe "$PROJECT_ID" --format="value(projectNumber)")
PUBSUB_SA="service-${PROJECT_NUMBER}@gcp-sa-pubsub.iam.gserviceaccount.com"

gcloud pubsub topics add-iam-policy-binding quipu-signals-dlq \
  --member="serviceAccount:${PUBSUB_SA}" --role="roles/pubsub.publisher"
gcloud pubsub subscriptions add-iam-policy-binding quipu-signals-sub \
  --member="serviceAccount:${PUBSUB_SA}" --role="roles/pubsub.subscriber"
```

**Live state as of 2026-08-30** (`quipu-507109`): both topics exist,
`quipu-signals-sub` has a 60s ack deadline and the DLQ policy above fully
wired, and both IAM bindings above are in place and verified via
`gcloud pubsub topics/subscriptions get-iam-policy`.

## 7. Agent Search (Discovery Engine) setup

Optional — `AgentContext.knowledge` defaults to an in-memory backend with
zero documents (`app/api/container.py`) unless wired to a real Discovery
Engine data store. To use the real backend:

```bash
# Create a data store in the Google Cloud Console (Discovery Engine ->
# Data Stores) or via the discoveryengine API, then:
DISCOVERY_ENGINE_DATA_STORE_ID=<your-data-store-id>
DISCOVERY_ENGINE_LOCATION=global
```

Nothing in `app/api/container.py` currently wires the real
`app/knowledge/backends/google_search.py` backend into the API's
`OrchestrationService` — this is a documented limitation (§8 of
`docs/architecture/control_plane_api.md`'s container design), not a bug:
Enterprise Knowledge grounding is real and tested
(`tests/test_knowledge_platform.py`) but not yet wired end-to-end through
the Control Plane API's dependency container. Fixing this is a small,
safe additive change (swap `InMemoryRetrievalBackend` for
`GoogleSearchBackend` behind the existing `KnowledgeGateway` Protocol) but
was not made in this audit-only task, per its own "do not overbuild"
instruction.

## 8. IAM / service accounts

Two service accounts, least-privilege, no keys (ADC via attached
identity only).

```bash
gcloud iam service-accounts create quipu-api-sa --display-name="Quipu Control Plane API"
gcloud iam service-accounts create quipu-worker-sa --display-name="Quipu Pub/Sub Signal Worker"

for ROLE in roles/datastore.user roles/aiplatform.user \
            roles/run.developer \
            roles/monitoring.viewer roles/logging.viewer; do
  gcloud projects add-iam-policy-binding "$PROJECT_ID" \
    --member="serviceAccount:quipu-api-sa@${PROJECT_ID}.iam.gserviceaccount.com" \
    --role="$ROLE"
done

for ROLE in roles/pubsub.subscriber roles/datastore.user roles/aiplatform.user; do
  gcloud projects add-iam-policy-binding "$PROJECT_ID" \
    --member="serviceAccount:quipu-worker-sa@${PROJECT_ID}.iam.gserviceaccount.com" \
    --role="$ROLE"
done
```

**IAM audited against actual code on 2026-08-30 — three roles previously
listed here were removed** after checking whether current code paths
actually need them (not granted speculatively just because an earlier
version of this document listed them):

- `roles/iam.serviceAccountUser` (was on `quipu-api-sa`): this role is
  needed only to *act as* another service account — e.g. attaching a
  non-default SA to a Cloud Run service being deployed. Checked
  `CloudRunDeployer.deploy()` (`app/core/cloud_run_client.py`): the
  `run_v2.Service`/`RevisionTemplate`/`Container` it builds never sets a
  `service_account` field, and no code anywhere under `app/` references
  `service_account` or does any impersonation (verified by repo-wide
  grep). `DeploymentAgent` therefore never needs to act as another
  identity today. **Not granted.** If a future change makes
  `deploy_cloud_run` pin a target service account, add this role back
  then, scoped to that specific service account resource where possible.
- `roles/logging.logWriter` (was on both service accounts): needed only
  to call the Cloud Logging *write* API directly. Checked
  `app/core/cloud_logging_client.py`: it only calls
  `LoggingServiceV2AsyncClient`'s `ListLogEntries` (read path), used by
  `MonitoringAgent` — covered by `roles/logging.viewer`, already granted.
  Repo-wide grep for `logging.Client`/log-write calls found nothing.
  Cloud Run itself captures container stdout/stderr into Cloud Logging
  automatically, with no IAM grant required on the running service's
  identity. **Not granted.**
- `roles/discoveryengine.viewer` (was on both service accounts): Agent
  Search (`app/knowledge/backends/google_search.py`) is real but not
  wired into the default API container (`app/api/container.py` uses
  `InMemoryRetrievalBackend`), and `discoveryengine.googleapis.com` isn't
  even enabled on the project (see §3). **Not granted** until that
  wiring exists and the API is enabled — add it at that point, not
  before.

Neither service account is `Owner`/`Editor`. Neither uses a downloaded
JSON key — both are attached directly to their Cloud Run
service/worker-pool identity, and every Google client in the codebase
(`app/core/*_client.py`, `app/persistence/firestore/client.py`,
`app/eventing/google_pubsub_client.py`) already constructs its client
with no explicit credentials argument, which resolves to ADC
automatically.

**Live state as of 2026-08-30** (`quipu-507109`): both service accounts
exist with exactly the reduced role sets above —
`quipu-api-sa`: `aiplatform.user`, `datastore.user`, `logging.viewer`,
`monitoring.viewer`, `run.developer`;
`quipu-worker-sa`: `aiplatform.user`, `datastore.user`,
`pubsub.subscriber`. No IAM changes were made to either account in this
pass (only Pub/Sub-resource-level bindings for DLQ forwarding, §6).

## 9. Cloud Run deployment — the Control Plane API + UI

```bash
gcloud builds submit --tag "${REGION}-docker.pkg.dev/${PROJECT_ID}/quipu/api:latest" .

gcloud run deploy quipu-api \
  --image="${REGION}-docker.pkg.dev/${PROJECT_ID}/quipu/api:latest" \
  --region="$REGION" \
  --service-account="quipu-api-sa@${PROJECT_ID}.iam.gserviceaccount.com" \
  --set-env-vars="GCP_PROJECT_ID=${PROJECT_ID},GCP_LOCATION=${REGION},GOOGLE_GENAI_USE_VERTEXAI=true,API_SERVE_UI=true,API_CORS_ALLOW_ORIGINS=[],PUBSUB_SIGNAL_TOPIC=quipu-signals,CLOUD_RUN_IMAGE_REGISTRY=${REGION}-docker.pkg.dev/${PROJECT_ID}/quipu" \
  --allow-unauthenticated \
  --concurrency=40 \
  --timeout=300 \
  --min-instances=0 \
  --max-instances=3 \
  --memory=512Mi
```

Notes:

- `--allow-unauthenticated` is a hackathon-demo choice (the API's own
  authorization boundary is `Settings.api_auth_mode`, deliberately
  development-mode-only — see `docs/architecture/control_plane_api.md`
  §5). Put this behind Cloud Run IAM (`--no-allow-unauthenticated` +
  `roles/run.invoker` grants) for anything beyond a demo.
- `CLOUD_RUN_IMAGE_REGISTRY` must point at an Artifact Registry repo
  `DeploymentAgent` is authorized to push to — this is what makes
  `deploy_cloud_run` build an app-controlled image URI (see
  `docs/architecture/*.md` on Deployment) rather than trusting a
  model-supplied one.
- `--timeout=300` matches `Settings.cloud_run_deploy_timeout_seconds`
  (Deployment's own bound on how long one `deploy_cloud_run` call may
  take) — increase both together if a real target app's build/deploy is
  slower.

This is **Quipu's own control-plane deployment** — distinct from the
Cloud Run services `DeploymentAgent` deploys as part of the SDLC it
orchestrates (§14 below).

## 10. Worker deployment — the Pub/Sub Signal Consumer Worker

`app/eventing/worker_main.py` is a long-running `asyncio` loop with **no
HTTP server** — it does not satisfy a standard Cloud Run Service's
"listen on `$PORT`" health-check contract as-is. See
`docs/hackathon/submission_readiness.md` §"Worker deployment model" for
the full trade-off analysis. Two supported paths, in order of preference:

**A. Cloud Run worker pools** (if available in your project/region) —
purpose-built for exactly this shape (a background process pulling from
a queue, no inbound HTTP required):

```bash
gcloud builds submit --tag "${REGION}-docker.pkg.dev/${PROJECT_ID}/quipu/worker:latest" \
  --config=/dev/stdin <<'EOF'
steps:
  - name: gcr.io/cloud-builders/docker
    args: ["build", "-f", "Dockerfile.worker", "-t", "$_IMAGE", "."]
images: ["$_IMAGE"]
EOF
# (or simply: docker build -f Dockerfile.worker -t "${REGION}-docker.pkg.dev/${PROJECT_ID}/quipu/worker:latest" . && docker push ...)

gcloud run worker-pools deploy quipu-worker \
  --image="${REGION}-docker.pkg.dev/${PROJECT_ID}/quipu/worker:latest" \
  --region="$REGION" \
  --service-account="quipu-worker-sa@${PROJECT_ID}.iam.gserviceaccount.com" \
  --set-env-vars="GCP_PROJECT_ID=${PROJECT_ID},GOOGLE_GENAI_USE_VERTEXAI=true,PUBSUB_SIGNAL_SUBSCRIPTION=quipu-signals-sub" \
  --min-instances=1 \
  --max-instances=1
```

The worker image is built from `Dockerfile.worker` (repo root, added
2026-08-30) — same `requirements.txt` as the API image, no UI build
stage, entrypoint `CMD ["python", "-m", "app.eventing.worker_main"]`. It
is independent of the root `Dockerfile` (API+UI); building one never
touches the other. Not yet built or pushed as of this document.

**B. Cloud Run Service with CPU always allocated** (fallback if worker
pools aren't available): requires the **minimum additive adapter** this
audit identified but did not implement — a trivial `/healthz` HTTP
handler alongside the existing `asyncio` loop (e.g. a second coroutine
running a tiny `http.server`/Starlette app bound to `$PORT`), so Cloud
Run's own health checking succeeds. No change to
`SignalConsumerWorker`/`SignalIngestionService`/`DetectionProcessor`
business logic would be needed — only `worker_main.py`'s process
entrypoint gains a few lines. Deploy with `--no-cpu-throttling
--min-instances=1 --max-instances=1` so the container keeps running
between Pub/Sub pulls instead of being throttled/scaled to zero.

**Why not rewrite as push delivery?** Pub/Sub push (an HTTP endpoint the
subscription posts to) would fit a request-driven Cloud Run *Service*
more naturally and would scale to zero — but it changes the ack/nack
model (`SignalIngestionService`'s explicit `message.ack()`/`nack()` calls
would need to become an HTTP response-status contract instead) and adds a
new authenticated ingress endpoint to the Control Plane API surface. That
is a real, valid future option but is a genuine redesign of the
transport boundary, not a "minimum additive adapter" — correctly out of
scope for this audit per its own "do not rewrite blindly" instruction.

## 11. Environment variables reference

See `.env.example` (repository root) for the complete, commented list.
The variables that matter specifically for a GCP deployment (beyond local
defaults):

| Variable | Required for | Notes |
|---|---|---|
| `GCP_PROJECT_ID` | everything | reused across every Google integration |
| `GOOGLE_GENAI_USE_VERTEXAI=true` | Gemini via ADK | §4 — not a Quipu `Settings` field, an SDK-level env var |
| `PUBSUB_SIGNAL_TOPIC` / `PUBSUB_SIGNAL_SUBSCRIPTION` | ingestion + worker | §6 |
| `CLOUD_RUN_IMAGE_REGISTRY` | `DeploymentAgent` | §9 |
| `CLOUD_RUN_ALLOWED_REGIONS` / `CLOUD_RUN_ALLOWED_ENVIRONMENTS` | `DeploymentAgent`/`MonitoringAgent` | scope allow-lists |
| `JIRA_BASE_URL` / `JIRA_EMAIL` / `JIRA_API_TOKEN` / `JIRA_PROJECT_KEY` | `PlanningAgent`, `FeatureReviewService` | optional — see below |
| `API_SERVE_UI=true` | serving the UI from the same Cloud Run service | §9 of `docs/architecture/control_plane_ui.md` |
| `API_CORS_ALLOW_ORIGINS` | a separately-hosted UI | leave `[]` when `API_SERVE_UI=true` (same-origin) |

**What still works without Jira configured**: everything except the
literal Jira ticket creation call. `PlanningAgent`/`FeatureReviewService`
will surface a real, typed failure (`TicketCreationFailedError` → HTTP
503) rather than silently faking a ticket — this is by design (evidence-
first: no fabricated ticket ID). For a demo without a real Jira site, this
means the Feature Review "Approve" command will fail at the ticket-
creation step; document this explicitly if demoing without Jira.

## 12. Verification steps

After deploying, confirm (see submission readiness §C for what this audit
itself could and could not verify locally):

1. `curl https://<api-url>/health` → `{"status": "ok"}`
2. `curl https://<api-url>/` → the UI's `index.html` (if `API_SERVE_UI=true`)
3. `curl https://<api-url>/ready` → `{"status": "ready"}` (touches Firestore)
4. Publish one message to `quipu-signals` (`gcloud pubsub topics publish
   quipu-signals --message='{...EventEnvelope JSON...}'`) and confirm a
   Signal appears (`curl https://<api-url>/signals`)
5. Confirm a `DetectionResult` appears after enough related signals
   accumulate (`curl https://<api-url>/detections`) — this exercises the
   real Gemini/Vertex AI call end-to-end
6. Approve a pending feature review through the UI or
   `POST /feature-reviews/{id}/approve` and confirm a workflow starts
7. Step a workflow (`POST /workflows/{id}/step`) through to a real
   `deploy_cloud_run` call and confirm a new Cloud Run revision appears in
   the Cloud Run console — this is **Quipu deploying a target
   application**, not Quipu's own service (§14)
8. Confirm `GET /verifications` returns a record after a remediation
   deployment, and that its `outcome` is never `verified_resolved` before
   fresh post-deployment Signals exist

## 13. Local fallback (no GCP required)

The entire application runs credential-free locally:

```bash
uvicorn app.main:app --reload   # in-memory repositories, no GCP project configured
cd ui && npm run dev
```

`app/api/container.py::build_default_container()` picks the in-memory
container automatically whenever `GCP_PROJECT_ID` is unset — this is the
same path the full `pytest` suite and the Vitest suite exercise, so
"works locally" and "works in CI" are the same code path minus the
Firestore/Pub/Sub/Vertex AI backends.

## 14. Cleanup

```bash
gcloud run services delete quipu-api --region="$REGION"
gcloud run worker-pools delete quipu-worker --region="$REGION"   # if deployed
gcloud pubsub subscriptions delete quipu-signals-sub
gcloud pubsub topics delete quipu-signals quipu-signals-dlq
gcloud iam service-accounts delete quipu-api-sa@${PROJECT_ID}.iam.gserviceaccount.com
gcloud iam service-accounts delete quipu-worker-sa@${PROJECT_ID}.iam.gserviceaccount.com
# Firestore has no "undeploy" — delete individual collections/documents,
# or the whole database, via the console if this was a throwaway project.
```

Never commit `.env` or any downloaded credential file — `.env` is already
git-ignored in this repository (verified during this audit); no service
account JSON key should ever be created for this project (ADC only, per
§8).
