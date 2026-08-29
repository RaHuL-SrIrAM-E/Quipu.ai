# Quipu Control Plane UI

A React + TypeScript + Vite frontend for the Quipu Control Plane API
(`app/api/`). See `docs/architecture/control_plane_ui.md` at the
repository root for the full architecture, security model, and Cloud Run
deployment details.

## Local development

1. Start the backend API (from the repository root):

   ```bash
   uvicorn app.main:app --reload --port 8000
   ```

   By default the API grants no CORS origins. For local UI development,
   set in your `.env` (repository root):

   ```
   API_CORS_ALLOW_ORIGINS=["http://localhost:5173"]
   ```

2. In this directory, install dependencies and start the dev server:

   ```bash
   npm install
   cp .env.example .env   # VITE_API_BASE_URL=http://localhost:8000
   npm run dev
   ```

3. Open <http://localhost:5173>. Set a reviewer identity (top-right of
   the header) before approving/rejecting a feature review — this is a
   development-mode attribution header, not a login; see
   `docs/architecture/control_plane_ui.md` "Authentication boundary".

## Scripts

- `npm run dev` — Vite dev server with hot reload.
- `npm run build` — type-check (`tsc -b`) and produce `dist/`.
- `npm run test` — run the Vitest suite once.
- `npm run preview` — serve the production build locally.

## Production build

`npm run build` produces `dist/`, a static bundle. The repository's root
`Dockerfile` builds this automatically and serves it from the same
FastAPI service as the API when `API_SERVE_UI=true` — see
`docs/architecture/control_plane_ui.md` "Cloud Run deployment".
