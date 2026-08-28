"""Translates google.api_core exceptions into app.persistence.errors at the
Firestore boundary. Repository code should never let a raw Firestore/
google.api_core exception escape."""

from google.api_core import exceptions as google_exceptions

from app.persistence.errors import EntityNotFoundError, PersistenceError


def translate_firestore_error(exc: Exception, entity_type: str, entity_id: str) -> PersistenceError:
    if isinstance(exc, google_exceptions.NotFound):
        return EntityNotFoundError(entity_type, entity_id)
    return PersistenceError(f"Firestore error on {entity_type} '{entity_id}': {exc}")
