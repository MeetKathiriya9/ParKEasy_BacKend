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
