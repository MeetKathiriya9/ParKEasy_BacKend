"""The `facilities` group: operator management of parking facilities.

    GET    /                         list what the caller may see
    POST   /                         create a facility (201)
    GET    /{facility_id}            read one
    PATCH  /{facility_id}            edit one
    DELETE /{facility_id}            remove one (204)
    PATCH  /{facility_id}/status     close / reopen / maintain (DOC 11.169)

This group is *management*: who may create and edit facilities. Driver-facing
discovery (nearby search, availability, details) is the separate `parking`
group, per DOC section 21.

Scope is role-aware rather than "own vehicles only": an operator manages the
facilities whose `operatorId` is theirs (one account, many facilities - DOC
section 8), an admin manages all of them, and staff may only read the ones
their token assigns them. Drivers are refused with a `403` here; their window
onto facilities is `/parking`.

DOC section 21 lists no CRUD row for facilities (it only names `/api/parking`
for discovery), so this group is a documented deviation - see the server
README. It is what DOC sections 7 and 11 ("Create and manage parking
facilities") actually require.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request, status
from pymongo.asynchronous.database import AsyncDatabase

from app.api.deps import CurrentUser, require_roles
from app.api.throttle import client_ip, throttle
from app.core.config import Settings, get_settings
from app.core.rate_limit import RateLimit
from app.db.client import get_database
from app.models.enums import FacilityStatus, Role
from app.models.facility import public_facility
from app.schemas.common import ErrorResponse
from app.schemas.facility import (
    ChangeFacilityStatusRequest,
    CreateFacilityRequest,
    FacilityResponse,
    UpdateFacilityRequest,
)
from app.services import audit
from app.services import facilities as facilities_service

router = APIRouter(prefix="/facilities", tags=["facilities"])

_MANAGE_ROLES = [Role.OPERATOR, Role.ADMIN]
_READ_ROLES = [Role.OPERATOR, Role.ADMIN, Role.STAFF]

# Shared by every route here: a missing/expired token is a 401, and a role
# outside the route's allow-list is a 403.
AUTHENTICATED_ERRORS: dict[int | str, dict[str, type[Any]]] = {
    401: {"model": ErrorResponse, "description": "Missing, invalid or revoked token"},
    403: {"model": ErrorResponse, "description": "Role may not perform this action"},
}

_FACILITIES_ERROR_RESPONSES: dict[int | str, dict[str, type[Any]]] = {
    **AUTHENTICATED_ERRORS,
    404: {"model": ErrorResponse, "description": "No such facility for this account"},
    409: {
        "model": ErrorResponse,
        "description": "Facility is still in use or the operator is at the facility limit",
    },
    422: {"model": ErrorResponse, "description": "Request validation failed"},
    429: {"model": ErrorResponse, "description": "Too many attempts"},
}

_CREATE_RESPONSES: dict[int | str, dict[str, type[Any]]] = {
    **_FACILITIES_ERROR_RESPONSES,
    status.HTTP_201_CREATED: {"model": FacilityResponse, "description": "Facility created"},
}


def _to_response(document: dict[str, Any], operator_name: str | None = None) -> FacilityResponse:
    return FacilityResponse.model_validate(public_facility(document, operator_name))


async def _one_response(db: AsyncDatabase, document: dict[str, Any]) -> FacilityResponse:
    """Serialise a single document, resolving the owning operator's name."""
    names = await facilities_service.operator_names(db, [document])
    return _to_response(document, names.get(str(document.get("operatorId"))))


def _write_throttle(request: Request, user: Any, settings: Settings) -> None:
    """One bucket for every facility write, so a script cannot hammer the group."""
    throttle(
        request,
        bucket="facility-write",
        identity=user.id,
        rule=RateLimit(
            limit=settings.facility_write_rate_limit,
            window_seconds=settings.facility_write_rate_window_seconds,
        ),
    )


@router.get(
    "",
    response_model=list[FacilityResponse],
    summary="List facilities the caller may see",
    description=(
        "Oldest first. Operators get the facilities they own, admins every "
        "facility, and staff only the facilities assigned to them by "
        "`facilityIds`. Drivers receive a `403` - discovery is the `parking` "
        "group. An optional `status` query parameter narrows the listing to one "
        "DOC section 17 state."
    ),
    responses=AUTHENTICATED_ERRORS,
)
async def list_facilities(
    user: Annotated[CurrentUser, Depends(require_roles(*_READ_ROLES))],
    db: Annotated[AsyncDatabase, Depends(get_database)],
    status_filter: Annotated[
        FacilityStatus | None, Query(alias="status", description="Filter by facility status")
    ] = None,
) -> list[FacilityResponse]:
    documents = await facilities_service.list_facilities(
        db,
        user_id=user.id,
        role=user.role,
        facility_ids=user.facility_ids,
        status=status_filter,
    )
    names = await facilities_service.operator_names(db, documents)
    return [
        _to_response(document, names.get(str(document.get("operatorId"))))
        for document in documents
    ]


@router.post(
    "",
    response_model=FacilityResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a parking facility",
    description=(
        "The facility is owned by the signed-in operator (`operatorId` comes "
        "from the token) and starts in the `ACTIVE` state. An admin may instead "
        "send `operatorId` to create it on another operator's behalf; an "
        "operator sending that field answers a `422`. `status`, `hasEV` and the "
        "other derived flags are not writable and are likewise a `422`. "
        "`lat`/`lng` are stored as a GeoJSON Point behind a `2dsphere` index "
        "(DOC section 19)."
    ),
    responses=_CREATE_RESPONSES,
)
async def create_facility(
    payload: CreateFacilityRequest,
    request: Request,
    user: Annotated[CurrentUser, Depends(require_roles(*_MANAGE_ROLES))],
    db: Annotated[AsyncDatabase, Depends(get_database)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> FacilityResponse:
    _write_throttle(request, user, settings)

    document = await facilities_service.create_facility(
        db, user_id=user.id, role=user.role, payload=payload, settings=settings
    )

    await audit.record(
        db,
        action="FACILITY_CREATED",
        entity="facility",
        actor_id=user.id,
        entity_id=document["_id"],
        ip=client_ip(request),
        # Field *names* only, matching the vehicle and profile audit rows.
        metadata={"fields": sorted(payload.model_fields_set)},
    )
    return await _one_response(db, document)


@router.get(
    "/{facility_id}",
    response_model=FacilityResponse,
    summary="Read one facility",
    description=(
        "Readable by the owning operator, any admin, and staff assigned to the "
        "facility. A malformed or foreign id answers `404` with the same "
        "message, so the endpoint cannot be used to probe for ids."
    ),
    responses=_FACILITIES_ERROR_RESPONSES,
)
async def get_facility(
    facility_id: str,
    user: Annotated[CurrentUser, Depends(require_roles(*_READ_ROLES))],
    db: Annotated[AsyncDatabase, Depends(get_database)],
) -> FacilityResponse:
    document = await facilities_service.get_facility(
        db,
        user_id=user.id,
        role=user.role,
        facility_ids=user.facility_ids,
        facility_id=facility_id,
    )
    return await _one_response(db, document)


@router.patch(
    "/{facility_id}",
    response_model=FacilityResponse,
    summary="Edit a facility",
    description=(
        "Partial update: send only what changed. `status` is deliberately not "
        "writable here and answers a `422` pointing at "
        "`PATCH /facilities/{facility_id}/status`; `operatorId` never changes. "
        "`open24h`, `openingTime` and `closingTime` are validated against the "
        "merged result, so flipping a 24-hour garage to timed hours without "
        "supplying them is refused. An id that does not belong to the caller "
        "answers `404`."
    ),
    responses=_FACILITIES_ERROR_RESPONSES,
)
async def update_facility(
    facility_id: str,
    payload: UpdateFacilityRequest,
    request: Request,
    user: Annotated[CurrentUser, Depends(require_roles(*_MANAGE_ROLES))],
    db: Annotated[AsyncDatabase, Depends(get_database)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> FacilityResponse:
    _write_throttle(request, user, settings)

    document, changed = await facilities_service.update_facility(
        db, user_id=user.id, role=user.role, facility_id=facility_id, payload=payload
    )

    if changed:
        await audit.record(
            db,
            action="FACILITY_UPDATED",
            entity="facility",
            actor_id=user.id,
            entity_id=document["_id"],
            ip=client_ip(request),
            metadata={"fields": sorted(payload.model_fields_set)},
        )
    return await _one_response(db, document)


@router.patch(
    "/{facility_id}/status",
    response_model=FacilityResponse,
    summary="Change a facility's status",
    description=(
        "The close/reopen workflow of DOC section 11.169: any transition among "
        "`ACTIVE`, `INACTIVE`, `TEMPORARILY_CLOSED` and `MAINTENANCE` is "
        "allowed, and an optional `reason` is stored with the audit row. "
        "Setting the status a facility already holds is a no-op that still "
        "answers `200`. (Notification of affected drivers arrives with the "
        "notifications group; until then the reason is for the operator's own "
        "audit trail.)"
    ),
    responses=_FACILITIES_ERROR_RESPONSES,
)
async def change_facility_status(
    facility_id: str,
    payload: ChangeFacilityStatusRequest,
    request: Request,
    user: Annotated[CurrentUser, Depends(require_roles(*_MANAGE_ROLES))],
    db: Annotated[AsyncDatabase, Depends(get_database)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> FacilityResponse:
    _write_throttle(request, user, settings)

    document, previous = await facilities_service.set_status(
        db, user_id=user.id, role=user.role, facility_id=facility_id, payload=payload
    )

    if previous != payload.status.value:
        metadata: dict[str, Any] = {"from": previous, "to": payload.status.value}
        if payload.reason is not None:
            metadata["reason"] = payload.reason
        await audit.record(
            db,
            action="FACILITY_STATUS_CHANGED",
            entity="facility",
            actor_id=user.id,
            entity_id=document["_id"],
            ip=client_ip(request),
            metadata=metadata,
        )
    return await _one_response(db, document)


@router.delete(
    "/{facility_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a facility",
    description=(
        "Hard delete, refused with `409` and code `FACILITY_IN_USE` while any "
        "reservation or parking session still points at the facility - retiring "
        "a busy garage is a status change, not a delete. An unknown id answers "
        "`404`."
    ),
    responses=_FACILITIES_ERROR_RESPONSES,
)
async def delete_facility(
    facility_id: str,
    request: Request,
    user: Annotated[CurrentUser, Depends(require_roles(*_MANAGE_ROLES))],
    db: Annotated[AsyncDatabase, Depends(get_database)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> None:
    _write_throttle(request, user, settings)

    document = await facilities_service.delete_facility(
        db, user_id=user.id, role=user.role, facility_id=facility_id
    )

    await audit.record(
        db,
        action="FACILITY_DELETED",
        entity="facility",
        actor_id=user.id,
        entity_id=document["_id"],
        ip=client_ip(request),
        metadata={"status": document.get("status")},
    )


__all__ = ["router"]
