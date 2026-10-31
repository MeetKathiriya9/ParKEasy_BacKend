from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.models.pricing import (
    MAX_RULE_NAME_LENGTH,
    MAX_RULE_PRIORITY,
    MAX_RULE_RATE,
    MIN_RULE_NAME_LENGTH,
    pricing_rule_error,
)
from app.schemas.common import PaginatedResponse

PricingRuleKind = Literal["hourly", "fixed", "daily"]


def _collapse(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


class CreatePricingRuleRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    facilityId: str = Field(min_length=1, max_length=64)
    name: str
    ruleType: PricingRuleKind
    rate: float = Field(gt=0.0, le=MAX_RULE_RATE)
    durationMinutes: int | None = None
    priority: int = Field(default=0, ge=0, le=MAX_RULE_PRIORITY)
    active: bool = True

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        collapsed = _collapse(value)
        if not MIN_RULE_NAME_LENGTH <= len(collapsed) <= MAX_RULE_NAME_LENGTH:
            raise ValueError(
                f"name must be {MIN_RULE_NAME_LENGTH}-{MAX_RULE_NAME_LENGTH} characters"
            )
        return collapsed

    @model_validator(mode="after")
    def _rule_is_coherent(self) -> CreatePricingRuleRequest:
        error = pricing_rule_error(self.ruleType, self.rate, self.durationMinutes)
        if error is not None:
            raise ValueError(error)
        return self


class UpdatePricingRuleRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    ruleType: PricingRuleKind | None = None
    rate: float | None = None
    durationMinutes: int | None = None
    priority: int | None = Field(default=None, ge=0, le=MAX_RULE_PRIORITY)
    active: bool | None = None

    @field_validator("name")
    @classmethod
    def _name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        collapsed = _collapse(value)
        if not MIN_RULE_NAME_LENGTH <= len(collapsed) <= MAX_RULE_NAME_LENGTH:
            raise ValueError(
                f"name must be {MIN_RULE_NAME_LENGTH}-{MAX_RULE_NAME_LENGTH} characters"
            )
        return collapsed

    @model_validator(mode="after")
    def _non_null_fields(self) -> UpdatePricingRuleRequest:
        fields = self.model_fields_set
        for field_name in ("name", "ruleType", "rate", "priority", "active"):
            if field_name in fields and getattr(self, field_name) is None:
                raise ValueError(f"{field_name} cannot be null")
        return self


class PricingRuleResponse(BaseModel):
    id: str
    facilityId: str
    operatorId: str
    name: str
    ruleType: PricingRuleKind
    rate: float
    durationMinutes: int | None = None
    priority: int
    active: bool
    createdAt: datetime | None = None
    updatedAt: datetime | None = None


class PricingRulePage(PaginatedResponse[PricingRuleResponse]):
    pass


class PriceLine(BaseModel):
    kind: str
    label: str
    minutes: int | None = None
    rate: float | None = None
    amount: float
    ruleId: str | None = None


class PricingBreakdown(BaseModel):
    currency: str = "INR"
    billableMinutes: int
    baseMinutes: int
    overtimeMinutes: int
    lines: list[PriceLine] = Field(default_factory=list)
    subtotal: float
    taxAmount: float = 0.0
    feeAmount: float = 0.0
    total: float
    ruleIds: list[str] = Field(default_factory=list)
    estimated: bool = True
    computedAt: datetime


class PricingPreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    facilityId: str = Field(min_length=1, max_length=64)
    entryTime: datetime
    exitTime: datetime
    bookedEndTime: datetime | None = None

    @field_validator("entryTime", "exitTime", "bookedEndTime")
    @classmethod
    def _aware(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)

    @model_validator(mode="after")
    def _window_is_coherent(self) -> PricingPreviewRequest:
        if self.exitTime <= self.entryTime:
            raise ValueError("exitTime must be after entryTime")
        if self.bookedEndTime is not None and self.bookedEndTime < self.entryTime:
            raise ValueError("bookedEndTime must not be before entryTime")
        return self


__all__ = [
    "CreatePricingRuleRequest",
    "PriceLine",
    "PricingBreakdown",
    "PricingPreviewRequest",
    "PricingRulePage",
    "PricingRuleResponse",
    "UpdatePricingRuleRequest",
]
