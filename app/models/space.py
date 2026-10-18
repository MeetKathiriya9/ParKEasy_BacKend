"""The `floors`, `zones` and `parkingSpaces` collections (DOC sections 7/11/18).

DOC section 18 lists `parkingSpaces` key fields as `facilityId, floor, zone,
spaceNumber, type, status`. This implementation promotes `floor` and `zone` to
their own collections (`floors`, `zones`) so an operator can rename a floor,
reorder levels, and close a single zone (DOC section 11.169) without rewriting
every space - while each space still answers "report by facility/zone/space"
(DOC section 33).

The hierarchy is Facility -> Floor -> Zone -> Space. A space keeps both the
`floorId`/`zoneId` references *and* denormalized `floorName`/`zoneName`
snapshots: reads are self-contained (a grid or a report needs no joins), and a
rename on the parent rewrites the snapshots in one `update_many`, which is rare
enough to be worth the fast read.

A facility's `capacity` is no longer stored: it is derived at the API boundary
as the count of that facility's spaces (see `app.services.facilities`).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, TypedDict

from bson import ObjectId

from app.models.enums import SpaceStatus, SpaceType, ZoneStatus
from app.utils.mongo import as_utc

FLOORS_COLLECTION = "floors"
ZONES_COLLECTION = "zones"
SPACES_COLLECTION = "parkingSpaces"

MIN_FLOOR_NAME_LENGTH = 1
MAX_FLOOR_NAME_LENGTH = 40
MIN_ZONE_NAME_LENGTH = 1
MAX_ZONE_NAME_LENGTH = 40
MIN_SPACE_NUMBER_LENGTH = 1
MAX_SPACE_NUMBER_LENGTH = 20
MIN_LEVEL = -20
MAX_LEVEL = 200
MAX_REASON_LENGTH = 200

# DOC section 7.79 lists space "types"; the tags live in the `reservable` flag,
# which controls whether the reservation engine (a later phase) may book it.
DEFAULT_SPACE_TYPE = SpaceType.STANDARD

# Only these three are ever set by a *person*. `OCCUPIED` and `RESERVED` are
# owned by the reservation/session engine (DOC sections 16-17); letting a human
# type them would immediately disagree with that engine once it lands.
MANUAL_SPACE_STATUSES = frozenset(
    {SpaceStatus.AVAILABLE, SpaceStatus.MAINTENANCE, SpaceStatus.BLOCKED}
)


class FloorDocument(TypedDict):
    """A row of the `floors` collection."""

    _id: ObjectId
    facilityId: ObjectId
    name: str
    level: int
    createdAt: datetime
    updatedAt: datetime


class ZoneDocument(TypedDict):
    """A row of the `zones` collection."""

    _id: ObjectId
    facilityId: ObjectId
    floorId: ObjectId
    name: str
    status: str
    closeReason: str | None
    createdAt: datetime
    updatedAt: datetime


class SpaceDocument(TypedDict):
    """A row of the `parkingSpaces` collection."""

    _id: ObjectId
    facilityId: ObjectId
    floorId: ObjectId
    floorName: str
    zoneId: ObjectId
    zoneName: str
    spaceNumber: str
    type: str
    status: str
    reservable: bool
    createdAt: datetime
    updatedAt: datetime


def public_floor(document: dict[str, Any]) -> dict[str, Any]:
    """Project a floor document for the API."""
    return {
        "id": str(document["_id"]),
        "facilityId": str(document["facilityId"]),
        "name": document.get("name"),
        "level": document.get("level", 0),
        "createdAt": as_utc(document.get("createdAt")),
        "updatedAt": as_utc(document.get("updatedAt")),
    }


def public_zone(document: dict[str, Any]) -> dict[str, Any]:
    """Project a zone document for the API."""
    return {
        "id": str(document["_id"]),
        "facilityId": str(document["facilityId"]),
        "floorId": str(document["floorId"]),
        "name": document.get("name"),
        "status": document.get("status", ZoneStatus.ACTIVE.value),
        "closeReason": document.get("closeReason"),
        "createdAt": as_utc(document.get("createdAt")),
        "updatedAt": as_utc(document.get("updatedAt")),
    }


def public_space(document: dict[str, Any]) -> dict[str, Any]:
    """Project a space document for the API.

    `floorName`/`zoneName` are the denormalized snapshots kept on the document,
    so a space stands on its own in a grid or a report.
    """
    return {
        "id": str(document["_id"]),
        "facilityId": str(document["facilityId"]),
        "floorId": str(document["floorId"]),
        "floorName": document.get("floorName"),
        "zoneId": str(document["zoneId"]),
        "zoneName": document.get("zoneName"),
        "spaceNumber": document.get("spaceNumber"),
        "type": document.get("type", DEFAULT_SPACE_TYPE.value),
        "status": document.get("status", SpaceStatus.AVAILABLE.value),
        "reservable": bool(document.get("reservable", True)),
        "createdAt": as_utc(document.get("createdAt")),
        "updatedAt": as_utc(document.get("updatedAt")),
    }


def space_numbers(prefix: str, start: int, count: int, pad: int | None = None) -> list[str]:
    """The labels a bulk create would generate, e.g. ``A01``..``A50``.

    Padding defaults to two digits (``A01`` rather than ``A1``) so the labels
    sort naturally, widening automatically once the run needs more digits.
    """
    last = start + count - 1
    if pad is None:
        pad = max(2, len(str(last)))
    return [f"{prefix}{number:0{pad}d}" for number in range(start, start + count)]


__all__ = [
    "DEFAULT_SPACE_TYPE",
    "FLOORS_COLLECTION",
    "MANUAL_SPACE_STATUSES",
    "MAX_FLOOR_NAME_LENGTH",
    "MAX_LEVEL",
    "MAX_REASON_LENGTH",
    "MAX_SPACE_NUMBER_LENGTH",
    "MAX_ZONE_NAME_LENGTH",
    "MIN_FLOOR_NAME_LENGTH",
    "MIN_LEVEL",
    "MIN_SPACE_NUMBER_LENGTH",
    "MIN_ZONE_NAME_LENGTH",
    "SPACES_COLLECTION",
    "ZONES_COLLECTION",
    "FloorDocument",
    "SpaceDocument",
    "ZoneDocument",
    "public_floor",
    "public_space",
    "public_zone",
    "space_numbers",
]
