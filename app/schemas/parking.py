"""Response bodies for the `parking` discovery group (DOC sections 7/19/20/22).

The discovery group is the driver-facing window onto facilities. It answers
DOC section 22's contract for `GET /api/parking/nearby`: a facility summary
plus how fresh the availability numbers are.

`distanceMeters` is measured server-side by the `2dsphere` index, so the client
never has to work out how far away a place is, and `availabilityAsOf` says
when the tally was read, so a driver can tell a fresh number from a stale one.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from app.schemas.facility import FacilityResponse
from app.schemas.space import SpaceCounts, SpaceResponse

# DOC section 20 asks for "facilities within X km" and "at least N available
# spaces"; DOC section 22 asks the endpoint to accept "filters, sorting, and
# pagination". These are the sort keys the driver page offers.
ParkingSortBy = Literal["distance", "price", "availability", "rating"]


class ParkingSearchResult(FacilityResponse):
    """One facility in a search result.

    A facility summary plus the two things a search row needs that a plain
    facility read does not carry: how far away it is, and when its availability
    was measured. `spaceTypes` lists the kinds of space the facility holds, so
    the client can say "EV bays here" without a second request.
    """

    distanceMeters: float | None = None
    availabilityAsOf: datetime | None = None
    spaceTypes: list[str] = Field(default_factory=list)


class ParkingAvailabilityResponse(BaseModel):
    """The live tally for one facility, with the time it was read."""

    facilityId: str
    spaceCounts: SpaceCounts = Field(default_factory=SpaceCounts)
    availabilityAsOf: datetime | None = None


class ParkingSpacesResponse(BaseModel):
    """A facility's spaces, for the driver's layout grid.

    The `facilities` group refuses drivers, so this is the driver-scoped
    equivalent of `GET /facilities/{id}/spaces`.
    """

    facilityId: str
    spaces: list[SpaceResponse] = Field(default_factory=list)
    availabilityAsOf: datetime | None = None


__all__ = [
    "ParkingAvailabilityResponse",
    "ParkingSearchResult",
    "ParkingSortBy",
    "ParkingSpacesResponse",
]
