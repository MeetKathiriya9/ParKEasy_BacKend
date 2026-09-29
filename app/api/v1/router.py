"""Aggregates every v1 router under the `/api/v1` prefix.

Real routers are included here as each domain lands; for Phase 1 the only
implemented endpoints are the health probes plus the DOC section 21 stubs.
"""

from fastapi import APIRouter

from app.api.v1 import health
from app.api.v1.stubs import build_stub_routers

api_router = APIRouter()

# Implemented in Phase 1.
api_router.include_router(health.router)

# DOC section 21 route groups, each currently a 501 stub.
for stub_router in build_stub_routers():
    api_router.include_router(stub_router)

__all__ = ["api_router"]
