"""Public liveness and database readiness endpoints."""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from rosapenna_backend.core.logging import logger

router = APIRouter()


@router.get(
    "/",
    tags=["health"],
    summary="Root service status probe",
    description="Returns basic service status on the root backend host. No API key required.",
    response_description="Service status.",
)
def health():
    return {
        "status": "ok",
        "service": "Rosapenna Booking API",
    }


@router.get(
    "/ready",
    tags=["health"],
    summary="Check MongoDB readiness",
    responses={503: {"description": "MongoDB is unavailable"}},
)
@router.get(
    "/health/ready",
    tags=["health"],
    summary="Check MongoDB readiness (legacy path)",
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
