"""Facility CRUD and status management (DOC sections 7/11/14/16-18).

Services own every database *write*, matching `app/services/vehicles.py`. Routes
in `app/api/v1/facilities.py` stay thin: authorise, delegate, serialise.

Ownership and visibility are role-aware, unlike the (so far) single-owner
vehicle group:

* an **operator** sees and manages only facilities whose `operatorId` is their
  own id - one account may hold many facilities (DOC section 8, "multi-facility
  operator support");
* an **admin** sees and manages every facility (DOC section 12);
* **staff** may *read* facilities assigned through `facilityIds`, which is what
  the scanner and space-status pages will need, but never write;
* a **driver** is not served here at all - discovery belongs to the `parking`
  group.

A foreign or malformed facility id is a `404`, never a `403`: a `403` would
confirm that the id exists, same rule the vehicle group follows.

Deleting is refused with `409 FACILITY_IN_USE` while any reservation, session or
active staff assignment still points at the facility; retiring a busy garage is a
status change to `INACTIVE` / `TEMPORARILY_CLOSED`, not a delete.
"""

from __future__ import annotations

import logging
from typing import Any

from bson import ObjectId
from pymongo import ReturnDocument
from pymongo.asynchronous.database import AsyncDatabase

from app.core.config import Settings
from app.core.errors import (
    AppError,
    ConflictError,
    ErrorCode,
    NotFoundError,
)
from app.models.enums import AssignmentStatus, FacilityStatus, Role, SpaceStatus
from app.models.facility import FACILITIES_COLLECTION, geojson_point, hours_error
from app.models.space import FLOORS_COLLECTION, SPACES_COLLECTION, ZONES_COLLECTION
from app.models.staff import STAFF_ASSIGNMENTS_COLLECTION
from app.schemas.facility import (
    ChangeFacilityStatusRequest,
    CreateFacilityRequest,
    UpdateFacilityRequest,
)
from app.utils.mongo import to_object_id, utcnow

logger = logging.getLogger(__name__)

# References checked before a hard delete. Both collections currently live only
# as index definitions; the moment reservations land, a facility that is in use
# becomes un-deletable instead of silently orphaning bookings.
RESERVATIONS_COLLECTION = "reservations"
SESSIONS_COLLECTION = "parkingSessions"

FACILITY_NOT_FOUND = "This facility does not exist"
ACCOUNT_NOT_FOUND = "This account no longer exists"
USERS_COLLECTION = "users"


class FacilityErrorCode:
    """Conflict reasons carried in `error.code`, so a client can branch on it."""

    FACILITY_IN_USE = "FACILITY_IN_USE"
    FACILITY_LIMIT_REACHED = "FACILITY_LIMIT_REACHED"


async def _operator_object_id(db: AsyncDatabase, account_id: str) -> ObjectId:
    """Resolve an operator's account id, or refuse.

    `404` when the account does not exist (same message as a bad id, so this
    cannot be used to probe for real accounts); `422` when it exists but is not
    an operator - the admin meant to hand the facility to an operator and the
    mistake is in the request, not the world.
    """
    object_id = to_object_id(account_id)
    if object_id is None:
        raise NotFoundError(ACCOUNT_NOT_FOUND)

    account = await db[USERS_COLLECTION].find_one({"_id": object_id}, {"role": 1})
    if account is None:
        raise NotFoundError(ACCOUNT_NOT_FOUND)
    if account.get("role") != Role.OPERATOR.value:
        raise AppError(
            "operatorId must reference an operator account",
            code=ErrorCode.VALIDATION_ERROR,
            status_code=422,
            details={"field": "operatorId"},
        )
    return object_id


async def owned_facility(
    db: AsyncDatabase, *, user_id: str, role: Role, facility_id: str
) -> dict[str, Any]:
    """Load a facility the caller may write to, or raise a 404.

    Admins reach everything; an operator only their own. Malformed, unknown and
    foreign ids are indistinguishable on purpose. Public because the floors /
    zones / spaces service scopes every one of its routes through the parent
    facility.
    """
    object_id = to_object_id(facility_id)
    if object_id is None:
        raise NotFoundError(FACILITY_NOT_FOUND)

    document = await db[FACILITIES_COLLECTION].find_one({"_id": object_id})
    if document is None:
        raise NotFoundError(FACILITY_NOT_FOUND)

    if role is Role.ADMIN:
        return document
    if str(document.get("operatorId")) != user_id:
        raise NotFoundError(FACILITY_NOT_FOUND)
    return document


async def visible_facility(
    db: AsyncDatabase,
    *,
    user_id: str,
    role: Role,
    facility_ids: frozenset[str],
    facility_id: str,
) -> dict[str, Any]:
    """Load a facility the caller may *read*, or raise a 404.

    Read scope is wider than write scope: staff may read the facilities their
    token assigns them even though they can never write one.
    """
    object_id = to_object_id(facility_id)
    if object_id is None:
        raise NotFoundError(FACILITY_NOT_FOUND)

    document = await db[FACILITIES_COLLECTION].find_one({"_id": object_id})
    if document is None:
        raise NotFoundError(FACILITY_NOT_FOUND)

    if role is Role.ADMIN:
        return document
    if role is Role.OPERATOR and str(document.get("operatorId")) == user_id:
        return document
    if role is Role.STAFF and facility_id in facility_ids:
        return document
    raise NotFoundError(FACILITY_NOT_FOUND)


async def _list_filter(
    db: AsyncDatabase, *, user_id: str, role: Role, facility_ids: frozenset[str]
) -> dict[str, Any]:
    """The query narrowing a listing to what the caller may see."""
    if role is Role.ADMIN:
        return {}
    if role is Role.OPERATOR:
        return {"operatorId": await _operator_object_id(db, user_id)}
    # Staff: only their assignments, and an empty assignment list matches
    # nothing rather than everything.
    assigned = [
        object_id
        for object_id in (to_object_id(value) for value in facility_ids)
        if object_id is not None
    ]
    return {"_id": {"$in": assigned}}


async def list_facilities(
    db: AsyncDatabase,
    *,
    user_id: str,
    role: Role,
    facility_ids: frozenset[str] = frozenset(),
    status: FacilityStatus | None = None,
) -> list[dict[str, Any]]:
    """Facilities the caller may see, oldest first, optionally by status."""
    query = await _list_filter(db, user_id=user_id, role=role, facility_ids=facility_ids)
    if status is not None:
        query["status"] = status.value
    return await (
        db[FACILITIES_COLLECTION].find(query).sort([("createdAt", 1), ("_id", 1)]).to_list()
    )


async def operator_names(
    db: AsyncDatabase, documents: list[dict[str, Any]]
) -> dict[str, str]:
    """Map each document's `operatorId` to that account's display name.

    The admin listing shows the owning operator, whose name lives in the
    `users` collection rather than on the facility. One query covers the whole
    page so the listing does not turn into an N+1.
    """
    object_ids = [
        object_id
        for object_id in (to_object_id(str(doc.get("operatorId"))) for doc in documents)
        if object_id is not None
    ]
    if not object_ids:
        return {}
    accounts = await db[USERS_COLLECTION].find({"_id": {"$in": object_ids}}, {"name": 1}).to_list()
    return {str(account["_id"]): account.get("name", "") for account in accounts}


# A facility's `capacity` is not stored: it is the count of its spaces. This is
# the single aggregation that answers both `capacity` and the per-status tally
# for a page of facilities, so the listing does not turn into an N+1.
_SPACE_COUNT_KEYS = {
    SpaceStatus.AVAILABLE.value: "available",
    SpaceStatus.OCCUPIED.value: "occupied",
    SpaceStatus.RESERVED.value: "reserved",
    SpaceStatus.MAINTENANCE.value: "maintenance",
    SpaceStatus.BLOCKED.value: "blocked",
}


def _empty_counts() -> dict[str, int]:
    return {
        "total": 0,
        "available": 0,
        "occupied": 0,
        "reserved": 0,
        "maintenance": 0,
        "blocked": 0,
    }


async def space_counts(
    db: AsyncDatabase, facility_ids: list[Any]
) -> dict[str, dict[str, int]]:
    """Map each facility id to its derived `{total, available, occupied, ...}`.

    One aggregation covers every id, so a facility listing stays a constant
    number of queries. Faculties with no spaces are not in the result; callers
    default them to zeros.
    """
    object_ids = [
        object_id
        for object_id in (to_object_id(str(value)) for value in facility_ids)
        if object_id is not None
    ]
    if not object_ids:
        return {}

    pipeline = [
        {"$match": {"facilityId": {"$in": object_ids}}},
        {
            "$group": {
                "_id": {"facilityId": "$facilityId", "status": "$status"},
                "count": {"$sum": 1},
            }
        },
    ]
    result: dict[str, dict[str, int]] = {}
    async for row in await db[SPACES_COLLECTION].aggregate(pipeline):
        key = str(row["_id"]["facilityId"])
        counts = result.setdefault(key, _empty_counts())
        counts["total"] += row["count"]
        bucket = _SPACE_COUNT_KEYS.get(row["_id"].get("status"))
        if bucket is not None:
            counts[bucket] += row["count"]
    return result


async def space_type_counts(
    db: AsyncDatabase, facility_ids: list[Any]
) -> dict[str, dict[str, dict[str, int]]]:
    """Map each facility id to its spaces broken down by type and status.

    The shape is `facilityId -> spaceType -> {status: count}`. DOC section 22
    asks a driver to filter by "parking type", and the useful question is not
    "does this facility have EV bays" but "does it have an EV bay I can take
    now" - so the status breakdown has to survive the grouping.

    One aggregation covers every id, the same way `space_counts` does.
    """
    object_ids = [
        object_id
        for object_id in (to_object_id(str(value)) for value in facility_ids)
        if object_id is not None
    ]
    if not object_ids:
        return {}

    pipeline = [
        {"$match": {"facilityId": {"$in": object_ids}}},
        {
            "$group": {
                "_id": {"facilityId": "$facilityId", "type": "$type", "status": "$status"},
                "count": {"$sum": 1},
            }
        },
    ]
    result: dict[str, dict[str, dict[str, int]]] = {}
    async for row in await db[SPACES_COLLECTION].aggregate(pipeline):
        facility_key = str(row["_id"]["facilityId"])
        by_type = result.setdefault(facility_key, {})
        status_counts = by_type.setdefault(row["_id"].get("type") or "", {})
        status_counts[row["_id"].get("status") or ""] = (
            status_counts.get(row["_id"].get("status") or "", 0) + row["count"]
        )
    return result


async def get_facility(
    db: AsyncDatabase,
    *,
    user_id: str,
    role: Role,
    facility_ids: frozenset[str],
    facility_id: str,
) -> dict[str, Any]:
    """One facility the caller may read."""
    return await visible_facility(
        db,
        user_id=user_id,
        role=role,
        facility_ids=facility_ids,
        facility_id=facility_id,
    )


async def create_facility(
    db: AsyncDatabase,
    *,
    user_id: str,
    role: Role,
    payload: CreateFacilityRequest,
    settings: Settings,
) -> dict[str, Any]:
    """Register a facility owned by the caller (or, for an admin, by
    `payload.operatorId`). New facilities always start `ACTIVE`."""
    if payload.operatorId is not None:
        if role is not Role.ADMIN:
            raise AppError(
                "Only an admin may create a facility for another operator",
                code=ErrorCode.VALIDATION_ERROR,
                status_code=422,
                details={"field": "operatorId"},
            )
        # On-behalf of: the target must exist and be an operator.
        owner_oid = await _operator_object_id(db, payload.operatorId)
    else:
        # The caller's own account just authenticated, so it exists; an admin
        # that does not name an operator owns the facility itself.
        owner_oid = to_object_id(user_id)
        if owner_oid is None:
            raise NotFoundError(ACCOUNT_NOT_FOUND)

    existing = await db[FACILITIES_COLLECTION].count_documents({"operatorId": owner_oid})
    if existing >= settings.facility_max_per_operator:
        raise ConflictError(
            f"An operator can manage at most {settings.facility_max_per_operator} facilities",
            code=FacilityErrorCode.FACILITY_LIMIT_REACHED,
            details={"limit": settings.facility_max_per_operator},
        )

    now = utcnow()
    document: dict[str, Any] = {
        "_id": ObjectId(),
        "operatorId": owner_oid,
        "name": payload.name,
        "description": payload.description,
        "address": payload.address,
        "city": payload.city,
        "location": geojson_point(payload.lat, payload.lng),
        "open24h": payload.open24h,
        "openingTime": payload.openingTime,
        "closingTime": payload.closingTime,
        "amenities": [amenity.value for amenity in payload.amenities],
        "hourlyRate": payload.hourlyRate,
        "dailyMax": payload.dailyMax,
        "graceMinutes": payload.graceMinutes,
        "overstayGraceMinutes": payload.overstayGraceMinutes,
        "overstayRate": payload.overstayRate,
        "rating": payload.rating,
        "reviewCount": payload.reviewCount,
        "status": FacilityStatus.ACTIVE.value,
        "createdAt": now,
        "updatedAt": now,
    }
    await db[FACILITIES_COLLECTION].insert_one(document)

    # Read back rather than returning the in-memory document: MongoDB stores
    # milliseconds, not microseconds, so the object built above would otherwise
    # carry a `createdAt` that no later read of the same facility ever returns.
    stored = await db[FACILITIES_COLLECTION].find_one({"_id": document["_id"]})
    document = stored or document

    logger.info(
        "Created facility %s for operator %s (%s)",
        document["_id"],
        owner_oid,
        payload.name,
    )
    return document


async def update_facility(
    db: AsyncDatabase,
    *,
    user_id: str,
    role: Role,
    facility_id: str,
    payload: UpdateFacilityRequest,
) -> tuple[dict[str, Any], bool]:
    """Apply a `PATCH` and return `(document, changed)`.

    `changed` is False when nothing was supplied, so the route can skip the
    audit row and the `updatedAt` bump for a request that did nothing.

    `status` and `operatorId` never reach here: the schema rejects both with a
    `422`. Hours are validated against the *merged* result, so a patch that
    only flips `open24h` to `false` is refused when the stored times cannot
    carry it.
    """
    fields = set(payload.model_fields_set) - {"status", "operatorId", "capacity"}
    document = await owned_facility(db, user_id=user_id, role=role, facility_id=facility_id)
    if not fields:
        return document, False

    updates: dict[str, Any] = {}
    for field_name in (
        "name",
        "address",
        "city",
        "description",
        "rating",
        "reviewCount",
    ):
        if field_name in fields:
            updates[field_name] = getattr(payload, field_name)
    for field_name in (
        "openingTime",
        "closingTime",
        "hourlyRate",
        "dailyMax",
        "graceMinutes",
        "overstayGraceMinutes",
        "overstayRate",
    ):
        if field_name in fields:
            updates[field_name] = getattr(payload, field_name)
    if "open24h" in fields:
        updates["open24h"] = payload.open24h
    if "amenities" in fields and payload.amenities is not None:
        updates["amenities"] = [amenity.value for amenity in payload.amenities]
    if "lat" in fields and "lng" in fields:
        updates["location"] = geojson_point(payload.lat, payload.lng)

    merged_open24h = payload.open24h if "open24h" in fields else bool(document.get("open24h", True))
    merged_opening = payload.openingTime if "openingTime" in fields else document.get("openingTime")
    merged_closing = payload.closingTime if "closingTime" in fields else document.get("closingTime")
    hours_problem = hours_error(merged_open24h, merged_opening, merged_closing)
    if hours_problem is not None:
        raise AppError(
            hours_problem,
            code=ErrorCode.VALIDATION_ERROR,
            status_code=422,
        )

    if not updates:
        return document, False

    updates["updatedAt"] = utcnow()
    updated = await db[FACILITIES_COLLECTION].find_one_and_update(
        {"_id": document["_id"]},
        {"$set": updates},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise NotFoundError(FACILITY_NOT_FOUND)

    logger.info("Updated facility %s (%s)", facility_id, ",".join(sorted(updates)))
    return updated, True


async def set_status(
    db: AsyncDatabase,
    *,
    user_id: str,
    role: Role,
    facility_id: str,
    payload: ChangeFacilityStatusRequest,
) -> tuple[dict[str, Any], str | None]:
    """Move a facility between the DOC section 17 states.

    Returns `(document, previous_status)`; the route uses `previous_status` to
    decide whether anything actually changed, so setting the status a facility
    already holds is a no-op that neither bumps `updatedAt` nor writes an audit
    row. Any transition is allowed - `ACTIVE` -> `MAINTENANCE` and back, a
    temporary closure, a deactivation - because the DOC names the states but no
    transition matrix, and the operator dashboard needs all of them.
    """
    document = await owned_facility(db, user_id=user_id, role=role, facility_id=facility_id)
    previous = document.get("status")
    if previous == payload.status.value:
        return document, previous

    updated = await db[FACILITIES_COLLECTION].find_one_and_update(
        {"_id": document["_id"]},
        {"$set": {"status": payload.status.value, "updatedAt": utcnow()}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise NotFoundError(FACILITY_NOT_FOUND)

    logger.info("Facility %s status %s -> %s", facility_id, previous, payload.status.value)
    return updated, previous


async def delete_facility(
    db: AsyncDatabase, *, user_id: str, role: Role, facility_id: str
) -> dict[str, Any]:
    """Delete a facility and return the document that was removed.

    Refuses with `409 FACILITY_IN_USE` while reservations or sessions still
    reference it: deleting the row would orphan live bookings, and the correct
    action there is a status change instead. The facility's floors, zones and
    spaces exist only for it, so they are removed with it rather than left
    dangling.
    """
    document = await owned_facility(db, user_id=user_id, role=role, facility_id=facility_id)
    facility_oid = document["_id"]
    # `$in` with both spellings so the check works whether the referencing
    # collections store the id as an ObjectId (the convention here) or as the
    # string a client once sent.
    reference = {"facilityId": {"$in": [facility_oid, str(facility_oid)]}}

    reservations = await db[RESERVATIONS_COLLECTION].count_documents(reference)
    sessions = await db[SESSIONS_COLLECTION].count_documents(reference)
    if reservations or sessions:
        raise ConflictError(
            "This facility still has reservations or sessions; "
            "set it to INACTIVE or TEMPORARILY_CLOSED instead of deleting it",
            code=FacilityErrorCode.FACILITY_IN_USE,
            details={"reservations": reservations, "sessions": sessions},
        )

    # Staff assignments are access, not history: an active row would keep
    # granting a (now deleted) facility to its staff on their next request, so
    # the delete is refused until the operator unassigns them. Closed rows are
    # cascaded with the rest of the facility below.
    active_assignments = await db[STAFF_ASSIGNMENTS_COLLECTION].count_documents(
        {
            "facilityId": facility_oid,
            "status": AssignmentStatus.ACTIVE.value,
        }
    )
    if active_assignments:
        raise ConflictError(
            "This facility still has staff assigned to it; unassign them first",
            code=FacilityErrorCode.FACILITY_IN_USE,
            details={"staffAssignments": active_assignments},
        )

    result = await db[FACILITIES_COLLECTION].delete_one({"_id": facility_oid})
    if result.deleted_count == 0:
        raise NotFoundError(FACILITY_NOT_FOUND)

    # Cascade: everything below a facility is meaningless without it.
    for collection in (
        SPACES_COLLECTION,
        ZONES_COLLECTION,
        FLOORS_COLLECTION,
        STAFF_ASSIGNMENTS_COLLECTION,
    ):
        await db[collection].delete_many(reference)

    logger.info("Deleted facility %s", facility_id)
    return document


__all__ = [
    "FacilityErrorCode",
    "create_facility",
    "delete_facility",
    "get_facility",
    "list_facilities",
    "operator_names",
    "owned_facility",
    "set_status",
    "space_counts",
    "update_facility",
    "visible_facility",
]
