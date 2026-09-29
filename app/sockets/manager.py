"""Real-time layer (DOC section 26).

Uses native FastAPI WebSockets. Event names are kept identical to the ones the
requirements document specifies so the client-side contract does not change.

Event flow: state change -> service -> database update -> broadcast -> client
re-renders the affected facility card, map or dashboard.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from typing import Any

from fastapi import WebSocket

logger = logging.getLogger(__name__)


class Events:
    """Canonical event names from DOC section 26."""

    AVAILABILITY_CHANGED = "parking:availabilityChanged"
    RESERVATION_CREATED = "reservation:created"
    RESERVATION_CANCELLED = "reservation:cancelled"
    VEHICLE_ENTERED = "parking:vehicleEntered"
    VEHICLE_EXITED = "parking:vehicleExited"
    PAYMENT_COMPLETED = "payment:completed"
    VIOLATION_CREATED = "violation:created"
    CHARGER_STATUS_CHANGED = "charger:statusChanged"
    FACILITY_STATUS_CHANGED = "facility:statusChanged"


class ConnectionManager:
    """Tracks live WebSocket connections and broadcasts events to them.

    Clients are tracked in two buckets so the same code can serve a single
    facility's dashboard and a platform-wide admin view: `by_facility` receives
    an event only if it subscribes to that facility, while `broadcast_all`
    reaches every connection.
    """

    def __init__(self) -> None:
        self._connections: dict[WebSocket, set[str]] = defaultdict(set)
        self._by_facility: dict[str, set[WebSocket]] = defaultdict(set)

    async def connect(
        self,
        websocket: WebSocket,
        *,
        user_id: str | None = None,
        facility_ids: set[str] | None = None,
    ) -> None:
        await websocket.accept()
        self._connections[websocket] = set(facility_ids or set())
        for facility_id in facility_ids or set():
            self._by_facility[facility_id].add(websocket)
        logger.info(
            "WebSocket connected (user=%s, facilities=%d, total=%d)",
            user_id or "-",
            len(facility_ids or set()),
            len(self._connections),
        )

    def disconnect(self, websocket: WebSocket) -> None:
        for facility_id in self._connections.pop(websocket, set()):
            sockets = self._by_facility.get(facility_id)
            if sockets:
                sockets.discard(websocket)
                if not sockets:
                    self._by_facility.pop(facility_id, None)
        logger.info("WebSocket disconnected (total=%d)", len(self._connections))

    def subscribe(self, websocket: WebSocket, facility_id: str) -> None:
        """Subscribe an open connection to a facility's events."""
        self._connections[websocket].add(facility_id)
        self._by_facility[facility_id].add(websocket)

    def unsubscribe(self, websocket: WebSocket, facility_id: str) -> None:
        self._connections[websocket].discard(facility_id)
        sockets = self._by_facility.get(facility_id)
        if sockets:
            sockets.discard(websocket)
            if not sockets:
                self._by_facility.pop(facility_id, None)

    async def send_to_facility(self, facility_id: str, event: str, payload: Any) -> None:
        """Send an event to every connection subscribed to one facility."""
        await self._send_many(self._by_facility.get(facility_id, set()), event, payload)

    async def send_to_user(self, user_id: str, event: str, payload: Any) -> None:
        """Send a targeted event to one user (Phase 5: notifications)."""
        await self._send_many(
            [ws for ws in self._connections if getattr(ws, "scope", {}).get("user") == user_id],
            event,
            payload,
        )

    async def broadcast_all(self, event: str, payload: Any) -> None:
        """Send an event to every connected client (admin dashboards)."""
        await self._send_many(set(self._connections), event, payload)

    async def _send_many(self, sockets: set[WebSocket], event: str, payload: Any) -> None:
        dead: list[WebSocket] = []
        for websocket in sockets:
            try:
                await websocket.send_json({"event": event, "data": payload})
            except Exception:  # a dead socket must not break the broadcast loop
                dead.append(websocket)
        for websocket in dead:
            self.disconnect(websocket)

    @property
    def connection_count(self) -> int:
        return len(self._connections)

    @property
    def facility_count(self) -> int:
        return len(self._by_facility)


# Process-wide manager. A multi-worker deployment would need a Redis pub/sub
# bridge here so broadcasts reach every worker (DOC section 25, Redis row).
manager = ConnectionManager()
