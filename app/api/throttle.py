"""Shared request throttling used by every sensitive endpoint.

DOC section 31 asks for rate limits on sensitive routes; the limits themselves
live in `Settings` so operators can tune them without a redeploy. Extracted from
the auth router because profile updates and photo uploads need exactly the
same behaviour, and two copies would inevitably drift apart.
"""

from __future__ import annotations

import logging

from fastapi import Request, status

from app.core.errors import AppError, ErrorCode
from app.core.rate_limit import RateLimit, rate_limiter

logger = logging.getLogger(__name__)


def client_ip(request: Request) -> str:
    """Best-effort client address used as a rate-limit key.

    `X-Forwarded-For` is only trusted because the API is expected to sit behind
    a reverse proxy in deployment. Exposed directly, that header is
    client-controlled and a determined attacker can forge it -- see README.md.
    """
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def throttle(request: Request, *, bucket: str, identity: str, rule: RateLimit) -> None:
    """Raise `429` when this caller has exceeded `rule`.

    Keyed on IP *and* identity so one attacker cannot lock out an entire office
    NAT, and a single account cannot be brute-forced from many addresses.
    """
    key = f"{bucket}:{client_ip(request)}:{identity.lower()}"
    allowed, _remaining, retry_after = rate_limiter.check(key, rule)
    if not allowed:
        logger.warning("Rate limit hit on '%s' for %s", bucket, identity)
        raise AppError(
            "Too many attempts. Please try again later.",
            code=ErrorCode.RATE_LIMITED,
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            details={"retry_after_seconds": retry_after},
        )


__all__ = ["RateLimit", "client_ip", "throttle"]
