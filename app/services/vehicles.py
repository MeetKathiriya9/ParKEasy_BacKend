"""Vehicle CRUD and default-vehicle management (DOC sections 10/18/21).

Services own every database *write*, matching `app/services/profile.py`. Routes
in `app/api/v1/vehicles.py` stay thin: validate, delegate here, serialise.

Two invariants are worth spelling out:

* **At most one default per user.** Enforced in the database by a partial
  unique index on `userId` where `isDefault: true` (see `app.db.indexes`), not
  only in this module, so no ordering mistake in a future edit can leave a user
  with two defaults. Because such an index rejects a second default outright,
  every switch is written "clear the old ones, then set the new one"; a crash
  in between leaves *zero* defaults, which `list_vehicles` repairs by promoting
  the oldest vehicle.
* **A default always exists while the user has vehicles.** The first created
  vehicle becomes the default, deleting the default promotes the oldest
  remaining one, and nothing else can clear the flag.

Everything is scoped to `userId`, and an id belonging to somebody else is a
`404` rather than a `403`: a `403` would confirm that the id exists.
"""

from __future__ import annotations

import logging
from typing import Any

from bson import ObjectId
from pymongo import ReturnDocument
from pymongo.asynchronous.database import AsyncDatabase
from pymongo.errors import DuplicateKeyError

from app.core.config import Settings
from app.core.errors import ConflictError, NotFoundError
from app.models.vehicle import (
    VEHICLES_COLLECTION,
    is_electric,
)
from app.schemas.vehicle import CreateVehicleRequest, UpdateVehicleRequest
from app.utils.mongo import to_object_id, utcnow

logger = logging.getLogger(__name__)


class VehicleErrorCode:
    """Conflict reasons carried in `error.code`, so a client can branch on it."""

    REGISTRATION_DUPLICATE = "REGISTRATION_DUPLICATE"
    VEHICLE_LIMIT_REACHED = "VEHICLE_LIMIT_REACHED"


def _user_object_id(user_id: str) -> ObjectId:
    object_id = to_object_id(user_id)
    if object_id is None:
        raise NotFoundError("This account no longer exists")
    return object_id


async def _owned_vehicle(
    db: AsyncDatabase, *, user_id: str, vehicle_id: str
) -> dict[str, Any]:
    """Load a vehicle the caller owns, or raise a 404.

    A malformed id and a foreign id are deliberately indistinguishable: both
    answer with the same message, so the endpoint cannot be used to probe for
    other users' vehicle ids.
    """
    object_id = to_object_id(vehicle_id)
    if object_id is None:
        raise NotFoundError("This vehicle does not exist")

    document = await db[VEHICLES_COLLECTION].find_one(
        {"_id": object_id, "userId": _user_object_id(user_id)}
    )
    if document is None:
        raise NotFoundError("This vehicle does not exist")
    return document


async def _clear_defaults(
    db: AsyncDatabase, *, user_id: ObjectId, except_id: ObjectId | None = None
) -> None:
    """Drop the default flag from this user's vehicles (optionally keeping one)."""
    query: dict[str, Any] = {"userId": user_id, "isDefault": True}
    if except_id is not None:
        query["_id"] = {"$ne": except_id}
    await db[VEHICLES_COLLECTION].update_many(
        query, {"$set": {"isDefault": False, "updatedAt": utcnow()}}
    )


async def _promote_default(
    db: AsyncDatabase, *, user_id: ObjectId, vehicle_id: ObjectId
) -> None:
    """Make `vehicle_id` the user's only default vehicle.

    Clear-then-set is required by the partial unique index: setting first would
    collide with the existing default.
    """
    await _clear_defaults(db, user_id=user_id, except_id=vehicle_id)
    await db[VEHICLES_COLLECTION].update_one(
        {"_id": vehicle_id}, {"$set": {"isDefault": True, "updatedAt": utcnow()}}
    )


def _duplicate_registration(registration: str) -> ConflictError:
    return ConflictError(
        "You already have a vehicle with this registration number",
        code=VehicleErrorCode.REGISTRATION_DUPLICATE,
        details={"field": "registrationNumber", "value": registration},
    )


async def list_vehicles(
    db: AsyncDatabase, *, user_id: str
) -> list[dict[str, Any]]:
    """The caller's vehicles, oldest first, with a missing default repaired."""
    user_oid = _user_object_id(user_id)
    documents = await (
        db[VEHICLES_COLLECTION].find({"userId": user_oid}).sort("createdAt", 1).to_list()
    )

    if documents and not any(document.get("isDefault") for document in documents):
        # A crash between "clear old default" and "set new default" leaves the
        # user with vehicles and no default. Promote the oldest so the client
        # always has one to preselect.
        await _promote_default(db, user_id=user_oid, vehicle_id=documents[0]["_id"])
        documents[0]["isDefault"] = True
        logger.info("Repaired missing default vehicle for user %s", user_id)

    return documents


async def create_vehicle(
    db: AsyncDatabase,
    *,
    user_id: str,
    payload: CreateVehicleRequest,
    settings: Settings,
) -> dict[str, Any]:
    """Register a vehicle. The first one becomes the default automatically."""
    user_oid = _user_object_id(user_id)

    existing = await db[VEHICLES_COLLECTION].count_documents({"userId": user_oid})
    if existing >= settings.vehicle_max_per_user:
        raise ConflictError(
            f"You can register at most {settings.vehicle_max_per_user} vehicles",
            code=VehicleErrorCode.VEHICLE_LIMIT_REACHED,
            details={"limit": settings.vehicle_max_per_user},
        )

    now = utcnow()
    document: dict[str, Any] = {
        "_id": ObjectId(),
        "userId": user_oid,
        "registrationNumber": payload.registrationNumber,
        "type": payload.type.value,
        "model": payload.model,
        "fuelType": payload.fuelType.value,
        "isEV": is_electric(payload.fuelType),
        "isDefault": False,
        "createdAt": now,
        "updatedAt": now,
    }

    # Inserted as a non-default first so that a duplicate registration can only
    # ever fail here, before anything else has been changed.
    try:
        await db[VEHICLES_COLLECTION].insert_one(document)
    except DuplicateKeyError as exc:
        raise _duplicate_registration(payload.registrationNumber) from exc

    if existing == 0:
        await _promote_default(db, user_id=user_oid, vehicle_id=document["_id"])

    # Read back rather than returning the in-memory document: MongoDB stores
    # milliseconds, not microseconds, so the object built above would otherwise
    # carry a `createdAt` that no later read of the same vehicle ever returns.
    stored = await db[VEHICLES_COLLECTION].find_one(
        {"_id": document["_id"], "userId": user_oid}
    )
    document = stored or document

    logger.info(
        "Created vehicle %s for user %s (%s)",
        document["_id"],
        user_id,
        payload.registrationNumber,
    )
    return document


async def update_vehicle(
    db: AsyncDatabase,
    *,
    user_id: str,
    vehicle_id: str,
    payload: UpdateVehicleRequest,
) -> tuple[dict[str, Any], bool]:
    """Apply a `PATCH` and return `(document, changed)`.

    `changed` is False when nothing was supplied, so the route can skip the
    audit row and the `updatedAt` bump for a request that did nothing.

    `isDefault` never reaches here: the schema rejects it with a 422 that names
    the `/default` endpoint.
    """
    fields = set(payload.model_fields_set)
    document = await _owned_vehicle(db, user_id=user_id, vehicle_id=vehicle_id)

    updates: dict[str, Any] = {}
    if "registrationNumber" in fields:
        updates["registrationNumber"] = payload.registrationNumber
    if "type" in fields:
        updates["type"] = payload.type.value
    if "model" in fields:
        updates["model"] = payload.model
    if "fuelType" in fields:
        updates["fuelType"] = payload.fuelType.value
        # Recomputed rather than trusted: `fuelType` is the field the user
        # actually edited.
        updates["isEV"] = is_electric(payload.fuelType)

    if not updates:
        return document, False

    updates["updatedAt"] = utcnow()
    try:
        updated = await db[VEHICLES_COLLECTION].find_one_and_update(
            {"_id": document["_id"], "userId": _user_object_id(user_id)},
            {"$set": updates},
            return_document=ReturnDocument.AFTER,
        )
    except DuplicateKeyError as exc:
        raise _duplicate_registration(
            payload.registrationNumber or document.get("registrationNumber", "")
        ) from exc

    if updated is None:
        raise NotFoundError("This vehicle does not exist")

    logger.info(
        "Updated vehicle %s (%s)", vehicle_id, ",".join(sorted(updates))
    )
    return updated, True


async def set_default(
    db: AsyncDatabase, *, user_id: str, vehicle_id: str
) -> dict[str, Any]:
    """Make this vehicle the user's default. Idempotent."""
    document = await _owned_vehicle(db, user_id=user_id, vehicle_id=vehicle_id)
    if document.get("isDefault"):
        return document

    user_oid = _user_object_id(user_id)
    await _clear_defaults(db, user_id=user_oid)

    updated = await db[VEHICLES_COLLECTION].find_one_and_update(
        {"_id": document["_id"], "userId": user_oid},
        {"$set": {"isDefault": True, "updatedAt": utcnow()}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise NotFoundError("This vehicle does not exist")

    logger.info("Set default vehicle %s for user %s", vehicle_id, user_id)
    return updated


async def delete_vehicle(
    db: AsyncDatabase, *, user_id: str, vehicle_id: str
) -> dict[str, Any]:
    """Delete a vehicle and return the document that was removed."""
    document = await _owned_vehicle(db, user_id=user_id, vehicle_id=vehicle_id)
    user_oid = _user_object_id(user_id)

    if document.get("isDefault"):
        # Promote the next vehicle *before* deleting: if the delete then fails,
        # the user is left with a default rather than with none.
        remaining = await (
            db[VEHICLES_COLLECTION]
            .find({"userId": user_oid, "_id": {"$ne": document["_id"]}})
            .sort("createdAt", 1)
            .to_list(length=1)
        )
        if remaining:
            await _promote_default(db, user_id=user_oid, vehicle_id=remaining[0]["_id"])

    result = await db[VEHICLES_COLLECTION].delete_one(
        {"_id": document["_id"], "userId": user_oid}
    )
    if result.deleted_count == 0:
        raise NotFoundError("This vehicle does not exist")

    logger.info("Deleted vehicle %s for user %s", vehicle_id, user_id)
    return document


__all__ = [
    "VehicleErrorCode",
    "create_vehicle",
    "delete_vehicle",
    "list_vehicles",
    "set_default",
    "update_vehicle",
]
