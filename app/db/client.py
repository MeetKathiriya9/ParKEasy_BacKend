"""PyMongo async client lifecycle.

`pymongo.AsyncMongoClient` is the current async driver. (`motor` is deprecated -
its async support was merged into PyMongo itself.)

The client is created once during application startup and closed on shutdown.
Collections are exposed as attributes: `db["parkingFacilities"]` or via
`get_collection("parkingFacilities")`.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

from fastapi import Depends
from pymongo import AsyncMongoClient
from pymongo.asynchronous.database import AsyncDatabase
from pymongo.errors import PyMongoError

from app.core.config import Settings, get_settings
from app.core.errors import AppError, ErrorCode

logger = logging.getLogger(__name__)

_client: AsyncMongoClient | None = None
_database: AsyncDatabase | None = None


class DatabaseUnavailableError(AppError):
    """Raised when MongoDB is unreachable, i.e. the API is running degraded."""

    code = ErrorCode.DATABASE_UNAVAILABLE
    status_code = 503


async def connect_to_mongo(settings: Settings) -> AsyncDatabase:
    """Open the client, verify connectivity, and cache the database handle."""
    global _client, _database

    if _client is not None:
        return _database

    kwargs: dict[str, Any] = {"serverSelectionTimeoutMS": 5000, "tz_aware": True}
    if settings.mongodb_tls:
        kwargs["tls"] = True

    client: AsyncMongoClient = AsyncMongoClient(settings.mongodb_uri, **kwargs)

    try:
        await client.admin.command("ping")
    except PyMongoError as exc:
        await client.close()
        raise DatabaseUnavailableError(
            f"Could not connect to MongoDB at {settings.mongodb_uri}"
        ) from exc

    _client = client
    _database = client[settings.mongodb_database]
    logger.info("Connected to MongoDB database '%s'", settings.mongodb_database)
    return _database


async def close_mongo_connection() -> None:
    """Close the client and clear the cached handles."""
    global _client, _database
    if _client is not None:
        await _client.close()
        logger.info("MongoDB connection closed")
    _client = None
    _database = None


def get_database() -> AsyncDatabase:
    """FastAPI dependency returning the active database handle."""
    if _database is None:
        raise DatabaseUnavailableError("Database is not initialised")
    return _database


async def get_collection(name: str) -> Any:
    """Return a collection handle, asserting the database is live."""
    db = get_database()
    return db[name]


DatabaseDep = Annotated[AsyncDatabase, Depends(get_database)]


async def ping_database() -> bool:
    """True when the server answers a ping command."""
    if _client is None:
        return False
    try:
        await _client.admin.command("ping")
    except PyMongoError:
        return False
    return True


def get_settings_dep() -> Settings:
    """Expose settings as a FastAPI dependency (convenience re-export)."""
    return get_settings()
