from __future__ import annotations

from datetime import datetime
from typing import Any, TypedDict

from bson import ObjectId

from app.models.enums import PricingRuleType
from app.utils.mongo import as_utc

PRICING_RULES_COLLECTION = "pricingRules"

MIN_RULE_NAME_LENGTH = 2
MAX_RULE_NAME_LENGTH = 80
MIN_RULE_RATE = 0.0
MAX_RULE_RATE = 100_000.0
MIN_FIXED_DURATION_MINUTES = 30
MAX_FIXED_DURATION_MINUTES = 24 * 60
MAX_RULE_PRIORITY = 100

SUPPORTED_RULE_TYPES = frozenset(
    {
        PricingRuleType.HOURLY.value,
        PricingRuleType.FIXED.value,
        PricingRuleType.DAILY.value,
    }
)


class PricingRuleDocument(TypedDict):
    _id: ObjectId
    facilityId: ObjectId
    operatorId: ObjectId
    name: str
    ruleType: str
    rate: float
    durationMinutes: int | None
    priority: int
    active: bool
    createdAt: datetime
    updatedAt: datetime


def pricing_rule_error(
    rule_type: str,
    rate: float,
    duration_minutes: int | None,
) -> str | None:
    if rule_type not in SUPPORTED_RULE_TYPES:
        return "ruleType must be hourly, fixed or daily"
    if not MIN_RULE_RATE < rate <= MAX_RULE_RATE:
        return f"rate must be greater than {MIN_RULE_RATE} and at most {MAX_RULE_RATE}"
    if rule_type == PricingRuleType.FIXED.value:
        if duration_minutes is None:
            return "durationMinutes is required for a fixed rule"
        if not MIN_FIXED_DURATION_MINUTES <= duration_minutes <= MAX_FIXED_DURATION_MINUTES:
            return (
                "durationMinutes must be between "
                f"{MIN_FIXED_DURATION_MINUTES} and {MAX_FIXED_DURATION_MINUTES}"
            )
        return None
    if duration_minutes is not None:
        return "durationMinutes is only valid for a fixed rule"
    return None


def public_pricing_rule(document: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(document["_id"]),
        "facilityId": str(document["facilityId"]),
        "operatorId": str(document["operatorId"]),
        "name": document.get("name"),
        "ruleType": document.get("ruleType"),
        "rate": document.get("rate"),
        "durationMinutes": document.get("durationMinutes"),
        "priority": int(document.get("priority", 0)),
        "active": bool(document.get("active", True)),
        "createdAt": as_utc(document.get("createdAt")),
        "updatedAt": as_utc(document.get("updatedAt")),
    }


__all__ = [
    "MAX_FIXED_DURATION_MINUTES",
    "MAX_RULE_NAME_LENGTH",
    "MAX_RULE_PRIORITY",
    "MAX_RULE_RATE",
    "MIN_FIXED_DURATION_MINUTES",
    "MIN_RULE_NAME_LENGTH",
    "MIN_RULE_RATE",
    "PRICING_RULES_COLLECTION",
    "SUPPORTED_RULE_TYPES",
    "PricingRuleDocument",
    "pricing_rule_error",
    "public_pricing_rule",
]
