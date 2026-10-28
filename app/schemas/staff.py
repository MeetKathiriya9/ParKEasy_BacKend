"""Request and response bodies for the `staff` group (DOC sections 7/10/11).

Staff accounts are created by an operator (or an admin on an operator's behalf)
rather than by public registration, so this group owns the first password too.
The rules match `app.schemas.auth` - the same 2-80 character name, the same
8-72 character password with a letter and a digit - so an account created here
logs in through the normal `/auth/login` route.

Not request fields, by design:

* `email` is the login identifier and is immutable, matching `PATCH /users/me`.
* `role` is always `staff`; this group cannot mint a driver/operator/admin.
* `status` is a first-class transition on `PATCH /staff/{id}/status`, so a
  casual edit cannot silently recharge a suspended account.
* `facilityIds` are assignments, managed on `POST/DELETE .../assignments` after
  creation (the create body accepts an initial list as a convenience).
* `operatorId` is ownership. An operator's own id is used automatically; only an
  admin may name another operator, exactly like `POST /facilities`.
"""

from __future__ import annotations

import re
from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator, model_validator

from app.models.enums import AssignmentStatus, UserStatus
from app.models.staff import MAX_ASSIGNMENT_NOTE_LENGTH
from app.schemas.auth import (
    MAX_NAME_LENGTH,
    MIN_NAME_LENGTH,
    Password,
)

MAX_PHONE_LENGTH = 20
MAX_STAFF_FACILITIES = 50


def _normalise_email(value: str) -> str:
    return value.strip().lower()


def _validate_password_strength(value: str) -> str:
    """Same rule as registration: a letter, a digit, and not blank."""
    if value.strip() == "":
        raise ValueError("Password must not be blank")
    if not re.search(r"[A-Za-z]", value):
        raise ValueError("Password must contain at least one letter")
    if not re.search(r"\d", value):
        raise ValueError("Password must contain at least one digit")
    return value


class CreateStaffRequest(BaseModel):
    """Body of `POST /api/v1/staff`."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=MIN_NAME_LENGTH, max_length=MAX_NAME_LENGTH)
    email: EmailStr
    phone: str | None = Field(default=None, max_length=MAX_PHONE_LENGTH)
    password: Password
    # Convenience for "create and assign in one step". The same rules as the
    # assignment route apply: every id must be a facility the caller may assign.
    facilityIds: list[str] = Field(default_factory=list, max_length=MAX_STAFF_FACILITIES)
    # Admin-only ownership. Operators sending it get a 422 from the service.
    operatorId: str | None = None

    @field_validator("email")
    @classmethod
    def _email(cls, value: str) -> str:
        return _normalise_email(value)

    @field_validator("name")
    @classmethod
    def _collapse_whitespace(cls, value: str) -> str:
        return re.sub(r"\s+", " ", value).strip()

    @field_validator("phone")
    @classmethod
    def _clean_phone(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = re.sub(r"[^\d+]", "", value).strip()
        return cleaned or None

    @field_validator("password")
    @classmethod
    def _strength(cls, value: str) -> str:
        return _validate_password_strength(value)

    @field_validator("facilityIds")
    @classmethod
    def _unique_facilities(cls, value: list[str]) -> list[str]:
        seen: set[str] = set()
        unique: list[str] = []
        for facility_id in value:
            cleaned = facility_id.strip()
            if cleaned and cleaned not in seen:
                seen.add(cleaned)
                unique.append(cleaned)
        return unique


class UpdateStaffRequest(BaseModel):
    """Body of `PATCH /api/v1/staff/{id}` - partial update."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=MIN_NAME_LENGTH, max_length=MAX_NAME_LENGTH)
    phone: str | None = Field(default=None, max_length=MAX_PHONE_LENGTH)
    # Admin-only; used to adopt a staff account that has no owning operator
    # (for example one created by public registration before this module).
    operatorId: str | None = None

    @field_validator("name")
    @classmethod
    def _collapse_whitespace(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return re.sub(r"\s+", " ", value).strip()

    @field_validator("phone")
    @classmethod
    def _clean_phone(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = re.sub(r"[^\d+]", "", value).strip()
        return cleaned or None

    @model_validator(mode="after")
    def _name_cannot_be_null(self) -> UpdateStaffRequest:
        if "name" in self.model_fields_set and not self.name:
            raise ValueError("name cannot be null or blank")
        return self


class ChangeStaffStatusRequest(BaseModel):
    """Body of `PATCH /api/v1/staff/{id}/status`."""

    model_config = ConfigDict(extra="forbid")

    status: UserStatus
    reason: str | None = Field(default=None, max_length=MAX_ASSIGNMENT_NOTE_LENGTH)

    @model_validator(mode="after")
    def _only_active_or_suspended(self) -> ChangeStaffStatusRequest:
        if self.status not in (UserStatus.ACTIVE, UserStatus.SUSPENDED):
            raise ValueError("status must be ACTIVE or SUSPENDED")
        return self


class CreateAssignmentRequest(BaseModel):
    """Body of `POST /api/v1/staff/{id}/assignments`."""

    model_config = ConfigDict(extra="forbid")

    facilityId: str = Field(min_length=1, max_length=64)
    note: str | None = Field(default=None, max_length=MAX_ASSIGNMENT_NOTE_LENGTH)


class StaffFacilitySummary(BaseModel):
    """The bit of a facility a staff card needs without a second request."""

    id: str
    name: str
    status: str


class StaffAssignmentResponse(BaseModel):
    """One assignment period, active or closed."""

    id: str
    staffId: str
    facilityId: str
    facilityName: str | None = None
    facilityStatus: str | None = None
    operatorId: str | None = None
    status: AssignmentStatus
    assignedById: str | None = None
    assignedByName: str | None = None
    assignedAt: datetime
    deactivatedById: str | None = None
    deactivatedByName: str | None = None
    deactivatedAt: datetime | None = None
    reason: str | None = None


class StaffResponse(BaseModel):
    """A staff member in a list."""

    id: str
    name: str
    email: str
    phone: str | None = None
    status: UserStatus
    operatorId: str | None = None
    operatorName: str | None = None
    facilityIds: list[str] = Field(default_factory=list)
    facilities: list[StaffFacilitySummary] = Field(default_factory=list)
    createdAt: datetime | None = None
    updatedAt: datetime | None = None
    lastLoginAt: datetime | None = None


class StaffDetailResponse(StaffResponse):
    """A staff member plus the full assignment history, newest first."""

    assignments: list[StaffAssignmentResponse] = Field(default_factory=list)


__all__ = [
    "ChangeStaffStatusRequest",
    "CreateAssignmentRequest",
    "CreateStaffRequest",
    "StaffAssignmentResponse",
    "StaffDetailResponse",
    "StaffFacilitySummary",
    "StaffResponse",
    "UpdateStaffRequest",
]
