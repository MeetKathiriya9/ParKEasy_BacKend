"""Application configuration loaded from environment variables / .env file.

All settings are declared once here so that nothing else in the codebase reads
`os.environ` directly. Use `get_settings()` (cached) to obtain the instance.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Annotated, Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

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

    # --- CORS ---
    # `NoDecode` stops pydantic-settings from trying to JSON-parse this field, so
    # .env may use the friendlier `A,B,C` form instead of `["A","B"]`.
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


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings instance (cached)."""
    return Settings()


SettingsDep = Annotated[Settings, "fastapi:settings"]
