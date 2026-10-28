"""Request and response bodies for the parking-sessions group (DOC sections 16-21).

A session starts at staff check-in and ends at staff check-out. The amount is a
*base-rate estimate*: payments are a later module, so the API returns the money
figure with `amountEstimated=true` rather than pretending a payment happened.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel

from app.models.enums import SessionStatus
from app.schemas.common import PaginatedResponse


class SessionResponse(BaseModel):
    id: str
    reservationId: str
    reference: str
    userId: str
    vehicleId: str
    vehiclePlate: str
    facilityId: str
    facilityName: str
    operatorId: str | None = None
    spaceId: str
    spaceNumber: str
    entryTime: datetime
    exitTime: datetime | None = None
    status: SessionStatus
    durationMinutes: int
    amount: float | None = None
    amountEstimated: bool = True
    overstayMinutes: int = 0
    closedById: str | None = None
    createdAt: datetime
    updatedAt: datetime


class SessionPage(PaginatedResponse[SessionResponse]):
    pass


__all__ = ["SessionPage", "SessionResponse"]
