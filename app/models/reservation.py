"""The `reservations` and `reservationSlots` collections (DOC sections 16-18).

A reservation is a *booking for a concrete space over a concrete interval*. The
two facts that make it safe are split across two collections:

* `reservations` holds the booking the driver and staff see - reference, QR
  token, vehicle snapshot, times, status and the estimated base-rate amount.
* `reservationSlots` holds one row per 30-minute slot the booking occupies.
  A unique index on `(spaceId, slotStart)` is what actually prevents two
  overlapping bookings from claiming the same space. The check runs in the
  database, not in Python, so two simultaneous requests cannot both win.

The slot grid is deliberate: Module 4 requires "the same space cannot be booked
for an overlapping time interval" while still allowing the same space to be
booked at 10:00-13:00 and 14:00-16:00. A single `RESERVED` status on the space
could not express that; interval rows can.

The `operatorId` snapshot is copied from the facility at creation. It lets the
operator listing filter by ownership without a join, the same way staff
assignments carry their owner.
"""

from __future__ import annotations

import re
import secrets
from datetime import datetime, timedelta
from typing import Any, TypedDict

from bson import ObjectId

from app.models.enums import ReservationStatus, SessionStatus
from app.utils.mongo import as_utc

RESERVATIONS_COLLECTION = "reservations"
RESERVATION_SLOTS_COLLECTION = "reservationSlots"
SESSIONS_COLLECTION = "parkingSessions"

# Booking grid. Everything a driver can request is aligned to this step, which
# is what makes the slot rows an exact representation of the interval.
RESERVATION_SLOT_MINUTES = 30
RESERVATION_MIN_DURATION_MINUTES = 30
RESERVATION_MAX_DURATION_MINUTES = 24 * 60
MAX_CANCEL_REASON_LENGTH = 200

_REFERENCE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
_REFERENCE_PATTERN = re.compile(r"^PKY-[A-Z0-9]{6,12}$")


class ReservationDocument(TypedDict):
    _id: ObjectId
    reference: str
    qrToken: str
    userId: ObjectId
    vehicleId: ObjectId
    vehiclePlate: str
    facilityId: ObjectId
    facilityName: str
    operatorId: ObjectId
    spaceId: ObjectId
    spaceNumber: str
    startTime: datetime
    endTime: datetime
    status: str
    estimatedAmount: float | None
    estimatedBreakdown: dict[str, Any] | None
    checkedInAt: datetime | None
    checkedOutAt: datetime | None
    cancelledAt: datetime | None
    cancelReason: str | None
    sessionId: ObjectId | None
    createdAt: datetime
    updatedAt: datetime


class ReservationSlotDocument(TypedDict):
    _id: ObjectId
    reservationId: ObjectId
    facilityId: ObjectId
    spaceId: ObjectId
    slotStart: datetime


class SessionDocument(TypedDict):
    _id: ObjectId
    reservationId: ObjectId
    reference: str
    userId: ObjectId
    vehicleId: ObjectId
    vehiclePlate: str
    facilityId: ObjectId
    facilityName: str
    operatorId: ObjectId
    spaceId: ObjectId
    spaceNumber: str
    entryTime: datetime
    exitTime: datetime | None
    scheduledEndTime: datetime
    status: str
    durationMinutes: int
    amount: float | None
    amountEstimated: bool
    estimatedAmount: float | None
    estimatedBreakdown: dict[str, Any] | None
    pricingBreakdown: dict[str, Any] | None
    overstayMinutes: int
    closedById: ObjectId | None
    createdAt: datetime
    updatedAt: datetime


def reservation_reference() -> str:
    """A short, human-readable booking reference such as ``PKY-7KQ4M2``."""
    return "PKY-" + "".join(secrets.choice(_REFERENCE_ALPHABET) for _ in range(6))


def qr_token() -> str:
    """The opaque value encoded in a reservation QR code."""
    return secrets.token_urlsafe(18)


def normalise_reference(value: str) -> str:
    """Upper-case and trim a reference typed by staff."""
    return value.strip().upper()


def reference_is_valid(value: str) -> bool:
    return bool(_REFERENCE_PATTERN.fullmatch(normalise_reference(value)))


def slot_starts(
    start: datetime, end: datetime, minutes: int = RESERVATION_SLOT_MINUTES
) -> list[datetime]:
    """Every slot start covered by ``[start, end)``, at the booking grid step.

    Callers validate that the interval is aligned and positive; this helper
    simply walks it. A one-hour booking yields two slots.
    """
    step = timedelta(minutes=minutes)
    slots: list[datetime] = []
    current = start
    while current < end:
        slots.append(current)
        current += step
    return slots


def _id_or_none(value: Any) -> str | None:
    return str(value) if value is not None else None


def public_reservation(document: dict[str, Any]) -> dict[str, Any]:
    """Project a reservation document for the API.

    `qrCode` is the token itself: the client renders it as a QR image, and the
    staff scanner sends it back verbatim. The token is not a secret the way a
    password is - it is a bearer code for one booking - but it is only ever
    returned to a caller already allowed to see that booking.
    """
    start = document.get("startTime")
    end = document.get("endTime")
    duration = None
    if isinstance(start, datetime) and isinstance(end, datetime):
        duration = max(0, int((end - start).total_seconds() // 60))
    return {
        "id": str(document["_id"]),
        "reference": document.get("reference"),
        "qrCode": document.get("qrToken"),
        "userId": _id_or_none(document.get("userId")),
        "vehicleId": _id_or_none(document.get("vehicleId")),
        "vehiclePlate": document.get("vehiclePlate"),
        "facilityId": _id_or_none(document.get("facilityId")),
        "facilityName": document.get("facilityName"),
        "operatorId": _id_or_none(document.get("operatorId")),
        "spaceId": _id_or_none(document.get("spaceId")),
        "spaceNumber": document.get("spaceNumber"),
        "startTime": as_utc(start),
        "endTime": as_utc(end),
        "durationMinutes": duration,
        "status": document.get("status", ReservationStatus.CONFIRMED.value),
        "estimatedAmount": document.get("estimatedAmount"),
        "estimatedBreakdown": document.get("estimatedBreakdown"),
        "checkedInAt": as_utc(document.get("checkedInAt")),
        "checkedOutAt": as_utc(document.get("checkedOutAt")),
        "cancelledAt": as_utc(document.get("cancelledAt")),
        "cancelReason": document.get("cancelReason"),
        "sessionId": _id_or_none(document.get("sessionId")),
        "createdAt": as_utc(document.get("createdAt")),
        "updatedAt": as_utc(document.get("updatedAt")),
    }


def public_session(document: dict[str, Any]) -> dict[str, Any]:
    """Project a parking session for the API."""
    return {
        "id": str(document["_id"]),
        "reservationId": _id_or_none(document.get("reservationId")),
        "reference": document.get("reference"),
        "userId": _id_or_none(document.get("userId")),
        "vehicleId": _id_or_none(document.get("vehicleId")),
        "vehiclePlate": document.get("vehiclePlate"),
        "facilityId": _id_or_none(document.get("facilityId")),
        "facilityName": document.get("facilityName"),
        "operatorId": _id_or_none(document.get("operatorId")),
        "spaceId": _id_or_none(document.get("spaceId")),
        "spaceNumber": document.get("spaceNumber"),
        "entryTime": as_utc(document.get("entryTime")),
        "exitTime": as_utc(document.get("exitTime")),
        "scheduledEndTime": as_utc(document.get("scheduledEndTime")),
        "status": document.get("status", SessionStatus.ACTIVE.value),
        "durationMinutes": int(document.get("durationMinutes") or 0),
        "amount": document.get("amount"),
        "amountEstimated": bool(document.get("amountEstimated", True)),
        "estimatedAmount": document.get("estimatedAmount"),
        "estimatedBreakdown": document.get("estimatedBreakdown"),
        "pricingBreakdown": document.get("pricingBreakdown"),
        "overstayMinutes": int(document.get("overstayMinutes") or 0),
        "closedById": _id_or_none(document.get("closedById")),
        "createdAt": as_utc(document.get("createdAt")),
        "updatedAt": as_utc(document.get("updatedAt")),
    }


__all__ = [
    "MAX_CANCEL_REASON_LENGTH",
    "RESERVATIONS_COLLECTION",
    "RESERVATION_MAX_DURATION_MINUTES",
    "RESERVATION_MIN_DURATION_MINUTES",
    "RESERVATION_SLOTS_COLLECTION",
    "RESERVATION_SLOT_MINUTES",
    "SESSIONS_COLLECTION",
    "ReservationDocument",
    "ReservationSlotDocument",
    "SessionDocument",
    "normalise_reference",
    "public_reservation",
    "public_session",
    "qr_token",
    "reference_is_valid",
    "reservation_reference",
    "slot_starts",
]
