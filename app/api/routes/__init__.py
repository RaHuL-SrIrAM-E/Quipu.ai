"""Route registration — one APIRouter per resource group, combined here
into a single router app/api/app.py includes."""

from fastapi import APIRouter

from app.api.routes import detections, feature_reviews, health, resolutions, signals, verifications, workflows

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(workflows.router)
api_router.include_router(signals.router)
api_router.include_router(detections.router)
api_router.include_router(resolutions.router)
api_router.include_router(verifications.router)
api_router.include_router(feature_reviews.router)

__all__ = ["api_router"]
