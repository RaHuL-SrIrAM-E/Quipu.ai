# Production image for the Quipu Control Plane API + UI (see
# docs/architecture/control_plane_ui.md "Cloud Run deployment"). Serves
# ONLY app.main:app (the FastAPI control plane, with the built UI mounted
# as static files when API_SERVE_UI=true — Option A: same Cloud Run
# service serves both, the simplest reproducible deployment for this
# project) — the Pub/Sub Signal Consumer Worker
# (app/eventing/worker_main.py) is a separate long-running process, not
# this HTTP server, and is not started by this image.

# ---- Stage 1: build the UI -------------------------------------------------
FROM node:22-slim AS ui-build
WORKDIR /ui
COPY ui/package.json ui/package-lock.json* ./
RUN npm install
COPY ui .
RUN npm run build

# ---- Stage 2: the API, with the built UI bundled ---------------------------
FROM python:3.13-slim

WORKDIR /app

# git is required at runtime by app/core/repo.py:clone_repo() (shells out to
# the system `git` binary via subprocess — no GitPython, no bundled
# fallback), which OrchestrationService._ensure_workspace() calls before
# Planning/Architecture/Codegen/Testing so those stages have a checked-out
# repository to work in. python:3.13-slim does not ship git by default.
RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY main.py ./main.py
COPY --from=ui-build /ui/dist ./ui/dist

# Cloud Run sets $PORT; default it for local `docker run`. API_SERVE_UI is
# only ever true in this image, which always has ui/dist bundled.
ENV PORT=8080
ENV API_SERVE_UI=true
EXPOSE 8080

# No shell/reload in production — a single uvicorn process; Cloud Run
# itself handles horizontal scaling (multiple container instances), not
# a multi-worker process manager inside one container.
CMD exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT}
