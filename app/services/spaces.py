"""Floors, zones and spaces: the Facility -> Floor -> Zone -> Space tree
(DOC sections 7.79, 11.159, 11.169, 16-18, 33).

Ownership mirrors the `facilities` group exactly: every route scopes through
the parent facility, so an operator may manage only the floors/zones/spaces of
facilities they own, an admin any of them, and staff may *read* the ones their
token assigns. A foreign, unknown or malformed id is a `404` with the local
"does not exist" message, never a `403`, so the routes cannot be used to probe
for ids.

Two behaviors worth stating up front:

* **Capacity is not stored.** A facility's capacity is the count of its spaces,
  computed by `app.services.facilities.space_counts`.
* **Zone closure cascades.** Closing a zone (`PATCH /zones/{id}/status` with
  `INACTIVE`, DOC section 11.169) flips its available/reserved/occupied spaces
  to `BLOCKED`; reopening flips blocked ones back to `AVAILABLE`. A space an
  operator had manually blocked is therefore also released on reopen - the
  phase has one block reason, not a stack.
"""

from __future__ import annotations

import logging
from typing import Any

from bson import ObjectId
from pymongo import ReturnDocument
from pymongo.asynchronous.database import AsyncDatabase

from app.core.config import Settings
from app.core.errors import AppError, ConflictError, ErrorCode, NotFoundError
from app.models.enums import Role, SpaceStatus, ZoneStatus
from app.models.space import (
    FLOORS_COLLECTION,
    SPACES_COLLECTION,
    ZONES_COLLECTION,
    space_numbers,
)
from app.schemas.space import (
    BulkCreateSpacesRequest,
    ChangeSpaceStatusRequest,
    ChangeZoneStatusRequest,
    CreateFloorRequest,
    CreateSpaceRequest,
    CreateZoneRequest,
    UpdateFloorRequest,
    UpdateSpaceRequest,
    UpdateZoneRequest,
)
from app.services.facilities import owned_facility, visible_facility
from app.utils.mongo import to_object_id, utcnow

logger = logging.getLogger(__name__)

FLOOR_NOT_FOUND = "This floor does not exist"
ZONE_NOT_FOUND = "This zone does not exist"
SPACE_NOT_FOUND = "This space does not exist"


class SpaceErrorCode:
    """Conflict reasons carried in `error.code`, so a client can branch on it."""

    FLOOR_LIMIT_REACHED = "FLOOR_LIMIT_REACHED"
    ZONE_LIMIT_REACHED = "ZONE_LIMIT_REACHED"
    SPACE_LIMIT_REACHED = "SPACE_LIMIT_REACHED"
    SPACE_DUPLICATE = "SPACE_DUPLICATE"


# --- Scope helpers (each loads the parent chain, then defers to facilities) ---


async def _owned_floor(
    db: AsyncDatabase, *, user_id: str, role: Role, floor_id: str
) -> dict[str, Any]:
    object_id = to_object_id(floor_id)
    if object_id is None:
        raise NotFoundError(FLOOR_NOT_FOUND)
    floor = await db[FLOORS_COLLECTION].find_one({"_id": object_id})
    if floor is None:
        raise NotFoundError(FLOOR_NOT_FOUND)
    try:
        await owned_facility(db, user_id=user_id, role=role, facility_id=str(floor["facilityId"]))
    except NotFoundError:
        raise NotFoundError(FLOOR_NOT_FOUND) from None
    return floor


async def _visible_floor(
    db: AsyncDatabase,
    *,
    user_id: str,
    role: Role,
    facility_ids: frozenset[str],
    floor_id: str,
) -> dict[str, Any]:
    object_id = to_object_id(floor_id)
    if object_id is None:
        raise NotFoundError(FLOOR_NOT_FOUND)
    floor = await db[FLOORS_COLLECTION].find_one({"_id": object_id})
    if floor is None:
        raise NotFoundError(FLOOR_NOT_FOUND)
    try:
        await visible_facility(
            db,
            user_id=user_id,
            role=role,
            facility_ids=facility_ids,
            facility_id=str(floor["facilityId"]),
        )
    except NotFoundError:
        raise NotFoundError(FLOOR_NOT_FOUND) from None
    return floor


async def _owned_zone(
    db: AsyncDatabase, *, user_id: str, role: Role, zone_id: str
) -> dict[str, Any]:
    object_id = to_object_id(zone_id)
    if object_id is None:
        raise NotFoundError(ZONE_NOT_FOUND)
    zone = await db[ZONES_COLLECTION].find_one({"_id": object_id})
    if zone is None:
        raise NotFoundError(ZONE_NOT_FOUND)
    try:
        await owned_facility(db, user_id=user_id, role=role, facility_id=str(zone["facilityId"]))
    except NotFoundError:
        raise NotFoundError(ZONE_NOT_FOUND) from None
    return zone


async def _visible_zone(
    db: AsyncDatabase,
    *,
    user_id: str,
    role: Role,
    facility_ids: frozenset[str],
    zone_id: str,
) -> dict[str, Any]:
    object_id = to_object_id(zone_id)
    if object_id is None:
        raise NotFoundError(ZONE_NOT_FOUND)
    zone = await db[ZONES_COLLECTION].find_one({"_id": object_id})
    if zone is None:
        raise NotFoundError(ZONE_NOT_FOUND)
    try:
        await visible_facility(
            db,
            user_id=user_id,
            role=role,
            facility_ids=facility_ids,
            facility_id=str(zone["facilityId"]),
        )
    except NotFoundError:
        raise NotFoundError(ZONE_NOT_FOUND) from None
    return zone


async def _owned_space(
    db: AsyncDatabase, *, user_id: str, role: Role, space_id: str
) -> dict[str, Any]:
    object_id = to_object_id(space_id)
    if object_id is None:
        raise NotFoundError(SPACE_NOT_FOUND)
    space = await db[SPACES_COLLECTION].find_one({"_id": object_id})
    if space is None:
        raise NotFoundError(SPACE_NOT_FOUND)
    try:
        await owned_facility(db, user_id=user_id, role=role, facility_id=str(space["facilityId"]))
    except NotFoundError:
        raise NotFoundError(SPACE_NOT_FOUND) from None
    return space


async def _writable_space(
    db: AsyncDatabase,
    *,
    user_id: str,
    role: Role,
    facility_ids: frozenset[str],
    space_id: str,
) -> dict[str, Any]:
    """A space the caller may *set the status of*.

    Operators and admins go through the owning-facility check; staff are allowed
    here too (DOC section 10.151 "staff mark/change space status where
    authorized") but only for a facility their token assigns, so they go through
    the read scope instead.
    """
    object_id = to_object_id(space_id)
    if object_id is None:
        raise NotFoundError(SPACE_NOT_FOUND)
    space = await db[SPACES_COLLECTION].find_one({"_id": object_id})
    if space is None:
        raise NotFoundError(SPACE_NOT_FOUND)
    try:
        if role is Role.STAFF:
            await visible_facility(
                db,
                user_id=user_id,
                role=role,
                facility_ids=facility_ids,
                facility_id=str(space["facilityId"]),
            )
        else:
            await owned_facility(
                db, user_id=user_id, role=role, facility_id=str(space["facilityId"])
            )
    except NotFoundError:
        raise NotFoundError(SPACE_NOT_FOUND) from None
    return space


async def _visible_space(
    db: AsyncDatabase,
    *,
    user_id: str,
    role: Role,
    facility_ids: frozenset[str],
    space_id: str,
) -> dict[str, Any]:
    object_id = to_object_id(space_id)
    if object_id is None:
        raise NotFoundError(SPACE_NOT_FOUND)
    space = await db[SPACES_COLLECTION].find_one({"_id": object_id})
    if space is None:
        raise NotFoundError(SPACE_NOT_FOUND)
    try:
        await visible_facility(
            db,
            user_id=user_id,
            role=role,
            facility_ids=facility_ids,
            facility_id=str(space["facilityId"]),
        )
    except NotFoundError:
        raise NotFoundError(SPACE_NOT_FOUND) from None
    return space


# --- Floors -----------------------------------------------------------------


async def list_floors(
    db: AsyncDatabase,
    *,
    user_id: str,
    role: Role,
    facility_ids: frozenset[str],
    facility_id: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return `(floors, zones)` for a facility, oldest/level order.

    Zones are returned flat and keyed by `floorId`; the route nests them so the
    layout tree is a single round trip.
    """
    facility = await visible_facility(
        db,
        user_id=user_id,
        role=role,
        facility_ids=facility_ids,
        facility_id=facility_id,
    )
    facility_oid = facility["_id"]
    floors = (
        await db[FLOORS_COLLECTION]
        .find({"facilityId": facility_oid})
        .sort([("level", 1), ("name", 1), ("_id", 1)])
        .to_list()
    )
    zones = (
        await db[ZONES_COLLECTION]
        .find({"facilityId": facility_oid})
        .sort([("name", 1), ("_id", 1)])
        .to_list()
    )
    return floors, zones


async def create_floor(
    db: AsyncDatabase,
    *,
    user_id: str,
    role: Role,
    facility_id: str,
    payload: CreateFloorRequest,
    settings: Settings,
) -> dict[str, Any]:
    facility = await owned_facility(db, user_id=user_id, role=role, facility_id=facility_id)
    existing = await db[FLOORS_COLLECTION].count_documents({"facilityId": facility["_id"]})
    if existing >= settings.floor_max_per_facility:
        raise ConflictError(
            f"A facility can have at most {settings.floor_max_per_facility} floors",
            code=SpaceErrorCode.FLOOR_LIMIT_REACHED,
            details={"limit": settings.floor_max_per_facility},
        )

    now = utcnow()
    document: dict[str, Any] = {
        "_id": ObjectId(),
        "facilityId": facility["_id"],
        "name": payload.name,
        "level": payload.level,
        "createdAt": now,
        "updatedAt": now,
    }
    await db[FLOORS_COLLECTION].insert_one(document)
    stored = await db[FLOORS_COLLECTION].find_one({"_id": document["_id"]})
    logger.info("Created floor %s on facility %s", document["_id"], facility_id)
    return stored or document


async def update_floor(
    db: AsyncDatabase,
    *,
    user_id: str,
    role: Role,
    floor_id: str,
    payload: UpdateFloorRequest,
) -> tuple[dict[str, Any], bool]:
    floor = await _owned_floor(db, user_id=user_id, role=role, floor_id=floor_id)
    fields = set(payload.model_fields_set)
    updates: dict[str, Any] = {}
    if "name" in fields:
        updates["name"] = payload.name
    if "level" in fields:
        updates["level"] = payload.level
    if not updates:
        return floor, False

    updates["updatedAt"] = utcnow()
    updated = await db[FLOORS_COLLECTION].find_one_and_update(
        {"_id": floor["_id"]},
        {"$set": updates},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise NotFoundError(FLOOR_NOT_FOUND)

    # Keep the denormalized snapshot on every space in step with the rename.
    if "name" in updates:
        await db[SPACES_COLLECTION].update_many(
            {"floorId": floor["_id"]}, {"$set": {"floorName": updated["name"]}}
        )
    return updated, True


async def delete_floor(
    db: AsyncDatabase, *, user_id: str, role: Role, floor_id: str
) -> tuple[dict[str, Any], dict[str, int]]:
    """Delete a floor and everything under it; returns `(floor, counts)`."""
    floor = await _owned_floor(db, user_id=user_id, role=role, floor_id=floor_id)
    spaces_result = await db[SPACES_COLLECTION].delete_many({"floorId": floor["_id"]})
    zones_result = await db[ZONES_COLLECTION].delete_many({"floorId": floor["_id"]})
    await db[FLOORS_COLLECTION].delete_one({"_id": floor["_id"]})
    logger.info(
        "Deleted floor %s (cascaded %d zones, %d spaces)",
        floor_id,
        zones_result.deleted_count,
        spaces_result.deleted_count,
    )
    return floor, {"zones": zones_result.deleted_count, "spaces": spaces_result.deleted_count}


# --- Zones ------------------------------------------------------------------


async def create_zone(
    db: AsyncDatabase,
    *,
    user_id: str,
    role: Role,
    floor_id: str,
    payload: CreateZoneRequest,
    settings: Settings,
) -> dict[str, Any]:
    floor = await _owned_floor(db, user_id=user_id, role=role, floor_id=floor_id)
    existing = await db[ZONES_COLLECTION].count_documents({"floorId": floor["_id"]})
    if existing >= settings.zone_max_per_floor:
        raise ConflictError(
            f"A floor can have at most {settings.zone_max_per_floor} zones",
            code=SpaceErrorCode.ZONE_LIMIT_REACHED,
            details={"limit": settings.zone_max_per_floor},
        )

    now = utcnow()
    document: dict[str, Any] = {
        "_id": ObjectId(),
        "facilityId": floor["facilityId"],
        "floorId": floor["_id"],
        "name": payload.name,
        "status": ZoneStatus.ACTIVE.value,
        "closeReason": None,
        "createdAt": now,
        "updatedAt": now,
    }
    await db[ZONES_COLLECTION].insert_one(document)
    stored = await db[ZONES_COLLECTION].find_one({"_id": document["_id"]})
    logger.info("Created zone %s on floor %s", document["_id"], floor_id)
    return stored or document


async def update_zone(
    db: AsyncDatabase,
    *,
    user_id: str,
    role: Role,
    zone_id: str,
    payload: UpdateZoneRequest,
) -> tuple[dict[str, Any], bool]:
    zone = await _owned_zone(db, user_id=user_id, role=role, zone_id=zone_id)
    if "name" not in payload.model_fields_set:
        return zone, False

    updated = await db[ZONES_COLLECTION].find_one_and_update(
        {"_id": zone["_id"]},
        {"$set": {"name": payload.name, "updatedAt": utcnow()}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise NotFoundError(ZONE_NOT_FOUND)

    await db[SPACES_COLLECTION].update_many(
        {"zoneId": zone["_id"]}, {"$set": {"zoneName": updated["name"]}}
    )
    return updated, True


async def set_zone_status(
    db: AsyncDatabase,
    *,
    user_id: str,
    role: Role,
    zone_id: str,
    payload: ChangeZoneStatusRequest,
) -> tuple[dict[str, Any], str | None, int]:
    """Close or reopen a zone (DOC section 11.169).

    Returns `(zone, previous_status, affected_spaces)`. A no-op change reports
    the previous status so the route can skip the audit row.
    """
    zone = await _owned_zone(db, user_id=user_id, role=role, zone_id=zone_id)
    previous = zone.get("status", ZoneStatus.ACTIVE.value)
    if previous == payload.status.value:
        return zone, previous, 0

    close_reason = payload.reason if payload.status is ZoneStatus.INACTIVE else None
    updated = await db[ZONES_COLLECTION].find_one_and_update(
        {"_id": zone["_id"]},
        {
            "$set": {
                "status": payload.status.value,
                "closeReason": close_reason,
                "updatedAt": utcnow(),
            }
        },
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise NotFoundError(ZONE_NOT_FOUND)

    # Closing blocks the spaces that were in service; reopening releases them.
    if payload.status is ZoneStatus.INACTIVE:
        active = [
            SpaceStatus.AVAILABLE.value,
            SpaceStatus.RESERVED.value,
            SpaceStatus.OCCUPIED.value,
        ]
        result = await db[SPACES_COLLECTION].update_many(
            {"zoneId": zone["_id"], "status": {"$in": active}},
            {"$set": {"status": SpaceStatus.BLOCKED.value, "updatedAt": utcnow()}},
        )
    else:
        result = await db[SPACES_COLLECTION].update_many(
            {"zoneId": zone["_id"], "status": SpaceStatus.BLOCKED.value},
            {"$set": {"status": SpaceStatus.AVAILABLE.value, "updatedAt": utcnow()}},
        )

    logger.info(
        "Zone %s status %s -> %s (%d spaces)",
        zone_id,
        previous,
        payload.status.value,
        result.modified_count,
    )
    return updated, previous, result.modified_count


async def delete_zone(
    db: AsyncDatabase, *, user_id: str, role: Role, zone_id: str
) -> tuple[dict[str, Any], dict[str, int]]:
    zone = await _owned_zone(db, user_id=user_id, role=role, zone_id=zone_id)
    spaces_result = await db[SPACES_COLLECTION].delete_many({"zoneId": zone["_id"]})
    await db[ZONES_COLLECTION].delete_one({"_id": zone["_id"]})
    logger.info("Deleted zone %s (cascaded %d spaces)", zone_id, spaces_result.deleted_count)
    return zone, {"spaces": spaces_result.deleted_count}


# --- Spaces -----------------------------------------------------------------


async def list_spaces(
    db: AsyncDatabase,
    *,
    user_id: str,
    role: Role,
    facility_ids: frozenset[str],
    facility_id: str | None = None,
    zone_id: str | None = None,
    floor_id: str | None = None,
    status: SpaceStatus | None = None,
) -> list[dict[str, Any]]:
    """Spaces of a facility or of one zone, newest-last.

    Exactly one of `facility_id` / `zone_id` must be given; `floor_id` and
    `status` are optional filters on top.
    """
    query: dict[str, Any] = {}
    if zone_id is not None:
        zone = await _visible_zone(
            db, user_id=user_id, role=role, facility_ids=facility_ids, zone_id=zone_id
        )
        query["zoneId"] = zone["_id"]
    elif facility_id is not None:
        facility = await visible_facility(
            db,
            user_id=user_id,
            role=role,
            facility_ids=facility_ids,
            facility_id=facility_id,
        )
        query["facilityId"] = facility["_id"]
    else:
        raise AppError(
            "A facilityId or zoneId is required",
            code=ErrorCode.VALIDATION_ERROR,
            status_code=422,
        )

    if floor_id is not None:
        floor_oid = to_object_id(floor_id)
        query["floorId"] = floor_oid if floor_oid is not None else ObjectId()
    if status is not None:
        query["status"] = status.value

    return (
        await db[SPACES_COLLECTION]
        .find(query)
        .sort([("floorName", 1), ("zoneName", 1), ("spaceNumber", 1), ("_id", 1)])
        .to_list()
    )


async def get_space(
    db: AsyncDatabase,
    *,
    user_id: str,
    role: Role,
    facility_ids: frozenset[str],
    space_id: str,
) -> dict[str, Any]:
    return await _visible_space(
        db, user_id=user_id, role=role, facility_ids=facility_ids, space_id=space_id
    )


async def _floor_for_zone(db: AsyncDatabase, zone: dict[str, Any]) -> dict[str, Any]:
    floor = await db[FLOORS_COLLECTION].find_one({"_id": zone["floorId"]})
    if floor is None:
        raise NotFoundError(FLOOR_NOT_FOUND)
    return floor


async def _guard_zone_capacity(
    db: AsyncDatabase, zone: dict[str, Any], *, adding: int, limit: int
) -> None:
    existing = await db[SPACES_COLLECTION].count_documents({"zoneId": zone["_id"]})
    if existing + adding > limit:
        raise ConflictError(
            f"A zone can have at most {limit} spaces",
            code=SpaceErrorCode.SPACE_LIMIT_REACHED,
            details={"limit": limit, "existing": existing, "requested": adding},
        )


def _space_document(
    *, facility_id: Any, floor: dict[str, Any], zone: dict[str, Any], number: str,
    space_type: str, reservable: bool, now: Any,
) -> dict[str, Any]:
    return {
        "_id": ObjectId(),
        "facilityId": facility_id,
        "floorId": floor["_id"],
        "floorName": floor.get("name"),
        "zoneId": zone["_id"],
        "zoneName": zone.get("name"),
        "spaceNumber": number,
        "type": space_type,
        "status": SpaceStatus.AVAILABLE.value,
        "reservable": reservable,
        "createdAt": now,
        "updatedAt": now,
    }


async def create_space(
    db: AsyncDatabase,
    *,
    user_id: str,
    role: Role,
    zone_id: str,
    payload: CreateSpaceRequest,
    settings: Settings,
) -> dict[str, Any]:
    zone = await _owned_zone(db, user_id=user_id, role=role, zone_id=zone_id)
    await _guard_zone_capacity(db, zone, adding=1, limit=settings.space_max_per_zone)

    clash = await db[SPACES_COLLECTION].find_one(
        {"zoneId": zone["_id"], "spaceNumber": payload.spaceNumber}, {"_id": 1}
    )
    if clash is not None:
        raise ConflictError(
            f"Space {payload.spaceNumber} already exists in this zone",
            code=SpaceErrorCode.SPACE_DUPLICATE,
            details={"spaceNumber": payload.spaceNumber},
        )

    floor = await _floor_for_zone(db, zone)
    document = _space_document(
        facility_id=zone["facilityId"],
        floor=floor,
        zone=zone,
        number=payload.spaceNumber,
        space_type=payload.type.value,
        reservable=payload.reservable,
        now=utcnow(),
    )
    await db[SPACES_COLLECTION].insert_one(document)
    stored = await db[SPACES_COLLECTION].find_one({"_id": document["_id"]})
    logger.info("Created space %s (%s) in zone %s", document["_id"], payload.spaceNumber, zone_id)
    return stored or document


async def bulk_create_spaces(
    db: AsyncDatabase,
    *,
    user_id: str,
    role: Role,
    zone_id: str,
    payload: BulkCreateSpacesRequest,
    settings: Settings,
) -> list[dict[str, Any]]:
    """Create a run of spaces (e.g. ``A01``..``A50``) in one request."""
    if payload.count > settings.spaces_bulk_max:
        raise AppError(
            f"count must be at most {settings.spaces_bulk_max}",
            code=ErrorCode.VALIDATION_ERROR,
            status_code=422,
            details={"field": "count", "limit": settings.spaces_bulk_max},
        )

    zone = await _owned_zone(db, user_id=user_id, role=role, zone_id=zone_id)
    await _guard_zone_capacity(db, zone, adding=payload.count, limit=settings.space_max_per_zone)

    numbers = space_numbers(payload.prefix, payload.start, payload.count, payload.pad)
    existing = {
        row["spaceNumber"]
        async for row in db[SPACES_COLLECTION].find(
            {"zoneId": zone["_id"], "spaceNumber": {"$in": numbers}}, {"spaceNumber": 1}
        )
    }
    duplicates = [number for number in numbers if number in existing]
    if duplicates:
        raise ConflictError(
            "Some of these space numbers already exist in this zone",
            code=SpaceErrorCode.SPACE_DUPLICATE,
            details={"duplicates": duplicates[:25], "count": len(duplicates)},
        )

    floor = await _floor_for_zone(db, zone)
    now = utcnow()
    documents = [
        _space_document(
            facility_id=zone["facilityId"],
            floor=floor,
            zone=zone,
            number=number,
            space_type=payload.type.value,
            reservable=payload.reservable,
            now=now,
        )
        for number in numbers
    ]
    await db[SPACES_COLLECTION].insert_many(documents)
    ids = [document["_id"] for document in documents]
    stored = await db[SPACES_COLLECTION].find({"_id": {"$in": ids}}).to_list()
    logger.info("Bulk-created %d spaces in zone %s", len(documents), zone_id)
    return stored


async def update_space(
    db: AsyncDatabase,
    *,
    user_id: str,
    role: Role,
    space_id: str,
    payload: UpdateSpaceRequest,
) -> tuple[dict[str, Any], bool]:
    space = await _owned_space(db, user_id=user_id, role=role, space_id=space_id)
    fields = set(payload.model_fields_set)
    updates: dict[str, Any] = {}
    if "spaceNumber" in fields:
        clash = await db[SPACES_COLLECTION].find_one(
            {
                "zoneId": space["zoneId"],
                "spaceNumber": payload.spaceNumber,
                "_id": {"$ne": space["_id"]},
            },
            {"_id": 1},
        )
        if clash is not None:
            raise ConflictError(
                f"Space {payload.spaceNumber} already exists in this zone",
                code=SpaceErrorCode.SPACE_DUPLICATE,
                details={"spaceNumber": payload.spaceNumber},
            )
        updates["spaceNumber"] = payload.spaceNumber
    if "type" in fields:
        updates["type"] = payload.type.value
    if "reservable" in fields:
        updates["reservable"] = payload.reservable
    if not updates:
        return space, False

    updates["updatedAt"] = utcnow()
    updated = await db[SPACES_COLLECTION].find_one_and_update(
        {"_id": space["_id"]}, {"$set": updates}, return_document=ReturnDocument.AFTER
    )
    if updated is None:
        raise NotFoundError(SPACE_NOT_FOUND)
    return updated, True


async def delete_space(
    db: AsyncDatabase, *, user_id: str, role: Role, space_id: str
) -> dict[str, Any]:
    space = await _owned_space(db, user_id=user_id, role=role, space_id=space_id)
    await db[SPACES_COLLECTION].delete_one({"_id": space["_id"]})
    logger.info("Deleted space %s", space_id)
    return space


async def set_space_status(
    db: AsyncDatabase,
    *,
    user_id: str,
    role: Role,
    facility_ids: frozenset[str],
    space_id: str,
    payload: ChangeSpaceStatusRequest,
) -> tuple[dict[str, Any], str | None]:
    """Move a space to a manually-settable state (DOC 10.151).

    `OCCUPIED`/`RESERVED` are refused by the schema. A no-op change returns the
    previous status so the route can skip the audit row. Staff reach this
    through their facility assignment; operators/admins through ownership.
    """
    space = await _writable_space(
        db, user_id=user_id, role=role, facility_ids=facility_ids, space_id=space_id
    )
    previous = space.get("status", SpaceStatus.AVAILABLE.value)
    if previous == payload.status.value:
        return space, previous

    updated = await db[SPACES_COLLECTION].find_one_and_update(
        {"_id": space["_id"]},
        {"$set": {"status": payload.status.value, "updatedAt": utcnow()}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise NotFoundError(SPACE_NOT_FOUND)
    logger.info("Space %s status %s -> %s", space_id, previous, payload.status.value)
    return updated, previous


__all__ = [
    "SpaceErrorCode",
    "bulk_create_spaces",
    "create_floor",
    "create_space",
    "create_zone",
    "delete_floor",
    "delete_space",
    "delete_zone",
    "get_space",
    "list_floors",
    "list_spaces",
    "set_space_status",
    "set_zone_status",
    "update_floor",
    "update_space",
    "update_zone",
]
