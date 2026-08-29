"""ASGI entrypoint — `uvicorn app.main:app` and Cloud Run both serve this.
Re-exports the real application factory result from app.api.app; see
docs/architecture/control_plane_api.md "Cloud Run deployment".
"""

from app.api.app import app

__all__ = ["app"]
