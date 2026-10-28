"""Aggregates every v1 router under the `/api/v1` prefix.

Real routers are included here as each domain lands. Phase 1 ships the health
probes, authentication, and the remaining DOC section 21 stubs.
"""

from fastapi import APIRouter

from app.api.v1 import (
    auth,
    avatars,
    facilities,
    health,
    parking,
    reservations,
    sessions,
    spaces,
    staff,
    users,
    vehicles,
)
from app.api.v1.stubs import build_stub_routers

api_router = APIRouter()

# Implemented in Phase 1.
api_router.include_router(health.router)
api_router.include_router(auth.router)
api_router.include_router(users.router)
api_router.include_router(vehicles.router)
api_router.include_router(facilities.router)
# Floors/zones/spaces hang off `/facilities`, `/floors`, `/zones` and `/spaces`.
api_router.include_router(spaces.router)
# Operator/admin management of staff accounts and facility assignments.
api_router.include_router(staff.router)
# Driver-facing discovery: nearby search, details, availability, spaces.
api_router.include_router(parking.router)
# Booking engine and staff operations over it.
api_router.include_router(reservations.router)
api_router.include_router(sessions.router)
# Avatar files are served from their own prefix so the URL is short and stable.
api_router.include_router(avatars.router)

# Remaining DOC section 21 route groups, each currently a 501 stub.
for stub_router in build_stub_routers():
    api_router.include_router(stub_router)

__all__ = ["api_router"]
