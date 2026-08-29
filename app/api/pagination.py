"""Bounded-limit query parameter — every collection endpoint uses this
instead of a hand-rolled `limit: int` field, so the ceiling is enforced in
exactly one place (Settings.api_max_page_size). No cursor pagination is
implemented: none of the underlying repositories (SignalRepository,
DetectionRepository, ResolutionRepository, RemediationVerificationRepository,
FeatureReviewRepository) expose a cursor today — see
docs/architecture/control_plane_api.md "Pagination/limits" for this
documented limitation.
"""

from fastapi import Query

from app.config import get_settings


def bounded_limit(limit: int | None = Query(default=None, ge=1, description="Maximum number of results to return.")) -> int:
    settings = get_settings()
    if limit is None:
        return settings.api_default_page_size
    return min(limit, settings.api_max_page_size)
