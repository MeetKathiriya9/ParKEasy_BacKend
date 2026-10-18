"""Request and response bodies for the floors / zones / spaces group.

The writable surface covers DOC section 7.79 ("space/zone/floor setup, status,
types, reservable flag, maintenance blocking"). Two things are deliberately not
writable here:

* a space's `status` when it is `OCCUPIED` or `RESERVED` - those belong to the
  reservation/session engine (DOC sections 16-17), so a human may only mark a
  space `AVAILABLE`, `MAINTENANCE` or `BLOCKED`, and the request answers a
  readable `422` otherwise;
* `facilityId`/`floorId`/`zoneId` on update - moving a space between zones is a
  delete-and-recreate, not a silent reparent.
"""

from __future__ import annotations

import re
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.enums import SpaceStatus, SpaceType, ZoneStatus
from app.models.space import (
    DEFAULT_SPACE_TYPE,
    MANUAL_SPACE_STATUSES,
    MAX_FLOOR_NAME_LENGTH,
    MAX_LEVEL,
    MAX_REASON_LENGTH,
    MAX_SPACE_NUMBER_LENGTH,
    MAX_ZONE_NAME_LENGTH,
    MIN_FLOOR_NAME_LENGTH,
    MIN_LEVEL,
    MIN_SPACE_NUMBER_LENGTH,
    MIN_ZONE_NAME_LENGTH,
)

FLOOR_NAME_HINT = f"name must be {MIN_FLOOR_NAME_LENGTH}-{MAX_FLOOR_NAME_LENGTH} characters"
ZONE_NAME_HINT = f"name must be {MIN_ZONE_NAME_LENGTH}-{MAX_ZONE_NAME_LENGTH} characters"
SPACE_NUMBER_HINT = (
    f"spaceNumber must be {MIN_SPACE_NUMBER_LENGTH}-{MAX_SPACE_NUMBER_LENGTH} "
    "characters and use only letters, digits, dashes or underscores"
)
REASON_HINT = f"reason must be at most {MAX_REASON_LENGTH} characters"

# Labels are normalised to upper case so "a01" and "A01" cannot both exist in a
# zone and confuse a driver reading the sign on the wall.
_SPACE_NUMBER_PATTERN = re.compile(r"^[A-Z0-9][A-Z0-9_-]*$")
_PREFIX_PATTERN = re.compile(r"^[A-Z0-9-]*$")


def _collapse(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _floor_name(value: str) -> str:
    collapsed = _collapse(value)
    if not MIN_FLOOR_NAME_LENGTH <= len(collapsed) <= MAX_FLOOR_NAME_LENGTH:
        raise ValueError(FLOOR_NAME_HINT)
    return collapsed


def _zone_name(value: str) -> str:
    collapsed = _collapse(value)
    if not MIN_ZONE_NAME_LENGTH <= len(collapsed) <= MAX_ZONE_NAME_LENGTH:
        raise ValueError(ZONE_NAME_HINT)
    return collapsed


def _space_number(value: str) -> str:
    collapsed = _collapse(value).upper()
    if not MIN_SPACE_NUMBER_LENGTH <= len(collapsed) <= MAX_SPACE_NUMBER_LENGTH:
        raise ValueError(SPACE_NUMBER_HINT)
    if not _SPACE_NUMBER_PATTERN.match(collapsed):
        raise ValueError(SPACE_NUMBER_HINT)
    return collapsed


def _reason(value: str | None) -> str | None:
    if value is None:
        return None
    collapsed = _collapse(value)
    if not collapsed or len(collapsed) > MAX_REASON_LENGTH:
        raise ValueError(REASON_HINT)
    return collapsed


# --- Floors -----------------------------------------------------------------


class CreateFloorRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    level: int = Field(default=0, ge=MIN_LEVEL, le=MAX_LEVEL)

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        return _floor_name(value)


class UpdateFloorRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    level: int | None = None

    @field_validator("name")
    @classmethod
    def _name(cls, value: str | None) -> str | None:
        return None if value is None else _floor_name(value)

    @field_validator("level")
    @classmethod
    def _level(cls, value: int | None) -> int | None:
        if value is None:
            return None
        if not MIN_LEVEL <= value <= MAX_LEVEL:
            raise ValueError(f"level must be {MIN_LEVEL}-{MAX_LEVEL}")
        return value


# --- Zones ------------------------------------------------------------------


class CreateZoneRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        return _zone_name(value)


class UpdateZoneRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = None

    @field_validator("name")
    @classmethod
    def _name(cls, value: str | None) -> str | None:
        return None if value is None else _zone_name(value)


class ChangeZoneStatusRequest(BaseModel):
    """Body of `PATCH /zones/{id}/status` (DOC section 11.169)."""

    model_config = ConfigDict(extra="forbid")

    status: ZoneStatus
    reason: str | None = None

    @field_validator("reason")
    @classmethod
    def _reason(cls, value: str | None) -> str | None:
        return _reason(value)


# --- Spaces -----------------------------------------------------------------


class CreateSpaceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    spaceNumber: str
    type: SpaceType = DEFAULT_SPACE_TYPE
    reservable: bool = True

    @field_validator("spaceNumber")
    @classmethod
    def _space_number(cls, value: str) -> str:
        return _space_number(value)


class BulkCreateSpacesRequest(BaseModel):
    """Body of `POST /zones/{id}/spaces/bulk`.

    Generates a run of labels such as ``A01``..``A50`` from a prefix, a start
    number and a count - the "Total Parking = 250" setup of DOC section 42
    without 250 round trips.
    """

    model_config = ConfigDict(extra="forbid")

    prefix: str = ""
    start: int = Field(default=1, ge=0, le=1_000_000)
    count: int = Field(ge=1, le=1000)
    type: SpaceType = DEFAULT_SPACE_TYPE
    reservable: bool = True
    # Optional fixed width; when omitted the service pads to two digits (or
    # wider if the run needs it).
    pad: int | None = Field(default=None, ge=1, le=8)

    @field_validator("prefix")
    @classmethod
    def _prefix(cls, value: str) -> str:
        collapsed = _collapse(value).upper()
        if len(collapsed) > 12 or not _PREFIX_PATTERN.match(collapsed):
            raise ValueError("prefix must be at most 12 letters, digits or dashes")
        return collapsed


class UpdateSpaceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    spaceNumber: str | None = None
    type: SpaceType | None = None
    reservable: bool | None = None

    @field_validator("spaceNumber")
    @classmethod
    def _space_number(cls, value: str | None) -> str | None:
        return None if value is None else _space_number(value)


class ChangeSpaceStatusRequest(BaseModel):
    """Body of `PATCH /spaces/{id}/status` (DOC sections 10.151/17.216)."""

    model_config = ConfigDict(extra="forbid")

    status: SpaceStatus
    reason: str | None = None

    @field_validator("status")
    @classmethod
    def _manual_only(cls, value: SpaceStatus) -> SpaceStatus:
        if value not in MANUAL_SPACE_STATUSES:
            raise ValueError(
                f"status {value.value} is managed by the reservation engine; "
                "a space may only be set to AVAILABLE, MAINTENANCE or BLOCKED"
            )
        return value

    @field_validator("reason")
    @classmethod
    def _reason(cls, value: str | None) -> str | None:
        return _reason(value)


# --- Responses --------------------------------------------------------------


class FloorResponse(BaseModel):
    id: str
    facilityId: str
    name: str
    level: int
    createdAt: datetime | None = None
    updatedAt: datetime | None = None


class ZoneResponse(BaseModel):
    id: str
    facilityId: str
    floorId: str
    name: str
    status: ZoneStatus
    closeReason: str | None = None
    createdAt: datetime | None = None
    updatedAt: datetime | None = None


class FloorLayoutResponse(FloorResponse):
    """A floor plus its zones, for the operator/staff layout tree."""

    zones: list[ZoneResponse] = Field(default_factory=list)


class SpaceResponse(BaseModel):
    id: str
    facilityId: str
    floorId: str
    floorName: str | None = None
    zoneId: str
    zoneName: str | None = None
    spaceNumber: str
    type: SpaceType
    status: SpaceStatus
    reservable: bool
    createdAt: datetime | None = None
    updatedAt: datetime | None = None


class SpaceCounts(BaseModel):
    """Per-facility space tally, the derived replacement for a stored capacity."""

    total: int = 0
    available: int = 0
    occupied: int = 0
    reserved: int = 0
    maintenance: int = 0
    blocked: int = 0


__all__ = [
    "BulkCreateSpacesRequest",
    "ChangeSpaceStatusRequest",
    "ChangeZoneStatusRequest",
    "CreateFloorRequest",
    "CreateSpaceRequest",
    "CreateZoneRequest",
    "FloorLayoutResponse",
    "FloorResponse",
    "SpaceCounts",
    "SpaceResponse",
    "UpdateFloorRequest",
    "UpdateSpaceRequest",
    "UpdateZoneRequest",
    "ZoneResponse",
]
