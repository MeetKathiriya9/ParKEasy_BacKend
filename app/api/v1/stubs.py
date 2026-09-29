"""Phase 1 route-group stubs.

Every route group from DOC section 21 is mounted and documented now, so the API
surface is explicit from day one and the client can be wired group by group.
Each stub returns `501 Not Implemented` until its domain is built; the planned
operations are listed in the OpenAPI description.

Replace a group by deleting its entry here and adding a real router module that
`app/api/v1/router.py` includes instead.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from fastapi import APIRouter, status

from app.core.errors import NotImplementedYetError
from app.schemas.common import ErrorResponse

# Response models shared by every stub.
_STUB_RESPONSES: dict[int | str, dict[str, type]] = {
    status.HTTP_501_NOT_IMPLEMENTED: {"model": ErrorResponse, "description": "Not implemented yet"},
}


@dataclass(frozen=True, slots=True)
class RouteGroupSpec:
    """One planned route group and the operations DOC section 21 assigns to it."""

    name: str
    summary: str
    operations: tuple[str, ...] = field(default_factory=tuple)


# DOC section 21 - "Example API Structure", in document order.
ROUTE_GROUPS: tuple[RouteGroupSpec, ...] = (
    RouteGroupSpec(
        "auth",
        "Authentication and session management",
        (
            "POST /register",
            "POST /login",
            "POST /logout",
            "GET /me",
            "POST /forgot-password",
            "POST /reset-password",
        ),
    ),
    RouteGroupSpec("users", "Profile, preferences and account status", ("GET /me", "PATCH /me")),
    RouteGroupSpec(
        "vehicles",
        "User vehicles and default vehicle",
        ("GET /", "POST /", "PATCH /{id}", "DELETE /{id}", "PATCH /{id}/default"),
    ),
    RouteGroupSpec(
        "parking",
        "Parking discovery: nearby search, details, filters",
        ("GET /nearby", "GET /search", "GET /{id}", "GET /{id}/availability"),
    ),
    RouteGroupSpec("spaces", "Spaces, availability and status", ("GET /", "PATCH /{id}/status")),
    RouteGroupSpec(
        "reservations",
        "Reservation lifecycle",
        (
            "POST /",
            "GET /",
            "GET /{id}",
            "PATCH /{id}/cancel",
            "POST /{id}/check-in",
            "POST /{id}/check-out",
        ),
    ),
    RouteGroupSpec(
        "sessions",
        "Active parking sessions",
        ("GET /active", "PATCH /{id}/extend", "POST /{id}/close"),
    ),
    RouteGroupSpec(
        "payments",
        "Payment, receipt and refund workflow",
        ("POST /", "GET /", "POST /{id}/verify", "GET /{id}/receipt", "POST /{id}/refund"),
    ),
    RouteGroupSpec(
        "pricing",
        "Pricing rules and rate preview",
        ("GET /rules", "POST /rules", "PATCH /rules/{id}", "POST /preview"),
    ),
    RouteGroupSpec(
        "violations",
        "Violations, evidence and fines",
        ("POST /", "GET /", "POST /{id}/resolve"),
    ),
    RouteGroupSpec(
        "ev",
        "EV chargers, reservations and charging sessions",
        ("GET /chargers", "GET /sessions"),
    ),
    RouteGroupSpec(
        "notifications",
        "In-app notifications and preferences",
        ("GET /", "PATCH /{id}/read"),
    ),
    RouteGroupSpec("reviews", "Ratings and category reviews", ("GET /", "POST /")),
    RouteGroupSpec(
        "complaints",
        "Support case workflow",
        ("POST /", "GET /", "PATCH /{id}/assign", "PATCH /{id}/resolve", "PATCH /{id}/escalate"),
    ),
    RouteGroupSpec(
        "analytics",
        "Occupancy, revenue, peak hours, utilization",
        ("GET /occupancy", "GET /revenue", "GET /peak-hours", "GET /utilization"),
    ),
    RouteGroupSpec(
        "recommendations",
        "Ranked parking recommendations (DOC section 15)",
        ("GET /parking",),
    ),
    RouteGroupSpec(
        "predictions",
        "Future occupancy and availability forecasts",
        ("GET /availability",),
    ),
    RouteGroupSpec(
        "events",
        "Events, temporary parking and demand settings",
        ("GET /", "POST /", "PATCH /{id}"),
    ),
    RouteGroupSpec(
        "admin",
        "Platform management and configuration",
        ("GET /metrics", "GET /config", "PATCH /config"),
    ),
)


def _build_router(spec: RouteGroupSpec) -> APIRouter:
    """Create a router exposing `GET /<group>` as a 501 placeholder."""
    router = APIRouter(prefix=f"/{spec.name}", tags=[spec.name])
    planned = "\n".join(f"- `{op}`" for op in spec.operations)

    @router.get(
        "",
        summary=f"{spec.summary} (stub)",
        description=(
            f"**Not implemented yet.**\n\nPlanned operations for this group:\n\n{planned}\n\n"
            "Source: ParkEasy requirements document, section 21."
        ),
        responses=_STUB_RESPONSES,
    )
    async def _stub() -> None:
        raise NotImplementedYetError(
            f"The '{spec.name}' API is planned but not implemented in Phase 1",
            details={"group": spec.name, "planned_operations": list(spec.operations)},
        )

    return router


def build_stub_routers() -> list[APIRouter]:
    """Return one stub router per planned route group."""
    return [_build_router(spec) for spec in ROUTE_GROUPS]


__all__ = ["ROUTE_GROUPS", "RouteGroupSpec", "build_stub_routers"]
