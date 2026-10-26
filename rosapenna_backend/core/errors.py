"""Safe, consistent API error responses."""

from fastapi import Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pymongo.errors import PyMongoError

from rosapenna_backend.core.logging import logger


async def unhandled_exception_handler(request: Request, exc: Exception):
    logger.error("Unhandled exception on %s %s (%s)", request.method, request.url.path, type(exc).__name__)
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})


async def request_validation_exception_handler(request: Request, exc: RequestValidationError):
    # Pydantic can include submitted values, including card data, in validation errors.
    details = [
        {"location": error.get("loc", ()), "message": error.get("msg", "Invalid value")}
        for error in exc.errors()
    ]
    return JSONResponse(status_code=422, content=jsonable_encoder({"detail": details}))


async def database_exception_handler(request: Request, exc: PyMongoError):
    logger.warning("MongoDB request failed (%s)", type(exc).__name__)
    return JSONResponse(status_code=503, content={
        "detail": "The database is temporarily unavailable. Please try again shortly.",
    })
