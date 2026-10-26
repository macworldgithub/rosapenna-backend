"""Compatibility command for running the Rosapenna API."""

from app import app, settings

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=settings.host, port=settings.port, reload=False, workers=1)
