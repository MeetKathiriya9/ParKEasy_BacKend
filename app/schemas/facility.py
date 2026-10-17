"""Request and response bodies for the `facilities` group (DOC sections 7/11/18).

The writable surface mirrors DOC section 18's key fields - `name`, `address`,
geo location (`lat`/`lng`), `capacity`, hours, `amenities` - plus the optional
base rates and a free-text `description`/`city` that the driver pages render.

Not request fields, by design:

* `status` - a first-class transition on `PATCH /facilities/{id}/status`, so a
  casual edit cannot silently close a garage. Sending it answers a `422` that
  names the endpoint, exactly like a vehicle's `isDefault`.
* `operatorId` - ownership comes from the token. An admin may pass it on
  `POST` to create a facility on another operator's behalf; anyone else
  sending it gets a `422`, and it is never re-assignable afterwards.
* `hasEV` / `hasCovered` / `hasAccessible` / `isOpenNow` / `location` - derived
  at the API boundary (see `app.models.facility.public_facility`).
"""

from __future__ import annotations

import re
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.models.enums import Amenity, FacilityStatus
from app.models.facility import (
    MAX_ADDRESS_LENGTH,
    MAX_CAPACITY,
    MAX_CITY_LENGTH,
    MAX_DAILY_MAX,
    MAX_DESCRIPTION_LENGTH,
    MAX_HOURLY_RATE,
    MAX_NAME_LENGTH,
    MIN_ADDRESS_LENGTH,
    MIN_CAPACITY,
    MIN_NAME_LENGTH,
    hours_error,
    time_is_valid,
)

NAME_HINT = f"name must be {MIN_NAME_LENGTH}-{MAX_NAME_LENGTH} characters"
ADDRESS_HINT = f"address must be {MIN_ADDRESS_LENGTH}-{MAX_ADDRESS_LENGTH} characters"
TIME_HINT = "openingTime and closingTime must be HH:MM on the 24-hour clock"
DESCRIPTION_HINT = f"description must be at most {MAX_DESCRIPTION_LENGTH} characters"
REASON_HINT = "reason must be at most 200 characters"


def _collapse(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


class CreateFacilityRequest(BaseModel):
    """Body of `POST /api/v1/facilities`."""

    # Rejects `status`, `hasEV` and friends outright with a 422 rather than
    # dropping them, so a client bug surfaces instead of being silently ignored.
    model_config = ConfigDict(extra="forbid")

    name: str
    address: str
    city: str | None = None
    lat: float = Field(ge=-90.0, le=90.0)
    lng: float = Field(ge=-180.0, le=180.0)
    capacity: int = Field(ge=MIN_CAPACITY, le=MAX_CAPACITY)
    open24h: bool = True
    openingTime: str | None = None
    closingTime: str | None = None
    amenities: list[Amenity] = Field(default_factory=list)
    description: str | None = None
    hourlyRate: float | None = Field(default=None, gt=0.0, le=MAX_HOURLY_RATE)
    dailyMax: float | None = Field(default=None, gt=0.0, le=MAX_DAILY_MAX)
    # Declared only so the service can answer a 422 for non-admin callers with
    # a readable message; a bare `extra="forbid"` failure would just say
    # "extra inputs are not permitted" and leave the caller guessing.
    operatorId: str | None = None

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        collapsed = _collapse(value)
        if not MIN_NAME_LENGTH <= len(collapsed) <= MAX_NAME_LENGTH:
            raise ValueError(NAME_HINT)
        return collapsed

    @field_validator("address")
    @classmethod
    def _address(cls, value: str) -> str:
        collapsed = _collapse(value)
        if not MIN_ADDRESS_LENGTH <= len(collapsed) <= MAX_ADDRESS_LENGTH:
            raise ValueError(ADDRESS_HINT)
        return collapsed

    @field_validator("city")
    @classmethod
    def _city(cls, value: str | None) -> str | None:
        if value is None:
            return None
        collapsed = _collapse(value)
        if not collapsed or len(collapsed) > MAX_CITY_LENGTH:
            raise ValueError(f"city must be 1-{MAX_CITY_LENGTH} characters")
        return collapsed

    @field_validator("description")
    @classmethod
    def _description(cls, value: str | None) -> str | None:
        if value is None:
            return None
        collapsed = _collapse(value)
        if not collapsed or len(collapsed) > MAX_DESCRIPTION_LENGTH:
            raise ValueError(DESCRIPTION_HINT)
        return collapsed

    @field_validator("openingTime", "closingTime")
    @classmethod
    def _time(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not time_is_valid(value):
            raise ValueError(TIME_HINT)
        return value

    @field_validator("amenities")
    @classmethod
    def _amenities(cls, value: list[Amenity]) -> list[Amenity]:
        # Order-preserving dedupe: `["covered", "covered"]` is one amenity, not
        # an error, because checkbox UIs resend the whole list.
        seen: set[Amenity] = set()
        unique: list[Amenity] = []
        for amenity in value:
            if amenity not in seen:
                seen.add(amenity)
                unique.append(amenity)
        return unique

    @model_validator(mode="after")
    def _hours_are_coherent(self) -> CreateFacilityRequest:
        error = hours_error(self.open24h, self.openingTime, self.closingTime)
        if error is not None:
            raise ValueError(error)
        return self


class UpdateFacilityRequest(BaseModel):
    """Body of `PATCH /api/v1/facilities/{id}`.

    Partial update: an absent key leaves the field alone. Fields with a
    "cleared" meaning (`city`, `description`, the times, the rates) accept
    `null`; the identity and structure fields do not.
    """

    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    address: str | None = None
    city: str | None = None
    description: str | None = None
    lat: float | None = None
    lng: float | None = None
    capacity: int | None = None
    open24h: bool | None = None
    openingTime: str | None = None
    closingTime: str | None = None
    amenities: list[Amenity] | None = None
    hourlyRate: float | None = None
    dailyMax: float | None = None
    # Declared only so the 422s below can name the endpoints that do handle
    # them; the general update route refuses both.
    status: FacilityStatus | None = None
    operatorId: str | None = None

    @field_validator("name")
    @classmethod
    def _name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        collapsed = _collapse(value)
        if not MIN_NAME_LENGTH <= len(collapsed) <= MAX_NAME_LENGTH:
            raise ValueError(NAME_HINT)
        return collapsed

    @field_validator("address")
    @classmethod
    def _address(cls, value: str | None) -> str | None:
        if value is None:
            return None
        collapsed = _collapse(value)
        if not MIN_ADDRESS_LENGTH <= len(collapsed) <= MAX_ADDRESS_LENGTH:
            raise ValueError(ADDRESS_HINT)
        return collapsed

    @field_validator("city")
    @classmethod
    def _city(cls, value: str | None) -> str | None:
        if value is None:
            return None
        collapsed = _collapse(value)
        if not collapsed or len(collapsed) > MAX_CITY_LENGTH:
            raise ValueError(f"city must be 1-{MAX_CITY_LENGTH} characters")
        return collapsed

    @field_validator("description")
    @classmethod
    def _description(cls, value: str | None) -> str | None:
        if value is None:
            return None
        collapsed = _collapse(value)
        if not collapsed or len(collapsed) > MAX_DESCRIPTION_LENGTH:
            raise ValueError(DESCRIPTION_HINT)
        return collapsed

    @field_validator("lat")
    @classmethod
    def _lat(cls, value: float | None) -> float | None:
        if value is None:
            return None
        if not -90.0 <= value <= 90.0:
            raise ValueError("lat must be between -90 and 90")
        return value

    @field_validator("lng")
    @classmethod
    def _lng(cls, value: float | None) -> float | None:
        if value is None:
            return None
        if not -180.0 <= value <= 180.0:
            raise ValueError("lng must be between -180 and 180")
        return value

    @field_validator("capacity")
    @classmethod
    def _capacity(cls, value: int | None) -> int | None:
        if value is None:
            return None
        if not MIN_CAPACITY <= value <= MAX_CAPACITY:
            raise ValueError(f"capacity must be {MIN_CAPACITY}-{MAX_CAPACITY}")
        return value

    @field_validator("hourlyRate")
    @classmethod
    def _hourly_rate(cls, value: float | None) -> float | None:
        if value is None:
            return None
        if not 0.0 < value <= MAX_HOURLY_RATE:
            raise ValueError(f"hourlyRate must be greater than 0 and at most {MAX_HOURLY_RATE}")
        return value

    @field_validator("dailyMax")
    @classmethod
    def _daily_max(cls, value: float | None) -> float | None:
        if value is None:
            return None
        if not 0.0 < value <= MAX_DAILY_MAX:
            raise ValueError(f"dailyMax must be greater than 0 and at most {MAX_DAILY_MAX}")
        return value

    @field_validator("openingTime", "closingTime")
    @classmethod
    def _time(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not time_is_valid(value):
            raise ValueError(TIME_HINT)
        return value

    @field_validator("amenities")
    @classmethod
    def _amenities(cls, value: list[Amenity] | None) -> list[Amenity] | None:
        if value is None:
            return None
        seen: set[Amenity] = set()
        unique: list[Amenity] = []
        for amenity in value:
            if amenity not in seen:
                seen.add(amenity)
                unique.append(amenity)
        return unique

    @model_validator(mode="after")
    def _reject_unusable_values(self) -> UpdateFacilityRequest:
        fields = self.model_fields_set

        if self.status is not None and "status" in fields:
            raise ValueError("status is not writable here; use PATCH /facilities/{id}/status")
        if "operatorId" in fields:
            raise ValueError("operatorId cannot be changed")

        for field_name in ("name", "address", "lat", "lng", "capacity", "open24h", "amenities"):
            if field_name in fields and getattr(self, field_name) is None:
                raise ValueError(f"{field_name} cannot be null")

        if ("lat" in fields) != ("lng" in fields):
            raise ValueError("lat and lng must be sent together")

        # Hours coherence that can be judged from this patch alone. The service
        # re-checks the *merged* result, because a patch of only `open24h: false`
        # is only wrong if the stored times cannot carry it.
        if self.open24h is True and (self.openingTime is not None or self.closingTime is not None):
            raise ValueError("openingTime and closingTime must be omitted while open24h is true")
        return self


class ChangeFacilityStatusRequest(BaseModel):
    """Body of `PATCH /api/v1/facilities/{id}/status` (DOC section 11.169)."""

    model_config = ConfigDict(extra="forbid")

    status: FacilityStatus
    reason: str | None = None

    @field_validator("reason")
    @classmethod
    def _reason(cls, value: str | None) -> str | None:
        if value is None:
            return None
        collapsed = _collapse(value)
        if not collapsed or len(collapsed) > 200:
            raise ValueError(REASON_HINT)
        return collapsed


class FacilityResponse(BaseModel):
    """Shape returned by every `facilities` route."""

    id: str
    operatorId: str
    operatorName: str | None = None
    name: str
    description: str | None = None
    address: str
    city: str | None = None
    lat: float | None = None
    lng: float | None = None
    capacity: int
    open24h: bool
    openingTime: str | None = None
    closingTime: str | None = None
    amenities: list[Amenity]
    hasEV: bool
    hasCovered: bool
    hasAccessible: bool
    isOpenNow: bool
    hourlyRate: float | None = None
    dailyMax: float | None = None
    status: FacilityStatus
    createdAt: datetime | None = None
    updatedAt: datetime | None = None


__all__ = [
    "ChangeFacilityStatusRequest",
    "CreateFacilityRequest",
    "FacilityResponse",
    "UpdateFacilityRequest",
]
