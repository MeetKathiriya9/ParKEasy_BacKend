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
    # True once a real router module implements the group; such groups are kept
    # here for documentation but no longer get a 501 stub.
    implemented: bool = False


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
        # register/login/logout/me live in app/api/v1/auth.py; password reset
        # is still outstanding.
        implemented=True,
    ),
    RouteGroupSpec(
        "users",
        "Profile, preferences and account status",
        ("GET /me", "PATCH /me"),
        # Profile read/update plus avatar upload live in app/api/v1/users.py;
        # the avatar files themselves are served by app/api/v1/avatars.py.
        implemented=True,
    ),
    RouteGroupSpec(
        "vehicles",
        "User vehicles and default vehicle",
        ("GET /", "POST /", "PATCH /{id}", "DELETE /{id}", "PATCH /{id}/default"),
        # CRUD plus the default-vehicle switch live in app/api/v1/vehicles.py.
        implemented=True,
    ),
    RouteGroupSpec(
        "facilities",
        "Parking facility management (operator/admin CRUD)",
        ("GET /", "POST /", "GET /{id}", "PATCH /{id}", "DELETE /{id}", "PATCH /{id}/status"),
        # CRUD plus the status transition live in app/api/v1/facilities.py;
        # driver-facing discovery is the separate `parking` group.
        implemented=True,
    ),
    RouteGroupSpec(
        "parking",
        "Parking discovery: nearby search, details, filters",
        (
            "GET /nearby",
            "GET /{id}",
            "GET /{id}/availability",
            "GET /{id}/spaces",
        ),
        # Nearby search, details, availability and the driver-scoped spaces
        # read live in app/api/v1/parking.py. DOC section 21 also lists
        # `GET /search`; it is not needed yet, because the client resolves a
        # free-text destination to coordinates and calls `/nearby`.
        implemented=True,
    ),
    RouteGroupSpec(
        "spaces",
        "Spaces, availability and status",
        (
            "GET /facilities/{id}/floors",
            "POST /facilities/{id}/floors",
            "PATCH /floors/{id}",
            "DELETE /floors/{id}",
            "POST /floors/{id}/zones",
            "PATCH /zones/{id}",
            "DELETE /zones/{id}",
            "PATCH /zones/{id}/status",
            "GET /zones/{id}/spaces",
            "POST /zones/{id}/spaces",
            "POST /zones/{id}/spaces/bulk",
            "GET /facilities/{id}/spaces",
            "GET /spaces/{id}",
            "PATCH /spaces/{id}",
            "DELETE /spaces/{id}",
            "PATCH /spaces/{id}/status",
        ),
        # The Facility -> Floor -> Zone -> Space tree lives in
        # app/api/v1/spaces.py; availability search is the `parking` group.
        implemented=True,
    ),
    RouteGroupSpec(
        "staff",
        "Staff accounts and facility assignments (operator/admin)",
        (
            "GET /",
            "POST /",
            "GET /{id}",
            "PATCH /{id}",
            "PATCH /{id}/status",
            "GET /{id}/assignments",
            "POST /{id}/assignments",
            "DELETE /{id}/assignments/{assignmentId}",
        ),
        # DOC section 21 has no staff group, but DOC sections 7, 10 and 11
        # require operators to create staff and assign them to facilities, so
        # this is a documented deviation (see the server README). The routes
        # live in app/api/v1/staff.py.
        implemented=True,
    ),
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
        # Booking, lookup, cancellation and QR check-in/out live in
        # app/api/v1/reservations.py. The engine is in
        # app/services/reservations.py.
        implemented=True,
    ),
    RouteGroupSpec(
        "sessions",
        "Active parking sessions",
        ("GET /active", "PATCH /{id}/extend", "POST /{id}/close"),
        # Listing, reading, extension and closing live in
        # app/api/v1/sessions.py; the pricing engine is in
        # app/services/pricing.py.
        implemented=True,
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
        # Rule CRUD, deletion and the preview live in
        # app/api/v1/pricing.py; the calculator is in app/services/pricing.py.
        implemented=True,
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
    """Return one stub router per planned route group that is not implemented yet."""
    return [_build_router(spec) for spec in ROUTE_GROUPS if not spec.implemented]


__all__ = ["ROUTE_GROUPS", "RouteGroupSpec", "build_stub_routers"]
