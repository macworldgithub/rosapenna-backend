"""FastAPI application factory and shared service lifecycle."""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from pymongo.errors import PyMongoError
from starlette.concurrency import run_in_threadpool

from rosapenna_backend.api.routes import availability, booking_intents, bookings, health, staff_operations
from rosapenna_backend.config import settings, validate_security_settings
from rosapenna_backend.core.display import start_virtual_display, start_vnc_server, stop_virtual_display, stop_vnc_server
from rosapenna_backend.core.errors import (
    database_exception_handler, request_validation_exception_handler, unhandled_exception_handler,
)
from rosapenna_backend.core.logging import logger
from rosapenna_backend.core.middleware import RequestBodySizeLimitMiddleware
from rosapenna_backend.database import connect_database
from rosapenna_backend.services.booking_intents import BookingIntentStore
from rosapenna_backend.services.booking_sessions import BookingSessionManager
from rosapenna_backend.services.staff_audit import StaffAuditLog
from rosapenna_backend.services.tee_sheets import TeesheetCache


OPENAPI_TAGS = [
    {
        "name": "booking intents",
        "description": "Protected, versioned online booking drafts; they do not confirm a reservation or payment.",
    },
    {
        "name": "availability",
        "description": "Read-only endpoints for checking what tee times are open. "
                       "Never touches the booking form — safe to call frequently.",
    },
    {
        "name": "bookings",
        "description": "Endpoints that drive the booking automation: opening a browser, "
                       "filling the form, optionally filling card details and paying.",
    },
    {
        "name": "staff operations",
        "description": "Staff operations require external OIDC role validation; unavailable until configured.",
    },
    {
        "name": "health",
        "description": "Public liveness and database readiness probes.",
    },
]

API_DESCRIPTION = """
Wraps a Playwright browser-automation flow for the Rosapenna (BRS Golf)
visitor tee-sheet.

### How it works

1. `POST /available-times` — headlessly scrapes open tee times.
2. `POST /bookings` — opens a real Chromium window, fills course/date/time/
   players/personal details, and optionally fills Stripe card details.
   Set `auto_pay: true` to also click Pay automatically.
3. `GET /bookings/{id}` — poll session status.
4. `POST /bookings/{id}/close` — close the browser once done.

### Card details

Pass `card_details: { card_number, expiry, cvv }` in the booking request
to have card details filled automatically. Stripe's iframe is cross-origin
so this works via simulated keyboard input (click → type number → Tab →
type expiry → Tab → type CVV).

Set `auto_pay: true` to also click the Pay button after filling card details.
Leave it `false` (default) to review manually before paying.

### Authentication

Backend service routes require an `X-API-Key` header. Staff operations also require an external OIDC identity and mapped staff role. OIDC is not configured, so `/sessions` and `POST /teesheets/refresh` fail closed with HTTP 503. No local staff accounts are supported. `/health` and `/health/ready` are public probes. API documentation is controlled by `BOOKING_DOCS_ENABLED` and is disabled in production.
"""


def create_app() -> FastAPI:
    """Build the API and attach resources to its application state."""

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        validate_security_settings()
        logger.info(
            "Starting up: max_concurrent_sessions=%d, ttl=%ds",
            settings.max_concurrent_sessions, settings.session_ttl_seconds,
        )
        if not settings.api_key:
            logger.warning("BOOKING_API_KEY is not set; protected routes will return HTTP 503")

        client, database = await run_in_threadpool(connect_database)
        application.state.mongo_client = client
        application.state.mongo_database = database
        cache = None
        try:
            cache = await run_in_threadpool(
                TeesheetCache, database, settings.teesheet_days, settings.teesheet_refresh_hour,
            )
            store = await run_in_threadpool(
                BookingIntentStore, database[settings.intents_collection],
            )
            staff_audit_log = await run_in_threadpool(
                StaffAuditLog, database["staff_audit_events"],
            )
            application.state.teesheet_cache = cache
            application.state.booking_intent_store = store
            application.state.staff_audit_log = staff_audit_log
            logger.info("MongoDB connected; tee-sheet and booking-intent indexes ready")

            start_virtual_display()
            start_vnc_server()
            application.state.session_manager.start()
            if settings.teesheet_refresh_enabled:
                cache.start()
            yield
        finally:
            if cache is not None:
                await run_in_threadpool(cache.close)
            application.state.teesheet_cache = None
            application.state.booking_intent_store = None
            application.state.staff_audit_log = None
            logger.info("Shutting down: closing all open sessions...")
            await run_in_threadpool(application.state.session_manager.close)
            stop_vnc_server()
            stop_virtual_display()
            client.close()
            application.state.mongo_client = None
            application.state.mongo_database = None
            logger.info("Shutdown complete.")

    application = FastAPI(
        title="Rosapenna Tee Time Booking API",
        description=API_DESCRIPTION,
        version="1.1.0",
        contact={"name": "Booking API Maintainer"},
        openapi_tags=OPENAPI_TAGS,
        docs_url="/docs" if settings.docs_enabled else None,
        redoc_url="/redoc" if settings.docs_enabled else None,
        openapi_url="/openapi.json" if settings.docs_enabled else None,
        lifespan=lifespan,
    )
    application.state.settings = settings
    application.state.session_manager = BookingSessionManager(settings)
    application.state.mongo_client = None
    application.state.mongo_database = None
    application.state.teesheet_cache = None
    application.state.booking_intent_store = None
    application.state.staff_audit_log = None

    application.add_middleware(RequestBodySizeLimitMiddleware, max_bytes=1_048_576)
    application.add_middleware(
        CORSMiddleware,
        allow_origins=(
            ["*"] if settings.cors_origins.strip() == "*" and settings.environment.lower() != "production"
            else [origin.strip() for origin in settings.cors_origins.split(",")
                  if origin.strip() and origin.strip() != "*"]
        ),
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    application.add_exception_handler(Exception, unhandled_exception_handler)
    application.add_exception_handler(RequestValidationError, request_validation_exception_handler)
    application.add_exception_handler(PyMongoError, database_exception_handler)

    application.include_router(health.router)
    application.include_router(availability.router)
    application.include_router(bookings.router)
    application.include_router(booking_intents.router)
    application.include_router(staff_operations.router)
    return application


app = create_app()
