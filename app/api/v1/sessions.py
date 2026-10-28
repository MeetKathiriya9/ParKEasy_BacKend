"""The `sessions` group: open parking sessions, close and (later) extend.

    GET    /active            the signed-in driver's open session, if any
    GET    /                  list sessions in the caller's scope
    GET    /{session_id}      one session
    PATCH  /{session_id}/extend   planned; answers 501
    POST   /{session_id}/close    close an active session

A session is created by staff check-in (`POST /reservations/{id}/check-in`) and
closed by staff check-out or by `POST /sessions/{id}/close` here. Drivers can
read their own sessions; staff, operators and admins see the sessions in their
facility scope. Extension is deliberately not implemented: it would need a
pricing rule for the extra time and a slot re-claim, both later modules.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query, Request
from pymongo.asynchronous.database import AsyncDatabase

from app.api.deps import CurrentUser, require_roles
from app.api.throttle import client_ip, throttle
from app.core.config import Settings, get_settings
from app.core.errors import NotImplementedYetError
from app.core.rate_limit import RateLimit
from app.db.client import get_database
from app.models.enums import Role, SessionStatus
from app.models.reservation import public_session
from app.schemas.common import ErrorResponse, PaginatedResponse
from app.schemas.session import SessionResponse
from app.services import audit
from app.services import reservations as reservation_service

router = APIRouter(prefix="/sessions", tags=["sessions"])

_ALL_ROLES = [Role.DRIVER, Role.STAFF, Role.OPERATOR, Role.ADMIN]
_STAFF_ROLES = [Role.STAFF, Role.OPERATOR, Role.ADMIN]

AUTHENTICATED_ERRORS: dict[int | str, dict[str, type[Any]]] = {
    401: {"model": ErrorResponse, "description": "Missing, invalid or revoked token"},
    403: {"model": ErrorResponse, "description": "Role may not perform this action"},
}

_SESSION_ERROR_RESPONSES: dict[int | str, dict[str, type[Any]]] = {
    **AUTHENTICATED_ERRORS,
    404: {"model": ErrorResponse, "description": "No such parking session"},
    409: {
        "model": ErrorResponse,
        "description": "Session already closed or not active",
    },
    422: {"model": ErrorResponse, "description": "Request validation failed"},
    429: {"model": ErrorResponse, "description": "Too many attempts"},
}


def _write_throttle(request: Request, user: Any, settings: Settings) -> None:
    throttle(
        request,
        bucket="session-write",
        identity=user.id,
        rule=RateLimit(
            limit=settings.session_write_rate_limit,
            window_seconds=settings.session_write_rate_window_seconds,
        ),
    )


def _session(document: dict[str, Any]) -> SessionResponse:
    return SessionResponse.model_validate(public_session(document))


@router.get(
    "/active",
    response_model=SessionResponse | None,
    summary="The signed-in driver's open session",
    description=(
        "Drivers only. Returns the `ACTIVE` or `EXTENDED` session the driver is "
        "currently parked in, or `null` when they are not parked. Staff use the "
        "list with `status=ACTIVE` instead."
    ),
    responses=_SESSION_ERROR_RESPONSES,
)
async def active_session(
    user: Annotated[CurrentUser, Depends(require_roles(Role.DRIVER))],
    db: Annotated[AsyncDatabase, Depends(get_database)],
) -> SessionResponse | None:
    document = await reservation_service.active_session(db, user_id=user.id)
    return _session(document) if document is not None else None


@router.get(
    "",
    response_model=PaginatedResponse[SessionResponse],
    summary="List parking sessions in the caller's scope",
    description=(
        "Drivers see their own sessions, staff the facilities assigned to them, "
        "operators the facilities they own, admins everything. `status` filters "
        "on the DOC section 17 state, `facilityId` narrows to one facility, "
        "`from`/`to` filter on entry time, and `q` matches reference or plate."
    ),
    responses=_SESSION_ERROR_RESPONSES,
)
async def list_sessions(
    user: Annotated[CurrentUser, Depends(require_roles(*_ALL_ROLES))],
    db: Annotated[AsyncDatabase, Depends(get_database)],
    status_filter: Annotated[
        SessionStatus | None, Query(alias="status", description="Session state")
    ] = None,
    facility_id: Annotated[
        str | None, Query(alias="facilityId", min_length=1, max_length=64)
    ] = None,
    start: Annotated[
        datetime | None, Query(alias="from", description="Only sessions entered at/after")
    ] = None,
    end: Annotated[
        datetime | None, Query(alias="to", description="Only sessions entered before")
    ] = None,
    q: Annotated[str | None, Query(max_length=64, description="Reference or plate")] = None,
    order: Annotated[Literal["asc", "desc"], Query(description="Entry-time order")] = "desc",
    page: Annotated[int, Query(ge=1, description="1-based page number")] = 1,
    limit: Annotated[int, Query(ge=1, le=100, description="Results per page")] = 20,
) -> PaginatedResponse[SessionResponse]:
    rows, total = await reservation_service.list_sessions(
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
    )
    return PaginatedResponse[SessionResponse].build(
        [_session(row) for row in rows], total=total, page=page, limit=limit
    )


@router.get(
    "/{session_id}",
    response_model=SessionResponse,
    summary="Read one parking session",
    description=(
        "Readable by the driver who owns it, staff assigned to its facility, "
        "the owning operator and any admin. A foreign or malformed id answers "
        "`404`."
    ),
    responses=_SESSION_ERROR_RESPONSES,
)
async def get_session(
    session_id: str,
    user: Annotated[CurrentUser, Depends(require_roles(*_ALL_ROLES))],
    db: Annotated[AsyncDatabase, Depends(get_database)],
) -> SessionResponse:
    document = await reservation_service.get_session(
        db,
        user_id=user.id,
        role=user.role,
        facility_ids=user.facility_ids,
        session_id=session_id,
    )
    return _session(document)


@router.patch(
    "/{session_id}/extend",
    summary="Extend a parking session (not implemented)",
    description=(
        "**Not implemented yet.** Extending a session needs a pricing rule for "
        "the extra time and a re-claim of the space's slots, both of which "
        "belong to later modules."
    ),
    responses={
        **_SESSION_ERROR_RESPONSES,
        501: {"model": ErrorResponse, "description": "Not implemented yet"},
    },
)
async def extend_session(
    session_id: str,
    user: Annotated[CurrentUser, Depends(require_roles(*_STAFF_ROLES))],
) -> None:
    raise NotImplementedYetError(
        "Session extension is planned but not implemented yet",
        details={"group": "sessions", "sessionId": session_id},
    )


@router.post(
    "/{session_id}/close",
    response_model=SessionResponse,
    summary="Close an active parking session",
    description=(
        "Staff only. Sets the exit time, computes the duration and base-rate "
        "estimate, returns the space to `AVAILABLE`, releases the booking's "
        "slots and marks the reservation `COMPLETED`."
    ),
    responses=_SESSION_ERROR_RESPONSES,
)
async def close_session(
    session_id: str,
    request: Request,
    user: Annotated[CurrentUser, Depends(require_roles(*_STAFF_ROLES))],
    db: Annotated[AsyncDatabase, Depends(get_database)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> SessionResponse:
    _write_throttle(request, user, settings)
    _, session = await reservation_service.close_session(
        db,
        user_id=user.id,
        role=user.role,
        facility_ids=user.facility_ids,
        session_id=session_id,
        closed_by_id=user.id,
    )
    await audit.record(
        db,
        action="PARKING_SESSION_CLOSED",
        entity="parkingSession",
        actor_id=user.id,
        entity_id=session["_id"],
        ip=client_ip(request),
        metadata={
            "reference": session.get("reference"),
            "spaceId": str(session.get("spaceId")),
        },
    )
    return _session(session)


__all__ = ["router"]
