"""Public liveness and database readiness endpoints."""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from rosapenna_backend.core.logging import logger

router = APIRouter()


@router.get(
    "/health",
    tags=["health"],
    summary="Service liveness probe",
    description="Returns basic service status. No API key required.",
    response_description="Service status and current session load.",
)
def health(request: Request):
    manager = request.app.state.session_manager
    settings = request.app.state.settings
    return {
        "status": "ok",
        "active_sessions": manager.active_count(),
        "max_concurrent_sessions": settings.max_concurrent_sessions,
    }


@router.get(
    "/health/ready",
    tags=["health"],
    summary="Check MongoDB readiness",
    responses={503: {"description": "MongoDB is unavailable"}},
)
def readiness(request: Request):
    database = getattr(request.app.state, "mongo_database", None)
    if database is None:
        logger.warning("MongoDB readiness check failed (database is not initialized)")
        return JSONResponse(status_code=503, content={"status": "unavailable", "database": "mongodb"})
    try:
        database.command("ping")
        return {"status": "ready", "database": "mongodb"}
    except Exception as exc:
        logger.warning("MongoDB readiness check failed (%s)", type(exc).__name__)
        return JSONResponse(status_code=503, content={"status": "unavailable", "database": "mongodb"})
