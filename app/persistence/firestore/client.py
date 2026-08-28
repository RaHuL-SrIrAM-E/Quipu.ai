"""Firestore client construction. Application Default Credentials only — no
embedded keys, no custom auth code. Isolated here so no other persistence
module needs to import google.cloud.firestore directly for client setup.
"""

from google.cloud import firestore

from app.config import get_settings


class FirestoreConfigError(Exception):
    pass


def get_firestore_client() -> firestore.AsyncClient:
    settings = get_settings()
    if not settings.gcp_project_id:
        raise FirestoreConfigError("GCP_PROJECT_ID is not set")

    kwargs: dict = {"project": settings.gcp_project_id}
    if settings.firestore_database_id:
        kwargs["database"] = settings.firestore_database_id
    return firestore.AsyncClient(**kwargs)
