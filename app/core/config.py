"""Application configuration loaded from environment variables / .env file.

All settings are declared once here so that nothing else in the codebase reads
`os.environ` directly. Use `get_settings()` (cached) to obtain the instance.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Annotated, Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from app.models.enums import Role

Environment = Literal["development", "test", "production"]


class Settings(BaseSettings):
    """Typed application settings."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- Application ---
    app_name: str = "ParkEasy API"
    environment: Environment = "development"
    debug: bool = True
    version: str = "0.1.0"

    # --- Database ---
    mongodb_uri: str = "mongodb://localhost:27017"
    mongodb_database: str = "parkeasy"
    mongodb_tls: bool = False

    # --- Auth ---
    jwt_secret: str = "change-me-to-a-long-random-string"
    jwt_algorithm: str = "HS256"
    jwt_expires_minutes: int = 60
    jwt_issuer: str = "parkeasy"

    self_register_roles: Annotated[list[Role], NoDecode] = Field(
        default_factory=lambda: [Role.DRIVER, Role.STAFF, Role.OPERATOR, Role.ADMIN]
    )

    login_rate_limit: int = 10
    login_rate_window_seconds: int = 60
    register_rate_limit: int = 5
    register_rate_window_seconds: int = 3600
    forgot_password_rate_limit: int = 5
    forgot_password_rate_window_seconds: int = 3600
    reset_password_rate_limit: int = 10
    reset_password_rate_window_seconds: int = 3600
    change_password_rate_limit: int = 5
    change_password_rate_window_seconds: int = 900

    # --- Password reset ---
    password_reset_expires_minutes: int = 30
    password_reset_token_bytes: int = 32

    # --- Email (DOC sections 24/25 name an email integration) ---
    # The console transport logs the message instead of sending it, so the
    # reset flow is demonstrable without an SMTP account. Switch to "smtp" once
    # the credentials below are filled in.
    email_transport: Literal["console", "smtp"] = "console"
    email_from: str = "no-reply@parkeasy.local"
    email_from_name: str = "ParkEasy"
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_use_tls: bool = True
    smtp_use_ssl: bool = False

    # Origin the emailed reset link points at. Server-side configuration only:
    # never accept this from a request body, or a caller could redirect a
    # victim's reset token to a host they control.
    client_base_url: str = "http://localhost:5173"

    # --- CORS ---
    cors_origins: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["http://localhost:5173", "http://127.0.0.1:5173"]
    )

    # --- Server ---
    host: str = "127.0.0.1"
    port: int = 9999
    reload: bool = True

    # --- Logging ---
    log_level: str = "INFO"

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_cors_origins(cls, value: object) -> object:
        """Accept `A,B,C` from .env as well as a real list from the environment."""
        if isinstance(value, str):
            return [origin.strip() for origin in value.split(",") if origin.strip()]
        return value

    @field_validator("self_register_roles", mode="before")
    @classmethod
    def _parse_self_register_roles(cls, value: object) -> object:
        """Accept `driver,admin` from .env; reject unknown role names loudly."""
        if isinstance(value, str):
            roles = [item.strip().lower() for item in value.split(",") if item.strip()]
            try:
                return [Role(item) for item in roles]
            except ValueError as exc:
                valid = ", ".join(r.value for r in Role)
                raise ValueError(
                    f"Unknown role in SELF_REGISTER_ROLES: {exc}. Valid roles: {valid}"
                ) from exc
        return value

    @field_validator("log_level")
    @classmethod
    def _upper_log_level(cls, value: str) -> str:
        return value.upper()

    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    @property
    def jwt_expire_seconds(self) -> int:
        return self.jwt_expires_minutes * 60

    @property
    def password_reset_expire_seconds(self) -> int:
        return self.password_reset_expires_minutes * 60

    @property
    def smtp_configured(self) -> bool:
        """True when there is enough SMTP configuration to actually send mail."""
        return bool(self.smtp_host and self.smtp_username and self.smtp_password)

    def reset_link(self, raw_token: str) -> str:
        """Build the link the user clicks. `client_base_url` is trusted config."""
        return f"{self.client_base_url.rstrip('/')}/reset-password?token={raw_token}"

    def can_self_register(self, role: Role) -> bool:
        return role in self.self_register_roles

    @property
    def privileged_self_registration_allowed(self) -> bool:
        """True when public registration can mint an operator or admin."""
        return bool({Role.OPERATOR, Role.ADMIN} & set(self.self_register_roles))


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings instance (cached)."""
    return Settings()


SettingsDep = Annotated[Settings, "fastapi:settings"]
