"""Compatibility entrypoint; the FastAPI application lives in the package."""

from rosapenna_backend.config import settings
from rosapenna_backend.main import app, create_app

__all__ = ["app", "create_app", "settings"]

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=settings.host, port=settings.port, reload=False, workers=1)
