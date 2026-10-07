"""Public avatar file serving.

    GET /api/v1/avatars/{file_name}

Deliberately unauthenticated: an `<img src>` cannot attach an `Authorization`
header, so a token-protected image URL would simply not render. The filename is
the capability -- 16 random bytes generated server-side, never derived from
anything the client sent -- and it only ever names a file this service wrote.

Served under `/api/v1` rather than a separate `/uploads` mount so the client's
existing `/api` proxy covers it in development without a second proxy rule.
Files are written by `app/services/profile.py`, which guarantees the name
matches `AVATAR_NAME_PATTERN`.
"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import FileResponse

from app.api.deps import SettingsDep
from app.core.errors import NotFoundError
from app.schemas.common import ErrorResponse
from app.services import profile as profile_service

router = APIRouter(prefix="/avatars", tags=["users"])

# Filenames are random per upload, so a stored URL never changes meaning and can
# be cached indefinitely.
_CACHE_CONTROL = "public, max-age=31536000, immutable"


@router.get(
    "/{file_name}",
    summary="Fetch an avatar image",
    response_class=FileResponse,
    responses={
        200: {"content": {"image/jpeg": {}}, "description": "The compressed JPEG"},
        404: {"model": ErrorResponse, "description": "No such avatar"},
    },
)
async def read_avatar(file_name: str, settings: SettingsDep) -> FileResponse:
    """Return a stored avatar, or 404 for any name this service did not mint.

    The name is matched against the exact pattern the writer produces *before*
    it touches the filesystem, which is what makes `../` and absolute paths
    unreachable rather than merely unlikely.
    """
    if not profile_service.is_valid_avatar_name(file_name):
        raise NotFoundError("Avatar not found")

    path = settings.avatar_path / file_name
    if not path.is_file():
        raise NotFoundError("Avatar not found")

    return FileResponse(
        path,
        media_type="image/jpeg",
        headers={"Cache-Control": _CACHE_CONTROL},
    )


__all__ = ["router"]
