"""Booking and parking-session engine (DOC sections 16, 17 and 21).

This module owns the two halves of the lifecycle:

* **Reservations** - a driver books a 30-minute-aligned interval at a facility.
  The engine picks a concrete space and claims one `reservationSlots` row per
  step. The unique index on `(spaceId, slotStart)` is the real overlap guard;
  Python only chooses a candidate and retries the next one when the database
  rejects the claim.
* **Sessions** - staff check a booking in (space becomes `OCCUPIED`) and check
  it out again (space returns to `AVAILABLE`, the amount is estimated from the
  facility's base rate). Payments are a later module, so every amount carries
  `amountEstimated: true`.

No MongoDB transactions are used: the deployment target is a standalone
mongod. Multi-document writes therefore follow one of two patterns:

* a conditional `find_one_and_update` that changes status only if it is still
  the expected value, plus a compensating revert if the follow-up insert fails;
* a pre-generated `ObjectId` so the slot rows can be written before the
  reservation row, with a cleanup by `reservationId` if the reservation insert
  fails.

Scope follows the rest of the API: drivers see their own rows, staff the
facilities assigned to them, operators the facilities they own, admins
everything. A malformed, unknown or foreign id is a `404`, never a `403`.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime, timedelta
from typing import Any

from bson import ObjectId
from pymongo import ReturnDocument
from pymongo.asynchronous.database import AsyncDatabase
from pymongo.errors import DuplicateKeyError

from app.core.config import Settings
from app.core.errors import AppError, ConflictError, ErrorCode, NotFoundError
from app.models.enums import (
    FacilityStatus,
    ReservationStatus,
    Role,
    SessionStatus,
    SpaceStatus,
    ZoneStatus,
)
from app.models.facility import FACILITIES_COLLECTION
from app.models.reservation import (
    RESERVATION_SLOTS_COLLECTION,
    RESERVATIONS_COLLECTION,
    SESSIONS_COLLECTION,
    normalise_reference,
    public_reservation,
    public_session,
    qr_token,
    reservation_reference,
    slot_starts,
)
from app.models.space import SPACES_COLLECTION, ZONES_COLLECTION
from app.models.vehicle import VEHICLES_COLLECTION
from app.schemas.reservation import CreateReservationRequest
from app.services.facilities import visible_facility
from app.utils.mongo import to_object_id, utcnow

logger = logging.getLogger(__name__)

RESERVATION_NOT_FOUND = "This reservation does not exist"
SESSION_NOT_FOUND = "This parking session does not exist"
SPACE_NOT_FOUND = "This space does not exist"
FACILITY_NOT_FOUND = "This facility does not exist"
VEHICLE_NOT_FOUND = "This vehicle does not exist"


class ReservationErrorCode:
    """Stable conflict reasons carried in `error.code`."""

    FACILITY_NOT_ACCEPTING = "FACILITY_NOT_ACCEPTING"
    SPACE_NOT_AVAILABLE = "SPACE_NOT_AVAILABLE"
    SPACE_ALREADY_BOOKED = "SPACE_ALREADY_BOOKED"
    NO_SPACE_AVAILABLE = "NO_SPACE_AVAILABLE"
    RESERVATION_NOT_CANCELLABLE = "RESERVATION_NOT_CANCELLABLE"
    RESERVATION_TOO_EARLY = "RESERVATION_TOO_EARLY"
    RESERVATION_EXPIRED = "RESERVATION_EXPIRED"
    RESERVATION_ALREADY_ACTIVE = "RESERVATION_ALREADY_ACTIVE"
    RESERVATION_NOT_ACTIVE = "RESERVATION_NOT_ACTIVE"
    SESSION_NOT_ACTIVE = "SESSION_NOT_ACTIVE"
    SESSION_ALREADY_CLOSED = "SESSION_ALREADY_CLOSED"


# --- Small helpers -----------------------------------------------------------


def _oid(value: str | ObjectId | None) -> ObjectId | None:
    if isinstance(value, ObjectId):
        return value
    if value is None:
        return None
    return to_object_id(value)


def _required_oid(value: str | ObjectId, message: str = RESERVATION_NOT_FOUND) -> ObjectId:
    object_id = _oid(value)
    if object_id is None:
        raise NotFoundError(message)
    return object_id


def _stored_utc(value: datetime | None) -> datetime | None:
    """Mongo returns naive UTC datetimes; normalise before comparing."""
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _facility_oids(facility_ids: frozenset[str]) -> list[ObjectId]:
    return [
        object_id
        for object_id in (_oid(value) for value in facility_ids)
        if object_id is not None
    ]


def _scope_query(
    *, user_id: str, role: Role, facility_ids: frozenset[str]
) -> dict[str, Any]:
    """The part of a reservation/session query that enforces read scope."""
    if role is Role.DRIVER:
        return {"userId": _required_oid(user_id)}
    if role is Role.STAFF:
        return {"facilityId": {"$in": _facility_oids(facility_ids)}}
    if role is Role.OPERATOR:
        return {"operatorId": _required_oid(user_id)}
    return {}


def _can_see(
    document: dict[str, Any],
    *,
    user_id: str,
    role: Role,
    facility_ids: frozenset[str],
) -> bool:
    if role is Role.ADMIN:
        return True
    if role is Role.DRIVER:
        return str(document.get("userId")) == user_id
    if role is Role.STAFF:
        return str(document.get("facilityId")) in facility_ids
    return str(document.get("operatorId")) == user_id


def _validate_interval(
    start: datetime,
    end: datetime,
    *,
    settings: Settings,
    now: datetime,
    allow_past: bool = False,
) -> int:
    """Check the booking grid and return the duration in minutes.

    The 30-minute grid is what makes the slot rows an exact representation of
    the interval: if a driver could ask for 10:07-11:07 the engine would have
    to round somewhere and the confirmation would not match the request.
    """
    start = _stored_utc(start) or start
    end = _stored_utc(end) or end
    if end <= start:
        raise AppError(
            "endTime must be after startTime",
            code=ErrorCode.VALIDATION_ERROR,
            status_code=422,
        )
    step = settings.reservation_slot_minutes
    for label, value in (("startTime", start), ("endTime", end)):
        if value.second or value.microsecond or value.minute % step:
            raise AppError(
                f"{label} must be aligned to a {step}-minute slot",
                code=ErrorCode.VALIDATION_ERROR,
                status_code=422,
            )
    minutes = int((end - start).total_seconds() // 60)
    if minutes < settings.reservation_min_minutes:
        raise AppError(
            f"A reservation must last at least {settings.reservation_min_minutes} minutes",
            code=ErrorCode.VALIDATION_ERROR,
            status_code=422,
        )
    if minutes > settings.reservation_max_minutes:
        raise AppError(
            f"A reservation may last at most {settings.reservation_max_minutes // 60} hours",
            code=ErrorCode.VALIDATION_ERROR,
            status_code=422,
        )
    if not allow_past:
        if start < now - timedelta(minutes=5):
            raise AppError(
                "A reservation cannot start in the past",
                code=ErrorCode.VALIDATION_ERROR,
                status_code=422,
            )
        latest = now + timedelta(days=settings.reservation_max_advance_days)
        if start > latest:
            raise AppError(
                "A reservation cannot start more than "
                f"{settings.reservation_max_advance_days} days ahead",
                code=ErrorCode.VALIDATION_ERROR,
                status_code=422,
            )
    return minutes


def _estimate_amount(facility: dict[str, Any], minutes: int) -> float | None:
    """Base-rate estimate; `None` when the facility has not published a rate.

    The daily cap is applied because a long stay should not exceed the posted
    day rate. This is explicitly *not* a payment: pricing rules and payments are
    later modules, and the response says `amountEstimated: true`.
    """
    hourly = facility.get("hourlyRate")
    if hourly is None:
        return None
    amount = float(hourly) * (minutes / 60)
    daily_max = facility.get("dailyMax")
    if daily_max is not None:
        amount = min(amount, float(daily_max))
    return round(amount, 2)


async def _facility(db: AsyncDatabase, facility_id: str | ObjectId) -> dict[str, Any]:
    object_id = _required_oid(facility_id, FACILITY_NOT_FOUND)
    document = await db[FACILITIES_COLLECTION].find_one({"_id": object_id})
    if document is None:
        raise NotFoundError(FACILITY_NOT_FOUND)
    return document


async def _active_facility(
    db: AsyncDatabase, facility_id: str | ObjectId
) -> dict[str, Any]:
    document = await _facility(db, facility_id)
    if document.get("status") != FacilityStatus.ACTIVE.value:
        raise ConflictError(
            "This facility is not accepting reservations",
            code=ReservationErrorCode.FACILITY_NOT_ACCEPTING,
            details={"status": document.get("status")},
        )
    return document


async def _owned_vehicle(
    db: AsyncDatabase, *, user_id: str, vehicle_id: str
) -> dict[str, Any]:
    object_id = _required_oid(vehicle_id, VEHICLE_NOT_FOUND)
    document = await db[VEHICLES_COLLECTION].find_one(
        {"_id": object_id, "userId": _required_oid(user_id)}
    )
    if document is None:
        raise NotFoundError(VEHICLE_NOT_FOUND)
    return document


async def _active_zone_ids(
    db: AsyncDatabase, facility_id: ObjectId
) -> list[ObjectId]:
    cursor = db[ZONES_COLLECTION].find(
        {"facilityId": facility_id, "status": ZoneStatus.ACTIVE.value}, {"_id": 1}
    )
    return [row["_id"] async for row in cursor]


def _space_query(
    facility_id: ObjectId,
    zone_ids: list[ObjectId],
    *,
    space_id: ObjectId | None = None,
) -> dict[str, Any]:
    query: dict[str, Any] = {
        "facilityId": facility_id,
        "status": SpaceStatus.AVAILABLE.value,
        "reservable": True,
        "zoneId": {"$in": zone_ids},
    }
    if space_id is not None:
        query["_id"] = space_id
    return query


async def _busy_space_ids(
    db: AsyncDatabase,
    *,
    facility_id: ObjectId,
    start: datetime,
    end: datetime,
    exclude_reservation_id: ObjectId | None = None,
) -> set[ObjectId]:
    query: dict[str, Any] = {
        "facilityId": facility_id,
        "slotStart": {"$gte": start, "$lt": end},
    }
    if exclude_reservation_id is not None:
        query["reservationId"] = {"$ne": exclude_reservation_id}
    cursor = db[RESERVATION_SLOTS_COLLECTION].find(query, {"spaceId": 1})
    return {row["spaceId"] async for row in cursor}


async def _claim_slots(
    db: AsyncDatabase,
    *,
    reservation_id: ObjectId,
    facility_id: ObjectId,
    space: dict[str, Any],
    start: datetime,
    end: datetime,
    minutes: int,
) -> bool:
    """Insert the slot rows for one space. False means another booking won."""
    documents = [
        {
            "_id": ObjectId(),
            "reservationId": reservation_id,
            "facilityId": facility_id,
            "spaceId": space["_id"],
            "slotStart": slot_start,
        }
        for slot_start in slot_starts(start, end, minutes)
    ]
    try:
        await db[RESERVATION_SLOTS_COLLECTION].insert_many(documents, ordered=True)
    except DuplicateKeyError:
        # `insert_many` is not transactional: an earlier row may have been
        # inserted before the duplicate was hit. Remove this reservation's
        # partial claim before trying the next space.
        await db[RESERVATION_SLOTS_COLLECTION].delete_many(
            {"reservationId": reservation_id}
        )
        return False
    return True


async def _space_has_conflict(
    db: AsyncDatabase,
    *,
    space_id: ObjectId,
    start: datetime,
    end: datetime,
    exclude_reservation_id: ObjectId,
) -> bool:
    document = await db[RESERVATION_SLOTS_COLLECTION].find_one(
        {
            "spaceId": space_id,
            "slotStart": {"$gte": start, "$lt": end},
            "reservationId": {"$ne": exclude_reservation_id},
        },
        {"_id": 1},
    )
    return document is not None


async def _release_slots(db: AsyncDatabase, reservation_id: ObjectId) -> None:
    await db[RESERVATION_SLOTS_COLLECTION].delete_many(
        {"reservationId": reservation_id}
    )


async def _load_visible_reservation(
    db: AsyncDatabase,
    *,
    user_id: str,
    role: Role,
    facility_ids: frozenset[str],
    reservation_id: str,
) -> dict[str, Any]:
    object_id = _required_oid(reservation_id)
    document = await db[RESERVATIONS_COLLECTION].find_one({"_id": object_id})
    if document is None or not _can_see(
        document, user_id=user_id, role=role, facility_ids=facility_ids
    ):
        raise NotFoundError(RESERVATION_NOT_FOUND)
    return document


async def _load_visible_session(
    db: AsyncDatabase,
    *,
    user_id: str,
    role: Role,
    facility_ids: frozenset[str],
    session_id: str,
) -> dict[str, Any]:
    object_id = _required_oid(session_id, SESSION_NOT_FOUND)
    document = await db[SESSIONS_COLLECTION].find_one({"_id": object_id})
    if document is None or not _can_see(
        document, user_id=user_id, role=role, facility_ids=facility_ids
    ):
        raise NotFoundError(SESSION_NOT_FOUND)
    return document


# --- Availability ------------------------------------------------------------


async def availability(
    db: AsyncDatabase,
    *,
    user_id: str,
    role: Role,
    facility_ids: frozenset[str],
    facility_id: str,
    start: datetime,
    end: datetime,
    settings: Settings,
    space_type: str | None = None,
) -> dict[str, Any]:
    """How many reservable spaces are free for the whole interval."""
    start = _stored_utc(start) or start
    end = _stored_utc(end) or end
    if end <= start:
        raise AppError(
            "endTime must be after startTime",
            code=ErrorCode.VALIDATION_ERROR,
            status_code=422,
        )
    if role is Role.DRIVER:
        facility = await _active_facility(db, facility_id)
    else:
        facility = await visible_facility(
            db,
            user_id=user_id,
            role=role,
            facility_ids=facility_ids,
            facility_id=facility_id,
        )
    facility_oid = facility["_id"]
    zone_ids = await _active_zone_ids(db, facility_oid)
    query: dict[str, Any] = {
        "facilityId": facility_oid,
        "status": SpaceStatus.AVAILABLE.value,
        "reservable": True,
        "zoneId": {"$in": zone_ids},
    }
    if space_type is not None:
        query["type"] = space_type
    busy = await _busy_space_ids(
        db, facility_id=facility_oid, start=start, end=end
    )
    total = 0
    available = 0
    async for space in db[SPACES_COLLECTION].find(query, {"_id": 1}):
        total += 1
        if space["_id"] not in busy:
            available += 1
    return {
        "facilityId": str(facility_oid),
        "startTime": start,
        "endTime": end,
        "totalReservable": total,
        "availableSpaces": available,
        "availabilityAsOf": utcnow(),
    }


# --- Create / read / cancel --------------------------------------------------


async def create_reservation(
    db: AsyncDatabase,
    *,
    user_id: str,
    payload: CreateReservationRequest,
    settings: Settings,
) -> dict[str, Any]:
    """Book a concrete space and claim its slots."""
    now = utcnow()
    start = _stored_utc(payload.startTime) or payload.startTime
    end = _stored_utc(payload.endTime) or payload.endTime
    minutes = _validate_interval(start, end, settings=settings, now=now)

    facility = await _active_facility(db, payload.facilityId)
    vehicle = await _owned_vehicle(db, user_id=user_id, vehicle_id=payload.vehicleId)
    facility_oid = facility["_id"]
    zone_ids = await _active_zone_ids(db, facility_oid)
    if not zone_ids:
        raise ConflictError(
            "This facility has no open zones to reserve",
            code=ReservationErrorCode.NO_SPACE_AVAILABLE,
        )

    explicit_space_id = _oid(payload.spaceId) if payload.spaceId else None
    if payload.spaceId and explicit_space_id is None:
        raise NotFoundError(SPACE_NOT_FOUND)
    busy = await _busy_space_ids(
        db, facility_id=facility_oid, start=start, end=end
    )
    if explicit_space_id is not None:
        space = await db[SPACES_COLLECTION].find_one(
            _space_query(facility_oid, zone_ids, space_id=explicit_space_id)
        )
        if space is None:
            raise ConflictError(
                "That space is not available for the selected time",
                code=ReservationErrorCode.SPACE_NOT_AVAILABLE,
            )
        if space["_id"] in busy:
            raise ConflictError(
                "That space is already booked for the selected time",
                code=ReservationErrorCode.SPACE_ALREADY_BOOKED,
            )
        candidates: list[dict[str, Any]] = [space]
    else:
        candidates = []
        cursor = db[SPACES_COLLECTION].find(
            _space_query(facility_oid, zone_ids), {"_id": 1, "spaceNumber": 1}
        ).sort("spaceNumber", 1)
        async for space in cursor:
            if space["_id"] not in busy:
                candidates.append(space)

    reservation_oid = ObjectId()
    chosen: dict[str, Any] | None = None
    for candidate in candidates:
        if await _claim_slots(
            db,
            reservation_id=reservation_oid,
            facility_id=facility_oid,
            space=candidate,
            start=start,
            end=end,
            minutes=settings.reservation_slot_minutes,
        ):
            chosen = candidate
            break
    if chosen is None:
        code = (
            ReservationErrorCode.SPACE_ALREADY_BOOKED
            if explicit_space_id is not None
            else ReservationErrorCode.NO_SPACE_AVAILABLE
        )
        raise ConflictError(
            "No space is available for the selected time",
            code=code,
        )

    document: dict[str, Any] = {
        "_id": reservation_oid,
        "reference": reservation_reference(),
        "qrToken": qr_token(),
        "userId": _required_oid(user_id),
        "vehicleId": vehicle["_id"],
        "vehiclePlate": vehicle.get("registrationNumber"),
        "facilityId": facility_oid,
        "facilityName": facility.get("name"),
        "operatorId": facility.get("operatorId"),
        "spaceId": chosen["_id"],
        "spaceNumber": chosen.get("spaceNumber"),
        "startTime": start,
        "endTime": end,
        "status": ReservationStatus.CONFIRMED.value,
        "estimatedAmount": _estimate_amount(facility, minutes),
        "checkedInAt": None,
        "checkedOutAt": None,
        "cancelledAt": None,
        "cancelReason": None,
        "sessionId": None,
        "createdAt": now,
        "updatedAt": now,
    }

    for _ in range(5):
        try:
            await db[RESERVATIONS_COLLECTION].insert_one(document)
            break
        except DuplicateKeyError:
            # A reference or QR token collision is astronomically unlikely, but
            # the unique indexes make it possible; mint fresh values and retry.
            document["reference"] = reservation_reference()
            document["qrToken"] = qr_token()
    else:
        await _release_slots(db, reservation_oid)
        raise ConflictError(
            "Could not allocate a booking reference. Please try again.",
            code=ReservationErrorCode.NO_SPACE_AVAILABLE,
        )

    logger.info(
        "Reservation %s created for user %s at facility %s (%s)",
        document["reference"],
        user_id,
        facility_oid,
        chosen.get("spaceNumber"),
    )
    return document


async def list_reservations(
    db: AsyncDatabase,
    *,
    user_id: str,
    role: Role,
    facility_ids: frozenset[str],
    status: ReservationStatus | None = None,
    facility_id: str | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    q: str | None = None,
    page: int = 1,
    limit: int = 20,
    order: str = "asc",
    settings: Settings,
) -> tuple[list[dict[str, Any]], int]:
    await sweep_reservations(db, settings=settings)
    query = _scope_query(user_id=user_id, role=role, facility_ids=facility_ids)
    if facility_id is not None:
        if role is not Role.DRIVER:
            await visible_facility(
                db,
                user_id=user_id,
                role=role,
                facility_ids=facility_ids,
                facility_id=facility_id,
            )
        query["facilityId"] = _required_oid(facility_id, FACILITY_NOT_FOUND)
    if status is not None:
        query["status"] = status.value
    if start is not None or end is not None:
        window: dict[str, Any] = {}
        if start is not None:
            window["$gte"] = start
        if end is not None:
            window["$lt"] = end
        query["startTime"] = window
    if q:
        pattern = re.escape(q.strip())
        query["$or"] = [
            {"reference": {"$regex": pattern, "$options": "i"}},
            {"qrToken": {"$regex": pattern, "$options": "i"}},
            {"vehiclePlate": {"$regex": pattern, "$options": "i"}},
        ]

    direction = -1 if order.strip().lower() == "desc" else 1
    total = await db[RESERVATIONS_COLLECTION].count_documents(query)
    cursor = (
        db[RESERVATIONS_COLLECTION]
        .find(query)
        .sort([("startTime", direction), ("_id", direction)])
        .skip((page - 1) * limit)
        .limit(limit)
    )
    rows = [row async for row in cursor]
    return rows, total


async def get_reservation(
    db: AsyncDatabase,
    *,
    user_id: str,
    role: Role,
    facility_ids: frozenset[str],
    reservation_id: str,
    settings: Settings,
) -> dict[str, Any]:
    await sweep_reservations(db, settings=settings)
    return await _load_visible_reservation(
        db,
        user_id=user_id,
        role=role,
        facility_ids=facility_ids,
        reservation_id=reservation_id,
    )


async def cancel_reservation(
    db: AsyncDatabase,
    *,
    user_id: str,
    role: Role,
    facility_ids: frozenset[str],
    reservation_id: str,
    reason: str | None,
) -> dict[str, Any]:
    document = await _load_visible_reservation(
        db,
        user_id=user_id,
        role=role,
        facility_ids=facility_ids,
        reservation_id=reservation_id,
    )
    if document.get("status") != ReservationStatus.CONFIRMED.value:
        raise ConflictError(
            "Only a confirmed reservation can be cancelled",
            code=ReservationErrorCode.RESERVATION_NOT_CANCELLABLE,
            details={"status": document.get("status")},
        )
    now = utcnow()
    updated = await db[RESERVATIONS_COLLECTION].find_one_and_update(
        {"_id": document["_id"], "status": ReservationStatus.CONFIRMED.value},
        {
            "$set": {
                "status": ReservationStatus.CANCELLED.value,
                "cancelledAt": now,
                "cancelReason": reason,
                "updatedAt": now,
            }
        },
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise ConflictError(
            "This reservation has already changed state",
            code=ReservationErrorCode.RESERVATION_NOT_CANCELLABLE,
        )
    await _release_slots(db, document["_id"])
    await db[SPACES_COLLECTION].update_one(
        {"_id": document["spaceId"], "status": SpaceStatus.RESERVED.value},
        {"$set": {"status": SpaceStatus.AVAILABLE.value, "updatedAt": now}},
    )
    logger.info("Reservation %s cancelled", document.get("reference"))
    return updated


async def lookup_reservation(
    db: AsyncDatabase,
    *,
    user_id: str,
    role: Role,
    facility_ids: frozenset[str],
    code: str,
) -> dict[str, Any]:
    """Staff lookup by reference or QR token, still scoped to their facilities."""
    value = code.strip()
    query: dict[str, Any] = {
        "$or": [
            {"reference": normalise_reference(value)},
            {"qrToken": value},
        ]
    }
    query.update(_scope_query(user_id=user_id, role=role, facility_ids=facility_ids))
    document = await db[RESERVATIONS_COLLECTION].find_one(query)
    if document is None:
        raise NotFoundError(RESERVATION_NOT_FOUND)
    return document


# --- Check-in / check-out ----------------------------------------------------


async def _checkin_space(
    db: AsyncDatabase,
    *,
    reservation: dict[str, Any],
    override_space_id: str | None,
) -> dict[str, Any]:
    facility_oid = reservation["facilityId"]
    start = _stored_utc(reservation["startTime"]) or reservation["startTime"]
    end = _stored_utc(reservation["endTime"]) or reservation["endTime"]
    if override_space_id is not None:
        space_oid = _required_oid(override_space_id, SPACE_NOT_FOUND)
        space = await db[SPACES_COLLECTION].find_one(
            {"_id": space_oid, "facilityId": facility_oid, "reservable": True}
        )
        if space is None:
            raise NotFoundError(SPACE_NOT_FOUND)
        if space.get("status") != SpaceStatus.AVAILABLE.value:
            raise ConflictError(
                "That space is not available",
                code=ReservationErrorCode.SPACE_NOT_AVAILABLE,
                details={"status": space.get("status")},
            )
        if await _space_has_conflict(
            db,
            space_id=space_oid,
            start=start,
            end=end,
            exclude_reservation_id=reservation["_id"],
        ):
            raise ConflictError(
                "That space is already booked for this time",
                code=ReservationErrorCode.SPACE_ALREADY_BOOKED,
            )
        return space

    booked = await db[SPACES_COLLECTION].find_one({"_id": reservation["spaceId"]})
    if booked is not None and booked.get("status") == SpaceStatus.AVAILABLE.value:
        return booked

    busy = await _busy_space_ids(
        db,
        facility_id=facility_oid,
        start=start,
        end=end,
        exclude_reservation_id=reservation["_id"],
    )
    zone_ids = await _active_zone_ids(db, facility_oid)
    cursor = db[SPACES_COLLECTION].find(
        _space_query(facility_oid, zone_ids), {"_id": 1, "spaceNumber": 1}
    ).sort("spaceNumber", 1)
    async for space in cursor:
        if space["_id"] not in busy:
            return space
    raise ConflictError(
        "No space is available for this booking",
        code=ReservationErrorCode.NO_SPACE_AVAILABLE,
    )


async def check_in(
    db: AsyncDatabase,
    *,
    user_id: str,
    role: Role,
    facility_ids: frozenset[str],
    reservation_id: str,
    override_space_id: str | None,
    closed_by_id: str,
    settings: Settings,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Start a parking session for a confirmed booking."""
    await sweep_reservations(db, settings=settings)
    reservation = await _load_visible_reservation(
        db,
        user_id=user_id,
        role=role,
        facility_ids=facility_ids,
        reservation_id=reservation_id,
    )
    status = reservation.get("status")
    if status == ReservationStatus.ACTIVE.value:
        raise ConflictError(
            "This reservation is already checked in",
            code=ReservationErrorCode.RESERVATION_ALREADY_ACTIVE,
            details={"sessionId": str(reservation.get("sessionId") or "")},
        )
    if status != ReservationStatus.CONFIRMED.value:
        raise ConflictError(
            "Only a confirmed reservation can be checked in",
            code=ReservationErrorCode.RESERVATION_NOT_ACTIVE,
            details={"status": status},
        )

    now = utcnow()
    start = _stored_utc(reservation["startTime"]) or reservation["startTime"]
    end = _stored_utc(reservation["endTime"]) or reservation["endTime"]
    if now < start - timedelta(minutes=settings.reservation_checkin_early_minutes):
        raise ConflictError(
            "It is too early to check in for this reservation",
            code=ReservationErrorCode.RESERVATION_TOO_EARLY,
            details={"startTime": start.isoformat()},
        )
    if now > end:
        raise ConflictError(
            "This reservation has expired",
            code=ReservationErrorCode.RESERVATION_EXPIRED,
            details={"endTime": end.isoformat()},
        )

    space = await _checkin_space(
        db, reservation=reservation, override_space_id=override_space_id
    )
    occupied = await db[SPACES_COLLECTION].find_one_and_update(
        {
            "_id": space["_id"],
            "status": {
                "$in": [SpaceStatus.AVAILABLE.value, SpaceStatus.RESERVED.value]
            },
        },
        {"$set": {"status": SpaceStatus.OCCUPIED.value, "updatedAt": now}},
        return_document=ReturnDocument.AFTER,
    )
    if occupied is None:
        raise ConflictError(
            "That space is no longer available",
            code=ReservationErrorCode.SPACE_NOT_AVAILABLE,
        )

    updated = await db[RESERVATIONS_COLLECTION].find_one_and_update(
        {"_id": reservation["_id"], "status": ReservationStatus.CONFIRMED.value},
        {
            "$set": {
                "status": ReservationStatus.ACTIVE.value,
                "checkedInAt": now,
                "spaceId": space["_id"],
                "spaceNumber": space.get("spaceNumber"),
                "updatedAt": now,
            }
        },
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        await db[SPACES_COLLECTION].update_one(
            {"_id": space["_id"], "status": SpaceStatus.OCCUPIED.value},
            {"$set": {"status": SpaceStatus.AVAILABLE.value, "updatedAt": now}},
        )
        raise ConflictError(
            "This reservation has already changed state",
            code=ReservationErrorCode.RESERVATION_NOT_ACTIVE,
        )

    session_document: dict[str, Any] = {
        "_id": ObjectId(),
        "reservationId": reservation["_id"],
        "reference": reservation.get("reference"),
        "userId": reservation["userId"],
        "vehicleId": reservation["vehicleId"],
        "vehiclePlate": reservation.get("vehiclePlate"),
        "facilityId": reservation["facilityId"],
        "facilityName": reservation.get("facilityName"),
        "operatorId": reservation.get("operatorId"),
        "spaceId": space["_id"],
        "spaceNumber": space.get("spaceNumber"),
        "entryTime": now,
        "exitTime": None,
        "status": SessionStatus.ACTIVE.value,
        "durationMinutes": 0,
        "amount": None,
        "amountEstimated": True,
        "overstayMinutes": 0,
        "closedById": _oid(closed_by_id),
        "createdAt": now,
        "updatedAt": now,
    }
    try:
        await db[SESSIONS_COLLECTION].insert_one(session_document)
    except DuplicateKeyError:
        await db[RESERVATIONS_COLLECTION].update_one(
            {
                "_id": reservation["_id"],
                "status": ReservationStatus.ACTIVE.value,
            },
            {
                "$set": {
                    "status": ReservationStatus.CONFIRMED.value,
                    "checkedInAt": None,
                    "updatedAt": now,
                }
            },
        )
        await db[SPACES_COLLECTION].update_one(
            {"_id": space["_id"], "status": SpaceStatus.OCCUPIED.value},
            {"$set": {"status": SpaceStatus.AVAILABLE.value, "updatedAt": now}},
        )
        raise ConflictError(
            "This reservation is already checked in",
            code=ReservationErrorCode.RESERVATION_ALREADY_ACTIVE,
        ) from None

    if space["_id"] != reservation["spaceId"]:
        # The vehicle moved to a different space. Move the slot claim with it so
        # availability reflects where the car actually is, and so the original
        # space can be booked for this interval once the session ends.
        await _release_slots(db, reservation["_id"])
        await _claim_slots(
            db,
            reservation_id=reservation["_id"],
            facility_id=reservation["facilityId"],
            space=space,
            start=start,
            end=end,
            minutes=settings.reservation_slot_minutes,
        )

    updated = await db[RESERVATIONS_COLLECTION].find_one_and_update(
        {"_id": reservation["_id"]},
        {"$set": {"sessionId": session_document["_id"], "updatedAt": now}},
        return_document=ReturnDocument.AFTER,
    )
    logger.info(
        "Reservation %s checked in to space %s",
        reservation.get("reference"),
        space.get("spaceNumber"),
    )
    return updated or {**reservation, "sessionId": session_document["_id"]}, session_document


async def _close_session_document(
    db: AsyncDatabase,
    *,
    session: dict[str, Any],
    closed_by_id: str,
    now: datetime,
) -> tuple[dict[str, Any], dict[str, Any]]:
    status = session.get("status")
    if status == SessionStatus.COMPLETED.value:
        raise ConflictError(
            "This parking session is already closed",
            code=ReservationErrorCode.SESSION_ALREADY_CLOSED,
        )
    if status not in (SessionStatus.ACTIVE.value, SessionStatus.EXTENDED.value):
        raise ConflictError(
            "This parking session is not active",
            code=ReservationErrorCode.SESSION_NOT_ACTIVE,
            details={"status": status},
        )

    reservation = await db[RESERVATIONS_COLLECTION].find_one(
        {"_id": session["reservationId"]}
    )
    entry = _stored_utc(session.get("entryTime")) or now
    exit_time = now if now > entry else entry
    minutes = max(0, int((exit_time - entry).total_seconds() // 60))
    facility = await db[FACILITIES_COLLECTION].find_one({"_id": session["facilityId"]})
    amount = _estimate_amount(facility, minutes) if facility is not None else None
    overstay = 0
    if reservation is not None:
        end = _stored_utc(reservation.get("endTime"))
        if end is not None and exit_time > end:
            overstay = max(0, int((exit_time - end).total_seconds() // 60))

    updated_session = await db[SESSIONS_COLLECTION].find_one_and_update(
        {"_id": session["_id"], "status": status},
        {
            "$set": {
                "status": SessionStatus.COMPLETED.value,
                "exitTime": exit_time,
                "durationMinutes": minutes,
                "amount": amount,
                "amountEstimated": True,
                "overstayMinutes": overstay,
                "closedById": _oid(closed_by_id),
                "updatedAt": exit_time,
            }
        },
        return_document=ReturnDocument.AFTER,
    )
    if updated_session is None:
        raise ConflictError(
            "This parking session has already changed state",
            code=ReservationErrorCode.SESSION_ALREADY_CLOSED,
        )

    updated_reservation: dict[str, Any] | None = None
    if reservation is not None:
        updated_reservation = await db[RESERVATIONS_COLLECTION].find_one_and_update(
            {
                "_id": reservation["_id"],
                "status": {
                    "$in": [
                        ReservationStatus.ACTIVE.value,
                        ReservationStatus.ARRIVED.value,
                    ]
                },
            },
            {
                "$set": {
                    "status": ReservationStatus.COMPLETED.value,
                    "checkedOutAt": exit_time,
                    "updatedAt": exit_time,
                }
            },
            return_document=ReturnDocument.AFTER,
        )
        await _release_slots(db, reservation["_id"])
    await db[SPACES_COLLECTION].update_one(
        {"_id": session["spaceId"], "status": SpaceStatus.OCCUPIED.value},
        {"$set": {"status": SpaceStatus.AVAILABLE.value, "updatedAt": exit_time}},
    )
    logger.info("Parking session %s closed", session["_id"])
    return updated_reservation or reservation or {}, updated_session


async def check_out(
    db: AsyncDatabase,
    *,
    user_id: str,
    role: Role,
    facility_ids: frozenset[str],
    reservation_id: str,
    closed_by_id: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Close the session opened for a reservation."""
    reservation = await _load_visible_reservation(
        db,
        user_id=user_id,
        role=role,
        facility_ids=facility_ids,
        reservation_id=reservation_id,
    )
    session = await db[SESSIONS_COLLECTION].find_one(
        {"reservationId": reservation["_id"]}
    )
    if session is None:
        raise NotFoundError(SESSION_NOT_FOUND)
    closed_reservation, closed_session = await _close_session_document(
        db, session=session, closed_by_id=closed_by_id, now=utcnow()
    )
    if not closed_reservation:
        closed_reservation = reservation
    return closed_reservation, closed_session


async def list_sessions(
    db: AsyncDatabase,
    *,
    user_id: str,
    role: Role,
    facility_ids: frozenset[str],
    status: SessionStatus | None = None,
    facility_id: str | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    q: str | None = None,
    page: int = 1,
    limit: int = 20,
    order: str = "desc",
) -> tuple[list[dict[str, Any]], int]:
    query = _scope_query(user_id=user_id, role=role, facility_ids=facility_ids)
    if facility_id is not None:
        if role is not Role.DRIVER:
            await visible_facility(
                db,
                user_id=user_id,
                role=role,
                facility_ids=facility_ids,
                facility_id=facility_id,
            )
        query["facilityId"] = _required_oid(facility_id, FACILITY_NOT_FOUND)
    if status is not None:
        query["status"] = status.value
    if start is not None or end is not None:
        window: dict[str, Any] = {}
        if start is not None:
            window["$gte"] = start
        if end is not None:
            window["$lt"] = end
        query["entryTime"] = window
    if q:
        pattern = re.escape(q.strip())
        query["$or"] = [
            {"reference": {"$regex": pattern, "$options": "i"}},
            {"vehiclePlate": {"$regex": pattern, "$options": "i"}},
        ]

    direction = -1 if order.strip().lower() != "asc" else 1
    total = await db[SESSIONS_COLLECTION].count_documents(query)
    cursor = (
        db[SESSIONS_COLLECTION]
        .find(query)
        .sort([("entryTime", direction), ("_id", direction)])
        .skip((page - 1) * limit)
        .limit(limit)
    )
    rows = [row async for row in cursor]
    return rows, total


async def get_session(
    db: AsyncDatabase,
    *,
    user_id: str,
    role: Role,
    facility_ids: frozenset[str],
    session_id: str,
) -> dict[str, Any]:
    return await _load_visible_session(
        db,
        user_id=user_id,
        role=role,
        facility_ids=facility_ids,
        session_id=session_id,
    )


async def close_session(
    db: AsyncDatabase,
    *,
    user_id: str,
    role: Role,
    facility_ids: frozenset[str],
    session_id: str,
    closed_by_id: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    session = await _load_visible_session(
        db,
        user_id=user_id,
        role=role,
        facility_ids=facility_ids,
        session_id=session_id,
    )
    return await _close_session_document(
        db, session=session, closed_by_id=closed_by_id, now=utcnow()
    )


async def active_session(
    db: AsyncDatabase, *, user_id: str
) -> dict[str, Any] | None:
    """The driver's currently open session, if any."""
    return await db[SESSIONS_COLLECTION].find_one(
        {
            "userId": _required_oid(user_id),
            "status": {"$in": [SessionStatus.ACTIVE.value, SessionStatus.EXTENDED.value]},
        },
        sort=[("entryTime", -1)],
    )


# --- Expiry / no-show sweep --------------------------------------------------


async def sweep_reservations(
    db: AsyncDatabase, *, settings: Settings, now: datetime | None = None
) -> int:
    """Expire past bookings and release unclaimed no-shows.

    Runs lazily on the read/check-in paths and from the background task in
    `app.main`. Every update is conditional on the old status, so two sweepers
    racing cannot double-count or resurrect a booking.
    """
    moment = now or utcnow()
    grace = timedelta(minutes=settings.reservation_no_show_grace_minutes)
    changed = 0

    expired_ids: list[ObjectId] = []
    cursor = db[RESERVATIONS_COLLECTION].find(
        {
            "status": ReservationStatus.CONFIRMED.value,
            "endTime": {"$lt": moment},
        },
        {"_id": 1},
    )
    async for row in cursor:
        expired_ids.append(row["_id"])
    if expired_ids:
        result = await db[RESERVATIONS_COLLECTION].update_many(
            {
                "_id": {"$in": expired_ids},
                "status": ReservationStatus.CONFIRMED.value,
            },
            {
                "$set": {
                    "status": ReservationStatus.EXPIRED.value,
                    "updatedAt": moment,
                }
            },
        )
        changed += result.modified_count
        await db[RESERVATION_SLOTS_COLLECTION].delete_many(
            {"reservationId": {"$in": expired_ids}}
        )

    no_show_ids: list[ObjectId] = []
    cursor = db[RESERVATIONS_COLLECTION].find(
        {
            "status": ReservationStatus.CONFIRMED.value,
            "startTime": {"$lt": moment - grace},
            "endTime": {"$gte": moment},
        },
        {"_id": 1},
    )
    async for row in cursor:
        no_show_ids.append(row["_id"])
    if no_show_ids:
        result = await db[RESERVATIONS_COLLECTION].update_many(
            {
                "_id": {"$in": no_show_ids},
                "status": ReservationStatus.CONFIRMED.value,
            },
            {
                "$set": {
                    "status": ReservationStatus.NO_SHOW.value,
                    "updatedAt": moment,
                }
            },
        )
        changed += result.modified_count
        await db[RESERVATION_SLOTS_COLLECTION].delete_many(
            {"reservationId": {"$in": no_show_ids}}
        )

    if changed:
        logger.info("Reservation sweep changed %d booking(s)", changed)
    return changed


def serialise_reservation(document: dict[str, Any]) -> dict[str, Any]:
    return public_reservation(document)


def serialise_session(document: dict[str, Any]) -> dict[str, Any]:
    return public_session(document)


__all__ = [
    "ReservationErrorCode",
    "active_session",
    "availability",
    "cancel_reservation",
    "check_in",
    "check_out",
    "close_session",
    "create_reservation",
    "get_reservation",
    "get_session",
    "list_reservations",
    "list_sessions",
    "lookup_reservation",
    "serialise_reservation",
    "serialise_session",
    "sweep_reservations",
]
