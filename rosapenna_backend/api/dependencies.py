"""FastAPI dependencies for runtime-owned services."""

from fastapi import HTTPException, Request

from rosapenna_backend.config import Settings
from rosapenna_backend.services.booking_sessions import BookingSessionManager
from rosapenna_backend.services.booking_intents import BookingIntentStore
from rosapenna_backend.services.tee_sheets import TeesheetCache
from rosapenna_backend.services.staff_audit import StaffAuditLog


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


\n