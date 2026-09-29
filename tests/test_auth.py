"""Tests for the authentication and authorization dependencies (DOC section 31).

A throwaway router is mounted on the app fixture so the dependencies can be
exercised over HTTP without depending on any real business route.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated, Any

import pytest
import pytest_asyncio
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient

from app.api.deps import CurrentUser, get_current_user, require_roles
from app.core.config import Settings
from app.core.security import create_access_token
from app.models.enums import Role


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _build_probe_app(app: FastAPI) -> FastAPI:
    """Attach a temporary `/__probe` router exercising the auth dependencies."""

    @app.get("/__probe/any", tags=["probe"])
    async def _any(user: Annotated[CurrentUser, Depends(get_current_user)]) -> dict[str, Any]:
        return {"id": user.id, "role": user.role.value, "facilities": sorted(user.facility_ids)}

    @app.get("/__probe/admin", tags=["probe"])
    async def _admin(
        user: Annotated[CurrentUser, Depends(require_roles(Role.ADMIN))],
    ) -> dict[str, Any]:
        return {"id": user.id, "role": user.role.value}

    @app.get("/__probe/ops", tags=["probe"])
    async def _ops(
        user: Annotated[CurrentUser, Depends(require_roles(Role.ADMIN, Role.OPERATOR))],
    ) -> dict[str, Any]:
        return {"id": user.id, "role": user.role.value}

    return app


@pytest_asyncio.fixture
async def probe_client(app: Any) -> AsyncIterator[AsyncClient]:
    _build_probe_app(app)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as http_client:
        yield http_client


def _token(settings: Settings, role: Role, **claims: Any) -> str:
    token, _ = create_access_token(
        subject="u-test", role=role, settings=settings, extra_claims=claims
    )
    return token


# --- Missing / malformed credentials ---------------------------------------


@pytest.mark.asyncio
async def test_missing_header_is_401(probe_client: AsyncClient) -> None:
    response = await probe_client.get("/__probe/any")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "UNAUTHORIZED"


@pytest.mark.asyncio
async def test_garbage_token_is_401(probe_client: AsyncClient) -> None:
    response = await probe_client.get("/__probe/any", headers=_auth("nope"))
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "INVALID_TOKEN"


@pytest.mark.asyncio
async def test_non_bearer_scheme_is_401(probe_client: AsyncClient) -> None:
    response = await probe_client.get("/__probe/any", headers={"Authorization": "Basic abc123"})
    assert response.status_code == 401


# --- Valid tokens -----------------------------------------------------------


@pytest.mark.asyncio
async def test_valid_driver_token_is_accepted(
    probe_client: AsyncClient, settings: Settings
) -> None:
    token = _token(settings, Role.DRIVER, email="driver@parkeasy.test")
    response = await probe_client.get("/__probe/any", headers=_auth(token))
    assert response.status_code == 200
    assert response.json() == {"id": "u-test", "role": "driver", "facilities": []}


@pytest.mark.asyncio
async def test_facility_ids_claim_is_exposed(
    probe_client: AsyncClient, settings: Settings
) -> None:
    token = _token(settings, Role.STAFF, facilityIds=["f1", "f5"])
    response = await probe_client.get("/__probe/any", headers=_auth(token))
    assert response.json()["facilities"] == ["f1", "f5"]


@pytest.mark.asyncio
async def test_expired_token_is_401(probe_client: AsyncClient, settings: Settings) -> None:
    from datetime import UTC, datetime, timedelta

    import jwt

    now = datetime.now(UTC)
    expired = jwt.encode(
        {
            "sub": "u-test",
            "role": "driver",
            "iss": settings.jwt_issuer,
            "iat": int((now - timedelta(hours=2)).timestamp()),
            "exp": int((now - timedelta(hours=1)).timestamp()),
        },
        settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
    )
    response = await probe_client.get("/__probe/any", headers=_auth(expired))
    assert response.status_code == 401
    assert "expired" in response.json()["error"]["message"].lower()


@pytest.mark.asyncio
async def test_token_with_unknown_role_is_401(
    probe_client: AsyncClient, settings: Settings
) -> None:
    # Correctly signed, but carrying a role the server does not recognise.
    token, _ = create_access_token(
        subject="u-test",
        role="superuser",  # type: ignore[arg-type]
        settings=settings,
    )
    response = await probe_client.get("/__probe/any", headers=_auth(token))
    assert response.status_code == 401
    assert "superuser" in response.json()["error"]["message"]


# --- Role-based access control ---------------------------------------------


@pytest.mark.asyncio
async def test_admin_route_allows_admin(probe_client: AsyncClient, settings: Settings) -> None:
    token = _token(settings, Role.ADMIN)
    response = await probe_client.get("/__probe/admin", headers=_auth(token))
    assert response.status_code == 200
    assert response.json()["role"] == "admin"


@pytest.mark.asyncio
async def test_admin_route_rejects_operator(
    probe_client: AsyncClient, settings: Settings
) -> None:
    token = _token(settings, Role.OPERATOR)
    response = await probe_client.get("/__probe/admin", headers=_auth(token))
    assert response.status_code == 403
    body = response.json()["error"]
    assert body["code"] == "FORBIDDEN"
    assert body["details"]["required_roles"] == ["admin"]


@pytest.mark.asyncio
async def test_multi_role_route_allows_operator(
    probe_client: AsyncClient, settings: Settings
) -> None:
    token = _token(settings, Role.OPERATOR)
    response = await probe_client.get("/__probe/ops", headers=_auth(token))
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_multi_role_route_rejects_driver(
    probe_client: AsyncClient, settings: Settings
) -> None:
    token = _token(settings, Role.DRIVER)
    response = await probe_client.get("/__probe/ops", headers=_auth(token))
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_role_gate_requires_a_token(probe_client: AsyncClient) -> None:
    response = await probe_client.get("/__probe/admin")
    assert response.status_code == 401


# --- Facility-level scope ---------------------------------------------------


def test_facility_access_rules() -> None:
    driver = CurrentUser(id="u1", role=Role.DRIVER)
    staff = CurrentUser(id="u2", role=Role.STAFF, facility_ids=frozenset({"f1"}))
    operator = CurrentUser(id="u3", role=Role.OPERATOR)
    admin = CurrentUser(id="u4", role=Role.ADMIN)

    assert not driver.has_facility_access("f1")
    assert staff.has_facility_access("f1")
    assert not staff.has_facility_access("f2")
    # Operators and admins are not restricted to assigned facilities.
    assert operator.has_facility_access("anything")
    assert admin.has_facility_access("anything")
