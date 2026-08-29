# Quipu Control Plane UI

## Diagram

```
Operator's browser
        │
        ▼
  Quipu Control Plane UI (ui/, React + TypeScript + Vite)
        │  fetch() — the ONLY thing this app talks to
        ▼
  Quipu Control Plane API (app/api/, FastAPI)
        │
        ▼
  Existing services / repositories → Firestore / Google Cloud
```

## 1. UI architecture

`ui/` is an isolated React + TypeScript + Vite application — a small,
hand-rolled component system (no heavy UI kit) rather than a large
dependency stack. Only two runtime dependencies beyond React itself:
`react-router-dom` for client-side routing. No state-management library —
every page owns its own data via one shared hook (`useApiData`).

```
ui/src/
  api/
    client.ts     the ONLY module allowed to call fetch() against the API
    types.ts       TypeScript mirrors of app/api/schemas/*.py — never a
                    field this app invents
  lib/
    useApiData.ts  loading/error/data + optional visibility-aware polling
    format.ts      date/time/id formatting helpers
  components/      StatusBadge, DomainBadge, StageTimeline, CommandButton,
                    DataView/LoadingState/ErrorState/EmptyState, Layout
  pages/            one file per route (see §3)
```

## 2. API dependency

The UI has **zero** direct access to Firestore, repositories, or any
Google service — it consumes the Control Plane API exclusively, through
`ui/src/api/client.ts`. Every endpoint that module calls already exists in
`app/api/routes/` (see `docs/architecture/control_plane_api.md`) — no
endpoint was invented for this task. The one additive backend change this
task required is documented in §11.

## 3. Page / route inventory

| Route | Page | Purpose |
|---|---|---|
| `/` | Overview | Command center — live workflow timeline + recent signals/detections/reviews/incidents/verifications; demo-only "Load Feature Scenario"/"Load Incident Scenario" |
| `/workflows` | Workflows | All workflows, linking into detail |
| `/workflows/:workflowId` | Workflow Detail | Stage progress, artifact lineage, executions, decisions, "Run Next Step" and "Run Workflow" |
| `/signals` | Signal Explorer | Filterable list + safe provenance detail pane |
| `/detections` | Detection Center | Product-opportunity vs. incident, filterable by domain |
| `/feature-reviews` | Feature Review Queue | AI detected → human reviews → approve/reject |
| `/resolutions` | Incidents | All diagnosed incidents, linking into the console |
| `/resolutions/:resolutionId` | Incident / Resolution Console | Diagnosis, remediation timeline, "Authorize Remediation", verification outcome |
| `/verifications` | Verification | Before/after evidence per verification record |

## 4. API mapping

| Page | Endpoints used |
|---|---|
| Overview | `GET /workflows`, `/signals`, `/detections`, `/feature-reviews`, `/resolutions`, `/verifications` (all bounded); `POST /demo/scenarios/{scenario}` (demo-only, see §8) |
| Workflows | `GET /workflows` |
| Workflow Detail | `GET /workflows/{id}`, `/artifacts`, `/executions`, `/decisions`; `POST /workflows/{id}/step`, `POST /workflows/{id}/run` |
| Signal Explorer | `GET /signals` (filtered), `GET /signals/{id}` |
| Detection Center | `GET /detections` (filtered by domain) |
| Feature Review Queue | `GET /feature-reviews`, `GET /detections/{id}`; `POST /feature-reviews/{id}/approve`\|`reject` |
| Incidents / Console | `GET /resolutions`, `GET /resolutions/{id}`; `POST /resolutions/{id}/remediate`; `GET /workflows/{id}` (for `latest_verification_id`); `GET /verifications/{id}` |
| Verification | `GET /verifications` (filtered by outcome) |

No page queries a resolution's verification by a `resolution_id` filter —
that parameter doesn't exist on `GET /verifications` (see
`docs/architecture/control_plane_api.md` §7). Instead the Incident
Console follows the existing chain the API already exposes:
`resolution.workflow_id` → `workflow.latest_verification_id` →
`GET /verifications/{id}` — three calls through existing endpoints, no
new query parameter needed.

## 5. Authentication boundary

Honest statement, matching `app/api/auth.py`: the "reviewer identity"
control in the header (`ReviewerIdentityControl`) sets an
`X-Quipu-Reviewer-Id` request header, sent only on the approve/reject
commands. **This is attribution, not authentication.** It is stored in
`localStorage` as a plain string the operator can edit freely; it grants
no privilege by itself (the server fixes the actual capability grant and
`reviewer_type=HUMAN` unconditionally — see
`docs/architecture/control_plane_api.md` §5). There is no login flow,
token, or session in this UI. A production deployment would replace both
`app/api/auth.py::require_reviewer_identity` and this header with real
authentication (e.g. a Cloud Run/IAM-fronted identity token) — the UI's
`client.ts` would then send that instead, but no other code would need to
change, since it's already isolated to one function on each side.

## 6. Security model

- No Google credential of any kind (Firestore, Gemini, Pub/Sub, Cloud Run,
  service-account key) exists anywhere in this app — the browser talks
  only to the Control Plane API (`VITE_API_BASE_URL`).
- `ui/src/api/client.ts` is the **only** module allowed to call `fetch`
  against the API — no component issues a raw request.
- No client-side role/privilege flag is ever sent (`{"is_admin": true}` or
  equivalent) — verified directly by `client.test.ts`.
- No dangerous route exists in the UI's router (`App.test.tsx` asserts
  `/tools/execute`, `/shell`, `/deploy`, `/agents/run`, `/admin` all fall
  back to Overview) — mirroring Invariant 6 of
  `docs/architecture/control_plane_api.md`.
- Commands (`CommandButton`) never let the operator supply a target agent,
  shell command, deployment image, or remediation strategy — every
  command sends only the resource id the corresponding API endpoint
  requires.
- `SignalDetail`'s evidence is rendered exactly as the API returns it
  (already sanitized server-side, per
  `docs/architecture/control_plane_api.md` §8) — the UI adds no further
  filtering, but also adds no new exposure.

## 7. Polling

`useApiData(fetcher, deps, pollIntervalMs)` — polling is **opt-in per
call site**, not global:

| Page | Interval |
|---|---|
| Overview | 12s (each panel) |
| Workflows list | 15s |
| Workflow Detail | 8s |
| Signals / Detections / Resolutions / Feature Reviews lists | 10–15s |
| Verifications | 10s |
| Signal/detection detail panes | no polling (fetched once per selection) |

Polling pauses automatically when the browser tab is hidden
(`document.visibilityState`, checked on every tick) so an inactive tab
never keeps hitting the API — verified by `useApiData.test.tsx`. No
WebSocket or second event transport was added; the Control Plane API is
purely request/response.

## 8. Demo mode

No second fake backend and no demo logic duplicated in TypeScript, per
the task's own constraint. The UI is a pure consumer of whatever the
Control Plane API returns — when the API is backed by
`app.demo.harness.DemoHarness`'s data (or any other seeded
`ApiContainer`), the UI renders it exactly the same way it would render
real production data. No backend change was needed to support this: the
existing `POST /feature-reviews/{id}/approve`\|`reject` and
`POST /workflows/{id}/step`\|`POST /resolutions/{id}/remediate` commands
are sufficient to drive the same story `DemoHarness.run_feature_flow()`/
`run_incident_flow()` already exercise end-to-end in Python — a presenter
can seed a container with that harness's data (or its own) and walk the
UI through Overview → Feature Review → Workflow Detail → Incident Console
→ Verification without any UI-specific fixture.

Overview now also renders two `CommandButton`s — "Load Feature Scenario"
and "Load Incident Scenario" — that call `api.runDemoScenario("feature" |
"incident")`, i.e. `POST /demo/scenarios/{scenario}`
(`docs/architecture/control_plane_api.md` §13). This replaces the need to
seed a container out-of-band before a demo: with
`Settings.demo_endpoints_enabled=True` on the backend, a presenter can
click one button in the running UI and immediately see the seeded
signals/detection/review/workflow (feature) or
incident/resolution/remediation/verification (incident) appear through
the same polling the rest of Overview already does. When the flag is
`False` (the default, and expected in any real deployment), the route
404s and the button surfaces that as an ordinary command error — the UI
does not hide or special-case the disabled state, it just reflects
whatever the backend allows, per the "UI never decides orchestration or
security policy" rule in §6.

Workflow Detail's "Run Workflow" button
(`api.runWorkflow` → `POST /workflows/{id}/run`) drives the same demo
story to completion in one click instead of one step at a time. "Run Next
Step" is deliberately kept alongside it, not replaced — stepping through
one stage at a time is what makes the autonomous orchestration visible
during a demo; "Run Workflow" is for skipping ahead once that point has
been made once.

## 9. Local development

See `ui/README.md` for the full quick-start. Summary: run the API
(`uvicorn app.main:app --reload`) with `API_CORS_ALLOW_ORIGINS` including
`http://localhost:5173`, then `npm install && npm run dev` in `ui/`.
`VITE_API_BASE_URL` (default `http://localhost:8000` in `ui/.env.example`)
points the UI at the API.

## 10. Cloud Run deployment

**Option A (chosen): same Cloud Run service serves both API and UI** —
the simplest reproducible deployment for this project (§18 of the task).
The repository's root `Dockerfile` is a two-stage build: stage 1 runs
`npm run build` (producing `ui/dist`, a static bundle); stage 2 is the
existing Python API image, with `ui/dist` copied in and
`API_SERVE_UI=true` set. `app/api/app.py::create_app` mounts
`StaticFiles(directory=ui/dist, html=True)` at `/`, **after** every API
route is registered, only when `Settings.api_serve_ui` is explicitly true
— so an unmatched path either falls through to the SPA's `index.html`
(client-side routing) in production, or 404s exactly as before in every
environment that doesn't set this flag (including the entire `pytest`
suite, which never builds or ships `ui/dist`). This flag is explicit,
not "detect `ui/dist` on disk" — the test suite must never behave
differently because a developer happened to run `npm run build` locally
(this was caught and fixed during this task — see git history).

No second Cloud Run service, no nginx/reverse-proxy container, no new
compute platform.

## 11. Additive backend change required

One, and it's the smallest possible: `app/api/app.py` gained the
`Settings.api_serve_ui`-gated static-file mount described in §10. No
existing route, schema, or service behavior changed. This is the only
backend change this task made.

## 12. Limitations

- No end-to-end browser test (Playwright/Cypress) — the frontend test
  suite is component/integration-level (Vitest + Testing Library) plus
  the existing Python test suite proving the API contract; adding a full
  E2E harness was judged out of scope for "do not create a massive
  frontend test suite."
- No cursor pagination in any list view — bounded by the API's own
  `limit`/`api_max_page_size` (see
  `docs/architecture/control_plane_api.md` §7); a workflow/signal/
  detection population larger than one page is not yet browsable past the
  first page from the UI.
- The reviewer identity control has no validation beyond non-empty — a
  typo'd name is indistinguishable from a real reviewer in development
  mode (§5's honest limitation, inherited from the API itself).
- The Incident Console shows only the **latest** verification for a
  workflow (via `workflow.latest_verification_id`) — a full verification
  history per resolution isn't surfaced, since the API has no
  `resolution_id` filter on `GET /verifications` (see §4).
- No offline/retry queue for failed commands — a failed command must be
  retried manually via the shown "Try again" control.
