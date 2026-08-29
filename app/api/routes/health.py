"""Health/readiness — GET /health is cheap and deterministic (no
dependency call, per the task's own instruction); GET /ready explicitly
distinguishes itself by touching the configured container's repositories
so a caller can tell the two apart."""

from fastapi import APIRouter, Depends

from app.api.container import ApiContainer
from app.api.dependencies import get_container

router = APIRouter(tags=["health"])


@router.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@router.get("/ready")
async def ready(container: ApiContainer = Depends(get_container)) -> dict:
    """Confirms the wired repositories are reachable — for an in-memory
    container this is always true; for a Firestore-backed one, a single
    bounded read against the workflow collection. Never touches
    Gemini/Pub/Sub/Cloud Monitoring — those are not on the API's own
    request path at all."""
    await container.workflow_repo.list_recent(limit=1)
    return {"status": "ready"}
