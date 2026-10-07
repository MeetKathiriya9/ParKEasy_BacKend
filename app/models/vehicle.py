"""The `vehicles` collection (DOC section 18).

Key fields straight from the requirements document: `userId`,
`registrationNumber`, `type`, `model`, `fuelType`, `isEV`. `isDefault` comes
from DOC section 10 ("default vehicle") and the `/api/vehicles` row of section
21 ("CRUD vehicles, default vehicle").

`isEV` is never accepted from a client: it is derived from `fuelType` by
`is_electric`, so the two cannot disagree. That keeps the DOC's EV flag present
while leaving a single source of truth for what counts as electric.

Registration numbers are normalised once, by `normalize_registration`, so the
unique index on `(userId, registrationNumber)` compares like with like and
`"gj 05 ab 1234"` collides with `"GJ05AB1234"`.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any, TypedDict

from bson import ObjectId

from app.models.enums import FuelType, VehicleType

VEHICLES_COLLECTION = "vehicles"

MIN_REGISTRATION_LENGTH = 2
MAX_REGISTRATION_LENGTH = 20
MIN_MODEL_LENGTH = 2
MAX_MODEL_LENGTH = 60

# After normalisation a registration number is uppercase letters and digits
# only. Everything else (`-`, `.`, spaces) is dropped so `ABC-1234`, `ABC 1234`
# and `abc1234` are the same key - three ways of typing one plate must not
# become three vehicles.
REGISTRATION_PATTERN = r"^[A-Z0-9]{2,20}$"
REGISTRATION_REQUIRED_ALNUM = 2


def normalize_registration(value: str) -> str:
    """Canonical form of a plate: uppercase letters and digits only.

    Uppercasing means `abc1234` and `ABC1234` are the same vehicle, and dropping
    separators means `GJ 05 AB 1234`, `GJ-05-AB-1234` and `GJ05AB1234` are too.
    All three spellings are in common use for the same plate, and a duplicate
    check that missed one would let a user register the same vehicle twice.
    """
    return re.sub(r"[^A-Za-z0-9]", "", value).upper()


def registration_is_valid(value: str) -> bool:
    """True when a *normalised* registration number is usable."""
    if not MIN_REGISTRATION_LENGTH <= len(value) <= MAX_REGISTRATION_LENGTH:
        return False
    if not re.fullmatch(REGISTRATION_PATTERN, value):
        return False
    return len(value) >= REGISTRATION_REQUIRED_ALNUM


def is_electric(fuel_type: FuelType) -> bool:
    """The DOC's `isEV` flag, derived so it can never contradict `fuelType`."""
    return fuel_type == FuelType.ELECTRIC


class VehicleDocument(TypedDict):
    """A row of the `vehicles` collection.

    `_id` is an `ObjectId`, like every other collection, and `userId` points at
    the owning `users` document - the same reference shape the reservations and
    violations collections already index on.
    """

    _id: ObjectId
    userId: ObjectId
    registrationNumber: str
    type: VehicleType
    model: str
    fuelType: FuelType
    isEV: bool
    isDefault: bool
    createdAt: datetime
    updatedAt: datetime


def _as_utc(value: datetime | None) -> datetime | None:
    """Label a stored timestamp as UTC.

    PyMongo hands back naive datetimes by default, and a naive value serialises
    without an offset (`2026-10-07T17:03:30.379`) while an aware one uses `Z`.
    Normalising here keeps `createdAt` identical whichever endpoint returned it.
    """
    if value is None or isinstance(value, str):
        return value  # type: ignore[return-value]
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def public_vehicle(document: dict[str, Any]) -> dict[str, Any]:
    """Project a vehicle document down to the fields safe to send to a client.

    Mirrors `public_user`: ids are stringified so the client never sees an
    `ObjectId`, and `_id` never escapes under its own name.
    """
    return {
        "id": str(document["_id"]),
        "userId": str(document["userId"]),
        "registrationNumber": document.get("registrationNumber"),
        "type": document.get("type"),
        "model": document.get("model"),
        "fuelType": document.get("fuelType"),
        "isEV": document.get("isEV"),
        "isDefault": bool(document.get("isDefault", False)),
        "createdAt": _as_utc(document.get("createdAt")),
    }


__all__ = [
    "MAX_MODEL_LENGTH",
    "MAX_REGISTRATION_LENGTH",
    "MIN_MODEL_LENGTH",
    "MIN_REGISTRATION_LENGTH",
    "VEHICLES_COLLECTION",
    "VehicleDocument",
    "is_electric",
    "normalize_registration",
    "public_vehicle",
    "registration_is_valid",
]
