"""Quipu Control Plane API — the FastAPI application factory. See
docs/architecture/control_plane_api.md for the full design.

`create_app(container=...)` is the single entrypoint both production and
tests use: production (`app = create_app()` below, what
`uvicorn app.api.app:app` and Cloud Run serve) picks a container via
`app.api.container.build_default_container()` (Firestore if
`Settings.gcp_project_id` is set, in-memory otherwise); tests pass an
explicit in-memory `ApiContainer` so nothing here ever touches Google
Cloud during `pytest`.
"""

import time
import uuid
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.api.container import ApiContainer, build_default_container
from app.api.errors import register_exception_handlers
from app.api.routes import api_router
from app.config import get_settings
from app.core.observability import get_logger

logger = get_logger("quipu.api")

# Option A from docs/architecture/control_plane_ui.md "Cloud Run
# deployment": the built UI (ui/dist, produced by `npm run build`) is
# served by this SAME service when Settings.api_serve_ui is explicitly
# enabled, so a single Cloud Run deployment covers both API and UI — the
# simplest reproducible option for this project. Mounted AFTER every API
# route below, so it can only ever serve paths that don't match a real
# API route. Gated by an explicit setting (default False), not by
# "ui/dist happens to exist on disk" — the test suite must never behave
# differently depending on whether someone previously ran `npm run build`
# locally.
_UI_DIST_DIR = Path(__file__).resolve().parents[2] / "ui" / "dist"


def create_app(container: ApiContainer | None = None) -> FastAPI:
    settings = get_settings()

    app = FastAPI(
        title="Quipu Control Plane API",
        description=(
            "A thin control plane over the existing Quipu services (OrchestrationService, "
            "FeatureReviewService) and repositories — see docs/architecture/control_plane_api.md. "
            "This API is not the orchestration engine and does not reason about incidents or "
            "features itself; every endpoint delegates to an existing, unmodified service boundary."
        ),
        version="1.0.0",
    )
    app.state.container = container if container is not None else build_default_container()

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.api_cors_allow_origins,  # empty by default — see app/config.py
        allow_credentials=False,
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type", "X-Quipu-Reviewer-Id", "X-Request-ID"],
    )

    @app.middleware("http")
    async def _correlation_id_and_timing(request: Request, call_next):
        correlation_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
        request.state.correlation_id = correlation_id
        started = time.perf_counter()
        response = await call_next(request)
        duration_ms = (time.perf_counter() - started) * 1000
        response.headers["X-Request-ID"] = correlation_id
        logger.info(
            "api.request correlation_id=%s method=%s path=%s status=%d duration_ms=%.1f",
            correlation_id,
            request.method,
            request.url.path,
            response.status_code,
            duration_ms,
        )
        return response

    register_exception_handlers(app)
    app.include_router(api_router)

    if settings.demo_endpoints_enabled:
        # Registered ONLY when explicitly enabled — when false, these
        # paths don't exist at all (a plain 404 from FastAPI's own
        # routing, not an internal "disabled" check a request could ever
        # reach). See app/api/routes/demo.py.
        from app.api.routes.demo import router as demo_router

        app.include_router(demo_router)
        logger.warning("api.demo_endpoints_enabled — do not use in a real production deployment")

    if settings.api_serve_ui and _UI_DIST_DIR.is_dir():
        app.mount("/", StaticFiles(directory=_UI_DIST_DIR, html=True), name="ui")
        logger.info("api.ui_mounted path=%s", _UI_DIST_DIR)

    return app


app = create_app()
