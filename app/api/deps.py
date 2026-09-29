"""Shared FastAPI dependencies: authentication, authorization, database access.

DOC section 31 requires role-based *and* facility-level authorization, so both
`require_roles` (what may you do) and `require_facility_scope` (where may you do
it) are provided here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pymongo.asynchronous.database import AsyncDatabase

from app.core.config import Settings, get_settings
from app.core.errors import ForbiddenError, UnauthorizedError
from app.core.security import decode_access_token
from app.db.client import get_database
from app.models.enums import Role

# auto_error=False so a missing header raises our own envelope, not Starlette's.
bearer_scheme = HTTPBearer(auto_error=False, description="JWT access token")


@dataclass(slots=True)
class CurrentUser:
    """The authenticated principal, decoded from the JWT."""

    id: str
    role: Role
    email: str | None = None
    facility_ids: frozenset[str] = frozenset()

    def has_facility_access(self, facility_id: str) -> bool:
        """Admins and operators reach everything; staff are scoped to assignments."""
        if self.role in (Role.ADMIN, Role.OPERATOR):
            return True
        return facility_id in self.facility_ids


async def get_current_user(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> CurrentUser:
    """Decode the Bearer token into a `CurrentUser`."""
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

    facility_ids = payload.get("facilityIds") or []

    # Kept on the request so WebSocket endpoints can read the same principal.
    request.scope["user"] = subject

    return CurrentUser(
        id=subject,
        role=role,
        email=payload.get("email"),
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
