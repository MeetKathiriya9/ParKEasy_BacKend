"""Pytest fixtures.

The health tests do not require a live MongoDB: the `app` fixture swaps the
database bootstrap functions for no-ops, so the suite runs offline. Tests that
genuinely need the database take the `mongo_available` fixture and skip
themselves when the server is unreachable.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.core.config import Settings, get_settings
from app.main import create_app


@pytest.fixture(scope="session")
def event_loop() -> Any:
    return asyncio.new_event_loop()


@pytest.fixture(scope="session")
def settings() -> Settings:
    return get_settings()


@pytest_asyncio.fixture
async def app(settings: Settings) -> AsyncIterator[Any]:
    """Application instance with the database bootstrap disabled."""
    from app.db import client as db_client
    from app.db import indexes as db_indexes

    real_connect, real_indexes = db_client.connect_to_mongo, db_indexes.ensure_indexes

    async def _no_connect(_: Settings) -> None:
        return None

    async def _no_indexes(_: Any) -> None:
        return None

    db_client.connect_to_mongo, db_indexes.ensure_indexes = _no_connect, _no_indexes
    try:
        yield create_app(settings)
    finally:
        db_client.connect_to_mongo, db_indexes.ensure_indexes = real_connect, real_indexes


@pytest_asyncio.fixture
async def client(app: Any) -> AsyncIterator[AsyncClient]:
    """HTTP client bound to the app, without running the real lifespan."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as http_client:
        yield http_client


@pytest_asyncio.fixture
async def mongo_available() -> AsyncIterator[bool]:
    """True when a real MongoDB answers a ping; otherwise the test skips."""
    from app.db.client import close_mongo_connection, connect_to_mongo, ping_database

    try:
        await connect_to_mongo(get_settings())
        reachable = await ping_database()
    except Exception:
        reachable = False
    else:
        await close_mongo_connection()

    if not reachable:
        pytest.skip("MongoDB is not reachable")
    yield True
