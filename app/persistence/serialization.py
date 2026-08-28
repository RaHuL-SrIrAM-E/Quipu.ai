"""Conversion boundary between Pydantic domain models and plain,
Firestore-safe dicts. Domain models never import Firestore types; this
module is the only place that knows both sides.

Handles: enums (-> .value), nested BaseModels/dicts/lists (recursively), and
datetimes (coerced timezone-aware). Some domain models default datetime
fields with naive datetime.utcnow() (Level 1.1, unchanged) — this boundary
fixes that up defensively rather than modifying the domain models just to
make Firestore serialization easier.
"""

from datetime import datetime, timezone
from enum import Enum
from typing import Any, TypeVar

from pydantic import BaseModel

ModelT = TypeVar("ModelT", bound=BaseModel)


def _normalize(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return _normalize(value.model_dump(mode="python"))
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {key: _normalize(val) for key, val in value.items()}
    if isinstance(value, list):
        return [_normalize(item) for item in value]
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    return value


def to_firestore_dict(model: BaseModel) -> dict[str, Any]:
    """Domain model -> a plain dict safe to hand to the Firestore client."""
    return _normalize(model.model_dump(mode="python"))


def from_firestore_dict(model_cls: type[ModelT], data: dict[str, Any]) -> ModelT:
    """Firestore document dict -> a validated domain model instance."""
    return model_cls.model_validate(data)
