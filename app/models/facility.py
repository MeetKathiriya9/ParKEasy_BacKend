"""The `parkingFacilities` collection (DOC sections 18/19).

Key fields from the requirements document: `name`, `operatorId`, `address`,
geo location, `capacity`, `hours`, `amenities`. Two deliberate extensions:

* **Base pricing.** `hourlyRate` and `dailyMax` are optional scalars holding the
  facility's default price, so a driver page can show a rate before the full
  pricing-rules group (DOC section 21, `/api/pricing`) exists. When that group
  lands these become "the rule everything else overrides".
* **Structured hours.** DOC asks for `hours` without a shape. `open24h` plus an
  optional `openingTime`/`closingTime` pair ("HH:MM", UTC) is the smallest
  representation that still answers DOC section 20's "find facilities open at a
  requested arrival time". A weekly schedule is deferred to the discovery phase.

`location` is a GeoJSON Point (`[lng, lat]` order, DOC section 19) and carries
the `2dsphere` index declared in `app.db.indexes`; clients send and receive
plain `lat`/`lng` numbers and never see the GeoJSON wrapper.

The `hasEV` / `hasCovered` / `hasAccessible` flags and `isOpenNow` are derived
at the API boundary, exactly like a vehicle's `isEV`: the stored document keeps
one source of truth (`amenities`, `open24h` + times) and the flags can never
disagree with it.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, TypedDict

from bson import ObjectId

from app.models.enums import Amenity, FacilityStatus
from app.utils.mongo import as_utc, utcnow

FACILITIES_COLLECTION = "parkingFacilities"

MIN_NAME_LENGTH = 3
MAX_NAME_LENGTH = 80
MIN_ADDRESS_LENGTH = 3
MAX_ADDRESS_LENGTH = 200
MAX_CITY_LENGTH = 80
MAX_DESCRIPTION_LENGTH = 500
MIN_CAPACITY = 1
MAX_CAPACITY = 10_000
MIN_HOURLY_RATE = 0.0
MAX_HOURLY_RATE = 10_000.0
MAX_DAILY_MAX = 100_000.0

# "HH:MM" on the 24-hour clock, no seconds, no date part.
TIME_PATTERN = r"^([01][0-9]|2[0-3]):[0-5][0-9]$"


def time_is_valid(value: str) -> bool:
    """True for an `HH:MM` string on the 24-hour clock."""
    return bool(re.fullmatch(TIME_PATTERN, value))


def minutes_since_midnight(value: str) -> int:
    """`"09:30"` -> 570. Caller has already validated the format."""
    hour, minute = value.split(":", 1)
    return int(hour) * 60 + int(minute)


def hours_error(open24h: bool, opening_time: str | None, closing_time: str | None) -> str | None:
    """Why this hours combination cannot be stored, or `None` when it can.

    Shared by the create schema (where every field is present) and the update
    service (which merges a partial patch onto the stored document first), so
    the two never disagree about what "open24h: false" requires.
    """
    if open24h:
        if opening_time or closing_time:
            return "openingTime and closingTime must be omitted while open24h is true"
        return None
    if not opening_time or not closing_time:
        return "openingTime and closingTime are required while open24h is false"
    if not time_is_valid(opening_time) or not time_is_valid(closing_time):
        return "openingTime and closingTime must be HH:MM on the 24-hour clock"
    if opening_time == closing_time:
        return "openingTime and closingTime must differ"
    return None


def is_open_now(document: dict[str, Any]) -> bool:
    """Whether the facility is open at this instant.

    `open24h` short-circuits. Otherwise the opening and closing times are
    compared against the current UTC clock; a closing time *before* the opening
    time means the facility wraps past midnight (`22:00`-`06:00`), which is
    common for garages. Times are UTC because the document carries no timezone
    yet - the discovery phase adds one when nearby search needs it.
    """
    if document.get("open24h", True):
        return True

    opening = document.get("openingTime")
    closing = document.get("closingTime")
    if not opening or not closing or not time_is_valid(opening) or not time_is_valid(closing):
        return False

    now = utcnow()
    current = now.hour * 60 + now.minute
    start = minutes_since_midnight(opening)
    end = minutes_since_midnight(closing)
    if start == end:
        return False
    if start < end:
        return start <= current < end
    return current >= start or current < end


class FacilityDocument(TypedDict):
    """A row of the `parkingFacilities` collection.

    `operatorId` points at the owning `users` document (the DOC section 18
    field name); `location` is the GeoJSON Point behind the `2dsphere` index.
    """

    _id: ObjectId
    operatorId: ObjectId
    name: str
    description: str | None
    address: str
    city: str | None
    location: dict[str, Any]
    capacity: int
    open24h: bool
    openingTime: str | None
    closingTime: str | None
    amenities: list[str]
    hourlyRate: float | None
    dailyMax: float | None
    status: str
    createdAt: datetime
    updatedAt: datetime


def geojson_point(lat: float, lng: float) -> dict[str, Any]:
    """A GeoJSON Point; coordinates are `[lng, lat]` per RFC 7946."""
    return {"type": "Point", "coordinates": [lng, lat]}


def public_facility(document: dict[str, Any], operator_name: str | None = None) -> dict[str, Any]:
    """Project a facility document down to the fields safe to send to a client.

    `lat`/`lng` are unwrapped from the GeoJSON `location`, and the three
    amenity flags plus `isOpenNow` are derived rather than stored. The owning
    account's display name is not part of the document (only `operatorId` is),
    so callers resolve it from the `users` collection and pass it in.
    """
    location = document.get("location") or {}
    coordinates = location.get("coordinates") or []
    lng = coordinates[0] if len(coordinates) > 0 else None
    lat = coordinates[1] if len(coordinates) > 1 else None

    amenities = list(document.get("amenities") or [])
    return {
        "id": str(document["_id"]),
        "operatorId": str(document["operatorId"]),
        "operatorName": operator_name,
        "name": document.get("name"),
        "description": document.get("description"),
        "address": document.get("address"),
        "city": document.get("city"),
        "lat": lat,
        "lng": lng,
        "capacity": document.get("capacity"),
        "open24h": bool(document.get("open24h", True)),
        "openingTime": document.get("openingTime"),
        "closingTime": document.get("closingTime"),
        "amenities": amenities,
        "hasEV": Amenity.EV_CHARGING.value in amenities,
        "hasCovered": Amenity.COVERED.value in amenities,
        "hasAccessible": Amenity.ACCESSIBLE.value in amenities,
        "isOpenNow": is_open_now(document),
        "hourlyRate": document.get("hourlyRate"),
        "dailyMax": document.get("dailyMax"),
        "status": document.get("status", FacilityStatus.ACTIVE.value),
        "createdAt": as_utc(document.get("createdAt")),
        "updatedAt": as_utc(document.get("updatedAt")),
    }


__all__ = [
    "FACILITIES_COLLECTION",
    "MAX_ADDRESS_LENGTH",
    "MAX_CAPACITY",
    "MAX_CITY_LENGTH",
    "MAX_DESCRIPTION_LENGTH",
    "MAX_HOURLY_RATE",
    "MAX_NAME_LENGTH",
    "MIN_ADDRESS_LENGTH",
    "MIN_CAPACITY",
    "MIN_NAME_LENGTH",
    "TIME_PATTERN",
    "FacilityDocument",
    "geojson_point",
    "hours_error",
    "is_open_now",
    "minutes_since_midnight",
    "public_facility",
    "time_is_valid",
]
