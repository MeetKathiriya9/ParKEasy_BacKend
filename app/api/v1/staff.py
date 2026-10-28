"""The `staff` group: operator management of staff accounts and assignments.

    GET    /                                      list staff the caller may see
    POST   /                                      create a staff account (201)
    GET    /{staff_id}                            one staff member + history
    PATCH  /{staff_id}                            edit name / phone / ownership
    PATCH  /{staff_id}/status                     suspend or reactivate
    GET    /{staff_id}/assignments                assignment history
    POST   /{staff_id}/assignments                assign a facility (201)
    DELETE /{staff_id}/assignments/{assignment_id}  end an assignment

Operators manage the staff accounts they own and assign them only to their own
facilities; admins reach every staff member and every facility. Drivers and
staff themselves are refused with a `403` - the staff portal is not a
management surface.

Assignment history is a list of periods, not a single "current facility": a
member moved from City Mall to Airport and back has three rows, which is what
the audit trail in DOC section 11 asks for. Ending an assignment never deletes
the row and never deletes the staff account; off-boarding is
`PATCH /staff/{id}/status` with `SUSPENDED`.
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
from app.models.enums import AssignmentStatus, Role, UserStatus
from app.models.staff import MAX_ASSIGNMENT_NOTE_LENGTH
from app.schemas.common import ErrorResponse
from app.schemas.staff import (
    ChangeStaffStatusRequest,
    CreateAssignmentRequest,
    CreateStaffRequest,
    StaffAssignmentResponse,
    StaffDetailResponse,
    StaffResponse,
    UpdateStaffRequest,
)
from app.services import audit
from app.services import staff as staff_service

router = APIRouter(prefix="/staff", tags=["staff"])

_MANAGE_ROLES = [Role.OPERATOR, Role.ADMIN]

# Shared by every route here: a missing/expired token is a 401, a role outside
# the allow-list is a 403.
AUTHENTICATED_ERRORS: dict[int | str, dict[str, type[Any]]] = {
    401: {"model": ErrorResponse, "description": "Missing, invalid or revoked token"},
    403: {"model": ErrorResponse, "description": "Role may not perform this action"},
}

_STAFF_ERROR_RESPONSES: dict[int | str, dict[str, type[Any]]] = {
    **AUTHENTICATED_ERRORS,
    404: {"model": ErrorResponse, "description": "No such staff member or assignment"},
    409: {
        "model": ErrorResponse,
        "description": "Duplicate assignment, suspended staff or operator limit reached",
    },
    422: {"model": ErrorResponse, "description": "Request validation failed"},
    429: {"model": ErrorResponse, "description": "Too many attempts"},
}

_CREATE_RESPONSES: dict[int | str, dict[str, type[Any]]] = {
    **_STAFF_ERROR_RESPONSES,
    status.HTTP_201_CREATED: {
        "model": StaffDetailResponse,
        "description": "Staff account created with any initial assignments",
    },
}

_ASSIGN_RESPONSES: dict[int | str, dict[str, type[Any]]] = {
    **_STAFF_ERROR_RESPONSES,
    status.HTTP_201_CREATED: {
        "model": StaffAssignmentResponse,
        "description": "Assignment opened",
    },
}


def _write_throttle(request: Request, user: Any, settings: Settings) -> None:
    """One bucket for every staff write, so a script cannot hammer the group."""
    throttle(
        request,
        bucket="staff-write",
        identity=user.id,
        rule=RateLimit(
            limit=settings.staff_write_rate_limit,
            window_seconds=settings.staff_write_rate_window_seconds,
        ),
    )


@router.get(
    "",
    response_model=list[StaffResponse],
    summary="List staff the caller may see",
    description=(
        "Operators get the staff accounts they own, admins every staff account. "
        "Sorted by name. `facilityId` narrows the list to members with an active "
        "assignment to that facility, `status` to one account state, and `q` "
        "matches name or email case-insensitively. `facilityIds` on each row is "
        "derived from the active assignment rows, so it always agrees with the "
        "staff member's live access."
    ),
    responses=_STAFF_ERROR_RESPONSES,
)
async def list_staff(
    user: Annotated[CurrentUser, Depends(require_roles(*_MANAGE_ROLES))],
    db: Annotated[AsyncDatabase, Depends(get_database)],
    facility_id: Annotated[
        str | None, Query(alias="facilityId", description="Only staff assigned here")
    ] = None,
    status_filter: Annotated[
        UserStatus | None, Query(alias="status", description="Filter by account status")
    ] = None,
    q: Annotated[str | None, Query(max_length=100, description="Match name or email")] = None,
) -> list[StaffResponse]:
    rows = await staff_service.list_staff(
        db,
        user_id=user.id,
        role=user.role,
        facility_id=facility_id,
        status=status_filter,
        query=q,
    )
    return [StaffResponse.model_validate(row) for row in rows]


@router.post(
    "",
    response_model=StaffDetailResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a staff account",
    description=(
        "The account is owned by the signed-in operator and starts `ACTIVE`, "
        "with the password supplied here (it can be changed later through the "
        "normal profile flow). An admin must send `operatorId` to say which "
        "operator owns the new account; an operator sending it answers a `422`. "
        "`facilityIds` is an optional convenience that opens the same "
        "assignments `POST /staff/{id}/assignments` would, and every facility "
        "must belong to the owning operator. The email must be unused across all "
        "roles."
    ),
    responses=_CREATE_RESPONSES,
)
async def create_staff(
    payload: CreateStaffRequest,
    request: Request,
    user: Annotated[CurrentUser, Depends(require_roles(*_MANAGE_ROLES))],
    db: Annotated[AsyncDatabase, Depends(get_database)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> StaffDetailResponse:
    _write_throttle(request, user, settings)

    document = await staff_service.create_staff(
        db, user_id=user.id, role=user.role, payload=payload, settings=settings
    )

    await audit.record(
        db,
        action="STAFF_CREATED",
        entity="staff",
        actor_id=user.id,
        entity_id=document["id"],
        ip=client_ip(request),
        metadata={
            "fields": sorted(payload.model_fields_set),
            "operatorId": document.get("operatorId"),
            "facilityIds": document.get("facilityIds", []),
        },
    )
    return StaffDetailResponse.model_validate(document)


@router.get(
    "/{staff_id}",
    response_model=StaffDetailResponse,
    summary="Read one staff member",
    description=(
        "Includes the full assignment history, newest first. A malformed, "
        "unknown or foreign id answers `404` with the same message, so the "
        "endpoint cannot be used to probe for ids."
    ),
    responses=_STAFF_ERROR_RESPONSES,
)
async def get_staff(
    staff_id: str,
    user: Annotated[CurrentUser, Depends(require_roles(*_MANAGE_ROLES))],
    db: Annotated[AsyncDatabase, Depends(get_database)],
) -> StaffDetailResponse:
    document = await staff_service.get_staff(
        db, user_id=user.id, role=user.role, staff_id=staff_id
    )
    return StaffDetailResponse.model_validate(document)


@router.patch(
    "/{staff_id}",
    response_model=StaffDetailResponse,
    summary="Edit a staff member",
    description=(
        "Partial update of `name` and `phone`. `email`, `role` and `status` are "
        "deliberately not writable here (`status` has its own route), so they "
        "answer a `422`. An admin may set `operatorId` to adopt a staff account "
        "that has no owner yet; ownership cannot be moved once set, and adoption "
        "is refused while the member has active assignments belonging to another "
        "operator."
    ),
    responses=_STAFF_ERROR_RESPONSES,
)
async def update_staff(
    staff_id: str,
    payload: UpdateStaffRequest,
    request: Request,
    user: Annotated[CurrentUser, Depends(require_roles(*_MANAGE_ROLES))],
    db: Annotated[AsyncDatabase, Depends(get_database)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> StaffDetailResponse:
    _write_throttle(request, user, settings)

    document, changed = await staff_service.update_staff(
        db, user_id=user.id, role=user.role, staff_id=staff_id, payload=payload
    )

    if changed:
        await audit.record(
            db,
            action="STAFF_UPDATED",
            entity="staff",
            actor_id=user.id,
            entity_id=document["_id"],
            ip=client_ip(request),
            metadata={"fields": sorted(payload.model_fields_set)},
        )
    detail = await staff_service.get_staff(
        db, user_id=user.id, role=user.role, staff_id=staff_id
    )
    return StaffDetailResponse.model_validate(detail)


@router.patch(
    "/{staff_id}/status",
    response_model=StaffDetailResponse,
    summary="Suspend or reactivate a staff member",
    description=(
        "The off-boarding workflow: `SUSPENDED` revokes access on the member's "
        "next request while leaving their assignment rows and `facilityIds` "
        "intact, so `ACTIVE` restores exactly the access they had. Only these "
        "two states are accepted. Setting the status the account already holds "
        "is a no-op that still answers `200`."
    ),
    responses=_STAFF_ERROR_RESPONSES,
)
async def change_staff_status(
    staff_id: str,
    payload: ChangeStaffStatusRequest,
    request: Request,
    user: Annotated[CurrentUser, Depends(require_roles(*_MANAGE_ROLES))],
    db: Annotated[AsyncDatabase, Depends(get_database)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> StaffDetailResponse:
    _write_throttle(request, user, settings)

    document, previous = await staff_service.set_staff_status(
        db, user_id=user.id, role=user.role, staff_id=staff_id, payload=payload
    )

    if previous != payload.status.value:
        metadata: dict[str, Any] = {"from": previous, "to": payload.status.value}
        if payload.reason is not None:
            metadata["reason"] = payload.reason
        await audit.record(
            db,
            action="STAFF_STATUS_CHANGED",
            entity="staff",
            actor_id=user.id,
            entity_id=document["_id"],
            ip=client_ip(request),
            metadata=metadata,
        )
    detail = await staff_service.get_staff(
        db, user_id=user.id, role=user.role, staff_id=staff_id
    )
    return StaffDetailResponse.model_validate(detail)


@router.get(
    "/{staff_id}/assignments",
    response_model=list[StaffAssignmentResponse],
    summary="Assignment history",
    description=(
        "Newest first, including closed periods. `status=ACTIVE` lists the "
        "facilities the member covers right now; omit it for the full history."
    ),
    responses=_STAFF_ERROR_RESPONSES,
)
async def list_assignments(
    staff_id: str,
    user: Annotated[CurrentUser, Depends(require_roles(*_MANAGE_ROLES))],
    db: Annotated[AsyncDatabase, Depends(get_database)],
    status_filter: Annotated[
        AssignmentStatus | None, Query(alias="status", description="Filter by period state")
    ] = None,
) -> list[StaffAssignmentResponse]:
    rows = await staff_service.list_assignments(
        db,
        user_id=user.id,
        role=user.role,
        staff_id=staff_id,
        status=status_filter,
    )
    return [StaffAssignmentResponse.model_validate(row) for row in rows]


@router.post(
    "/{staff_id}/assignments",
    response_model=StaffAssignmentResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Assign a facility",
    description=(
        "Opens an `ACTIVE` assignment period. The staff member must be `ACTIVE` "
        "(`409 STAFF_NOT_ACTIVE` otherwise) and already assigned to that facility "
        "answers `409 ASSIGNMENT_EXISTS`. An operator may assign only their own "
        "facilities, and any id they may not use - malformed, unknown or foreign "
        "- answers `404`. Re-assigning after ending an earlier period starts a "
        "new row, preserving the history."
    ),
    responses=_ASSIGN_RESPONSES,
)
async def assign_facility(
    staff_id: str,
    payload: CreateAssignmentRequest,
    request: Request,
    user: Annotated[CurrentUser, Depends(require_roles(*_MANAGE_ROLES))],
    db: Annotated[AsyncDatabase, Depends(get_database)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> StaffAssignmentResponse:
    _write_throttle(request, user, settings)

    assignment = await staff_service.assign_staff(
        db, user_id=user.id, role=user.role, staff_id=staff_id, payload=payload
    )

    await audit.record(
        db,
        action="STAFF_ASSIGNED",
        entity="staff",
        actor_id=user.id,
        entity_id=staff_id,
        ip=client_ip(request),
        metadata={
            "assignmentId": assignment["id"],
            "facilityId": assignment["facilityId"],
        },
    )
    return StaffAssignmentResponse.model_validate(assignment)


@router.delete(
    "/{staff_id}/assignments/{assignment_id}",
    response_model=StaffAssignmentResponse,
    summary="End an assignment",
    description=(
        "Closes the period as `INACTIVE` with who/when/why (an optional "
        "`reason` query parameter) instead of deleting it, so the history stays "
        "complete. The staff member may already be suspended - that is the "
        "normal off-boarding order. An assignment that is unknown, belongs to "
        "another staff member, or is already closed answers `404`."
    ),
    responses=_STAFF_ERROR_RESPONSES,
)
async def unassign_facility(
    staff_id: str,
    assignment_id: str,
    request: Request,
    user: Annotated[CurrentUser, Depends(require_roles(*_MANAGE_ROLES))],
    db: Annotated[AsyncDatabase, Depends(get_database)],
    settings: Annotated[Settings, Depends(get_settings)],
    reason: Annotated[
        str | None, Query(max_length=MAX_ASSIGNMENT_NOTE_LENGTH)
    ] = None,
) -> StaffAssignmentResponse:
    _write_throttle(request, user, settings)

    assignment = await staff_service.unassign_staff(
        db,
        user_id=user.id,
        role=user.role,
        staff_id=staff_id,
        assignment_id=assignment_id,
        reason=reason,
    )

    await audit.record(
        db,
        action="STAFF_UNASSIGNED",
        entity="staff",
        actor_id=user.id,
        entity_id=staff_id,
        ip=client_ip(request),
        metadata={
            "assignmentId": assignment["id"],
            "facilityId": assignment["facilityId"],
            "reason": reason,
        },
    )
    return StaffAssignmentResponse.model_validate(assignment)


__all__ = ["router"]
