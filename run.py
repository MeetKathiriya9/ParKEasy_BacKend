"""Convenience launcher: `python run.py` starts the API on the configured port."""

from __future__ import annotations

import uvicorn

from app.core.config import get_settings

if __name__ == "__main__":
    settings = get_settings()
    print(f"  ParkEasy API  ->  http://{settings.host}:{settings.port}")
    print(f"  Swagger docs  ->  http://{settings.host}:{settings.port}/docs")
    uvicorn.run("app.main:app", host=settings.host, port=settings.port, reload=settings.reload)
