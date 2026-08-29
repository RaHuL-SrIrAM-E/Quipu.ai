# Quipu System Overview (Submission Architecture Diagram)

This is the submission-quality diagram referenced by
`docs/hackathon/submission_readiness.md` §F. Every component shown is
real and exercised by the current codebase/tests — nothing here is
aspirational. See the linked per-component doc for full detail.

```
 Customer / product feedback           Production telemetry
 (feedback tools, support)             (Cloud Monitoring, Cloud Logging)
          │                                     │
          └───────────────┬─────────────────────┘
                           ▼
                    Google Pub/Sub
              (topic + subscription — app/eventing/)
                           │
                           ▼
              SignalConsumerWorker (pull loop)
              app/eventing/worker.py
                           │
                           ▼
              SignalIngestionService
              validate → normalize → sanitize → dedup → persist → ack
              app/eventing/ingestion_service.py, app/signals/adapters.py
                           │
                           ▼
                        Signal
                 (Firestore: signals/{id})
                           │
                           ▼
              DetectionTrigger → DetectionProcessor
              app/detection/  (bounded evidence, minimum-signal gate)
                           │
                           ▼
                    DetectingAgent  ◄── Gemini (Vertex AI) via Google ADK
              app/agents/detecting.py                │
                           │                    Agent Search
                           │              (Enterprise Knowledge grounding,
                           │               optional, in-memory by default)
                 ┌─────────┴─────────┐
                 ▼                   ▼
        FEATURE_OPPORTUNITY      INCIDENT
                 │                   │
                 ▼                   ▼
      FeatureReviewService   IncidentResolutionAgent ◄── Gemini/ADK
      (human approve/reject)   app/agents/incident_resolution.py
                 │                   │
                 ▼                   ▼
            Jira Ticket      ResolutionResult (diagnosis, strategy, risk)
                 │                   │
                 ▼                   ▼
          OrchestrationService.start_workflow_from_review /
          start_remediation_from_resolution
          app/orchestration/service.py — the ONE workflow engine
                           │
                           ▼
     ┌─────────┬───────────┼───────────┬───────────┐
     ▼         ▼           ▼           ▼           ▼
 Planning  Architecture  Codegen    Testing    Deployment
 (Gemini/ADK, per-agent) (Gemini/ADK) (Gemini/ADK, (real pytest, (real Cloud Run
                                       bounded tools) evidence-first) deploy — see below)
                           │
                           ▼
              Cloud Run  ◄── DEPLOYED APPLICATION (not Quipu itself — §B)
                           │
                           ▼
                    MonitoringAgent
              (Cloud Monitoring + Cloud Logging queries,
               real telemetry → Signal, deterministic thresholds)
                           │
                           ▼
                    new Signal(s) ──────────────► (loops back to Pub/Sub/
                                                     DetectionTrigger path)
                           │
                           ▼
              RemediationVerificationService
              app/verification/ — deterministic comparison,
              baseline vs. post-deployment evidence
                           │
                           ▼
        VERIFIED_RESOLVED | STILL_DEGRADED | INSUFFICIENT_EVIDENCE | ESCALATED
                     (never inferred from deployment success alone)


 Everything above is reached through:

        Operator / Judge's browser
                 │
                 ▼
     Quipu Control Plane UI (React + TS, ui/)
                 │  fetch() only
                 ▼
     Quipu Control Plane API (FastAPI, app/api/)
                 │  same-origin static mount when API_SERVE_UI=true
                 ▼
     Cloud Run  ◄── QUIPU'S OWN DEPLOYMENT (§A — distinct from the
                     Cloud Run services Quipu deploys as part of the SDLC, above)
                 │
                 ▼
             Firestore
     (workflows, signals, detections, resolutions,
      remediation_verifications, feature_reviews)
```

## A vs. B: two different Cloud Run usages (task §9)

**A. Quipu's own Control Plane deployment** — one Cloud Run *service*
(`quipu-api`) running `app.main:app` (FastAPI + the built UI), described
in `docs/architecture/control_plane_api.md` and
`docs/architecture/control_plane_ui.md`. This is "where Quipu lives."

**B. Cloud Run services deployed BY Quipu** — an arbitrary number of
*separate* Cloud Run services that `DeploymentAgent`
(`app/agents/deployment.py` → `app/core/cloud_run_client.py`) deploys as
the output of the SDLC pipeline it orchestrates — these are the
applications Quipu is building/fixing on a human's behalf, not Quipu
itself. `MonitoringAgent` observes *these* services' Cloud Monitoring/
Cloud Logging telemetry, never Quipu's own Control Plane API's telemetry.
`Settings.cloud_run_image_registry`/`cloud_run_allowed_regions`/
`cloud_run_allowed_environments` scope exactly what (B) can touch; Quipu's
own deployment (A) is configured entirely separately, by whoever deploys
Quipu itself (see `docs/deployment/gcp.md`).

## Google technologies used, by layer

| Layer | Technology | File(s) |
|---|---|---|
| Agent framework | Google ADK (`LlmAgent`, `SequentialAgent`, `LoopAgent`) | `app/agents/*.py`, `app/orchestration/adk/*.py` |
| LLM | Gemini via Vertex AI (`google-genai`, ADK-mediated) | see `docs/deployment/gcp.md` §4 for the credential-mode caveat |
| Enterprise knowledge | Agent Search / Discovery Engine | `app/knowledge/backends/google_search.py` (real, not yet wired into the API container by default — see `docs/deployment/gcp.md` §7) |
| Event transport | Pub/Sub | `app/eventing/google_pubsub_client.py`, `app/eventing/worker.py` |
| Durable state | Firestore | `app/persistence/firestore/*.py` |
| Target-app deployment | Cloud Run | `app/core/cloud_run_client.py` |
| Production observability | Cloud Monitoring, Cloud Logging | `app/core/cloud_monitoring_client.py`, `app/core/cloud_logging_client.py` |
| Quipu's own deployment | Cloud Run | `Dockerfile`, `docs/deployment/gcp.md` |

Non-Google: Jira (`app/core/jira_client.py`, human-facing ticket tracker
— not a "Google technology" requirement, additive).
