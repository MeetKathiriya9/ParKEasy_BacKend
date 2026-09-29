"""ParkEasy API - application entry point.

Run in development:

    .venv\\Scripts\\python -m app.main

or via run.py. The factory `create_app()` is what Uvicorn should target:

    uvicorn app.main:app --port 9999 --reload
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware

from app.api.v1.router import api_router
from app.core.config import Settings, get_settings
from app.core.errors import register_exception_handlers
from app.core.logging import configure_logging
from app.db.client import close_mongo_connection, connect_to_mongo
from app.db.indexes import ensure_indexes
from app.sockets.manager import manager

logger = logging.getLogger(__name__)

DESCRIPTION = """
Backend for **ParkEasy Web** - intelligent parking discovery, reservation and
parking operations.

Built from the *ParkEasy Web Client Requirement and Project Plan* v1.0
(sections 7, 17, 18, 19, 21, 24, 26).

**Phase 1 (current):** foundation only - health probes, MongoDB connectivity
and indexes, error envelope, CORS, and the documented route-group surface.
Business endpoints return `501` until their phase lands.

**Auth:** `Authorization: Bearer <access token>` on protected routes.
"""

TAGS_METADATA = [
    {"name": "health", "description": "Liveness and readiness probes."},
    {"name": "auth", "description": "Registration, login, tokens, password reset."},
    {"name": "users", "description": "Profile, preferences, account status."},
    {"name": "vehicles", "description": "User vehicles and default vehicle."},
    {"name": "parking", "description": "Nearby search, facility details, availability."},
    {"name": "spaces", "description": "Individual spaces, zones and status."},
    {"name": "reservations", "description": "Booking lifecycle and QR entry/exit."},
    {"name": "sessions", "description": "Active parking sessions, extend and close."},
    {"name": "payments", "description": "Billing, receipts and refunds."},
    {"name": "pricing", "description": "Rate configuration and rate preview."},
    {"name": "violations", "description": "Violations, evidence and fines."},
    {"name": "ev", "description": "EV chargers and charging sessions."},
    {"name": "notifications", "description": "In-app and email notifications."},
    {"name": "reviews", "description": "Ratings and category reviews."},
    {"name": "complaints", "description": "Support cases and SLA workflow."},
    {"name": "analytics", "description": "Occupancy, revenue, peak hours, utilization."},
    {"name": "recommendations", "description": "Ranked parking recommendations."},
    {"name": "predictions", "description": "Future occupancy forecasts."},
    {"name": "events", "description": "Event and temporary parking."},
    {"name": "admin", "description": "Platform management and configuration."},
    {"name": "realtime", "description": "Live occupancy WebSocket channel."},
]


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Open the database on startup, ensure indexes, tear down cleanly."""
    settings = get_settings()
    configure_logging(settings.log_level)

    logger.info("Starting %s v%s (%s)", settings.app_name, settings.version, settings.environment)
    try:
        app.state.db = await connect_to_mongo(settings)
        await ensure_indexes(app.state.db)
    except Exception:
        # Stay up even without a database so /health still reports liveness and
        # /health/db reports the degraded state; routes needing Mongo will 503.
        logger.exception("Database bootstrap failed - API will run in degraded mode")

    try:
        yield
    finally:
        await close_mongo_connection()
        logger.info("Shutdown complete")


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build and configure the FastAPI application."""
    settings = settings or get_settings()

    app = FastAPI(
        title=settings.app_name,
        version=settings.version,
        description=DESCRIPTION,
        openapi_tags=TAGS_METADATA,
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
    )

    # Health probes must answer even if the database is down.
    app.state.db = None

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["X-Request-ID"],
    )
    app.add_middleware(GZipMiddleware, minimum_size=1000)

    register_exception_handlers(app)
    _include_realtime_routes(app)

    app.include_router(api_router, prefix="/api/v1")

    @app.get("/", tags=["health"], summary="Service banner")
    async def root() -> dict[str, str]:
        return {
            "service": settings.app_name,
            "version": settings.version,
            "docs": "/docs",
            "api": "/api/v1",
            "websocket": "/ws",
        }

    return app


def _include_realtime_routes(app: FastAPI) -> None:
    """Mount the real-time channel described in DOC section 26."""

    @app.get("/ws/health", tags=["realtime"], summary="WebSocket channel status")
    async def websocket_health() -> dict[str, int | str]:
        return {"status": "ok", "connections": manager.connection_count, "channel": "/ws"}

    @app.websocket("/ws")
    async def realtime(websocket: WebSocket) -> None:
        """Live occupancy channel.

        `?facilityIds=f1,f2` narrows the subscription to specific facilities.
        Authentication arrives as `?token=<access token>`; Phase 1 accepts an
        unauthenticated connection and Phase 5 will enforce the token here.
        """
        facility_ids = {
            value
            for value in websocket.query_params.get("facilityIds", "").split(",")
            if value
        }
        user_id = websocket.query_params.get("userId")
        await manager.connect(websocket, user_id=user_id, facility_ids=facility_ids)
        try:
            while True:
                # Receives client `subscribe` / `unsubscribe` messages and
                # heartbeat pings; ignored content keeps the loop simple.
                message = await websocket.receive_text()
                if message == "ping":
                    await websocket.send_json({"event": "pong", "data": {}})
        except WebSocketDisconnect:
            manager.disconnect(websocket)


app = create_app()


if __name__ == "__main__":
    import uvicorn

    _settings = get_settings()
    uvicorn.run(
        "app.main:app",
        host=_settings.host,
        port=_settings.port,
        reload=_settings.reload,
    )
