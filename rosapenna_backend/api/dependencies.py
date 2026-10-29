"""FastAPI dependencies for runtime-owned services."""

from fastapi import HTTPException, Request

from rosapenna_backend.config import Settings
from rosapenna_backend.integrations.brs.booking_client import BrsBookingClient
from rosapenna_backend.integrations.opera.client import OperaClient
from rosapenna_backend.services.booking_intents import BookingIntentStore
from rosapenna_backend.services.booking_sessions import BookingSessionManager
from rosapenna_backend.services.email_delivery import EmailDeliveryService
from rosapenna_backend.services.package_pricing import PackagePricingService
from rosapenna_backend.services.staff_audit import StaffAuditLog
from rosapenna_backend.services.tee_sheets import TeesheetCache


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_session_manager(request: Request) -> BookingSessionManager:
    return request.app.state.session_manager


def get_teesheet_cache(request: Request) -> TeesheetCache:
    cache = getattr(request.app.state, "teesheet_cache", None)
    if cache is None:
        raise HTTPException(503, "Tee-sheet service is starting")
    return cache


def get_booking_intent_store(request: Request) -> BookingIntentStore:
    store = getattr(request.app.state, "booking_intent_store", None)
    if store is None:
        raise HTTPException(503, "Booking-intent storage is starting")
    return store


def get_staff_audit_log(request: Request) -> StaffAuditLog:
    audit = getattr(request.app.state, "staff_audit_log", None)
    if audit is None:
        raise HTTPException(503, "Staff audit storage is unavailable")
    return audit


def get_opera_client(request: Request) -> OperaClient:
    client = getattr(request.app.state, "opera_client", None)
    if client is None:
        client = OperaClient()
        request.app.state.opera_client = client
    return client


def get_brs_booking_client(request: Request) -> BrsBookingClient:
    client = getattr(request.app.state, "brs_booking_client", None)
    if client is None:
        client = BrsBookingClient()
        request.app.state.brs_booking_client = client
    return client


def get_package_pricing_service(request: Request) -> PackagePricingService:
    service = getattr(request.app.state, "package_pricing_service", None)
    if service is None:
        opera_client = get_opera_client(request)
        teesheet_cache = get_teesheet_cache(request)
        booking_store = get_booking_intent_store(request)
        db = getattr(request.app.state, "mongo_database", None)
        service = PackagePricingService(
            opera_client=opera_client,
            teesheet_cache=teesheet_cache,
            booking_store=booking_store,
            database=db,
        )
        request.app.state.package_pricing_service = service
    return service


def get_email_delivery_service(request: Request) -> EmailDeliveryService:
    service = getattr(request.app.state, "email_delivery_service", None)
    if service is None:
        service = EmailDeliveryService()
        request.app.state.email_delivery_service = service
    return service

