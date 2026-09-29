"""Security primitives: JWT issuing/decoding and password hashing.

Passwords use the `bcrypt` library directly. The `passlib` library is NOT used
because version 1.7.4 is incompatible with bcrypt 5.x.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import bcrypt
import jwt

from app.core.config import Settings
from app.core.errors import UnauthorizedError

# bcrypt refuses inputs longer than 72 bytes; we pre-hash with SHA-256 to keep
# long passphrases usable while still using bcrypt's cost factor + salt.
_MAX_BCRYPT_BYTES = 72


class TokenError(UnauthorizedError):
    code = "INVALID_TOKEN"


def _prepare(password: str) -> bytes:
    import hashlib

    if len(password.encode("utf-8")) > _MAX_BCRYPT_BYTES:
        return hashlib.sha256(password.encode("utf-8")).hexdigest().encode("ascii")
    return password.encode("utf-8")


def hash_password(password: str) -> str:
    """Return a bcrypt hash of `password` (never store the plain text)."""
    return bcrypt.hashpw(_prepare(password), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    """Constant-time comparison of `password` against a stored bcrypt hash."""
    try:
        return bcrypt.checkpw(_prepare(password), password_hash.encode("utf-8"))
    except (ValueError, TypeError):
        return False


def create_access_token(
    *,
    subject: str,
    role: str,
    settings: Settings,
    extra_claims: dict[str, Any] | None = None,
) -> tuple[str, datetime]:
    """Create a signed JWT access token.

    Returns the token and its expiry so callers can attach `expires_in` to a
    login response without re-decoding.
    """
    now = datetime.now(UTC)
    expires_at = now + timedelta(minutes=settings.jwt_expires_minutes)

    payload: dict[str, Any] = {
        "sub": subject,
        "role": role,
        "iss": settings.jwt_issuer,
        "iat": int(now.timestamp()),
        "exp": int(expires_at.timestamp()),
        "jti": uuid.uuid4().hex,
    }
    if extra_claims:
        payload.update(extra_claims)

    token = jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)
    return token, expires_at


def decode_access_token(token: str, settings: Settings) -> dict[str, Any]:
    """Decode and verify a JWT. Raises `TokenError` on any failure."""
    try:
        return jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=[settings.jwt_algorithm],
            issuer=settings.jwt_issuer,
            options={"require": ["exp", "sub", "iss"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise TokenError("Access token has expired") from exc
    except jwt.InvalidTokenError as exc:
        raise TokenError("Access token is invalid") from exc
