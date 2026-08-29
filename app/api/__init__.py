"""Quipu Control Plane API — a thin HTTP layer over the existing
OrchestrationService/FeatureReviewService and repositories. See
docs/architecture/control_plane_api.md.
"""

from app.api.app import create_app

__all__ = ["create_app"]
