# Deploying Quipu to Google Cloud

**Status of this document**: written from a static audit of the codebase
in an environment with no `gcloud` CLI and no GCP credentials available
(see `docs/hackathon/submission_readiness.md` §C). Every command below is
believed correct against the current code but has **not** been executed
end-to-end against a live project during this audit. Treat this as a
validated plan, not a verified runbook, until someone with GCP access
walks it once and reports back.

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
  discoveryengine.googleapis.com \
  monitoring.googleapis.com \
  logging.googleapis.com \
  artifactregistry.googleapis.com
```

`aiplatform.googleapis.com` is required because Quipu's ADK agents call
Gemini through **Vertex AI**, not the standalone Gemini Developer API —
see §4's `GOOGLE_GENAI_USE_VERTEXAI` note, which is the single most
important environment variable in this whole deployment.

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
`gcloud firestore indexes composite create` reproducibility. This was not
done in this audit (no live Firestore available).

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
identity only) — see `docs/hackathon/submission_readiness.md` §F for the
full role-by-role justification.

```bash
gcloud iam service-accounts create quipu-api-sa --display-name="Quipu Control Plane API"
gcloud iam service-accounts create quipu-worker-sa --display-name="Quipu Pub/Sub Signal Worker"

for ROLE in roles/datastore.user roles/aiplatform.user roles/discoveryengine.viewer \
            roles/run.developer roles/iam.serviceAccountUser \
            roles/monitoring.viewer roles/logging.viewer roles/logging.logWriter; do
  gcloud projects add-iam-policy-binding "$PROJECT_ID" \
    --member="serviceAccount:quipu-api-sa@${PROJECT_ID}.iam.gserviceaccount.com" \
    --role="$ROLE"
done

for ROLE in roles/pubsub.subscriber roles/datastore.user roles/aiplatform.user \
            roles/discoveryengine.viewer roles/logging.logWriter; do
  gcloud projects add-iam-policy-binding "$PROJECT_ID" \
    --member="serviceAccount:quipu-worker-sa@${PROJECT_ID}.iam.gserviceaccount.com" \
    --role="$ROLE"
done
```

Neither service account is `Owner`/`Editor`. Neither uses a downloaded
JSON key — both are attached directly to their Cloud Run
service/worker-pool identity, and every Google client in the codebase
(`app/core/*_client.py`, `app/persistence/firestore/client.py`,
`app/eventing/google_pubsub_client.py`) already constructs its client
with no explicit credentials argument, which resolves to ADC
automatically.

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
gcloud run worker-pools deploy quipu-worker \
  --image="${REGION}-docker.pkg.dev/${PROJECT_ID}/quipu/worker:latest" \
  --region="$REGION" \
  --service-account="quipu-worker-sa@${PROJECT_ID}.iam.gserviceaccount.com" \
  --set-env-vars="GCP_PROJECT_ID=${PROJECT_ID},GOOGLE_GENAI_USE_VERTEXAI=true,PUBSUB_SIGNAL_SUBSCRIPTION=quipu-signals-sub" \
  --min-instances=1 \
  --max-instances=1
```

(This uses a separate worker image — same `requirements.txt`, entrypoint
`CMD ["python", "-m", "app.eventing.worker_main"]` — not built in this
audit; see "next actions" in submission readiness.)

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
