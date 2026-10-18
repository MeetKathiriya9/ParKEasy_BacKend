"""The `spaces` group: the Facility -> Floor -> Zone -> Space tree.

    GET    /facilities/{facility_id}/floors         layout: floors + nested zones
    POST   /facilities/{facility_id}/floors         add a floor (201)
    PATCH  /floors/{floor_id}                        rename / relevel a floor
    DELETE /floors/{floor_id}                        remove a floor (204, cascades)
    POST   /floors/{floor_id}/zones                  add a zone (201)
    PATCH  /zones/{zone_id}                           rename a zone
    DELETE /zones/{zone_id}                           remove a zone (204, cascades)
    PATCH  /zones/{zone_id}/status                    close / reopen a zone (DOC 11.169)
    GET    /zones/{zone_id}/spaces                    spaces of one zone
    POST   /zones/{zone_id}/spaces                    add a space (201)
    POST   /zones/{zone_id}/spaces/bulk               add a run of spaces (201)
    GET    /facilities/{facility_id}/spaces           grid across a facility
    GET    /spaces/{space_id}                         read one
    PATCH  /spaces/{space_id}                         edit a space
    DELETE /spaces/{space_id}                         remove a space (204)
    PATCH  /spaces/{space_id}/status                  mark available/maintenance/blocked

DOC section 21 lists `spaces` as "Spaces, availability and status". Availability
(query for a free space, hold it) belongs to the `parking`/`reservations` groups;
here a space's `status` is *set*, and the tree it lives in is *managed*.

Scope follows the `facilities` group: operators manage their own facilities'
tree, admins any, staff read the facilities their token assigns and may also
change a space's status (DOC section 10.151). Drivers are refused.
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
from app.models.enums import Role, SpaceStatus
from app.models.space import public_floor, public_space, public_zone
from app.schemas.common import ErrorResponse
from app.schemas.space import (
    BulkCreateSpacesRequest,
    ChangeSpaceStatusRequest,
    ChangeZoneStatusRequest,
    CreateFloorRequest,
    CreateSpaceRequest,
    CreateZoneRequest,
    FloorLayoutResponse,
    FloorResponse,
    SpaceResponse,
    UpdateFloorRequest,
    UpdateSpaceRequest,
    UpdateZoneRequest,
    ZoneResponse,
)
from app.services import audit
from app.services import spaces as spaces_service

# No single prefix: the routes hang off `/facilities`, `/floors`, `/zones` and
# `/spaces`. The tag keeps them together in the OpenAPI docs.
router = APIRouter(tags=["spaces"])

_MANAGE_ROLES = [Role.OPERATOR, Role.ADMIN]
_READ_ROLES = [Role.OPERATOR, Role.ADMIN, Role.STAFF]

AUTHENTICATED_ERRORS: dict[int | str, dict[str, type[Any]]] = {
    401: {"model": ErrorResponse, "description": "Missing, invalid or revoked token"},
    403: {"model": ErrorResponse, "description": "Role may not perform this action"},
}

_SPACES_ERROR_RESPONSES: dict[int | str, dict[str, type[Any]]] = {
    **AUTHENTICATED_ERRORS,
    404: {"model": ErrorResponse, "description": "No such floor, zone or space for this account"},
    409: {
        "model": ErrorResponse,
        "description": "A limit was reached or a space number is already in use",
    },
    422: {"model": ErrorResponse, "description": "Request validation failed"},
    429: {"model": ErrorResponse, "description": "Too many attempts"},
}


def _write_throttle(request: Request, user: Any, settings: Settings) -> None:
    """One bucket for every floor/zone/space write, so a script cannot hammer the group."""
    throttle(
        request,
        bucket="space-write",
        identity=user.id,
        rule=RateLimit(
            limit=settings.space_write_rate_limit,
            window_seconds=settings.space_write_rate_window_seconds,
        ),
    )


# --- Floors -----------------------------------------------------------------


@router.get(
    "/facilities/{facility_id}/floors",
    response_model=list[FloorLayoutResponse],
    summary="Read a facility's floor and zone layout",
    description=(
        "The floors of a facility, each with its zones nested, so the "
        "operator and staff layout views are a single request. Readable by the "
        "owning operator, any admin, and staff assigned to the facility."
    ),
    responses=_SPACES_ERROR_RESPONSES,
)
async def list_floors(
    facility_id: str,
    user: Annotated[CurrentUser, Depends(require_roles(*_READ_ROLES))],
    db: Annotated[AsyncDatabase, Depends(get_database)],
) -> list[FloorLayoutResponse]:
    floors, zones = await spaces_service.list_floors(
        db,
        user_id=user.id,
        role=user.role,
        facility_ids=user.facility_ids,
        facility_id=facility_id,
    )
    by_floor: dict[str, list[ZoneResponse]] = {}
    for zone in zones:
        by_floor.setdefault(str(zone["floorId"]), []).append(
            ZoneResponse.model_validate(public_zone(zone))
        )
    layout: list[FloorLayoutResponse] = []
    for floor in floors:
        payload = public_floor(floor)
        payload["zones"] = by_floor.get(str(floor["_id"]), [])
        layout.append(FloorLayoutResponse.model_validate(payload))
    return layout


@router.post(
    "/facilities/{facility_id}/floors",
    response_model=FloorResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Add a floor to a facility",
    description=(
        "A floor is the top level of the Facility -> Floor -> Zone -> Space "
        "tree. `level` orders floors in the layout (basements use negative "
        "values). A duplicate name on the same facility answers `409`."
    ),
    responses={**_SPACES_ERROR_RESPONSES, 201: {"model": FloorResponse}},
)
async def create_floor(
    facility_id: str,
    payload: CreateFloorRequest,
    request: Request,
    user: Annotated[CurrentUser, Depends(require_roles(*_MANAGE_ROLES))],
    db: Annotated[AsyncDatabase, Depends(get_database)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> FloorResponse:
    _write_throttle(request, user, settings)
    document = await spaces_service.create_floor(
        db,
        user_id=user.id,
        role=user.role,
        facility_id=facility_id,
        payload=payload,
        settings=settings,
    )
    await audit.record(
        db,
        action="FLOOR_CREATED",
        entity="floor",
        actor_id=user.id,
        entity_id=document["_id"],
        ip=client_ip(request),
        metadata={"facilityId": facility_id, "name": document.get("name")},
    )
    return FloorResponse.model_validate(public_floor(document))


@router.patch(
    "/floors/{floor_id}",
    response_model=FloorResponse,
    summary="Edit a floor",
    description=(
        "Partial update: send only what changed. Renaming a floor rewrites the "
        "denormalized `floorName` on every space under it in one pass, so a grid "
        "or report never shows a stale label."
    ),
    responses=_SPACES_ERROR_RESPONSES,
)
async def update_floor(
    floor_id: str,
    payload: UpdateFloorRequest,
    request: Request,
    user: Annotated[CurrentUser, Depends(require_roles(*_MANAGE_ROLES))],
    db: Annotated[AsyncDatabase, Depends(get_database)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> FloorResponse:
    _write_throttle(request, user, settings)
    document, changed = await spaces_service.update_floor(
        db, user_id=user.id, role=user.role, floor_id=floor_id, payload=payload
    )
    if changed:
        await audit.record(
            db,
            action="FLOOR_UPDATED",
            entity="floor",
            actor_id=user.id,
            entity_id=document["_id"],
            ip=client_ip(request),
            metadata={"fields": sorted(payload.model_fields_set)},
        )
    return FloorResponse.model_validate(public_floor(document))


@router.delete(
    "/floors/{floor_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a floor",
    description=(
        "Hard delete. The floor's zones and spaces exist only for it, so they "
        "are removed with it rather than left dangling."
    ),
    responses=_SPACES_ERROR_RESPONSES,
)
async def delete_floor(
    floor_id: str,
    request: Request,
    user: Annotated[CurrentUser, Depends(require_roles(*_MANAGE_ROLES))],
    db: Annotated[AsyncDatabase, Depends(get_database)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> None:
    _write_throttle(request, user, settings)
    document, counts = await spaces_service.delete_floor(
        db, user_id=user.id, role=user.role, floor_id=floor_id
    )
    await audit.record(
        db,
        action="FLOOR_DELETED",
        entity="floor",
        actor_id=user.id,
        entity_id=document["_id"],
        ip=client_ip(request),
        metadata=counts,
    )


# --- Zones ------------------------------------------------------------------


@router.post(
    "/floors/{floor_id}/zones",
    response_model=ZoneResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Add a zone to a floor",
    description=(
        "A zone groups spaces within a floor (DOC section 11.169 closes or "
        "reopens a zone). New zones start `ACTIVE`. A duplicate name on the "
        "same floor answers `409`."
    ),
    responses={**_SPACES_ERROR_RESPONSES, 201: {"model": ZoneResponse}},
)
async def create_zone(
    floor_id: str,
    payload: CreateZoneRequest,
    request: Request,
    user: Annotated[CurrentUser, Depends(require_roles(*_MANAGE_ROLES))],
    db: Annotated[AsyncDatabase, Depends(get_database)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> ZoneResponse:
    _write_throttle(request, user, settings)
    document = await spaces_service.create_zone(
        db, user_id=user.id, role=user.role, floor_id=floor_id, payload=payload, settings=settings
    )
    await audit.record(
        db,
        action="ZONE_CREATED",
        entity="zone",
        actor_id=user.id,
        entity_id=document["_id"],
        ip=client_ip(request),
        metadata={"floorId": floor_id, "name": document.get("name")},
    )
    return ZoneResponse.model_validate(public_zone(document))


@router.patch(
    "/zones/{zone_id}",
    response_model=ZoneResponse,
    summary="Rename a zone",
    description=(
        "Renaming a zone rewrites the denormalized `zoneName` on every space "
        "under it in one pass. Zone status is not writable here; use "
        "`PATCH /zones/{zone_id}/status`."
    ),
    responses=_SPACES_ERROR_RESPONSES,
)
async def update_zone(
    zone_id: str,
    payload: UpdateZoneRequest,
    request: Request,
    user: Annotated[CurrentUser, Depends(require_roles(*_MANAGE_ROLES))],
    db: Annotated[AsyncDatabase, Depends(get_database)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> ZoneResponse:
    _write_throttle(request, user, settings)
    document, changed = await spaces_service.update_zone(
        db, user_id=user.id, role=user.role, zone_id=zone_id, payload=payload
    )
    if changed:
        await audit.record(
            db,
            action="ZONE_UPDATED",
            entity="zone",
            actor_id=user.id,
            entity_id=document["_id"],
            ip=client_ip(request),
            metadata={"fields": sorted(payload.model_fields_set)},
        )
    return ZoneResponse.model_validate(public_zone(document))


@router.patch(
    "/zones/{zone_id}/status",
    response_model=ZoneResponse,
    summary="Close or reopen a zone",
    description=(
        "The zone half of DOC section 11.169. Setting a zone `INACTIVE` (with "
        "an optional `reason`) blocks every available/reserved/occupied space "
        "in it without deleting them; setting it `ACTIVE` releases the blocked "
        "spaces back to `AVAILABLE`. Setting the status a zone already holds is "
        "a no-op that still answers `200`."
    ),
    responses=_SPACES_ERROR_RESPONSES,
)
async def change_zone_status(
    zone_id: str,
    payload: ChangeZoneStatusRequest,
    request: Request,
    user: Annotated[CurrentUser, Depends(require_roles(*_MANAGE_ROLES))],
    db: Annotated[AsyncDatabase, Depends(get_database)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> ZoneResponse:
    _write_throttle(request, user, settings)
    document, previous, affected = await spaces_service.set_zone_status(
        db, user_id=user.id, role=user.role, zone_id=zone_id, payload=payload
    )
    if previous != payload.status.value:
        metadata: dict[str, Any] = {
            "from": previous,
            "to": payload.status.value,
            "affectedSpaces": affected,
        }
        if payload.reason is not None:
            metadata["reason"] = payload.reason
        await audit.record(
            db,
            action="ZONE_STATUS_CHANGED",
            entity="zone",
            actor_id=user.id,
            entity_id=document["_id"],
            ip=client_ip(request),
            metadata=metadata,
        )
    return ZoneResponse.model_validate(public_zone(document))


@router.delete(
    "/zones/{zone_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a zone",
    description=(
        "Hard delete. The zone's spaces exist only for it, so they are removed "
        "with it rather than left dangling."
    ),
    responses=_SPACES_ERROR_RESPONSES,
)
async def delete_zone(
    zone_id: str,
    request: Request,
    user: Annotated[CurrentUser, Depends(require_roles(*_MANAGE_ROLES))],
    db: Annotated[AsyncDatabase, Depends(get_database)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> None:
    _write_throttle(request, user, settings)
    document, counts = await spaces_service.delete_zone(
        db, user_id=user.id, role=user.role, zone_id=zone_id
    )
    await audit.record(
        db,
        action="ZONE_DELETED",
        entity="zone",
        actor_id=user.id,
        entity_id=document["_id"],
        ip=client_ip(request),
        metadata=counts,
    )


# --- Spaces -----------------------------------------------------------------


@router.get(
    "/facilities/{facility_id}/spaces",
    response_model=list[SpaceResponse],
    summary="List a facility's spaces",
    description=(
        "The flat grid view: every space in a facility, optionally narrowed by "
        "`floorId` and/or `status`. Ordered by floor, zone then number."
    ),
    responses=_SPACES_ERROR_RESPONSES,
)
async def list_facility_spaces(
    facility_id: str,
    user: Annotated[CurrentUser, Depends(require_roles(*_READ_ROLES))],
    db: Annotated[AsyncDatabase, Depends(get_database)],
    floor_id: Annotated[str | None, Query(alias="floorId")] = None,
    status_filter: Annotated[SpaceStatus | None, Query(alias="status")] = None,
) -> list[SpaceResponse]:
    documents = await spaces_service.list_spaces(
        db,
        user_id=user.id,
        role=user.role,
        facility_ids=user.facility_ids,
        facility_id=facility_id,
        floor_id=floor_id,
        status=status_filter,
    )
    return [SpaceResponse.model_validate(public_space(document)) for document in documents]


@router.get(
    "/zones/{zone_id}/spaces",
    response_model=list[SpaceResponse],
    summary="List a zone's spaces",
    description=(
        "Every space in one zone, optionally narrowed by `status`. Ordered by "
        "space number."
    ),
    responses=_SPACES_ERROR_RESPONSES,
)
async def list_zone_spaces(
    zone_id: str,
    user: Annotated[CurrentUser, Depends(require_roles(*_READ_ROLES))],
    db: Annotated[AsyncDatabase, Depends(get_database)],
    status_filter: Annotated[SpaceStatus | None, Query(alias="status")] = None,
) -> list[SpaceResponse]:
    documents = await spaces_service.list_spaces(
        db,
        user_id=user.id,
        role=user.role,
        facility_ids=user.facility_ids,
        zone_id=zone_id,
        status=status_filter,
    )
    return [SpaceResponse.model_validate(public_space(document)) for document in documents]


@router.post(
    "/zones/{zone_id}/spaces",
    response_model=SpaceResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Add a space to a zone",
    description=(
        "`spaceNumber` is normalised to upper case and must be unique within "
        "its zone; a clash answers `409` with code `SPACE_DUPLICATE`. New "
        "spaces start `AVAILABLE` and `reservable`."
    ),
    responses={**_SPACES_ERROR_RESPONSES, 201: {"model": SpaceResponse}},
)
async def create_space(
    zone_id: str,
    payload: CreateSpaceRequest,
    request: Request,
    user: Annotated[CurrentUser, Depends(require_roles(*_MANAGE_ROLES))],
    db: Annotated[AsyncDatabase, Depends(get_database)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> SpaceResponse:
    _write_throttle(request, user, settings)
    document = await spaces_service.create_space(
        db, user_id=user.id, role=user.role, zone_id=zone_id, payload=payload, settings=settings
    )
    await audit.record(
        db,
        action="SPACE_CREATED",
        entity="space",
        actor_id=user.id,
        entity_id=document["_id"],
        ip=client_ip(request),
        metadata={"zoneId": zone_id, "spaceNumber": document.get("spaceNumber")},
    )
    return SpaceResponse.model_validate(public_space(document))


@router.post(
    "/zones/{zone_id}/spaces/bulk",
    response_model=list[SpaceResponse],
    status_code=status.HTTP_201_CREATED,
    summary="Add a run of spaces to a zone",
    description=(
        "Generates consecutive labels such as `A01`..`A50` from a `prefix`, "
        "`start` and `count` - the \"Total Parking = 250\" setup of DOC section "
        "42 without one request per space. The whole run is validated first: if "
        "any number already exists the request answers `409` with code "
        "`SPACE_DUPLICATE` and nothing is created."
    ),
    responses={**_SPACES_ERROR_RESPONSES, 201: {"model": list[SpaceResponse]}},
)
async def bulk_create_spaces(
    zone_id: str,
    payload: BulkCreateSpacesRequest,
    request: Request,
    user: Annotated[CurrentUser, Depends(require_roles(*_MANAGE_ROLES))],
    db: Annotated[AsyncDatabase, Depends(get_database)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> list[SpaceResponse]:
    _write_throttle(request, user, settings)
    documents = await spaces_service.bulk_create_spaces(
        db, user_id=user.id, role=user.role, zone_id=zone_id, payload=payload, settings=settings
    )
    await audit.record(
        db,
        action="SPACES_BULK_CREATED",
        entity="space",
        actor_id=user.id,
        ip=client_ip(request),
        metadata={
            "zoneId": zone_id,
            "count": len(documents),
            "prefix": payload.prefix,
            "start": payload.start,
        },
    )
    return [SpaceResponse.model_validate(public_space(document)) for document in documents]


@router.get(
    "/spaces/{space_id}",
    response_model=SpaceResponse,
    summary="Read one space",
    description=(
        "Readable by the owning operator, any admin, and staff assigned to the "
        "facility. A malformed or foreign id answers `404`."
    ),
    responses=_SPACES_ERROR_RESPONSES,
)
async def get_space(
    space_id: str,
    user: Annotated[CurrentUser, Depends(require_roles(*_READ_ROLES))],
    db: Annotated[AsyncDatabase, Depends(get_database)],
) -> SpaceResponse:
    document = await spaces_service.get_space(
        db, user_id=user.id, role=user.role, facility_ids=user.facility_ids, space_id=space_id
    )
    return SpaceResponse.model_validate(public_space(document))


@router.patch(
    "/spaces/{space_id}",
    response_model=SpaceResponse,
    summary="Edit a space",
    description=(
        "Partial update of the label, type and reservable flag. A space's "
        "`status` is not writable here and answers a `422` pointing at "
        "`PATCH /spaces/{space_id}/status`; moving a space between zones is a "
        "delete-and-recreate."
    ),
    responses=_SPACES_ERROR_RESPONSES,
)
async def update_space(
    space_id: str,
    payload: UpdateSpaceRequest,
    request: Request,
    user: Annotated[CurrentUser, Depends(require_roles(*_MANAGE_ROLES))],
    db: Annotated[AsyncDatabase, Depends(get_database)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> SpaceResponse:
    _write_throttle(request, user, settings)
    document, changed = await spaces_service.update_space(
        db, user_id=user.id, role=user.role, space_id=space_id, payload=payload
    )
    if changed:
        await audit.record(
            db,
            action="SPACE_UPDATED",
            entity="space",
            actor_id=user.id,
            entity_id=document["_id"],
            ip=client_ip(request),
            metadata={"fields": sorted(payload.model_fields_set)},
        )
    return SpaceResponse.model_validate(public_space(document))


@router.delete(
    "/spaces/{space_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a space",
    description="Hard delete. An unknown id answers `404`.",
    responses=_SPACES_ERROR_RESPONSES,
)
async def delete_space(
    space_id: str,
    request: Request,
    user: Annotated[CurrentUser, Depends(require_roles(*_MANAGE_ROLES))],
    db: Annotated[AsyncDatabase, Depends(get_database)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> None:
    _write_throttle(request, user, settings)
    document = await spaces_service.delete_space(
        db, user_id=user.id, role=user.role, space_id=space_id
    )
    await audit.record(
        db,
        action="SPACE_DELETED",
        entity="space",
        actor_id=user.id,
        entity_id=document["_id"],
        ip=client_ip(request),
        metadata={"zoneId": str(document.get("zoneId"))},
    )


@router.patch(
    "/spaces/{space_id}/status",
    response_model=SpaceResponse,
    summary="Change a space's status",
    description=(
        "Mark a space `AVAILABLE`, `MAINTENANCE` or `BLOCKED` (DOC sections "
        "10.151/17.216). `OCCUPIED` and `RESERVED` belong to the reservation "
        "engine and are refused with a `422`. Operators and admins reach any "
        "space they own; staff may change spaces of a facility their token "
        "assigns. Setting the status a space already holds is a no-op."
    ),
    responses=_SPACES_ERROR_RESPONSES,
)
async def change_space_status(
    space_id: str,
    payload: ChangeSpaceStatusRequest,
    request: Request,
    user: Annotated[CurrentUser, Depends(require_roles(*_READ_ROLES))],
    db: Annotated[AsyncDatabase, Depends(get_database)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> SpaceResponse:
    _write_throttle(request, user, settings)
    document, previous = await spaces_service.set_space_status(
        db,
        user_id=user.id,
        role=user.role,
        facility_ids=user.facility_ids,
        space_id=space_id,
        payload=payload,
    )
    if previous != payload.status.value:
        metadata: dict[str, Any] = {"from": previous, "to": payload.status.value}
        if payload.reason is not None:
            metadata["reason"] = payload.reason
        await audit.record(
            db,
            action="SPACE_STATUS_CHANGED",
            entity="space",
            actor_id=user.id,
            entity_id=document["_id"],
            ip=client_ip(request),
            metadata=metadata,
        )
    return SpaceResponse.model_validate(public_space(document))


__all__ = ["router"]
