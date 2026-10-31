"""Request and response bodies for the `facilities` group (DOC sections 7/11/18).

The writable surface mirrors DOC section 18's key fields - `name`, `address`,
geo location (`lat`/`lng`), hours, `amenities` - plus the optional base rates
and a free-text `description`/`city` that the driver pages render.

Not request fields, by design:

* `status` - a first-class transition on `PATCH /facilities/{id}/status`, so a
  casual edit cannot silently close a garage. Sending it answers a `422` that
  names the endpoint, exactly like a vehicle's `isDefault`.
* `capacity` - derived from the facility's spaces (the Facility -> Floor ->
  Zone -> Space hierarchy), so it is read-only and sending it answers a `422`;
  `spaceCounts` reports the per-status tally.
* `operatorId` - ownership comes from the token. An admin may pass it on
  `POST` to create a facility on another operator's behalf; anyone else
  sending it gets a `422`, and it is never re-assignable afterwards.
* `hasEV` / `hasCovered` / `hasAccessible` / `isOpenNow` / `location` - derived
  at the API boundary (see `app.models.facility.public_facility`).

`rating` and `reviewCount` are the facility's standing on the five-point scale.
They are writable for now so a driver page can show a star; the reviews group
will own them once DOC section 21's `/api/reviews` exists.
"""

from __future__ import annotations

import re
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.models.enums import Amenity, FacilityStatus
from app.models.facility import (
    DEFAULT_GRACE_MINUTES,
    DEFAULT_OVERSTAY_GRACE_MINUTES,
    MAX_ADDRESS_LENGTH,
    MAX_CITY_LENGTH,
    MAX_DAILY_MAX,
    MAX_DESCRIPTION_LENGTH,
    MAX_GRACE_MINUTES,
    MAX_HOURLY_RATE,
    MAX_NAME_LENGTH,
    MAX_OVERSTAY_RATE,
    MAX_RATING,
    MAX_REVIEW_COUNT,
    MIN_ADDRESS_LENGTH,
    MIN_NAME_LENGTH,
    MIN_RATING,
    hours_error,
    time_is_valid,
)
from app.schemas.space import SpaceCounts

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
    open24h: bool = True
    openingTime: str | None = None
    closingTime: str | None = None
    amenities: list[Amenity] = Field(default_factory=list)
    description: str | None = None
    hourlyRate: float | None = Field(default=None, gt=0.0, le=MAX_HOURLY_RATE)
    dailyMax: float | None = Field(default=None, gt=0.0, le=MAX_DAILY_MAX)
    graceMinutes: int = Field(default=DEFAULT_GRACE_MINUTES, ge=0, le=MAX_GRACE_MINUTES)
    overstayGraceMinutes: int = Field(
        default=DEFAULT_OVERSTAY_GRACE_MINUTES, ge=0, le=MAX_GRACE_MINUTES
    )
    overstayRate: float | None = Field(default=None, gt=0.0, le=MAX_OVERSTAY_RATE)
    # The facility's standing on the five-point scale. Written by the operator
    # for now; the reviews group will own it once DOC section 21's `/api/reviews`
    # lands, and until then it is simply the number the driver page shows.
    rating: float | None = Field(default=None, ge=MIN_RATING, le=MAX_RATING)
    reviewCount: int | None = Field(default=None, ge=0, le=MAX_REVIEW_COUNT)
    # Derived from the facility's spaces, so not writable. Declared only so the
    # validator below can answer a readable `422` instead of the bare
    # "extra inputs are not permitted".
    capacity: int | None = None
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
        if self.capacity is not None:
            raise ValueError(
                "capacity is derived from the facility's spaces and cannot be set; "
                "add floors, zones and spaces instead"
            )
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
    graceMinutes: int | None = None
    overstayGraceMinutes: int | None = None
    overstayRate: float | None = None
    rating: float | None = None
    reviewCount: int | None = None
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

    @field_validator("graceMinutes", "overstayGraceMinutes")
    @classmethod
    def _grace_minutes(cls, value: int | None) -> int | None:
        if value is None:
            return None
        if not 0 <= value <= MAX_GRACE_MINUTES:
            raise ValueError(f"grace minutes must be between 0 and {MAX_GRACE_MINUTES}")
        return value

    @field_validator("overstayRate")
    @classmethod
    def _overstay_rate(cls, value: float | None) -> float | None:
        if value is None:
            return None
        if not 0.0 < value <= MAX_OVERSTAY_RATE:
            raise ValueError(f"overstayRate must be greater than 0 and at most {MAX_OVERSTAY_RATE}")
        return value

    @field_validator("openingTime", "closingTime")
    @classmethod
    def _time(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not time_is_valid(value):
            raise ValueError(TIME_HINT)
        return value

    @field_validator("rating")
    @classmethod
    def _rating(cls, value: float | None) -> float | None:
        if value is None:
            return None
        if not MIN_RATING <= value <= MAX_RATING:
            raise ValueError(f"rating must be between {MIN_RATING} and {MAX_RATING}")
        return value

    @field_validator("reviewCount")
    @classmethod
    def _review_count(cls, value: int | None) -> int | None:
        if value is None:
            return None
        if not 0 <= value <= MAX_REVIEW_COUNT:
            raise ValueError(f"reviewCount must be between 0 and {MAX_REVIEW_COUNT}")
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
        if "capacity" in fields:
            raise ValueError(
                "capacity is derived from the facility's spaces and cannot be set"
            )

        for field_name in (
            "name",
            "address",
            "lat",
            "lng",
            "open24h",
            "amenities",
            "graceMinutes",
            "overstayGraceMinutes",
        ):
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
    spaceCounts: SpaceCounts = Field(default_factory=SpaceCounts)
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
    graceMinutes: int = DEFAULT_GRACE_MINUTES
    overstayGraceMinutes: int = DEFAULT_OVERSTAY_GRACE_MINUTES
    overstayRate: float | None = None
    rating: float | None = None
    reviewCount: int | None = None
    status: FacilityStatus
    createdAt: datetime | None = None
    updatedAt: datetime | None = None


__all__ = [
    "ChangeFacilityStatusRequest",
    "CreateFacilityRequest",
    "FacilityResponse",
    "UpdateFacilityRequest",
]
