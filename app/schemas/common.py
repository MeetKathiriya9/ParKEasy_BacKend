"""Shared Pydantic schemas: response envelopes and pagination."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class MessageResponse(BaseModel):
    """Simple acknowledgement body."""

    message: str
    ok: bool = True


class ErrorBody(BaseModel):
    code: str = Field(examples=["NOT_FOUND"])
    message: str = Field(examples=["Reservation not found"])
    details: Any | None = None


class ErrorResponse(BaseModel):
    """The single error envelope produced by `app.core.errors`."""

    error: ErrorBody

    model_config = {
        "json_schema_extra": {
            "example": {
                "error": {
                    "code": "RESERVATION_NOT_FOUND",
                    "message": "Reservation not found",
                    "details": None,
                }
            }
        }
    }


class PageMeta(BaseModel):
    page: int = Field(ge=1, examples=[1])
    limit: int = Field(ge=1, le=200, examples=[20])
    total: int = Field(ge=0, examples=[137])
    pages: int = Field(ge=0, examples=[7])


class PaginatedResponse[T](BaseModel):
    """Envelope for list endpoints."""

    items: list[T] = Field(default_factory=list)
    meta: PageMeta

    @classmethod
    def build(cls, items: list[T], *, total: int, page: int, limit: int) -> PaginatedResponse[T]:
        pages = (total + limit - 1) // limit if limit else 0
        return cls(items=items, meta=PageMeta(page=page, limit=limit, total=total, pages=pages))
