"""The `parking` group: driver-facing discovery over parking facilities.

    GET /nearby              facilities around a point, with filters and sorting
    GET /{facility_id}       one facility, with its live availability
    GET /{facility_id}/availability   the per-status space tally
    GET /{facility_id}/spaces         the facility's spaces, for the layout grid

DOC section 21 names `/api/parking` for discovery and section 22 describes what
a nearby search must be able to do: "find nearby parking", "filter by price,
EV charging, covered parking, availability, parking type", and "sort by
distance, price, or availability".

Two deliberate choices:

* **The search centre arrives as coordinates.** `lat`/`lng` are required, so
  the server never has to guess which city a free-text query meant. The client
  turns "near me", a typed address, or a dropped map pin into a point before
  it calls this group.
* **Every authenticated role may search.** A driver is the obvious caller, but
  an operator looking for a competitor's garage is just as entitled to the same
  answer, and the management group (`/facilities`) already refuses drivers.

The management group is the write side of the same collection. This one is
read-only and never mutates anything.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Path, Query, Request
from pymongo.asynchronous.database import AsyncDatabase

from app.api.deps import CurrentUser, require_roles
from app.api.throttle import throttle
from app.core.config import Settings, get_settings
from app.core.rate_limit import RateLimit
from app.db.client import get_database
from app.models.enums import Role, SpaceType
from app.schemas.common import ErrorResponse, PaginatedResponse
from app.schemas.parking import (
    ParkingAvailabilityResponse,
    ParkingSearchResult,
    ParkingSpacesResponse,
)
from app.schemas.space import SpaceResponse
from app.services import parking as parking_service

router = APIRouter(prefix="/parking", tags=["parking"])

# Any signed-in account may search. The management group (`/facilities`) is
# where the role check bites.
_SEARCH_ROLES = [Role.DRIVER, Role.STAFF, Role.OPERATOR, Role.ADMIN]

# The path id is a Mongo ObjectId in hex. Validating it here means a malformed
# id is a `422` naming the parameter, not a `404` about the facility.
_FACILITY_ID = Path(
    min_length=24,
    max_length=24,
    pattern=r"^[0-9a-fA-F]{24}$",
    description="The facility's id",
)

_AUTHENTICATED_ERRORS: dict[int | str, dict[str, type[Any]]] = {
    401: {"model": ErrorResponse, "description": "Missing, invalid or revoked token"},
    403: {"model": ErrorResponse, "description": "Role may not perform this action"},
}

_PARKING_ERROR_RESPONSES: dict[int | str, dict[str, type[Any]]] = {
    **_AUTHENTICATED_ERRORS,
    404: {"model": ErrorResponse, "description": "No such facility"},
    422: {"model": ErrorResponse, "description": "Request validation failed"},
    429: {"model": ErrorResponse, "description": "Too many attempts"},
}


def _search_throttle(request: Request, user: Any, settings: Settings) -> None:
    """One bucket for the whole group, so a script cannot grind the geo index."""
    throttle(
        request,
        bucket="parking-search",
        identity=user.id,
        rule=RateLimit(
            limit=settings.parking_search_rate_limit,
            window_seconds=settings.parking_search_rate_window_seconds,
        ),
    )


def _space_row(row: dict[str, Any]) -> dict[str, Any]:
    """Flatten a space document into the shape `SpaceResponse` expects."""
    return {
        "id": str(row["_id"]),
        "facilityId": str(row.get("facilityId")),
        "floorId": str(row.get("floorId")),
        "floorName": row.get("floorName"),
        "zoneId": str(row.get("zoneId")),
        "zoneName": row.get("zoneName"),
        "spaceNumber": row.get("spaceNumber"),
        "type": row.get("type"),
        "status": row.get("status"),
        "reservable": bool(row.get("reservable", False)),
        "createdAt": row.get("createdAt"),
        "updatedAt": row.get("updatedAt"),
    }


@router.get(
    "/nearby",
    response_model=PaginatedResponse[ParkingSearchResult],
    summary="Find parking facilities near a point",
    description=(
        "Returns the `ACTIVE` facilities around `lat`/`lng`, with the ones "
        "inside `radiusKm` (default 5 km) first and the nearest facilities "
        "beyond it filling the rest of the page. The radius therefore orders "
        "the results rather than hiding the world outside it: a driver far "
        "from every garage still gets a full page, closest first. The distance "
        "is measured by the `2dsphere` index, so it is the real distance on the "
        "ground rather than a straight-line guess the client had to make.\n\n"
        "Filters (`minAvailable`, `maxPrice`, `ev`, `covered`, `accessible`, "
        "`openNow`, `spaceType`) narrow the result, `sort` orders it within "
        "each tier (`distance`, `price`, `availability`, `rating`), and "
        "`page`/`limit` page through it. `meta.total` counts the facilities "
        "that matched every filter across both tiers, not just the ones on this "
        "page.\n\n"
        "Two rules are worth knowing. A facility with no published `hourlyRate` "
        "is excluded by `maxPrice` rather than counted as free, and "
        "`spaceType` matches only when the facility has an *available* space of "
        "that type."
    ),
    responses=_PARKING_ERROR_RESPONSES,
)
async def search_nearby(
    request: Request,
    user: Annotated[CurrentUser, Depends(require_roles(*_SEARCH_ROLES))],
    db: Annotated[AsyncDatabase, Depends(get_database)],
    settings: Annotated[Settings, Depends(get_settings)],
    lat: Annotated[float, Query(ge=-90, le=90, description="Search centre latitude")],
    lng: Annotated[float, Query(ge=-180, le=180, description="Search centre longitude")],
    radius_km: Annotated[
        float | None,
        Query(
            alias="radiusKm",
            ge=0.1,
            le=100.0,
            description="Radius of the first (nearest) tier, in kilometres",
        ),
    ] = None,
    min_available: Annotated[
        int,
        Query(
            alias="minAvailable",
            ge=0,
            le=10_000,
            description="Only facilities with this many free spaces",
        ),
    ] = 0,
    max_price: Annotated[
        float | None,
        Query(alias="maxPrice", ge=0, le=10_000, description="Maximum hourly rate"),
    ] = None,
    ev: Annotated[bool, Query(description="Only facilities with EV charging")] = False,
    covered: Annotated[bool, Query(description="Only covered facilities")] = False,
    accessible: Annotated[bool, Query(description="Only accessible facilities")] = False,
    open_now: Annotated[
        bool, Query(alias="openNow", description="Only facilities open now")
    ] = False,
    space_type: Annotated[
        SpaceType | None,
        Query(alias="spaceType", description="Only facilities with a free space of this type"),
    ] = None,
    sort: Annotated[
        str | None,
        Query(description="distance, price, availability or rating"),
    ] = None,
    page: Annotated[int, Query(ge=1, description="1-based page number")] = 1,
    limit: Annotated[int, Query(ge=1, le=200, description="Results per page")] = 20,
) -> PaginatedResponse[ParkingSearchResult]:
    _search_throttle(request, user, settings)

    default_radius = settings.parking_search_default_radius_km
    effective_radius = radius_km if radius_km is not None else default_radius
    effective_radius = min(effective_radius, settings.parking_search_max_radius_km)
    effective_limit = min(limit, settings.parking_search_max_limit)

    rows, total = await parking_service.nearby(
        db,
        lat=lat,
        lng=lng,
        radius_meters=effective_radius * 1000.0,
        min_available=min_available,
        max_price=max_price,
        want_ev=ev,
        want_covered=covered,
        want_accessible=accessible,
        open_now=open_now,
        space_type=space_type.value if space_type is not None else None,
        sort_by=parking_service.normalise_sort(sort),
        page=page,
        limit=effective_limit,
    )
    return PaginatedResponse[ParkingSearchResult].build(
        [ParkingSearchResult.model_validate(row) for row in rows],
        total=total,
        page=page,
        limit=effective_limit,
    )


@router.get(
    "/{facility_id}",
    response_model=ParkingSearchResult,
    summary="Read one facility",
    description=(
        "The driver-facing twin of `GET /facilities/{id}`: the same facility, "
        "readable by a driver, with the live availability a search row would "
        "have shown. A malformed or unknown id answers `404`."
    ),
    responses=_PARKING_ERROR_RESPONSES,
)
async def get_parking_facility(
    facility_id: Annotated[str, _FACILITY_ID],
    user: Annotated[CurrentUser, Depends(require_roles(*_SEARCH_ROLES))],
    db: Annotated[AsyncDatabase, Depends(get_database)],
) -> ParkingSearchResult:
    row = await parking_service.detail(db, facility_id=facility_id)
    return ParkingSearchResult.model_validate(row)


@router.get(
    "/{facility_id}/availability",
    response_model=ParkingAvailabilityResponse,
    summary="Live availability for one facility",
    description=(
        "The per-status space tally - total, available, occupied, reserved, "
        "maintenance, blocked - and the moment it was read, so a client can "
        "show how fresh the number is."
    ),
    responses=_PARKING_ERROR_RESPONSES,
)
async def get_availability(
    facility_id: Annotated[str, _FACILITY_ID],
    user: Annotated[CurrentUser, Depends(require_roles(*_SEARCH_ROLES))],
    db: Annotated[AsyncDatabase, Depends(get_database)],
) -> ParkingAvailabilityResponse:
    row = await parking_service.availability(db, facility_id=facility_id)
    return ParkingAvailabilityResponse.model_validate(row)


@router.get(
    "/{facility_id}/spaces",
    response_model=ParkingSpacesResponse,
    summary="The spaces of one facility",
    description=(
        "Every space in the facility with its floor and zone names, ordered "
        "floor by floor. This is the driver-scoped read: the `facilities` "
        "group's spaces route is part of the management surface and refuses "
        "drivers."
    ),
    responses=_PARKING_ERROR_RESPONSES,
)
async def get_spaces(
    facility_id: Annotated[str, _FACILITY_ID],
    user: Annotated[CurrentUser, Depends(require_roles(*_SEARCH_ROLES))],
    db: Annotated[AsyncDatabase, Depends(get_database)],
) -> ParkingSpacesResponse:
    rows, measured_at = await parking_service.spaces(db, facility_id=facility_id)
    return ParkingSpacesResponse(
        facilityId=facility_id,
        spaces=[SpaceResponse.model_validate(_space_row(row)) for row in rows],
        availabilityAsOf=measured_at,
    )


__all__ = ["router"]
