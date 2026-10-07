"""The `users` group: the signed-in user's profile (DOC sections 18/21).

    GET    /me         read the profile
    PATCH  /me         update name and phone
    POST   /me/photo   upload/replace the photo (compressed server-side)
    DELETE /me/photo   remove the photo

Every route acts on the caller's own account. There is deliberately no
`/users/{id}` yet: an admin editing arbitrary users needs an RBAC story that
this phase does not have, and DOC section 21 only calls for `/me`.

`email` is read-only -- see `app/schemas/user.py` for why.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, Request, UploadFile, status
from pymongo.asynchronous.database import AsyncDatabase

from app.api.deps import CurrentUserDep
from app.api.throttle import client_ip, throttle
from app.core.config import Settings, get_settings
from app.core.rate_limit import RateLimit
from app.db.client import get_database
from app.models.user import public_user
from app.schemas.auth import UserResponse
from app.schemas.common import ErrorResponse
from app.schemas.user import UpdateProfileRequest
from app.services import audit
from app.services import profile as profile_service

router = APIRouter(prefix="/users", tags=["users"])

# Shared by every route here: a missing/expired token is a 401.
AUTHENTICATED_ERRORS: dict[int | str, dict[str, type[Any]]] = {
    401: {"model": ErrorResponse, "description": "Missing, invalid or revoked token"},
}

_USERS_ERROR_RESPONSES: dict[int | str, dict[str, type[Any]]] = {
    **AUTHENTICATED_ERRORS,
    400: {"model": ErrorResponse, "description": "Unusable image or invalid field"},
    404: {"model": ErrorResponse, "description": "Account no longer exists"},
    413: {"model": ErrorResponse, "description": "Request body exceeded the global limit"},
    422: {
        "model": ErrorResponse,
        "description": "Request validation failed (e.g. email is not writable)",
    },
    429: {"model": ErrorResponse, "description": "Too many attempts"},
}


def _to_response(document: dict[str, Any]) -> UserResponse:
    """Project a user document, which also guarantees no `passwordHash` leaks."""
    return UserResponse.model_validate(public_user(document))


@router.get(
    "/me",
    response_model=UserResponse,
    summary="Read the signed-in user's profile",
    description=(
        "Reads the stored document, so it reflects a change made in another tab "
        "immediately. `/auth/me` is the cheaper session-restore variant."
    ),
    responses=AUTHENTICATED_ERRORS,
)
async def read_profile(
    user: CurrentUserDep,
    db: Annotated[AsyncDatabase, Depends(get_database)],
) -> UserResponse:
    document = await profile_service.get_profile(db, user_id=user.id)
    return _to_response(document)


@router.patch(
    "/me",
    response_model=UserResponse,
    summary="Update the signed-in user's profile",
    description=(
        "Partial update: send only what changed. `phone: null` clears the "
        "number, while omitting the key leaves it untouched. `email`, `role`, "
        "`status` and `facilityIds` are not writable and produce a 422."
    ),
    responses=_USERS_ERROR_RESPONSES,
)
async def update_profile(
    payload: UpdateProfileRequest,
    request: Request,
    user: CurrentUserDep,
    db: Annotated[AsyncDatabase, Depends(get_database)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> UserResponse:
    throttle(
        request,
        bucket="profile-update",
        identity=user.id,
        rule=RateLimit(
            limit=settings.profile_update_rate_limit,
            window_seconds=settings.profile_update_rate_window_seconds,
        ),
    )

    document, changed = await profile_service.update_profile(
        db, user_id=user.id, payload=payload
    )

    if changed:
        # Field *names* only -- never the values, which may be personal data.
        await audit.record(
            db,
            action="PROFILE_UPDATED",
            entity="user",
            actor_id=user.id,
            ip=client_ip(request),
            metadata={"fields": sorted(payload.model_fields_set)},
        )

    return _to_response(document)


@router.post(
    "/me/photo",
    response_model=UserResponse,
    summary="Upload or replace the profile photo",
    description=(
        "Accepts a JPEG, PNG or WebP image. The file is always re-encoded "
        "server-side into a downscaled progressive JPEG, so the stored file is "
        "compressed and the client's filename, MIME type and original encoding "
        "are never trusted."
    ),
    responses=_USERS_ERROR_RESPONSES,
)
async def upload_photo(
    request: Request,
    user: CurrentUserDep,
    db: Annotated[AsyncDatabase, Depends(get_database)],
    settings: Annotated[Settings, Depends(get_settings)],
    file: Annotated[UploadFile, File(description="JPEG, PNG or WebP image")],
) -> UserResponse:
    throttle(
        request,
        bucket="avatar-write",
        identity=user.id,
        rule=RateLimit(
            limit=settings.avatar_upload_rate_limit,
            window_seconds=settings.avatar_upload_rate_window_seconds,
        ),
    )

    # One byte past the cap is enough to know it is over it, so a huge upload is
    # never held in memory here. The global body-limit middleware has already
    # rejected anything grossly oversized.
    raw = await file.read(settings.avatar_max_bytes + 1)
    original_name = (file.filename or "")[:255] or None
    content_type = file.content_type

    document = await profile_service.save_photo(
        db, user_id=user.id, raw=raw, settings=settings
    )

    await audit.record(
        db,
        action="PROFILE_PHOTO_CHANGED",
        entity="user",
        actor_id=user.id,
        ip=client_ip(request),
        metadata={
            "bytes": len(raw),
            # Untrusted, stored for forensics only; never used to build a path.
            "uploadedName": original_name,
            "declaredType": content_type,
        },
    )

    return _to_response(document)


@router.delete(
    "/me/photo",
    response_model=UserResponse,
    status_code=status.HTTP_200_OK,
    summary="Remove the profile photo",
    description=(
        "Clears the photo and deletes the stored file. Succeeds even when no "
        "photo was set, so the call is safe to retry."
    ),
    responses=_USERS_ERROR_RESPONSES,
)
async def remove_photo(
    request: Request,
    user: CurrentUserDep,
    db: Annotated[AsyncDatabase, Depends(get_database)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> UserResponse:
    throttle(
        request,
        bucket="avatar-write",
        identity=user.id,
        rule=RateLimit(
            limit=settings.avatar_upload_rate_limit,
            window_seconds=settings.avatar_upload_rate_window_seconds,
        ),
    )

    document = await profile_service.remove_photo(db, user_id=user.id, settings=settings)

    await audit.record(
        db,
        action="PROFILE_PHOTO_REMOVED",
        entity="user",
        actor_id=user.id,
        ip=client_ip(request),
    )

    return _to_response(document)


__all__ = ["router"]
