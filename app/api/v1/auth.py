"""Phase 1 authentication routes (DOC section 12).

Seven endpoints, all under `/api/v1/auth`:

    POST /register         create an account and sign in
    POST /login            exchange credentials for an access token
    GET  /me               the current user from the Bearer token
    POST /logout           revoke the presented access token
    POST /forgot-password  email a single-use reset link
    POST /reset-password   redeem that link and set a new password
    POST /change-password  rotate the password of a signed-in user
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response, status
from pymongo.asynchronous.database import AsyncDatabase

from app.api.deps import CurrentUserDep
from app.core.config import Settings, get_settings
from app.core.errors import AppError, ErrorCode, UnauthorizedError
from app.core.rate_limit import RateLimit, rate_limiter
from app.core.security import decode_access_token
from app.db.client import get_database
from app.models.enums import Role
from app.models.user import public_user
from app.schemas.auth import (
    AUTH_ERROR_RESPONSES,
    PASSWORD_RESET_ERROR_RESPONSES,
    ChangePasswordRequest,
    ForgotPasswordRequest,
    ForgotPasswordResponse,
    LoginRequest,
    PasswordChangedResponse,
    RegisterRequest,
    RegisterResponse,
    ResetPasswordRequest,
    TokenResponse,
    UserResponse,
)
from app.schemas.common import ErrorResponse, MessageResponse
from app.services import audit
from app.services import email as email_service
from app.services.auth import (
    authenticate_user,
    change_password,
    consume_password_reset,
    create_password_reset,
    issue_token_for_user,
    register_user,
    revoke_token,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])

AUTHENTICATED_ERRORS: dict[int | str, dict[str, type]] = {
    401: {"model": ErrorResponse, "description": "Missing, invalid or revoked token"},
}


def _client_ip(request: Request) -> str:
    """Best-effort client address used as a rate-limit key.

    `X-Forwarded-For` is only trusted because the API is expected to sit behind
    a reverse proxy in deployment. Exposed directly, that header is
    client-controlled and a determined attacker can forge it -- see README.md.
    """
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _throttle(request: Request, *, bucket: str, identity: str, rule: RateLimit) -> None:
    """Raise `429` when this caller has exceeded `rule`.

    Keyed on IP *and* identity so one attacker cannot lock out an entire office
    NAT, and a single account cannot be brute-forced from many addresses.
    """
    key = f"{bucket}:{_client_ip(request)}:{identity.lower()}"
    allowed, _remaining, retry_after = rate_limiter.check(key, rule)
    if not allowed:
        logger.warning("Rate limit hit on '%s' for %s", bucket, identity)
        raise AppError(
            "Too many attempts. Please try again later.",
            code=ErrorCode.RATE_LIMITED,
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            details={"retry_after_seconds": retry_after},
        )


def _to_user_response(document: dict) -> UserResponse:
    return UserResponse.model_validate(public_user(document))


@router.post(
    "/register",
    response_model=RegisterResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Register a new account",
    description=(
        "Creates a user and returns a signed access token so the client can go "
        "straight to the dashboard. Which roles may self-register is controlled "
        "by the `SELF_REGISTER_ROLES` setting."
    ),
    responses=AUTH_ERROR_RESPONSES,
)
async def register(
    payload: RegisterRequest,
    request: Request,
    response: Response,
    db: Annotated[AsyncDatabase, Depends(get_database)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> RegisterResponse:
    _throttle(
        request,
        bucket="register",
        identity=payload.email,
        rule=RateLimit(
            limit=settings.register_rate_limit,
            window_seconds=settings.register_rate_window_seconds,
        ),
    )

    document = await register_user(
        db,
        name=payload.name,
        email=payload.email,
        password=payload.password,
        phone=payload.phone,
        role=payload.role,
        settings=settings,
    )

    token, expires_in = issue_token_for_user(document, settings)
    response.headers["Cache-Control"] = "no-store"
    logger.info("Registered %s as %s", payload.email, payload.role)

    return RegisterResponse(
        accessToken=token,
        expiresIn=expires_in,
        role=Role(document["role"]),
        user=_to_user_response(document),
    )


@router.post(
    "/login",
    response_model=TokenResponse,
    summary="Log in and receive an access token",
    responses=AUTH_ERROR_RESPONSES,
)
async def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    db: Annotated[AsyncDatabase, Depends(get_database)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> TokenResponse:
    _throttle(
        request,
        bucket="login",
        identity=payload.email,
        rule=RateLimit(
            limit=settings.login_rate_limit,
            window_seconds=settings.login_rate_window_seconds,
        ),
    )

    document = await authenticate_user(db, email=payload.email, password=payload.password)
    token, expires_in = issue_token_for_user(document, settings)
    response.headers["Cache-Control"] = "no-store"
    logger.info("Login OK for %s (%s)", payload.email, document["role"])

    return TokenResponse(
        accessToken=token,
        expiresIn=expires_in,
        role=Role(document["role"]),
        user=_to_user_response(document),
    )


@router.get(
    "/me",
    response_model=UserResponse,
    summary="Current user from the access token",
    responses=AUTHENTICATED_ERRORS,
)
async def read_current_user(user: CurrentUserDep) -> UserResponse:
    """Return the principal decoded from the Bearer token.

    Built from the token claims rather than a fresh database read, so it is
    cheap; the trade-off is that a role change lands on the next login.
    """
    return UserResponse(
        id=user.id,
        name=user.name or user.email or user.id,
        email=user.email or "",
        role=user.role,
        status=user.status,
        facilityIds=sorted(user.facility_ids),
    )


@router.post(
    "/logout",
    response_model=MessageResponse,
    summary="Revoke the presented access token",
    responses=AUTHENTICATED_ERRORS,
)
async def logout(
    request: Request,
    user: CurrentUserDep,
    db: Annotated[AsyncDatabase, Depends(get_database)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> MessageResponse:
    """Deny-list this token so it stops working immediately, not at expiry."""
    header = request.headers.get("authorization", "")
    scheme, _, credentials = header.partition(" ")
    if scheme.lower() != "bearer" or not credentials:
        raise UnauthorizedError("Authorization header with a Bearer token is required")

    payload = decode_access_token(credentials, settings)
    jti = payload.get("jti")
    if jti:
        await revoke_token(
            db,
            jti=str(jti),
            user_id=user.id,
            expires_at=int(payload.get("exp", 0)),
        )
        logger.info("Logout: revoked token for %s", user.email or user.id)

    return MessageResponse(message="Signed out")


_RESET_SUBJECT = "ParkEasy password reset"
_RESET_BODY = (
    "We received a request to reset the password for your ParkEasy account.\n\n"
    "Open this link to choose a new password:\n"
    "{link}\n\n"
    "The link expires in {minutes} minutes and can only be used once.\n\n"
    "If you did not request this, you can safely ignore this email -- your "
    "current password will keep working."
)


@router.post(
    "/forgot-password",
    response_model=ForgotPasswordResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Email a password reset link",
    description=(
        "Always returns the same body whether or not the address is registered, "
        "so this endpoint cannot be used to discover which emails have accounts. "
        "Rate limited per IP and per address."
    ),
    responses=PASSWORD_RESET_ERROR_RESPONSES,
)
async def forgot_password(
    payload: ForgotPasswordRequest,
    request: Request,
    db: Annotated[AsyncDatabase, Depends(get_database)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> ForgotPasswordResponse:
    _throttle(
        request,
        bucket="forgot-password",
        identity=payload.email,
        rule=RateLimit(
            limit=settings.forgot_password_rate_limit,
            window_seconds=settings.forgot_password_rate_window_seconds,
        ),
    )

    user_id, raw_token = await create_password_reset(
        db,
        email=payload.email,
        settings=settings,
        request_ip=_client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )

    if user_id is None or raw_token is None:
        # Unknown or non-active address. Audited with no actor, then reported
        # exactly like the success case below -- see the docstring.
        logger.info("Password reset requested for unknown address %s", payload.email)
        await audit.record(
            db,
            action="PASSWORD_RESET_REQUESTED",
            entity="user",
            outcome="UNKNOWN_ACCOUNT",
            ip=_client_ip(request),
            metadata={"email": payload.email},
        )
        return ForgotPasswordResponse()

    link = settings.reset_link(raw_token)
    delivered = await email_service.send(
        email_service.EmailMessage(
            to=payload.email,
            subject=_RESET_SUBJECT,
            text_body=_RESET_BODY.format(
                link=link, minutes=settings.password_reset_expires_minutes
            ),
            # Deliberately absent: a reply-to, and any account detail. The mail
            # should not tell an attacker whether the address is registered.
            headers={"X-Priority": "3"},
        ),
        settings,
    )

    await audit.record(
        db,
        action="PASSWORD_RESET_REQUESTED",
        entity="user",
        actor_id=user_id,
        ip=_client_ip(request),
        metadata={"email": payload.email, "delivered": delivered},
    )
    logger.info("Password reset email queued for %s (delivered=%s)", payload.email, delivered)

    # Outside production there is often no mailbox to check, so hand the link
    # back to make the flow testable. Gated on `is_production` because in
    # production this would let anyone reset any account by asking for it.
    if not settings.is_production and not delivered:
        return ForgotPasswordResponse(devResetLink=link)

    return ForgotPasswordResponse()


@router.post(
    "/reset-password",
    response_model=PasswordChangedResponse,
    summary="Redeem a reset link and set a new password",
    description=(
        "The token is single-use and expires. On success every access token "
        "issued before now stops working, so all devices must sign in again."
    ),
    responses=PASSWORD_RESET_ERROR_RESPONSES,
)
async def reset_password(
    payload: ResetPasswordRequest,
    request: Request,
    db: Annotated[AsyncDatabase, Depends(get_database)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> PasswordChangedResponse:
    _throttle(
        request,
        # Keyed on the token rather than an email: this endpoint has no account
        # context, and the token is the only thing worth limiting per-caller.
        bucket="reset-password",
        identity=payload.token[:16],
        rule=RateLimit(
            limit=settings.reset_password_rate_limit,
            window_seconds=settings.reset_password_rate_window_seconds,
        ),
    )

    user_id = await consume_password_reset(
        db,
        raw_token=payload.token,
        new_password=payload.newPassword,
    )

    await audit.record(
        db,
        action="PASSWORD_RESET_COMPLETED",
        entity="user",
        actor_id=user_id,
        ip=_client_ip(request),
    )
    logger.info("Password reset completed for %s", user_id)

    return PasswordChangedResponse()


@router.post(
    "/change-password",
    response_model=PasswordChangedResponse,
    summary="Change the password of the signed-in user",
    description=(
        "Requires the current password. A wrong current password returns 400, "
        "not 401 - the session stays valid. On success every access token "
        "issued before now is invalidated, including this session."
    ),
    responses={
        **AUTHENTICATED_ERRORS,
        400: {"model": ErrorResponse, "description": "Current password is incorrect"},
        422: {"model": ErrorResponse, "description": "Request validation failed"},
        429: {"model": ErrorResponse, "description": "Too many attempts"},
    },
)
async def change_password_route(
    payload: ChangePasswordRequest,
    request: Request,
    user: CurrentUserDep,
    db: Annotated[AsyncDatabase, Depends(get_database)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> PasswordChangedResponse:
    _throttle(
        request,
        bucket="change-password",
        identity=user.id,
        rule=RateLimit(
            limit=settings.change_password_rate_limit,
            window_seconds=settings.change_password_rate_window_seconds,
        ),
    )

    await change_password(
        db,
        user_id=user.id,
        current_password=payload.currentPassword,
        new_password=payload.newPassword,
    )

    await audit.record(
        db,
        action="PASSWORD_CHANGED",
        entity="user",
        actor_id=user.id,
        ip=_client_ip(request),
    )
    logger.info("Password changed for %s", user.id)

    # No token is returned: the caller's own token is already dead because
    # `passwordChangedAt` now postdates its `iat`, so the client must sign in.
    return PasswordChangedResponse()
