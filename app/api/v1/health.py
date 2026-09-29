"""Health and readiness endpoints."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

from fastapi import APIRouter

from app.core.config import Settings, get_settings
from app.db.client import ping_database
from app.schemas.common import MessageResponse

router = APIRouter(tags=["health"])


def _server_label(uri: str) -> str:
    """Host[:port] from a connection URI, with scheme and any credentials stripped."""
    parts = urlsplit(uri)
    host = parts.hostname
    if not host:
        # Fall back to the raw value rather than guessing at a malformed URI.
        return uri
    if parts.port:
        return f"{host}:{parts.port}"
    return host


@router.get("/health", summary="Liveness probe", response_model=MessageResponse)
async def health() -> MessageResponse:
    """Process is up. Deliberately does not touch MongoDB."""
    return MessageResponse(message="ok")


@router.get("/health/db", summary="Readiness probe", response_model=dict[str, Any])
async def health_db(settings: Settings = get_settings()) -> dict[str, Any]:
    """Round-trips a real `ping` against MongoDB and reports the target."""
    reachable = await ping_database()
    return {
        "status": "ok" if reachable else "degraded",
        "database": settings.mongodb_database,
        "server": _server_label(settings.mongodb_uri),
        "mongodb": "connected" if reachable else "unreachable",
    }
