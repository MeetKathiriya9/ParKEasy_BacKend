"""The `vehicles` group: the signed-in user's own vehicles (DOC sections 10/18/21).

    GET    /                     list the caller's vehicles
    POST   /                     register a vehicle (201)
    PATCH  /{vehicle_id}         edit one
    DELETE /{vehicle_id}         remove one (204)
    PATCH  /{vehicle_id}/default make it the default vehicle

Every route acts on the caller's own vehicles: `userId` is taken from the
authenticated principal, never from the body or the query string, so there is no
way to address another user's vehicle without their token.

DOC section 21 assigns exactly these operations to the group ("CRUD vehicles,
default vehicle"), and DOC section 10 asks for the default vehicle as a first-
class concept rather than an afterthought.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request, status
from pymongo.asynchronous.database import AsyncDatabase

from app.api.deps import CurrentUserDep
from app.api.throttle import client_ip, throttle
from app.core.config import Settings, get_settings
from app.core.rate_limit import RateLimit
from app.db.client import get_database
from app.models.vehicle import public_vehicle
from app.schemas.common import ErrorResponse
from app.schemas.vehicle import (
    CreateVehicleRequest,
    UpdateVehicleRequest,
    VehicleResponse,
)
from app.services import audit
from app.services import vehicles as vehicles_service

router = APIRouter(prefix="/vehicles", tags=["vehicles"])

# Shared by every route here: a missing/expired token is a 401.
AUTHENTICATED_ERRORS: dict[int | str, dict[str, type[Any]]] = {
    401: {"model": ErrorResponse, "description": "Missing, invalid or revoked token"},
}

_VEHICLES_ERROR_RESPONSES: dict[int | str, dict[str, type[Any]]] = {
    **AUTHENTICATED_ERRORS,
    404: {"model": ErrorResponse, "description": "No such vehicle for this account"},
    409: {
        "model": ErrorResponse,
        "description": "Registration number already registered or vehicle limit reached",
    },
    422: {"model": ErrorResponse, "description": "Request validation failed"},
    429: {"model": ErrorResponse, "description": "Too many attempts"},
}

_CREATE_RESPONSES: dict[int | str, dict[str, type[Any]]] = {
    **_VEHICLES_ERROR_RESPONSES,
    status.HTTP_201_CREATED: {"model": VehicleResponse, "description": "Vehicle created"},
}


def _to_response(document: dict[str, Any]) -> VehicleResponse:
    return VehicleResponse.model_validate(public_vehicle(document))


def _write_throttle(request: Request, user: Any, settings: Settings) -> None:
    """One bucket for every vehicle write, so a script cannot hammer the group."""
    throttle(
        request,
        bucket="vehicle-write",
        identity=user.id,
        rule=RateLimit(
            limit=settings.vehicle_write_rate_limit,
            window_seconds=settings.vehicle_write_rate_window_seconds,
        ),
    )


@router.get(
    "",
    response_model=list[VehicleResponse],
    summary="List the signed-in user's vehicles",
    description=(
        "Oldest first. Exactly one vehicle is flagged `isDefault` whenever the "
        "account has at least one: the flag is repaired on read if a previous "
        "request was interrupted mid-switch."
    ),
    responses=AUTHENTICATED_ERRORS,
)
async def list_vehicles(
    user: CurrentUserDep,
    db: Annotated[AsyncDatabase, Depends(get_database)],
) -> list[VehicleResponse]:
    documents = await vehicles_service.list_vehicles(db, user_id=user.id)
    return [_to_response(document) for document in documents]


@router.post(
    "",
    response_model=VehicleResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Register a vehicle",
    description=(
        "The registration number is normalised before it is stored - uppercased "
        "with separators removed, so `ABC-1234` and `abc 1234` are the same "
        "key - and must be unique among this user's vehicles: a repeat answers "
        "`409` with code `REGISTRATION_DUPLICATE`. "
        "`isEV` is derived from `fuelType`; sending it is a `422`. The first "
        "vehicle an account registers becomes its default automatically."
    ),
    responses=_CREATE_RESPONSES,
)
async def create_vehicle(
    payload: CreateVehicleRequest,
    request: Request,
    user: CurrentUserDep,
    db: Annotated[AsyncDatabase, Depends(get_database)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> VehicleResponse:
    _write_throttle(request, user, settings)

    document = await vehicles_service.create_vehicle(
        db, user_id=user.id, payload=payload, settings=settings
    )

    await audit.record(
        db,
        action="VEHICLE_CREATED",
        entity="vehicle",
        actor_id=user.id,
        entity_id=document["_id"],
        ip=client_ip(request),
        # Field *names* only, matching the profile audit rows.
        metadata={"fields": sorted(payload.model_fields_set)},
    )
    return _to_response(document)


@router.patch(
    "/{vehicle_id}",
    response_model=VehicleResponse,
    summary="Edit a vehicle",
    description=(
        "Partial update: send only what changed. `isDefault` is deliberately "
        "not writable here and answers a `422` pointing at "
        "`PATCH /vehicles/{vehicle_id}/default`. Changing `fuelType` recomputes "
        "`isEV`. An id that does not belong to the caller answers `404`."
    ),
    responses=_VEHICLES_ERROR_RESPONSES,
)
async def update_vehicle(
    vehicle_id: str,
    payload: UpdateVehicleRequest,
    request: Request,
    user: CurrentUserDep,
    db: Annotated[AsyncDatabase, Depends(get_database)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> VehicleResponse:
    _write_throttle(request, user, settings)

    document, changed = await vehicles_service.update_vehicle(
        db, user_id=user.id, vehicle_id=vehicle_id, payload=payload
    )

    if changed:
        await audit.record(
            db,
            action="VEHICLE_UPDATED",
            entity="vehicle",
            actor_id=user.id,
            entity_id=document["_id"],
            ip=client_ip(request),
            metadata={"fields": sorted(payload.model_fields_set)},
        )
    return _to_response(document)


@router.delete(
    "/{vehicle_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Remove a vehicle",
    description=(
        "Deleting the default vehicle promotes the caller's oldest remaining "
        "vehicle to default, so the account never keeps a default that no "
        "longer exists. An unknown id answers `404`."
    ),
    responses=_VEHICLES_ERROR_RESPONSES,
)
async def delete_vehicle(
    vehicle_id: str,
    request: Request,
    user: CurrentUserDep,
    db: Annotated[AsyncDatabase, Depends(get_database)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> None:
    _write_throttle(request, user, settings)

    document = await vehicles_service.delete_vehicle(
        db, user_id=user.id, vehicle_id=vehicle_id
    )

    await audit.record(
        db,
        action="VEHICLE_DELETED",
        entity="vehicle",
        actor_id=user.id,
        entity_id=document["_id"],
        ip=client_ip(request),
        metadata={"wasDefault": bool(document.get("isDefault"))},
    )


@router.patch(
    "/{vehicle_id}/default",
    response_model=VehicleResponse,
    summary="Make this the default vehicle",
    description=(
        "Clears the flag from every other vehicle first, then sets it here, so "
        "the account can never hold two defaults. Calling it on the vehicle "
        "that is already default is a no-op that still returns `200`."
    ),
    responses=_VEHICLES_ERROR_RESPONSES,
)
async def set_default_vehicle(
    vehicle_id: str,
    request: Request,
    user: CurrentUserDep,
    db: Annotated[AsyncDatabase, Depends(get_database)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> VehicleResponse:
    _write_throttle(request, user, settings)

    document = await vehicles_service.set_default(
        db, user_id=user.id, vehicle_id=vehicle_id
    )

    await audit.record(
        db,
        action="VEHICLE_DEFAULT_CHANGED",
        entity="vehicle",
        actor_id=user.id,
        entity_id=document["_id"],
        ip=client_ip(request),
    )
    return _to_response(document)


__all__ = ["router"]
