"""Pricing rules and rate preview (DOC section 21)."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request, Response, status
from pymongo.asynchronous.database import AsyncDatabase

from app.api.deps import CurrentUser, require_roles
from app.api.throttle import client_ip, throttle
from app.core.config import Settings, get_settings
from app.core.errors import ConflictError, NotFoundError
from app.core.rate_limit import RateLimit
from app.db.client import get_database
from app.models.enums import FacilityStatus, Role
from app.models.facility import FACILITIES_COLLECTION
from app.models.pricing import public_pricing_rule
from app.schemas.common import ErrorResponse
from app.schemas.pricing import (
    CreatePricingRuleRequest,
    PricingBreakdown,
    PricingPreviewRequest,
    PricingRulePage,
    PricingRuleResponse,
    UpdatePricingRuleRequest,
)
from app.services import audit
from app.services import pricing as pricing_service
from app.services.facilities import visible_facility
from app.utils.mongo import to_object_id

router = APIRouter(prefix="/pricing", tags=["pricing"])

_MANAGE_ROLES = [Role.OPERATOR, Role.ADMIN]
_READ_ROLES = [Role.STAFF, Role.OPERATOR, Role.ADMIN]
_ALL_ROLES = [Role.DRIVER, Role.STAFF, Role.OPERATOR, Role.ADMIN]

AUTHENTICATED_ERRORS: dict[int | str, dict[str, type[Any]]] = {
    401: {"model": ErrorResponse, "description": "Missing, invalid or revoked token"},
    403: {"model": ErrorResponse, "description": "Role may not perform this action"},
}

_PRICING_ERROR_RESPONSES: dict[int | str, dict[str, type[Any]]] = {
    **AUTHENTICATED_ERRORS,
    404: {"model": ErrorResponse, "description": "No such facility or pricing rule"},
    409: {"model": ErrorResponse, "description": "Pricing is not available or rule name is taken"},
    422: {"model": ErrorResponse, "description": "Request validation failed"},
    429: {"model": ErrorResponse, "description": "Too many attempts"},
}


def _write_throttle(request: Request, user: Any, settings: Settings) -> None:
    throttle(
        request,
        bucket="pricing-write",
        identity=user.id,
        rule=RateLimit(
            limit=settings.pricing_write_rate_limit,
            window_seconds=settings.pricing_write_rate_window_seconds,
        ),
    )


def _rule(document: dict[str, Any]) -> PricingRuleResponse:
    return PricingRuleResponse.model_validate(public_pricing_rule(document))


async def _preview_facility(
    db: AsyncDatabase,
    *,
    user: Any,
    facility_id: str,
) -> dict[str, Any]:
    if user.role is not Role.DRIVER:
        return await visible_facility(
            db,
            user_id=user.id,
            role=user.role,
            facility_ids=user.facility_ids,
            facility_id=facility_id,
        )
    object_id = to_object_id(facility_id)
    document = (
        await db[FACILITIES_COLLECTION].find_one({"_id": object_id})
        if object_id is not None
        else None
    )
    if document is None or document.get("status") != FacilityStatus.ACTIVE.value:
        raise NotFoundError("This facility does not exist")
    return document


@router.get(
    "/rules",
    response_model=PricingRulePage,
    summary="List a facility's pricing rules",
    description=(
        "Staff, operators and admins. Active rules come first, then by "
        "priority. A driver uses `POST /pricing/preview` instead."
    ),
    responses=_PRICING_ERROR_RESPONSES,
)
async def list_rules(
    user: Annotated[CurrentUser, Depends(require_roles(*_READ_ROLES))],
    db: Annotated[AsyncDatabase, Depends(get_database)],
    facility_id: Annotated[str, Query(alias="facilityId", min_length=1, max_length=64)],
    page: Annotated[int, Query(ge=1)] = 1,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> PricingRulePage:
    await visible_facility(
        db,
        user_id=user.id,
        role=user.role,
        facility_ids=user.facility_ids,
        facility_id=facility_id,
    )
    rows, total = await pricing_service.list_rules(
        db, facility_id=facility_id, page=page, limit=limit
    )
    return PricingRulePage.build(
        [_rule(row) for row in rows], total=total, page=page, limit=limit
    )


@router.post(
    "/rules",
    response_model=PricingRuleResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a pricing rule",
    description=(
        "Operators and admins. Supported rule types are `hourly`, `fixed` and "
        "`daily`. A fixed rule requires `durationMinutes`; an hourly or daily "
        "rule must omit it. Rates are in INR."
    ),
    responses=_PRICING_ERROR_RESPONSES,
)
async def create_rule(
    payload: CreatePricingRuleRequest,
    request: Request,
    user: Annotated[CurrentUser, Depends(require_roles(*_MANAGE_ROLES))],
    db: Annotated[AsyncDatabase, Depends(get_database)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> PricingRuleResponse:
    _write_throttle(request, user, settings)
    document = await pricing_service.create_rule(
        db, user_id=user.id, role=user.role, payload=payload, settings=settings
    )
    await audit.record(
        db,
        action="PRICING_RULE_CREATED",
        entity="pricingRule",
        actor_id=user.id,
        entity_id=document["_id"],
        ip=client_ip(request),
        metadata={
            "facilityId": str(document.get("facilityId")),
            "ruleType": document.get("ruleType"),
            "rate": document.get("rate"),
        },
    )
    return _rule(document)


@router.patch(
    "/rules/{rule_id}",
    response_model=PricingRuleResponse,
    summary="Update a pricing rule",
    description="Operators and admins. Partial update; omitted fields are unchanged.",
    responses=_PRICING_ERROR_RESPONSES,
)
async def update_rule(
    rule_id: str,
    payload: UpdatePricingRuleRequest,
    request: Request,
    user: Annotated[CurrentUser, Depends(require_roles(*_MANAGE_ROLES))],
    db: Annotated[AsyncDatabase, Depends(get_database)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> PricingRuleResponse:
    _write_throttle(request, user, settings)
    document, changed = await pricing_service.update_rule(
        db, user_id=user.id, role=user.role, rule_id=rule_id, payload=payload
    )
    if changed:
        await audit.record(
            db,
            action="PRICING_RULE_UPDATED",
            entity="pricingRule",
            actor_id=user.id,
            entity_id=document["_id"],
            ip=client_ip(request),
            metadata={"facilityId": str(document.get("facilityId"))},
        )
    return _rule(document)


@router.delete(
    "/rules/{rule_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a pricing rule",
    description="Operators and admins.",
    responses=_PRICING_ERROR_RESPONSES,
)
async def delete_rule(
    rule_id: str,
    request: Request,
    user: Annotated[CurrentUser, Depends(require_roles(*_MANAGE_ROLES))],
    db: Annotated[AsyncDatabase, Depends(get_database)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> Response:
    _write_throttle(request, user, settings)
    document = await pricing_service.delete_rule(
        db, user_id=user.id, role=user.role, rule_id=rule_id
    )
    await audit.record(
        db,
        action="PRICING_RULE_DELETED",
        entity="pricingRule",
        actor_id=user.id,
        entity_id=document["_id"],
        ip=client_ip(request),
        metadata={"facilityId": str(document.get("facilityId"))},
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/preview",
    response_model=PricingBreakdown,
    summary="Preview the price for a time window",
    description=(
        "Any authenticated role. Applies the facility's active rules, grace, "
        "overstay grace, fixed packages and daily cap. `bookedEndTime` is the "
        "reservation's scheduled end; omit it for a simple entry-to-exit quote."
    ),
    responses=_PRICING_ERROR_RESPONSES,
)
async def preview_price(
    payload: PricingPreviewRequest,
    user: Annotated[CurrentUser, Depends(require_roles(*_ALL_ROLES))],
    db: Annotated[AsyncDatabase, Depends(get_database)],
) -> PricingBreakdown:
    facility = await _preview_facility(db, user=user, facility_id=payload.facilityId)
    rules = await pricing_service.active_rules(db, facility["_id"])
    breakdown = pricing_service.calculate_price(
        facility=facility,
        rules=rules,
        entry_time=payload.entryTime,
        exit_time=payload.exitTime,
        booked_end_time=payload.bookedEndTime,
        estimated=True,
    )
    if breakdown is None:
        raise ConflictError(
            "No pricing is published for this facility",
            code=pricing_service.PricingErrorCode.NOT_AVAILABLE,
        )
    return breakdown


__all__ = ["router"]
