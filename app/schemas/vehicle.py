"""Request and response bodies for the `vehicles` group (DOC sections 10/18/21).

The vehicle is deliberately narrow, exactly as DOC section 18 lists it:
`registrationNumber`, `type`, `model`, `fuelType` - plus `isDefault`, which
section 10 and section 21 both call for. There is no `color` and no `make`:
`model` is free text that carries both ("Honda Civic").

`isEV` is not a request field. The server derives it from `fuelType` (see
`app.models.vehicle.is_electric`), so a client cannot store a vehicle that says
`fuelType: "petrol"` and `isEV: true`.

The wire format stays camelCase to match the rest of the API.
"""

from __future__ import annotations

import re
from datetime import datetime

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from app.models.enums import FuelType, VehicleType
from app.models.vehicle import (
    MAX_MODEL_LENGTH,
    MIN_MODEL_LENGTH,
    normalize_registration,
    registration_is_valid,
)

REGISTRATION_HINT = (
    "registrationNumber must be 2-20 letters and digits "
    "(spaces, hyphens and other separators are removed)"
)
MODEL_HINT = f"model must be {MIN_MODEL_LENGTH}-{MAX_MODEL_LENGTH} characters"


def _clean_model(value: str) -> str:
    """Collapse whitespace and re-check the length *after* collapsing.

    Checking first would let a five-word string of spaces through `min_length`
    and then arrive in the database as an empty `model`.
    """
    collapsed = re.sub(r"\s+", " ", value).strip()
    if not MIN_MODEL_LENGTH <= len(collapsed) <= MAX_MODEL_LENGTH:
        raise ValueError(MODEL_HINT)
    return collapsed


class CreateVehicleRequest(BaseModel):
    """Body of `POST /api/v1/vehicles`."""

    # Rejects `isDefault`, `isEV` and `userId` outright with a 422 rather than
    # dropping them, so a client bug surfaces instead of being silently ignored.
    model_config = ConfigDict(extra="forbid")

    registrationNumber: str
    type: VehicleType
    model: str
    fuelType: FuelType

    @field_validator("registrationNumber")
    @classmethod
    def _registration(cls, value: str) -> str:
        normalised = normalize_registration(value)
        if not registration_is_valid(normalised):
            raise ValueError(REGISTRATION_HINT)
        return normalised

    @field_validator("model")
    @classmethod
    def _model(cls, value: str) -> str:
        return _clean_model(value)


class UpdateVehicleRequest(BaseModel):
    """Body of `PATCH /api/v1/vehicles/{id}`.

    Partial update: an absent key leaves the field alone, while the four data
    fields cannot be `null` because none of them has a "cleared" meaning in the
    database. `model_fields_set` - not the value - is what tells the two apart.
    """

    model_config = ConfigDict(extra="forbid")

    registrationNumber: str | None = None
    type: VehicleType | None = None
    model: str | None = None
    fuelType: FuelType | None = None
    # Declared only so the 422 below can name the endpoint that does handle it;
    # a bare `extra="forbid"` failure would just say "extra inputs are not
    # permitted" and leave the caller guessing.
    isDefault: bool | None = None

    @field_validator("registrationNumber")
    @classmethod
    def _registration(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalised = normalize_registration(value)
        if not registration_is_valid(normalised):
            raise ValueError(REGISTRATION_HINT)
        return normalised

    @field_validator("model")
    @classmethod
    def _model(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _clean_model(value)

    @model_validator(mode="after")
    def _reject_unusable_values(self) -> UpdateVehicleRequest:
        if self.isDefault is not None:
            raise ValueError(
                "isDefault is not writable here; use PATCH /vehicles/{id}/default"
            )
        for field_name in ("registrationNumber", "type", "model", "fuelType"):
            if field_name in self.model_fields_set and getattr(self, field_name) is None:
                raise ValueError(f"{field_name} cannot be null")
        return self


class VehicleResponse(BaseModel):
    """Shape returned by every `vehicles` route."""

    id: str
    userId: str
    registrationNumber: str
    type: VehicleType
    model: str
    fuelType: FuelType
    isEV: bool
    isDefault: bool
    createdAt: datetime | None = None


__all__ = ["CreateVehicleRequest", "UpdateVehicleRequest", "VehicleResponse"]
