"""Helpers for converting between MongoDB documents and API payloads.

MongoDB stores `_id` as an `ObjectId`; the client works with string ids
(its mock data uses `"f1"`, `"r1"`, ...). These helpers keep that conversion in
one place so no route handler has to remember it.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from bson import ObjectId
from bson.errors import InvalidId


def utcnow() -> datetime:
    """Timezone-aware current UTC time."""
    return datetime.now(UTC)


def new_id() -> str:
    """A fresh string id for a new document."""
    return str(ObjectId())


def to_object_id(value: str | ObjectId) -> ObjectId | None:
    """Best-effort conversion to ObjectId; `None` when the string is not one.

    Returning `None` (rather than raising) lets a route treat "malformed id"
    and "not found" as the same 404, which avoids leaking id format details.
    """
    if isinstance(value, ObjectId):
        return value
    try:
        return ObjectId(value)
    except (InvalidId, TypeError):
        return None


def as_utc(value: datetime | None) -> datetime | None:
    """Label a stored timestamp as UTC.

    PyMongo hands back naive datetimes by default, and a naive value serialises
    without an offset (`2026-10-07T17:03:30.379`) while an aware one uses `Z`.
    Normalising here keeps `createdAt` identical whichever endpoint returned it.
    """
    if value is None or isinstance(value, str):
        return value  # type: ignore[return-value]
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def serialize(document: Any) -> Any:
    """Recursively make a Mongo document JSON-safe.

    - `_id` becomes the string `id`
    - `ObjectId` values become strings
    - `datetime` becomes an ISO-8601 string (UTC, `Z` suffix)
    """
    if document is None:
        return None
    if isinstance(document, list):
        return [serialize(item) for item in document]
    if isinstance(document, dict):
        out: dict[str, Any] = {}
        for key, value in document.items():
            if key == "_id":
                out["id"] = str(value)
            else:
                out[key] = serialize(value)
        return out
    if isinstance(document, ObjectId):
        return str(document)
    if isinstance(document, datetime):
        if document.tzinfo is None:
            document = document.replace(tzinfo=UTC)
        return document.astimezone(UTC).isoformat().replace("+00:00", "Z")
    return document
