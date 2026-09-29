"""Tests for the Phase 1 foundation: routing, envelopes, and configuration."""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from app.core.config import Settings
from app.core.security import (
    create_access_token,
    decode_access_token,
    hash_password,
    verify_password,
)
from app.models.enums import FacilityStatus, ReservationStatus, Role, SpaceStatus

STUB_GROUPS = [
    "auth",
    "users",
    "vehicles",
    "parking",
    "spaces",
    "reservations",
    "sessions",
    "payments",
    "pricing",
    "violations",
    "ev",
    "notifications",
    "reviews",
    "complaints",
    "analytics",
    "recommendations",
    "predictions",
    "events",
    "admin",
]


# --- Health -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_root_returns_service_banner(client: AsyncClient) -> None:
    response = await client.get("/")
    assert response.status_code == 200
    body = response.json()
    assert body["service"] == "ParkEasy API"
    assert body["api"] == "/api/v1"


@pytest.mark.asyncio
async def test_health_is_ok(client: AsyncClient) -> None:
    response = await client.get("/api/v1/health")
    assert response.status_code == 200
    assert response.json() == {"message": "ok", "ok": True}


@pytest.mark.asyncio
async def test_health_db_reports_degraded_without_database(client: AsyncClient) -> None:
    response = await client.get("/api/v1/health/db")
    assert response.status_code == 200
    body = response.json()
    assert body["database"] == "parkeasy"
    # The app fixture never opens a real connection.
    assert body["mongodb"] == "unreachable"


# --- DOC section 21 stubs ---------------------------------------------------


@pytest.mark.parametrize("group", STUB_GROUPS)
@pytest.mark.asyncio
async def test_route_group_stub_returns_501(client: AsyncClient, group: str) -> None:
    response = await client.get(f"/api/v1/{group}")
    assert response.status_code == 501
    error = response.json()["error"]
    assert error["code"] == "NOT_IMPLEMENTED"
    assert error["details"]["group"] == group
    assert error["details"]["planned_operations"]


def test_all_doc_route_groups_are_mounted() -> None:
    from app.api.v1.stubs import ROUTE_GROUPS

    assert [spec.name for spec in ROUTE_GROUPS] == STUB_GROUPS


# --- OpenAPI ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_openapi_documents_every_group(client: AsyncClient) -> None:
    response = await client.get("/openapi.json")
    assert response.status_code == 200
    tags = {tag["name"] for tag in response.json()["tags"]}
    assert set(STUB_GROUPS).issubset(tags)
    assert {"health", "realtime"}.issubset(tags)


# --- Errors -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_unknown_path_uses_error_envelope(client: AsyncClient) -> None:
    response = await client.get("/api/v1/does-not-exist")
    assert response.status_code == 404
    assert "error" in response.json()
    assert set(response.json()["error"]) == {"code", "message", "details"}


# --- Security primitives ---------------------------------------------------


def test_password_hash_round_trip(settings: Settings) -> None:
    password = "correct horse battery staple"
    hashed = hash_password(password)

    assert hashed != password
    assert hashed.startswith("$2")
    assert verify_password(password, hashed)
    assert not verify_password("wrong password", hashed)


def test_password_hash_is_salted(settings: Settings) -> None:
    assert hash_password("same") != hash_password("same")


def test_verify_password_handles_garbage_hash(settings: Settings) -> None:
    assert verify_password("anything", "not-a-bcrypt-hash") is False


def test_access_token_round_trip(settings: Settings) -> None:
    token, expires_at = create_access_token(subject="u1", role=Role.DRIVER, settings=settings)
    payload = decode_access_token(token, settings)

    assert payload["sub"] == "u1"
    assert payload["role"] == "driver"
    assert payload["iss"] == settings.jwt_issuer
    assert expires_at is not None


def test_tampered_token_is_rejected(settings: Settings) -> None:
    from app.core.errors import UnauthorizedError

    token, _ = create_access_token(subject="u1", role=Role.DRIVER, settings=settings)
    with pytest.raises(UnauthorizedError):
        decode_access_token(f"{token}x", settings)


def test_token_signed_with_other_secret_is_rejected(settings: Settings) -> None:
    from app.core.errors import UnauthorizedError

    long_secret = "a-completely-different-secret-of-sufficient-length"
    other = settings.model_copy(update={"jwt_secret": long_secret})
    token, _ = create_access_token(subject="u1", role=Role.ADMIN, settings=other)
    with pytest.raises(UnauthorizedError):
        decode_access_token(token, settings)


# --- Configuration ----------------------------------------------------------


def test_cors_origins_accept_comma_separated_env() -> None:
    configured = Settings(cors_origins="http://a.test, http://b.test")
    assert configured.cors_origins == ["http://a.test", "http://b.test"]


@pytest.mark.parametrize(
    ("uri", "expected"),
    [
        ("mongodb://localhost:27017", "localhost:27017"),
        ("mongodb://localhost:27017/parkeasy", "localhost:27017"),
        ("mongodb+srv://user:pw@cluster0.abcde.mongodb.net", "cluster0.abcde.mongodb.net"),
        ("mongodb://10.0.0.5:27017", "10.0.0.5:27017"),
    ],
)
def test_server_label_strips_scheme_and_credentials(uri: str, expected: str) -> None:
    from app.api.v1.health import _server_label

    assert _server_label(uri) == expected


def test_jwt_expire_seconds(settings: Settings) -> None:
    assert settings.jwt_expire_seconds == settings.jwt_expires_minutes * 60


# --- Enums mirror DOC section 17 -------------------------------------------


def test_doc_states_match_requirements() -> None:
    assert [s.value for s in FacilityStatus] == [
        "ACTIVE",
        "INACTIVE",
        "TEMPORARILY_CLOSED",
        "MAINTENANCE",
    ]
    assert [s.value for s in SpaceStatus] == [
        "AVAILABLE",
        "RESERVED",
        "OCCUPIED",
        "MAINTENANCE",
        "BLOCKED",
    ]
    assert {s.value for s in ReservationStatus} == {
        "PENDING",
        "CONFIRMED",
        "ARRIVED",
        "ACTIVE",
        "COMPLETED",
        "CANCELLED",
        "EXPIRED",
        "NO_SHOW",
    }


def test_roles_match_client_navigation() -> None:
    assert {r.value for r in Role} == {"driver", "staff", "operator", "admin"}


# --- Utilities --------------------------------------------------------------


def test_serialize_converts_mongo_types() -> None:
    from bson import ObjectId

    from app.utils.mongo import serialize

    oid = ObjectId()
    result = serialize({"_id": oid, "nested": {"ref": oid}, "list": [oid]})

    assert result["id"] == str(oid)
    assert result["nested"]["ref"] == str(oid)
    assert result["list"] == [str(oid)]
    assert "_id" not in result


def test_serialize_formats_datetimes_as_utc() -> None:
    from datetime import UTC, datetime

    from app.utils.mongo import serialize

    stamp = datetime(2026, 9, 28, 10, 0, tzinfo=UTC)
    assert serialize({"createdAt": stamp})["createdAt"] == "2026-09-28T10:00:00Z"


def test_to_object_id_rejects_garbage() -> None:
    from app.utils.mongo import to_object_id

    assert to_object_id("not-an-id") is None
    assert to_object_id("507f1f77bcf86cd799439011") is not None
