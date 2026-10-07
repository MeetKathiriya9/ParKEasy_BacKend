"""Request body for the `users` group (DOC sections 18/21).

The profile is deliberately narrow: `name`, `phone` and a photo. `email` is
returned by the API but is **not** writable -- it is the account's login
identifier and the DOC gives no change-of-address flow, so silently accepting a
new value would leave the user believing they had re-keyed an address they had
not. Sending it produces a `422` instead of being dropped.

The response shape is `app.schemas.auth.UserResponse`, shared with `/auth/me`
so the two endpoints cannot drift into subtly different profiles.

The wire format stays camelCase to match the rest of the API.
"""

from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.schemas.auth import MAX_NAME_LENGTH, MIN_NAME_LENGTH

MAX_PHONE_LENGTH = 20


class UpdateProfileRequest(BaseModel):
    """Body of `PATCH /api/v1/users/me`.

    Every field is optional because this is a PATCH: a key that is absent is
    left alone, while `phone` sent as an explicit `null` clears it. That
    distinction is carried by `model_fields_set`, not by the value itself.
    """

    # Rejects `email`, `role`, `status` and `facilityIds` outright with a 422
    # rather than dropping them, so a client bug surfaces instead of being
    # silently ignored.
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(
        default=None,
        min_length=MIN_NAME_LENGTH,
        max_length=MAX_NAME_LENGTH,
    )
    phone: str | None = Field(default=None, max_length=MAX_PHONE_LENGTH)

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
    def _name_cannot_be_null(self) -> UpdateProfileRequest:
        # `name` is non-nullable in the database, so `{"name": null}` is a
        # mistake rather than a way to clear the field.
        if "name" in self.model_fields_set and self.name is None:
            raise ValueError("name cannot be null")
        if "name" in self.model_fields_set and not self.name:
            raise ValueError("name cannot be blank")
        return self
