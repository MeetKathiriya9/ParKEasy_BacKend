"""Profile reads/writes and avatar storage (DOC sections 7/18/21).

Services own every database *write*, matching `app/services/auth.py`. Routes in
`app/api/v1/users.py` stay thin: validate, delegate here, serialise.

Avatars are always re-encoded on the way in. The upload directory therefore
only ever contains small, server-generated progressive JPEGs whatever the
client sent -- there is no path by which an untrusted file lands on disk
verbatim. The decoder is the content check: `PIL.Image.open` fails on anything
that is not really an image, and `image.format` is read from the decoded bytes
rather than from the declared MIME type or the filename, so a `.jpg` that is
actually a GIF or an HTML document is rejected.
"""

from __future__ import annotations

import asyncio
import io
import logging
import re
import secrets
from typing import Any

from PIL import Image, ImageOps
from pymongo import ReturnDocument
from pymongo.asynchronous.database import AsyncDatabase

from app.core.config import Settings
from app.core.errors import BadRequestError, NotFoundError
from app.models.user import USERS_COLLECTION
from app.schemas.user import UpdateProfileRequest
from app.utils.mongo import to_object_id, utcnow

logger = logging.getLogger(__name__)

# Inputs we are willing to decode. Everything is written back out as JPEG.
AVATAR_SOURCE_FORMATS = frozenset({"JPEG", "PNG", "WEBP"})

# `/api/v1/avatars/<file>` -- served by `app/api/v1/avatars.py`.
AVATAR_URL_PREFIX = "/api/v1/avatars"

# Names we generate ourselves: `<24 hex id>-<16 hex nonce>.jpg`. Anything else
# is a 404 by construction, which is what keeps `../` out of the file path.
AVATAR_NAME_PATTERN = re.compile(r"^[0-9a-f]{24}-[0-9a-f]{16}\.jpg$")

# Accepted formats, used for the error message only.
_FORMAT_LABELS = {"JPEG": "JPEG", "PNG": "PNG", "WEBP": "WebP"}


class ProfileErrorCode:
    """Machine-readable reasons carried in `error.details` on a 400."""

    PHOTO_EMPTY = "PHOTO_EMPTY"
    PHOTO_TOO_LARGE = "PHOTO_TOO_LARGE"
    PHOTO_UNREADABLE = "PHOTO_UNREADABLE"
    PHOTO_FORMAT_UNSUPPORTED = "PHOTO_FORMAT_UNSUPPORTED"
    PHOTO_TOO_MANY_PIXELS = "PHOTO_TOO_MANY_PIXELS"


def _bad_request(message: str, reason: str) -> BadRequestError:
    """A 400 whose `details` let a client branch without parsing prose."""
    return BadRequestError(message, details={"reason": reason})


def is_valid_avatar_name(file_name: str) -> bool:
    """True for a filename this module could have produced."""
    return AVATAR_NAME_PATTERN.fullmatch(file_name) is not None


def avatar_url(file_name: str) -> str:
    return f"{AVATAR_URL_PREFIX}/{file_name}"


def avatar_file_name(url: str | None) -> str | None:
    """Extract the filename from a stored `photoUrl`, or `None` if it is not one."""
    if not url:
        return None
    prefix = f"{AVATAR_URL_PREFIX}/"
    if not url.startswith(prefix):
        return None
    name = url[len(prefix) :]
    return name if is_valid_avatar_name(name) else None


def compress_avatar(raw: bytes, settings: Settings) -> bytes:
    """Re-encode an uploaded image into a small progressive JPEG.

    Performs every content check here, synchronously; the route runs it in a
    worker thread because Pillow is CPU-bound and would otherwise block the
    event loop.

    Raises `BadRequestError` (400) for anything that is not a usable image.
    """
    if not raw:
        raise _bad_request("No image was received", ProfileErrorCode.PHOTO_EMPTY)

    if len(raw) > settings.avatar_max_bytes:
        limit_kb = settings.avatar_max_bytes // 1024
        raise _bad_request(
            f"Image is larger than the {limit_kb} KB limit",
            ProfileErrorCode.PHOTO_TOO_LARGE,
        )

    # `verify()` validates the container structure without decoding pixels.
    try:
        probe = Image.open(io.BytesIO(raw))
        detected_format = probe.format
        probe.verify()
    except Image.DecompressionBombError as exc:
        raise _bad_request(
            "Image dimensions are too large", ProfileErrorCode.PHOTO_TOO_MANY_PIXELS
        ) from exc
    except (Image.UnidentifiedImageError, OSError, ValueError, SyntaxError) as exc:
        raise _bad_request(
            "File is not a valid image", ProfileErrorCode.PHOTO_UNREADABLE
        ) from exc

    # Read from the decoded bytes, never from the client's MIME type or name.
    if detected_format not in AVATAR_SOURCE_FORMATS:
        supported = ", ".join(sorted(_FORMAT_LABELS[f] for f in AVATAR_SOURCE_FORMATS))
        raise _bad_request(
            f"Only {supported} images are supported",
            ProfileErrorCode.PHOTO_FORMAT_UNSUPPORTED,
        )

    try:
        image = Image.open(io.BytesIO(raw))
        if image.width * image.height > settings.avatar_max_pixels:
            raise _bad_request(
                "Image dimensions are too large", ProfileErrorCode.PHOTO_TOO_MANY_PIXELS
            )
        # Honour EXIF orientation before downscaling, otherwise a phone photo
        # that needs a rotation is baked in sideways.
        image = ImageOps.exif_transpose(image)
        if image.mode in ("RGBA", "LA", "P"):
            # JPEG has no alpha channel; flatten onto white so the result does
            # not come out black where the source was transparent.
            rgba = image.convert("RGBA")
            flat = Image.new("RGB", rgba.size, (255, 255, 255))
            flat.paste(rgba, mask=rgba.getchannel("A"))
            image = flat
        else:
            image = image.convert("RGB")
        image.thumbnail(
            (settings.avatar_max_dimension, settings.avatar_max_dimension),
            Image.LANCZOS,
        )
        buffer = io.BytesIO()
        image.save(
            buffer,
            format="JPEG",
            quality=settings.avatar_jpeg_quality,
            optimize=True,
            progressive=True,
        )
    except BadRequestError:
        raise
    except Image.DecompressionBombError as exc:
        raise _bad_request(
            "Image dimensions are too large", ProfileErrorCode.PHOTO_TOO_MANY_PIXELS
        ) from exc
    except (OSError, ValueError, SyntaxError) as exc:
        raise _bad_request(
            "File is not a valid image", ProfileErrorCode.PHOTO_UNREADABLE
        ) from exc

    return buffer.getvalue()


async def get_profile(db: AsyncDatabase, *, user_id: str) -> dict[str, Any]:
    """Load a user document for the profile endpoints."""
    object_id = to_object_id(user_id)
    if object_id is None:
        raise NotFoundError("This account no longer exists")

    document = await db[USERS_COLLECTION].find_one({"_id": object_id})
    if document is None:
        raise NotFoundError("This account no longer exists")
    return document


async def update_profile(
    db: AsyncDatabase,
    *,
    user_id: str,
    payload: UpdateProfileRequest,
) -> tuple[dict[str, Any], bool]:
    """Apply a `PATCH` and return `(document, changed)`.

    `changed` is False when no field was supplied, so the route can skip the
    audit row and the `updatedAt` bump for a request that did nothing.

    `email`, `role`, `status` and `facilityIds` are not writable here: the
    schema rejects them with a 422 before this function is reached.
    """
    fields = payload.model_fields_set
    if not fields:
        return await get_profile(db, user_id=user_id), False

    object_id = to_object_id(user_id)
    if object_id is None:
        raise NotFoundError("This account no longer exists")

    updates: dict[str, Any] = {}
    # Absent means "leave alone"; an explicit `phone: null` clears it.
    if "name" in fields:
        updates["name"] = payload.name
    if "phone" in fields:
        updates["phone"] = payload.phone
    if not updates:
        return await get_profile(db, user_id=user_id), False

    updates["updatedAt"] = utcnow()
    document = await db[USERS_COLLECTION].find_one_and_update(
        {"_id": object_id},
        {"$set": updates},
        return_document=ReturnDocument.AFTER,
    )
    if document is None:
        raise NotFoundError("This account no longer exists")

    logger.info("Profile updated for user %s (%s)", user_id, ",".join(sorted(fields)))
    return document, True


async def save_photo(
    db: AsyncDatabase,
    *,
    user_id: str,
    raw: bytes,
    settings: Settings,
) -> dict[str, Any]:
    """Compress, store and record a new avatar. Returns the updated document.

    Ordering matters: the new file is written first, then the document points
    at it, then the previous file is removed. If the write fails the new file
    is unlinked again, so there is never a `photoUrl` pointing at a file that
    does not exist. The old file is removed last and best-effort -- an orphan
    costs a few kilobytes, a missing avatar costs a broken image.
    """
    object_id = to_object_id(user_id)
    if object_id is None:
        raise NotFoundError("This account no longer exists")

    existing = await db[USERS_COLLECTION].find_one({"_id": object_id}, {"photoUrl": 1})
    if existing is None:
        raise NotFoundError("This account no longer exists")

    data = await asyncio.to_thread(compress_avatar, raw, settings)

    root = settings.avatar_path
    # Created here as well as at startup so a wiped directory heals itself.
    await asyncio.to_thread(root.mkdir, parents=True, exist_ok=True)

    file_name = f"{object_id}-{secrets.token_hex(8)}.jpg"
    path = root / file_name
    await asyncio.to_thread(path.write_bytes, data)

    try:
        document = await db[USERS_COLLECTION].find_one_and_update(
            {"_id": object_id},
            {"$set": {"photoUrl": avatar_url(file_name), "updatedAt": utcnow()}},
            return_document=ReturnDocument.AFTER,
        )
    except Exception:
        await asyncio.to_thread(path.unlink, True)
        raise

    if document is None:
        await asyncio.to_thread(path.unlink, True)
        raise NotFoundError("This account no longer exists")

    await _discard_file(settings, avatar_file_name(existing.get("photoUrl")))
    logger.info("Saved avatar %s for user %s (%d bytes)", file_name, user_id, len(data))
    return document


async def remove_photo(
    db: AsyncDatabase,
    *,
    user_id: str,
    settings: Settings,
) -> dict[str, Any]:
    """Clear `photoUrl` and delete the stored JPEG. Returns the updated document.

    The document is updated before the file is removed: a stale file is
    harmless, a `photoUrl` with no file behind it is a broken image in the UI.
    """
    object_id = to_object_id(user_id)
    if object_id is None:
        raise NotFoundError("This account no longer exists")

    existing = await db[USERS_COLLECTION].find_one({"_id": object_id}, {"photoUrl": 1})
    if existing is None:
        raise NotFoundError("This account no longer exists")

    document = await db[USERS_COLLECTION].find_one_and_update(
        {"_id": object_id},
        {"$set": {"photoUrl": None, "updatedAt": utcnow()}},
        return_document=ReturnDocument.AFTER,
    )
    if document is None:
        raise NotFoundError("This account no longer exists")

    await _discard_file(settings, avatar_file_name(existing.get("photoUrl")))
    logger.info("Removed avatar for user %s", user_id)
    return document


async def _discard_file(settings: Settings, file_name: str | None) -> None:
    """Best-effort unlink of a stored avatar; never raises."""
    if not file_name:
        return
    try:
        await asyncio.to_thread((settings.avatar_path / file_name).unlink, True)
    except OSError:
        logger.exception("Could not remove avatar file %s", file_name)
