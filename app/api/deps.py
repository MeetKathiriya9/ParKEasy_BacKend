"""Shared FastAPI dependencies: authentication, authorization, database access.

DOC section 31 requires role-based *and* facility-level authorization, so both
`require_roles` (what may you do) and `require_facility_scope` (where may you do
it) are provided here.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pymongo.asynchronous.database import AsyncDatabase

from app.core.config import Settings, get_settings
from app.core.errors import ForbiddenError, UnauthorizedError
from app.core.security import decode_access_token
from app.db.client import get_database
from app.models.enums import Role, UserStatus
from app.models.user import USERS_COLLECTION
from app.services.auth import is_token_revoked
from app.utils.mongo import to_object_id

# auto_error=False so a missing header raises our own envelope, not Starlette's.
bearer_scheme = HTTPBearer(auto_error=False, description="JWT access token")


def _as_utc(value: Any) -> datetime | None:
    """Coerce a stored datetime to an aware UTC datetime.

    PyMongo hands back naive datetimes by default unless the codec options ask
    otherwise, and mixing naive with aware values raises on comparison, so this
    normalises both cases to something safe to compare against `iat`.
    """
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return None


@dataclass(slots=True)
class CurrentUser:
    """The authenticated principal, decoded from the JWT."""

    id: str
    role: Role
    email: str | None = None
    name: str | None = None
    status: UserStatus = UserStatus.ACTIVE
    facility_ids: frozenset[str] = frozenset()

    def has_facility_access(self, facility_id: str) -> bool:
        """Admins and operators reach everything; staff are scoped to assignments."""
        if self.role in (Role.ADMIN, Role.OPERATOR):
            return True
        return facility_id in self.facility_ids


class TokenRevokedError(UnauthorizedError):
    code = "TOKEN_REVOKED"


class PasswordChangedError(UnauthorizedError):
    code = "PASSWORD_CHANGED"


async def get_current_user(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
    db: Annotated[AsyncDatabase, Depends(get_database)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> CurrentUser:
    """Decode the Bearer token into a `CurrentUser`.

    The JWT signature alone cannot express "logged out" or "password changed",
    so the payload is checked against two pieces of server state:

    * `revokedTokens._id` -- an explicit sign-out. One indexed lookup on the
      primary key.
    * `users.passwordChangedAt` -- rejects every token issued before the last
      password change, which is how a reset invalidates all other devices
      without having to track individual tokens.

    Both lookups are issued concurrently, so this costs roughly one round trip
    rather than two. A short-lived cache would remove the reads but would also
    delay revocation by the cache TTL, which is the wrong trade for a
    credential change.
    """
    if credentials is None or not credentials.credentials:
        raise UnauthorizedError("Authorization header with a Bearer token is required")

    payload = decode_access_token(credentials.credentials, settings)

    subject = payload.get("sub")
    if not subject:
        raise UnauthorizedError("Access token is missing a subject")

    raw_role = payload.get("role")
    try:
        role = Role(raw_role)
    except ValueError as exc:
        raise UnauthorizedError(f"Unknown role '{raw_role}'") from exc

    jti = payload.get("jti")

    async def _is_revoked() -> bool:
        return bool(jti) and await is_token_revoked(db, str(jti))

    revoked, account = await asyncio.gather(
        _is_revoked(),
        db[USERS_COLLECTION].find_one({"_id": to_object_id(subject)}, {"passwordChangedAt": 1}),
    )

    if revoked:
        raise TokenRevokedError("This session has been signed out. Please log in again.")

    # `to_object_id` returns None for anything that is not a 24-character hex
    # string, so a malformed `sub` lands here instead of raising a raw
    # `InvalidId` out of PyMongo.
    if account is None:
        raise TokenRevokedError("This session is no longer valid. Please log in again.")

    password_changed_at = account.get("passwordChangedAt")
    # Prefer the millisecond claim: it makes "issued before the change" an exact
    # test. Tokens minted before `iat_ms` existed fall back to `iat`, which has
    # one-second resolution, so the comparison is floored to whole seconds.
    issued_at_ms = payload.get("iat_ms")
    issued_at = payload.get("iat")
    # A missing `passwordChangedAt` means the document predates the field and
    # the password has never been changed since. Treating that as "expired"
    # would lock out every account created before the column was added.
    if password_changed_at is not None:
        changed_at = _as_utc(password_changed_at)
        if changed_at is not None:
            if issued_at_ms is not None:
                stale = issued_at_ms < int(changed_at.timestamp() * 1000)
            elif issued_at is not None:
                stale = issued_at < int(changed_at.timestamp())
            else:
                stale = False
            if stale:
                raise PasswordChangedError(
                    "Your password was changed. Please sign in again on this device."
                )

    raw_status = payload.get("status") or UserStatus.ACTIVE
    try:
        status = UserStatus(raw_status)
    except ValueError:
        status = UserStatus.ACTIVE

    facility_ids = payload.get("facilityIds") or []

    # Kept on the request so WebSocket endpoints can read the same principal.
    request.scope["user"] = subject

    return CurrentUser(
        id=subject,
        role=role,
        email=payload.get("email"),
        name=payload.get("name"),
        status=status,
        facility_ids=frozenset(facility_ids),
    )


def require_roles(*allowed: Role) -> Any:
    """Dependency factory restricting a route to the given roles.

    Usage:  `@router.post(..., dependencies=[Depends(require_roles(Role.ADMIN))])`
    """

    permitted = frozenset(allowed)

    async def _dependency(
        user: Annotated[CurrentUser, Depends(get_current_user)],
    ) -> CurrentUser:
        if user.role not in permitted:
            raise ForbiddenError(
                f"Role '{user.role}' may not perform this action",
                details={"required_roles": sorted(permitted)},
            )
        return user

    return _dependency


def require_facility_scope(facility_id_param: str = "facility_id") -> Any:
    """Dependency factory enforcing facility-level access (DOC section 31).

    Resolves the target facility id from a path parameter, and additionally
    refuses to let a user act on a facility they do not own by reading the
    `ownerId` of the target document.
    """

    async def _dependency(
        user: Annotated[CurrentUser, Depends(get_current_user)],
        request: Request,
        db: Annotated[AsyncDatabase, Depends(get_database)],
    ) -> CurrentUser:
        facility_id = request.path_params.get(facility_id_param)
        if facility_id is None:
            raise UnauthorizedError(f"Missing path parameter '{facility_id_param}'")

        if user.has_facility_access(facility_id):
            return user

        # Operator: verify ownership rather than trusting the request alone.
        if user.role is Role.OPERATOR:
            facility = await db["parkingFacilities"].find_one(
                {"_id": facility_id}, {"ownerId": 1}
            )
            owner_id = (facility or {}).get("ownerId")
            if owner_id is None or str(owner_id) == user.id:
                return user

        raise ForbiddenError("You do not have access to this facility")

    return _dependency


CurrentUserDep = Annotated[CurrentUser, Depends(get_current_user)]
DatabaseDep = Annotated[AsyncDatabase, Depends(get_database)]
SettingsDep = Annotated[Settings, Depends(get_settings)]
