"""The `reservations` group: booking lifecycle and QR check-in/out.

    GET    /availability                 free-space count for an interval
    POST   /                             book a space (201)
    GET    /                             list bookings in the caller's scope
    GET    /lookup                       staff lookup by reference or QR token
    GET    /{reservation_id}             one booking
    PATCH  /{reservation_id}/cancel      cancel a confirmed booking
    POST   /{reservation_id}/check-in    staff starts a parking session
    POST   /{reservation_id}/check-out   staff closes the parking session

Drivers create and read their own bookings; staff read the facilities assigned
to them and perform check-in/out; operators see their facilities; admins see
everything. Scope is enforced in `app.services.reservations`, and a foreign id
answers `404` rather than `403` so ids cannot be probed.

The space is chosen by the server unless the driver sends `spaceId`. Either way
the interval must be aligned to the 30-minute grid, which is what makes the
slot rows an exact representation of the booking.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query, Request, status
from pymongo.asynchronous.database import AsyncDatabase

from app.api.deps import CurrentUser, require_roles
from app.api.throttle import client_ip, throttle
from app.core.config import Settings, get_settings
from app.core.rate_limit import RateLimit
from app.db.client import get_database
from app.models.enums import ReservationStatus, Role, SpaceType
from app.models.reservation import public_reservation, public_session
from app.schemas.common import ErrorResponse, PaginatedResponse
from app.schemas.reservation import (
    CancelReservationRequest,
    CheckInRequest,
    CheckInResponse,
    CheckOutResponse,
    CreateReservationRequest,
    ReservationAvailabilityResponse,
    ReservationResponse,
)
from app.schemas.session import SessionResponse
from app.services import audit
from app.services import reservations as reservation_service

router = APIRouter(prefix="/reservations", tags=["reservations"])

_ALL_ROLES = [Role.DRIVER, Role.STAFF, Role.OPERATOR, Role.ADMIN]
_STAFF_ROLES = [Role.STAFF, Role.OPERATOR, Role.ADMIN]

AUTHENTICATED_ERRORS: dict[int | str, dict[str, type[Any]]] = {
    401: {"model": ErrorResponse, "description": "Missing, invalid or revoked token"},
    403: {"model": ErrorResponse, "description": "Role may not perform this action"},
}

_RESERVATION_ERROR_RESPONSES: dict[int | str, dict[str, type[Any]]] = {
    **AUTHENTICATED_ERRORS,
    404: {"model": ErrorResponse, "description": "No such reservation, space or vehicle"},
    409: {
        "model": ErrorResponse,
        "description": "No space free, already booked, or wrong booking state",
    },
    422: {"model": ErrorResponse, "description": "Request validation failed"},
    429: {"model": ErrorResponse, "description": "Too many attempts"},
}

_CREATE_RESPONSES: dict[int | str, dict[str, type[Any]]] = {
    **_RESERVATION_ERROR_RESPONSES,
    status.HTTP_201_CREATED: {
        "model": ReservationResponse,
        "description": "Booking confirmed and slots claimed",
    },
}


def _write_throttle(request: Request, user: Any, settings: Settings) -> None:
    throttle(
        request,
        bucket="reservation-write",
        identity=user.id,
        rule=RateLimit(
            limit=settings.reservation_write_rate_limit,
            window_seconds=settings.reservation_write_rate_window_seconds,
        ),
    )


def _reservation(document: dict[str, Any]) -> ReservationResponse:
    return ReservationResponse.model_validate(public_reservation(document))


def _session(document: dict[str, Any]) -> SessionResponse:
    return SessionResponse.model_validate(public_session(document))


@router.get(
    "/availability",
    response_model=ReservationAvailabilityResponse,
    summary="Free-space count for a time window",
    description=(
        "Counts reservable spaces in open zones that have no slot claimed for "
        "the interval. `totalReservable` is the facility's bookable capacity; "
        "`availableSpaces` is how many of those are still free for the whole "
        "window. `spaceType` narrows both counts to one space type."
    ),
    responses=_RESERVATION_ERROR_RESPONSES,
)
async def get_reservation_availability(
    user: Annotated[CurrentUser, Depends(require_roles(*_ALL_ROLES))],
    db: Annotated[AsyncDatabase, Depends(get_database)],
    settings: Annotated[Settings, Depends(get_settings)],
    facility_id: Annotated[str, Query(alias="facilityId", min_length=1, max_length=64)],
    start_time: Annotated[datetime, Query(alias="startTime")],
    end_time: Annotated[datetime, Query(alias="endTime")],
    space_type: Annotated[
        SpaceType | None, Query(alias="spaceType", description="Only this space type")
    ] = None,
) -> ReservationAvailabilityResponse:
    row = await reservation_service.availability(
        db,
        user_id=user.id,
        role=user.role,
        facility_ids=user.facility_ids,
        facility_id=facility_id,
        start=start_time,
        end=end_time,
        settings=settings,
        space_type=space_type.value if space_type is not None else None,
    )
    return ReservationAvailabilityResponse.model_validate(row)


@router.post(
    "",
    response_model=ReservationResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Book a parking space",
    description=(
        "Drivers only. `startTime` and `endTime` are ISO-8601 with an offset and "
        "must align to a 30-minute slot. The booking may start up to the "
        "configured advance window and last up to 24 hours. When `spaceId` is "
        "omitted the engine picks the first free space in an open zone; when it "
        "is supplied, that exact space is booked or the request answers `409`. "
        "`estimatedAmount` is the facility's base-rate estimate, not a payment."
    ),
    responses=_CREATE_RESPONSES,
)
async def create_reservation(
    payload: CreateReservationRequest,
    request: Request,
    user: Annotated[CurrentUser, Depends(require_roles(Role.DRIVER))],
    db: Annotated[AsyncDatabase, Depends(get_database)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> ReservationResponse:
    _write_throttle(request, user, settings)
    document = await reservation_service.create_reservation(
        db, user_id=user.id, payload=payload, settings=settings
    )
    await audit.record(
        db,
        action="RESERVATION_CREATED",
        entity="reservation",
        actor_id=user.id,
        entity_id=document["_id"],
        ip=client_ip(request),
        metadata={
            "reference": document.get("reference"),
            "facilityId": str(document.get("facilityId")),
            "spaceId": str(document.get("spaceId")),
        },
    )
    return _reservation(document)


@router.get(
    "",
    response_model=PaginatedResponse[ReservationResponse],
    summary="List reservations in the caller's scope",
    description=(
        "Drivers see their own bookings, staff the facilities assigned to them, "
        "operators the facilities they own, admins everything. `from`/`to` "
        "filter on the booking start, `status` on the DOC section 17 state, "
        "`facilityId` narrows to one facility, and `q` matches reference, QR "
        "token or vehicle plate. `order=desc` is useful for history."
    ),
    responses=_RESERVATION_ERROR_RESPONSES,
)
async def list_reservations(
    user: Annotated[CurrentUser, Depends(require_roles(*_ALL_ROLES))],
    db: Annotated[AsyncDatabase, Depends(get_database)],
    settings: Annotated[Settings, Depends(get_settings)],
    status_filter: Annotated[
        ReservationStatus | None, Query(alias="status", description="Booking state")
    ] = None,
    facility_id: Annotated[
        str | None, Query(alias="facilityId", min_length=1, max_length=64)
    ] = None,
    start: Annotated[
        datetime | None, Query(alias="from", description="Only bookings starting at/after")
    ] = None,
    end: Annotated[
        datetime | None, Query(alias="to", description="Only bookings starting before")
    ] = None,
    q: Annotated[str | None, Query(max_length=64, description="Reference, QR or plate")] = None,
    order: Annotated[Literal["asc", "desc"], Query(description="Start-time order")] = "asc",
    page: Annotated[int, Query(ge=1, description="1-based page number")] = 1,
    limit: Annotated[int, Query(ge=1, le=100, description="Results per page")] = 20,
) -> PaginatedResponse[ReservationResponse]:
    rows, total = await reservation_service.list_reservations(
        db,
        user_id=user.id,
        role=user.role,
        facility_ids=user.facility_ids,
        status=status_filter,
        facility_id=facility_id,
        start=start,
        end=end,
        q=q,
        page=page,
        limit=limit,
        order=order,
        settings=settings,
    )
    return PaginatedResponse[ReservationResponse].build(
        [_reservation(row) for row in rows], total=total, page=page, limit=limit
    )


@router.get(
    "/lookup",
    response_model=ReservationResponse,
    summary="Look up a booking by reference or QR token",
    description=(
        "Staff, operators and admins only. Accepts the human-readable reference "
        "(case-insensitive) or the exact QR token scanned from the driver's "
        "code, and still scopes the result to the caller's facilities."
    ),
    responses=_RESERVATION_ERROR_RESPONSES,
)
async def lookup_reservation(
    user: Annotated[CurrentUser, Depends(require_roles(*_STAFF_ROLES))],
    db: Annotated[AsyncDatabase, Depends(get_database)],
    code: Annotated[str, Query(min_length=1, max_length=128, description="Reference or QR token")],
) -> ReservationResponse:
    document = await reservation_service.lookup_reservation(
        db,
        user_id=user.id,
        role=user.role,
        facility_ids=user.facility_ids,
        code=code,
    )
    return _reservation(document)


@router.get(
    "/{reservation_id}",
    response_model=ReservationResponse,
    summary="Read one reservation",
    description=(
        "Readable by the driver who owns it, staff assigned to its facility, "
        "the owning operator and any admin. A foreign or malformed id answers "
        "`404`."
    ),
    responses=_RESERVATION_ERROR_RESPONSES,
)
async def get_reservation(
    reservation_id: str,
    user: Annotated[CurrentUser, Depends(require_roles(*_ALL_ROLES))],
    db: Annotated[AsyncDatabase, Depends(get_database)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> ReservationResponse:
    document = await reservation_service.get_reservation(
        db,
        user_id=user.id,
        role=user.role,
        facility_ids=user.facility_ids,
        reservation_id=reservation_id,
        settings=settings,
    )
    return _reservation(document)


@router.patch(
    "/{reservation_id}/cancel",
    response_model=ReservationResponse,
    summary="Cancel a confirmed reservation",
    description=(
        "Only a `CONFIRMED` booking can be cancelled. The slot rows are "
        "released immediately, so the space becomes bookable again. A driver "
        "may cancel their own booking; staff, operators and admins may cancel "
        "one in their scope."
    ),
    responses=_RESERVATION_ERROR_RESPONSES,
)
async def cancel_reservation(
    reservation_id: str,
    payload: CancelReservationRequest,
    request: Request,
    user: Annotated[CurrentUser, Depends(require_roles(*_ALL_ROLES))],
    db: Annotated[AsyncDatabase, Depends(get_database)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> ReservationResponse:
    _write_throttle(request, user, settings)
    document = await reservation_service.cancel_reservation(
        db,
        user_id=user.id,
        role=user.role,
        facility_ids=user.facility_ids,
        reservation_id=reservation_id,
        reason=payload.reason,
    )
    await audit.record(
        db,
        action="RESERVATION_CANCELLED",
        entity="reservation",
        actor_id=user.id,
        entity_id=document["_id"],
        ip=client_ip(request),
        metadata={"reference": document.get("reference")},
    )
    return _reservation(document)


@router.post(
    "/{reservation_id}/check-in",
    response_model=CheckInResponse,
    summary="Check a reservation in",
    description=(
        "Staff only. Starts the parking session and marks the space `OCCUPIED`. "
        "Check-in opens `reservation_checkin_early_minutes` before the start "
        "and closes at the booking's end. If the booked space is unusable, "
        "staff may send `spaceId` to move the vehicle to another free space."
    ),
    responses=_RESERVATION_ERROR_RESPONSES,
)
async def check_in(
    reservation_id: str,
    payload: CheckInRequest,
    request: Request,
    user: Annotated[CurrentUser, Depends(require_roles(*_STAFF_ROLES))],
    db: Annotated[AsyncDatabase, Depends(get_database)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> CheckInResponse:
    _write_throttle(request, user, settings)
    reservation, session = await reservation_service.check_in(
        db,
        user_id=user.id,
        role=user.role,
        facility_ids=user.facility_ids,
        reservation_id=reservation_id,
        override_space_id=payload.spaceId,
        closed_by_id=user.id,
        settings=settings,
    )
    await audit.record(
        db,
        action="RESERVATION_CHECKED_IN",
        entity="reservation",
        actor_id=user.id,
        entity_id=reservation["_id"],
        ip=client_ip(request),
        metadata={
            "reference": reservation.get("reference"),
            "spaceId": str(reservation.get("spaceId")),
        },
    )
    return CheckInResponse(reservation=_reservation(reservation), session=_session(session))


@router.post(
    "/{reservation_id}/check-out",
    response_model=CheckOutResponse,
    summary="Check a reservation out",
    description=(
        "Staff only. Closes the session, returns the space to `AVAILABLE`, "
        "releases the booking's slots and returns the duration and a base-rate "
        "estimate (`amountEstimated: true`). Payments are a later module."
    ),
    responses=_RESERVATION_ERROR_RESPONSES,
)
async def check_out(
    reservation_id: str,
    request: Request,
    user: Annotated[CurrentUser, Depends(require_roles(*_STAFF_ROLES))],
    db: Annotated[AsyncDatabase, Depends(get_database)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> CheckOutResponse:
    _write_throttle(request, user, settings)
    reservation, session = await reservation_service.check_out(
        db,
        user_id=user.id,
        role=user.role,
        facility_ids=user.facility_ids,
        reservation_id=reservation_id,
        closed_by_id=user.id,
    )
    await audit.record(
        db,
        action="RESERVATION_CHECKED_OUT",
        entity="reservation",
        actor_id=user.id,
        entity_id=reservation["_id"],
        ip=client_ip(request),
        metadata={
            "reference": reservation.get("reference"),
            "sessionId": str(session["_id"]),
        },
    )
    return CheckOutResponse(reservation=_reservation(reservation), session=_session(session))


__all__ = ["router"]
