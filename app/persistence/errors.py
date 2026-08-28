"""Structured persistence errors. Repository implementations (memory,
Firestore) translate their own backend-specific failures into these at the
boundary — higher-level Quipu code never needs to catch a raw Firestore or
SQLAlchemy exception.
"""


class PersistenceError(Exception):
    """Base class for all persistence-layer errors."""


class EntityNotFoundError(PersistenceError):
    def __init__(self, entity_type: str, entity_id: str):
        self.entity_type = entity_type
        self.entity_id = entity_id
        super().__init__(f"{entity_type} '{entity_id}' not found")


class DuplicateEntityError(PersistenceError):
    def __init__(self, entity_type: str, entity_id: str):
        self.entity_type = entity_type
        self.entity_id = entity_id
        super().__init__(f"{entity_type} '{entity_id}' already exists")


class VersionConflictError(PersistenceError):
    """The write's expected_version no longer matches what's stored — someone
    else updated this entity first. The caller must re-read and retry, not
    blindly overwrite."""

    def __init__(self, entity_id: str, expected_version: int, actual_version: int | None):
        self.entity_id = entity_id
        self.expected_version = expected_version
        self.actual_version = actual_version
        super().__init__(
            f"version conflict on '{entity_id}': expected version {expected_version}, "
            f"but stored version is {actual_version}"
        )
