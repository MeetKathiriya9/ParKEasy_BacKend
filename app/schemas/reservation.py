"""Request and response bodies for the reservations group (DOC sections 16-21).

Times are ISO-8601 with an offset. A naive datetime is read as UTC rather than
rejected: the browser always sends `Z`, but a hand-written test or a curl call
should not have to remember the suffix. The service still enforces alignment to
the 30-minute booking grid and the maximum advance window.
"""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.enums import ReservationStatus
from app.models.reservation import MAX_CANCEL_REASON_LENGTH
from app.schemas.common import PaginatedResponse
from app.schemas.pricing import PricingBreakdown


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


class CreateReservationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    vehicleId: str = Field(min_length=1, max_length=64)
    facilityId: str = Field(min_length=1, max_length=64)
    # Optional: when omitted the engine picks the first free space in the
    # facility. A driver who picks one gets it only if it is still free.
    spaceId: str | None = Field(default=None, min_length=1, max_length=64)
    startTime: datetime
    endTime: datetime

    @field_validator("startTime", "endTime")
    @classmethod
    def _timezone(cls, value: datetime) -> datetime:
        return _aware(value)  # type: ignore[return-value]


class CancelReservationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str | None = Field(default=None, max_length=MAX_CANCEL_REASON_LENGTH)

    @field_validator("reason")
    @classmethod
    def _reason(cls, value: str | None) -> str | None:
        if value is None:
            return None
        collapsed = " ".join(value.split())
        if not collapsed:
            return None
        if len(collapsed) > MAX_CANCEL_REASON_LENGTH:
            raise ValueError(
                f"reason must be at most {MAX_CANCEL_REASON_LENGTH} characters"
            )
        return collapsed


class CheckInRequest(BaseModel):
    """Staff may override the booked space when the original is unusable."""

    model_config = ConfigDict(extra="forbid")

    spaceId: str | None = Field(default=None, min_length=1, max_length=64)


class ReservationResponse(BaseModel):
    id: str
    reference: str
    qrCode: str
    userId: str
    vehicleId: str
    vehiclePlate: str
    facilityId: str
    facilityName: str
    operatorId: str | None = None
    spaceId: str
    spaceNumber: str
    startTime: datetime
    endTime: datetime
    durationMinutes: int
    status: ReservationStatus
    estimatedAmount: float | None = None
    estimatedBreakdown: PricingBreakdown | None = None
    checkedInAt: datetime | None = None
    checkedOutAt: datetime | None = None
    cancelledAt: datetime | None = None
    cancelReason: str | None = None
    sessionId: str | None = None
    createdAt: datetime
    updatedAt: datetime


class ReservationAvailabilityResponse(BaseModel):
    facilityId: str
    startTime: datetime
    endTime: datetime
    totalReservable: int
    availableSpaces: int
    availabilityAsOf: datetime


class CheckInResponse(BaseModel):
    reservation: ReservationResponse
    session: SessionResponse


class CheckOutResponse(BaseModel):
    reservation: ReservationResponse
    session: SessionResponse


class ExtendSessionResponse(BaseModel):
    reservation: ReservationResponse
    session: SessionResponse


class ReservationPage(PaginatedResponse[ReservationResponse]):
    pass


# Imported at the bottom to keep the circular reference between the two schema
# modules explicit and local to the response models above.
from app.schemas.session import SessionResponse  # noqa: E402

CheckInResponse.model_rebuild()
CheckOutResponse.model_rebuild()
ExtendSessionResponse.model_rebuild()

__all__ = [
    "CancelReservationRequest",
    "CheckInRequest",
    "CheckInResponse",
    "CheckOutResponse",
    "CreateReservationRequest",
    "ExtendSessionResponse",
    "ReservationAvailabilityResponse",
    "ReservationPage",
    "ReservationResponse",
]
