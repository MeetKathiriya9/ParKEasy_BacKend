from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from bson import ObjectId
from pymongo import ReturnDocument
from pymongo.asynchronous.database import AsyncDatabase
from pymongo.errors import DuplicateKeyError

from app.core.config import Settings
from app.core.errors import ConflictError, ErrorCode, NotFoundError
from app.models.enums import PricingRuleType, Role
from app.models.facility import (
    DEFAULT_GRACE_MINUTES,
    DEFAULT_OVERSTAY_GRACE_MINUTES,
)
from app.models.pricing import (
    PRICING_RULES_COLLECTION,
    PricingRuleDocument,
    pricing_rule_error,
)
from app.schemas.pricing import (
    CreatePricingRuleRequest,
    PriceLine,
    PricingBreakdown,
    UpdatePricingRuleRequest,
)
from app.services.facilities import owned_facility
from app.utils.mongo import to_object_id, utcnow

logger = logging.getLogger(__name__)

CURRENCY = "INR"
RULE_NOT_FOUND = "This pricing rule does not exist"


class PricingErrorCode:
    RULE_NOT_FOUND = "PRICING_RULE_NOT_FOUND"
    RULE_LIMIT_REACHED = "PRICING_RULE_LIMIT_REACHED"
    RULE_NAME_TAKEN = "PRICING_RULE_NAME_TAKEN"
    NOT_AVAILABLE = "PRICING_NOT_AVAILABLE"


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=utcnow().tzinfo)


def _minutes(start: datetime, end: datetime) -> int:
    if end <= start:
        return 0
    return int((end - start).total_seconds() // 60)


def _positive(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _money(value: float) -> float:
    return round(value + 0.0, 2)


def _rule_rate(rule: dict[str, Any] | None) -> float | None:
    if not rule:
        return None
    return _positive(rule.get("rate"))


def _pick_rule(rules: list[dict[str, Any]], rule_type: str) -> dict[str, Any] | None:
    for rule in rules:
        if rule.get("ruleType") == rule_type and rule.get("active", True):
            return rule
    return None


def _pick_fixed_rule(
    rules: list[dict[str, Any]], minutes: int
) -> dict[str, Any] | None:
    candidates = [
        rule
        for rule in rules
        if rule.get("ruleType") == PricingRuleType.FIXED.value
        and rule.get("active", True)
        and _positive(rule.get("rate")) is not None
        and int(rule.get("durationMinutes") or 0) >= minutes
    ]
    if not candidates:
        return None
    return min(
        candidates,
        key=lambda rule: (
            -int(rule.get("priority") or 0),
            int(rule.get("durationMinutes") or 0),
            str(rule.get("_id")),
        ),
    )


def _facility_grace(facility: dict[str, Any]) -> int:
    value = facility.get("graceMinutes")
    return DEFAULT_GRACE_MINUTES if value is None else max(0, int(value))


def _facility_overstay_grace(facility: dict[str, Any]) -> int:
    value = facility.get("overstayGraceMinutes")
    return (
        DEFAULT_OVERSTAY_GRACE_MINUTES if value is None else max(0, int(value))
    )


def calculate_price(
    *,
    facility: dict[str, Any],
    rules: list[dict[str, Any]],
    entry_time: datetime,
    exit_time: datetime,
    booked_end_time: datetime | None,
    estimated: bool,
) -> PricingBreakdown | None:
    entry = _aware(entry_time)
    exit_at = _aware(exit_time)
    if exit_at <= entry:
        return None

    scheduled_end = _aware(booked_end_time) if booked_end_time is not None else exit_at
    base_end = min(exit_at, scheduled_end)
    base_minutes = _minutes(entry, base_end)
    grace = _facility_grace(facility)
    billable_base = max(0, base_minutes - grace)
    overstay_grace = _facility_overstay_grace(facility)
    overtime_start = scheduled_end + timedelta(minutes=overstay_grace)
    overtime_minutes = _minutes(overtime_start, exit_at) if exit_at > overtime_start else 0

    hourly_rule = _pick_rule(rules, PricingRuleType.HOURLY.value)
    hourly_rate = _rule_rate(hourly_rule) or _positive(facility.get("hourlyRate"))
    fixed_rule = _pick_fixed_rule(rules, billable_base) if billable_base else None

    lines: list[PriceLine] = []
    rule_ids: list[str] = []
    base_charge: float | None = None

    if billable_base == 0:
        base_charge = 0.0
    elif fixed_rule is not None:
        fixed_rate = _positive(fixed_rule.get("rate"))
        fixed_duration = int(fixed_rule.get("durationMinutes") or 0)
        if fixed_rate is not None:
            base_charge = fixed_rate
            lines.append(
                PriceLine(
                    kind="fixed",
                    label=f"Fixed package ({fixed_duration} min)",
                    minutes=fixed_duration,
                    rate=fixed_rate,
                    amount=_money(fixed_rate),
                    ruleId=str(fixed_rule.get("_id")),
                )
            )
            rule_ids.append(str(fixed_rule.get("_id")))
            excess = max(0, billable_base - fixed_duration)
            if excess and hourly_rate is not None:
                excess_charge = hourly_rate * excess / 60
                base_charge += excess_charge
                lines.append(
                    PriceLine(
                        kind="hourly_excess",
                        label="Time beyond fixed package",
                        minutes=excess,
                        rate=hourly_rate,
                        amount=_money(excess_charge),
                        ruleId=str(hourly_rule.get("_id")) if hourly_rule else None,
                    )
                )
                if hourly_rule is not None:
                    rule_ids.append(str(hourly_rule.get("_id")))
    elif hourly_rate is not None:
        base_charge = hourly_rate * billable_base / 60
        lines.append(
            PriceLine(
                kind="hourly",
                label="Parking time",
                minutes=billable_base,
                rate=hourly_rate,
                amount=_money(base_charge),
                ruleId=str(hourly_rule.get("_id")) if hourly_rule else None,
            )
        )
        if hourly_rule is not None:
            rule_ids.append(str(hourly_rule.get("_id")))

    overtime_charge: float | None = None
    if overtime_minutes:
        overtime_rate = _positive(facility.get("overstayRate")) or hourly_rate
        if overtime_rate is not None:
            overtime_charge = overtime_rate * overtime_minutes / 60
            lines.append(
                PriceLine(
                    kind="overtime",
                    label="Overstay",
                    minutes=overtime_minutes,
                    rate=overtime_rate,
                    amount=_money(overtime_charge),
                    ruleId=str(hourly_rule.get("_id")) if hourly_rule else None,
                )
            )
            if hourly_rule is not None:
                rule_ids.append(str(hourly_rule.get("_id")))

    if base_charge is None and overtime_charge is None:
        return None

    subtotal = _money((base_charge or 0.0) + (overtime_charge or 0.0))

    daily_rule = _pick_rule(rules, PricingRuleType.DAILY.value)
    daily_cap = _rule_rate(daily_rule) or _positive(facility.get("dailyMax"))
    if daily_cap is not None and subtotal > daily_cap:
        adjustment = daily_cap - subtotal
        lines.append(
            PriceLine(
                kind="daily_cap",
                label="Daily maximum applied",
                amount=_money(adjustment),
                ruleId=str(daily_rule.get("_id")) if daily_rule else None,
            )
        )
        if daily_rule is not None:
            rule_ids.append(str(daily_rule.get("_id")))
        subtotal = _money(daily_cap)

    tax_amount = 0.0
    fee_amount = 0.0
    total = _money(subtotal + tax_amount + fee_amount)

    return PricingBreakdown(
        currency=CURRENCY,
        billableMinutes=billable_base + overtime_minutes,
        baseMinutes=billable_base,
        overtimeMinutes=overtime_minutes,
        lines=lines,
        subtotal=subtotal,
        taxAmount=tax_amount,
        feeAmount=fee_amount,
        total=total,
        ruleIds=list(dict.fromkeys(rule_ids)),
        estimated=estimated,
        computedAt=utcnow(),
    )


async def active_rules(
    db: AsyncDatabase, facility_id: ObjectId | str
) -> list[dict[str, Any]]:
    object_id = to_object_id(facility_id)
    if object_id is None:
        return []
    return await (
        db[PRICING_RULES_COLLECTION]
        .find({"facilityId": object_id, "active": True})
        .sort([("priority", -1), ("createdAt", 1), ("_id", 1)])
        .to_list()
    )


def reservation_estimate(
    *,
    facility: dict[str, Any],
    rules: list[dict[str, Any]],
    start_time: datetime,
    end_time: datetime,
) -> PricingBreakdown | None:
    return calculate_price(
        facility=facility,
        rules=rules,
        entry_time=start_time,
        exit_time=end_time,
        booked_end_time=end_time,
        estimated=True,
    )


def session_estimate(
    *,
    facility: dict[str, Any],
    rules: list[dict[str, Any]],
    entry_time: datetime,
    scheduled_end_time: datetime,
) -> PricingBreakdown | None:
    return calculate_price(
        facility=facility,
        rules=rules,
        entry_time=entry_time,
        exit_time=scheduled_end_time,
        booked_end_time=scheduled_end_time,
        estimated=True,
    )


def session_final_price(
    *,
    facility: dict[str, Any],
    rules: list[dict[str, Any]],
    entry_time: datetime,
    exit_time: datetime,
    scheduled_end_time: datetime,
) -> PricingBreakdown | None:
    return calculate_price(
        facility=facility,
        rules=rules,
        entry_time=entry_time,
        exit_time=exit_time,
        booked_end_time=scheduled_end_time,
        estimated=False,
    )


async def create_rule(
    db: AsyncDatabase,
    *,
    user_id: str,
    role: Role,
    payload: CreatePricingRuleRequest,
    settings: Settings,
) -> dict[str, Any]:
    facility = await owned_facility(
        db, user_id=user_id, role=role, facility_id=payload.facilityId
    )
    count = await db[PRICING_RULES_COLLECTION].count_documents(
        {"facilityId": facility["_id"]}
    )
    if count >= settings.pricing_max_rules_per_facility:
        raise ConflictError(
            f"A facility can have at most {settings.pricing_max_rules_per_facility} pricing rules",
            code=PricingErrorCode.RULE_LIMIT_REACHED,
            details={"limit": settings.pricing_max_rules_per_facility},
        )

    error = pricing_rule_error(payload.ruleType, payload.rate, payload.durationMinutes)
    if error is not None:
        raise ConflictError(error, code=ErrorCode.VALIDATION_ERROR, status_code=422)

    now = utcnow()
    document: PricingRuleDocument = {
        "_id": ObjectId(),
        "facilityId": facility["_id"],
        "operatorId": facility["operatorId"],
        "name": payload.name,
        "ruleType": payload.ruleType,
        "rate": float(payload.rate),
        "durationMinutes": payload.durationMinutes,
        "priority": payload.priority,
        "active": payload.active,
        "createdAt": now,
        "updatedAt": now,
    }
    try:
        await db[PRICING_RULES_COLLECTION].insert_one(document)
    except DuplicateKeyError as exc:
        raise ConflictError(
            "A pricing rule with this name already exists for the facility",
            code=PricingErrorCode.RULE_NAME_TAKEN,
        ) from exc

    stored = await db[PRICING_RULES_COLLECTION].find_one({"_id": document["_id"]})
    logger.info("Created pricing rule %s for facility %s", document["_id"], facility["_id"])
    return stored or document


async def update_rule(
    db: AsyncDatabase,
    *,
    user_id: str,
    role: Role,
    rule_id: str,
    payload: UpdatePricingRuleRequest,
) -> tuple[dict[str, Any], bool]:
    object_id = to_object_id(rule_id)
    if object_id is None:
        raise NotFoundError(RULE_NOT_FOUND)
    existing = await db[PRICING_RULES_COLLECTION].find_one({"_id": object_id})
    if existing is None:
        raise NotFoundError(RULE_NOT_FOUND)

    await owned_facility(
        db,
        user_id=user_id,
        role=role,
        facility_id=str(existing["facilityId"]),
    )

    fields = payload.model_fields_set
    if not fields:
        return existing, False

    rule_type = payload.ruleType if "ruleType" in fields else existing.get("ruleType")
    rate = float(payload.rate) if "rate" in fields else float(existing.get("rate") or 0)
    if "durationMinutes" in fields:
        duration = payload.durationMinutes
    elif "ruleType" in fields and rule_type != PricingRuleType.FIXED.value:
        duration = None
    else:
        duration = existing.get("durationMinutes")

    error = pricing_rule_error(str(rule_type), rate, duration)
    if error is not None:
        raise ConflictError(error, code=ErrorCode.VALIDATION_ERROR, status_code=422)

    updates: dict[str, Any] = {
        "ruleType": rule_type,
        "rate": rate,
        "durationMinutes": duration,
        "updatedAt": utcnow(),
    }
    for field_name in ("name", "priority", "active"):
        if field_name in fields:
            updates[field_name] = getattr(payload, field_name)

    try:
        updated = await db[PRICING_RULES_COLLECTION].find_one_and_update(
            {"_id": object_id},
            {"$set": updates},
            return_document=ReturnDocument.AFTER,
        )
    except DuplicateKeyError as exc:
        raise ConflictError(
            "A pricing rule with this name already exists for the facility",
            code=PricingErrorCode.RULE_NAME_TAKEN,
        ) from exc
    if updated is None:
        raise NotFoundError(RULE_NOT_FOUND)
    logger.info("Updated pricing rule %s", rule_id)
    return updated, True


async def delete_rule(
    db: AsyncDatabase,
    *,
    user_id: str,
    role: Role,
    rule_id: str,
) -> dict[str, Any]:
    object_id = to_object_id(rule_id)
    if object_id is None:
        raise NotFoundError(RULE_NOT_FOUND)
    existing = await db[PRICING_RULES_COLLECTION].find_one({"_id": object_id})
    if existing is None:
        raise NotFoundError(RULE_NOT_FOUND)
    await owned_facility(
        db,
        user_id=user_id,
        role=role,
        facility_id=str(existing["facilityId"]),
    )
    result = await db[PRICING_RULES_COLLECTION].delete_one({"_id": object_id})
    if result.deleted_count == 0:
        raise NotFoundError(RULE_NOT_FOUND)
    logger.info("Deleted pricing rule %s", rule_id)
    return existing


async def list_rules(
    db: AsyncDatabase,
    *,
    facility_id: str,
    page: int,
    limit: int,
) -> tuple[list[dict[str, Any]], int]:
    object_id = to_object_id(facility_id)
    if object_id is None:
        return [], 0
    query = {"facilityId": object_id}
    total = await db[PRICING_RULES_COLLECTION].count_documents(query)
    items = await (
        db[PRICING_RULES_COLLECTION]
        .find(query)
        .sort([("active", -1), ("priority", -1), ("createdAt", 1), ("_id", 1)])
        .skip((page - 1) * limit)
        .limit(limit)
        .to_list()
    )
    return items, total


__all__ = [
    "CURRENCY",
    "PricingErrorCode",
    "active_rules",
    "calculate_price",
    "create_rule",
    "delete_rule",
    "list_rules",
    "reservation_estimate",
    "session_estimate",
    "session_final_price",
    "update_rule",
]
