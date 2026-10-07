"""Request and response bodies for the Phase 1 authentication endpoints.

Field rules come from DOC section 12 plus the section 31 security checklist:
emails are normalised to lowercase, passwords are length-checked, and every
registration failure is reported as a 422 by Pydantic rather than by hand.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, EmailStr, Field, field_validator, model_validator

from app.models.enums import Role, UserStatus
from app.schemas.common import ErrorResponse

MIN_PASSWORD_LENGTH = 8
MAX_PASSWORD_LENGTH = 72
MIN_NAME_LENGTH = 2
MAX_NAME_LENGTH = 80

Password = Annotated[str, Field(min_length=MIN_PASSWORD_LENGTH, max_length=MAX_PASSWORD_LENGTH)]


def _normalise_email(value: str) -> str:
    """Trim and lowercase so `A@B.com` and `a@b.com` are the same account."""
    return value.strip().lower()


def _validate_password_strength(value: str) -> str:
    """Require a letter and a digit, and reject an email-as-password."""
    if value.strip() == "":
        raise ValueError("Password must not be blank")
    if not re.search(r"[A-Za-z]", value):
        raise ValueError("Password must contain at least one letter")
    if not re.search(r"\d", value):
        raise ValueError("Password must contain at least one digit")
    return value


class RegisterRequest(BaseModel):
    """Body of `POST /api/v1/auth/register`."""

    name: str = Field(min_length=MIN_NAME_LENGTH, max_length=MAX_NAME_LENGTH)
    email: EmailStr
    phone: str | None = Field(default=None, max_length=20)
    password: Password
    role: Role = Role.DRIVER

    @field_validator("email")
    @classmethod
    def _normalise(cls, value: str) -> str:
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

    model_config = {
        "json_schema_extra": {
            "example": {
                "name": "Aarav Shah",
                "email": "aarav@example.com",
                "phone": "+919876543210",
                "password": "parkeasy123",
                "role": "driver",
            }
        }
    }


class LoginRequest(BaseModel):
    """Body of `POST /api/v1/auth/login`."""

    email: EmailStr
    password: str = Field(min_length=1, max_length=MAX_PASSWORD_LENGTH)

    @field_validator("email")
    @classmethod
    def _normalise(cls, value: str) -> str:
        return _normalise_email(value)

    model_config = {
        "json_schema_extra": {
            "example": {"email": "aarav@example.com", "password": "parkeasy123"}
        }
    }


class UserResponse(BaseModel):
    """The public projection of a user (never contains `passwordHash`)."""

    id: str
    name: str
    email: str
    phone: str | None = None
    role: Role
    status: UserStatus
    photoUrl: str | None = None
    facilityIds: list[str] = Field(default_factory=list)
    # Typed as datetime so OpenAPI documents the format; Pydantic serialises
    # UTC values with a trailing "Z", which is what the client expects.
    createdAt: datetime | None = None
    updatedAt: datetime | None = None
    lastLoginAt: datetime | None = None
    passwordChangedAt: datetime | None = None

    model_config = {
        "json_schema_extra": {
            "example": {
                "id": "65f2c1a9e1b2c3d4e5f60718",
                "name": "Aarav Shah",
                "email": "aarav@example.com",
                "phone": "+919876543210",
                "role": "driver",
                "status": "ACTIVE",
                "facilityIds": [],
            }
        }
    }


class TokenResponse(BaseModel):
    """Body of `POST /api/v1/auth/login` and `POST /api/v1/auth/register`.

    Phase 1 issues an access token only; refresh tokens are a later phase.
    """

    accessToken: str
    tokenType: str = "Bearer"
    expiresIn: int = Field(description="Access token lifetime in seconds")
    role: Role
    user: UserResponse

    model_config = {
        "json_schema_extra": {
            "example": {
                "accessToken": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...",
                "tokenType": "Bearer",
                "expiresIn": 3600,
                "role": "driver",
                "user": {
                    "id": "65f2c1a9e1b2c3d4e5f60718",
                    "name": "Aarav Shah",
                    "email": "aarav@example.com",
                    "phone": None,
                    "role": "driver",
                    "status": "ACTIVE",
                    "facilityIds": [],
                },
            }
        }
    }


class RegisterResponse(TokenResponse):
    """`POST /auth/register` returns a token too, so the client can go straight in."""

    message: str = "Registration successful"


class ForgotPasswordRequest(BaseModel):
    """Body of `POST /api/v1/auth/forgot-password`.

    The response is identical whether or not the address is registered, so this
    schema deliberately exposes nothing beyond the address itself.
    """

    email: EmailStr

    @field_validator("email")
    @classmethod
    def _normalise(cls, value: str) -> str:
        return _normalise_email(value)

    model_config = {
        "json_schema_extra": {
            "example": {"email": "aarav@example.com"}
        }
    }


class ForgotPasswordResponse(BaseModel):
    """Neutral acknowledgement for `POST /auth/forgot-password`.

    `devResetLink` is populated only when `ENVIRONMENT` is not `production` and
    the console transport is in use, so the flow is testable without a mailbox.
    It is never set in production -- that would hand the caller a reset token
    for any address they guess.
    """

    message: str = (
        "If an account exists for that address, a password reset link has been sent."
    )
    devResetLink: str | None = None


class ResetPasswordRequest(BaseModel):
    """Body of `POST /api/v1/auth/reset-password`."""

    token: str = Field(min_length=16, max_length=256)
    newPassword: Password

    @field_validator("newPassword")
    @classmethod
    def _strength(cls, value: str) -> str:
        return _validate_password_strength(value)

    model_config = {
        "json_schema_extra": {
            "example": {
                "token": "b7f1c2e4a9d84f0e8c3b5a17d6e94f20c8b1a35d7e2f904",
                "newPassword": "newsecret456",
            }
        }
    }


class ChangePasswordRequest(BaseModel):
    """Body of `POST /api/v1/auth/change-password`.

    Requires the current password because the access token alone is not treated
    as proof of identity for a credential change. Reusing the current password
    is rejected: silently succeeding would leave the caller believing they had
    rotated a compromised password when they had not.
    """

    currentPassword: str = Field(min_length=1, max_length=MAX_PASSWORD_LENGTH)
    newPassword: Password

    @field_validator("newPassword")
    @classmethod
    def _strength(cls, value: str) -> str:
        return _validate_password_strength(value)

    @model_validator(mode="after")
    def _different_from_current(self) -> ChangePasswordRequest:
        if self.currentPassword == self.newPassword:
            raise ValueError("New password must be different from the current password")
        return self

    model_config = {
        "json_schema_extra": {
            "example": {
                "currentPassword": "oldsecret123",
                "newPassword": "newsecret456",
            }
        }
    }


class PasswordChangedResponse(BaseModel):
    """Acknowledgement for both `reset-password` and `change-password`."""

    message: str = "Password updated. Please sign in again."


AUTH_ERROR_RESPONSES: dict[int | str, dict[str, type]] = {
    400: {"model": ErrorResponse, "description": "Invalid credentials or validation rule"},
    401: {"model": ErrorResponse, "description": "Bad email or password"},
    403: {"model": ErrorResponse, "description": "Role may not self-register"},
    409: {"model": ErrorResponse, "description": "Email already registered"},
    422: {"model": ErrorResponse, "description": "Request validation failed"},
    429: {"model": ErrorResponse, "description": "Too many attempts"},
}


PASSWORD_RESET_ERROR_RESPONSES: dict[int | str, dict[str, type]] = {
    400: {"model": ErrorResponse, "description": "Reset token is invalid, expired or already used"},
    422: {"model": ErrorResponse, "description": "Request validation failed"},
    429: {"model": ErrorResponse, "description": "Too many attempts"},
}
