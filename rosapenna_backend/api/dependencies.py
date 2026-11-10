"""FastAPI dependencies for runtime-owned services."""

from fastapi import Depends, HTTPException, Request

from rosapenna_backend.config import Settings
from rosapenna_backend.integrations.brs.booking_client import BrsBookingClient
from rosapenna_backend.integrations.opera.client import OperaClient
from rosapenna_backend.integrations.prommt import PrommtClient
from rosapenna_backend.services.booking_intents import BookingIntentStore
from rosapenna_backend.services.booking_linkage import BookingLinkageService
from rosapenna_backend.services.booking_sessions import BookingSessionManager
from rosapenna_backend.services.email_delivery import EmailDeliveryService
from rosapenna_backend.services.mmb_amendments import MmbAmendmentService
from rosapenna_backend.services.mmb_auth import (
    ForbiddenBookingScopeError,
    InvalidSessionError,
    MmbAuthService,
)
from rosapenna_backend.api.schemas.mmb_auth import GuestSessionPrincipal
from rosapenna_backend.services.mmb_gate import MmbGateService
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


def get_prommt_client(request: Request) -> PrommtClient:
    client = getattr(request.app.state, "prommt_client", None)
    if client is None:
        client = PrommtClient()
        request.app.state.prommt_client = client
    return client


def get_package_pricing_service(request: Request) -> PackagePricingService:
    service = getattr(request.app.state, "package_pricing_service", None)
    if service is None:
        opera_client = get_opera_client(request)
        teesheet_cache = get_teesheet_cache(request)
        booking_store = get_booking_intent_store(request)
        db = getattr(request.app.state, "mongo_database", None)
        prommt_client = get_prommt_client(request)
        linkage_service = get_booking_linkage_service(request)
        service = PackagePricingService(
            opera_client=opera_client,
            teesheet_cache=teesheet_cache,
            booking_store=booking_store,
            database=db,
            prommt_client=prommt_client,
            linkage_service=linkage_service,
        )
        request.app.state.package_pricing_service = service
    return service


def get_email_delivery_service(request: Request) -> EmailDeliveryService:
    service = getattr(request.app.state, "email_delivery_service", None)
    if service is None:
        service = EmailDeliveryService()
        request.app.state.email_delivery_service = service
    return service


def get_mmb_gate_service(request: Request) -> MmbGateService:
    service = getattr(request.app.state, "mmb_gate_service", None)
    if service is None:
        db = getattr(request.app.state, "mongo_database", None)
        settings = getattr(request.app.state, "settings", None)
        service = MmbGateService(database=db, settings=settings)
        request.app.state.mmb_gate_service = service
    return service


def get_booking_linkage_service(request: Request) -> BookingLinkageService:
    service = getattr(request.app.state, "booking_linkage_service", None)
    if service is None:
        db = getattr(request.app.state, "mongo_database", None)
        opera_client = get_opera_client(request)
        service = BookingLinkageService(database=db, opera_client=opera_client)
        request.app.state.booking_linkage_service = service
    return service


def require_mmb_enabled(
    request: Request,
) -> None:
    """Enforce that Manage My Booking is explicitly enabled before permitting guest amendment operations."""
    service = get_mmb_gate_service(request)
    if not service.is_enabled():
        raise HTTPException(
            status_code=503,
            detail="Manage My Booking is currently unavailable.",
            headers={"Retry-After": "300"},
        )


def get_mmb_auth_service(request: Request) -> MmbAuthService:
    service = getattr(request.app.state, "mmb_auth_service", None)
    if service is None:
        db = getattr(request.app.state, "mongo_database", None)
        email_svc = get_email_delivery_service(request)
        service = MmbAuthService(database=db, email_service=email_svc)
        request.app.state.mmb_auth_service = service
    return service


def get_current_guest_session(
    request: Request,
    auth_service: MmbAuthService = Depends(get_mmb_auth_service),
) -> GuestSessionPrincipal:
    """Enforce active guest session, checking MMB release gate and session validity."""
    # 1. Gate check: disabling MMB immediately invalidates access
    require_mmb_enabled(request)

    # 2. Extract token from header: X-MMB-Session or Authorization: Bearer <token>
    token = request.headers.get("X-MMB-Session")
    if not token:
        auth_header = request.headers.get("Authorization", "")
        if auth_header.lower().startswith("bearer "):
            token = auth_header[7:].strip()

    if not token:
        raise HTTPException(
            status_code=401,
            detail="Guest session token is missing. Please sign in via verification code.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    try:
        return auth_service.validate_session(token)
    except InvalidSessionError as exc:
        raise HTTPException(
            status_code=401,
            detail=exc.message,
            headers={"WWW-Authenticate": "Bearer"},
        )


def require_guest_booking_scope(
    booking_reference: str,
    session: GuestSessionPrincipal = Depends(get_current_guest_session),
) -> GuestSessionPrincipal:
    """IDOR Prevention: enforce that the session is strictly authorized for the requested booking."""
    if session.booking_reference != booking_reference.strip().upper():
        raise HTTPException(
            status_code=403,
            detail="Access denied. Session is not authorized for this booking.",
        )
    return session


def get_mmb_amendment_service(request: Request) -> MmbAmendmentService:
    service = getattr(request.app.state, "mmb_amendment_service", None)
    if service is None:
        db = getattr(request.app.state, "mongo_database", None)
        linkage_svc = get_booking_linkage_service(request)
        pricing_svc = get_package_pricing_service(request)
        gate_svc = get_mmb_gate_service(request)
        service = MmbAmendmentService(database=db, linkage_service=linkage_svc, pricing_service=pricing_svc, gate_service=gate_svc)
        request.app.state.mmb_amendment_service = service
    return service


def get_mmb_occupants_service(request: Request):
    from rosapenna_backend.services.mmb_occupants import MmbOccupantsService
    service = getattr(request.app.state, "mmb_occupants_service", None)
    if service is None:
        db = getattr(request.app.state, "mongo_database", None)
        linkage_svc = get_booking_linkage_service(request)
        opera_client = get_opera_client(request)
        gate_svc = get_mmb_gate_service(request)
        amendment_svc = get_mmb_amendment_service(request)
        service = MmbOccupantsService(
            database=db,
            linkage_service=linkage_svc,
            opera_client=opera_client,
            gate_service=gate_svc,
            amendment_service=amendment_svc,
        )
        request.app.state.mmb_occupants_service = service
    return service


def get_booking_operation_engine(request: Request):
    from rosapenna_backend.services.booking_operations import BookingOperationEngine
    engine = getattr(request.app.state, "booking_operation_engine", None)
    if engine is None:
        db = getattr(request.app.state, "mongo_database", None)
        pricing_svc = get_package_pricing_service(request)
        linkage_svc = get_booking_linkage_service(request)
        opera_client = get_opera_client(request)
        brs_client = get_brs_booking_client(request)
        email_svc = get_email_delivery_service(request)
        amendment_svc = get_mmb_amendment_service(request)
        occupants_svc = get_mmb_occupants_service(request)
        engine = BookingOperationEngine(
            database=db,
            pricing_service=pricing_svc,
            linkage_service=linkage_svc,
            opera_client=opera_client,
            brs_client=brs_client,
            email_service=email_svc,
            amendment_service=amendment_svc,
            occupants_service=occupants_svc,
        )
        request.app.state.booking_operation_engine = engine
    return engine


def get_mmb_contact_service(request: Request):
    from rosapenna_backend.services.mmb_contact import MmbContactService
    service = getattr(request.app.state, "mmb_contact_service", None)
    if service is None:
        db = getattr(request.app.state, "mongo_database", None)
        linkage_svc = get_booking_linkage_service(request)
        opera_client = get_opera_client(request)
        service = MmbContactService(
            database=db,
            linkage_service=linkage_svc,
            opera_client=opera_client,
        )
        request.app.state.mmb_contact_service = service
    return service


def get_mmb_caddy_service(request: Request):
    from rosapenna_backend.services.mmb_caddy import MmbCaddyService
    service = getattr(request.app.state, "mmb_caddy_service", None)
    if service is None:
        db = getattr(request.app.state, "mongo_database", None)
        linkage_svc = get_booking_linkage_service(request)
        brs_client = get_brs_booking_client(request)
        service = MmbCaddyService(
            database=db,
            linkage_service=linkage_svc,
            brs_client=brs_client,
        )
        request.app.state.mmb_caddy_service = service
    return service


def get_mmb_request_service(request: Request):
    from rosapenna_backend.services.mmb_requests import MmbRequestService
    service = getattr(request.app.state, "mmb_request_service", None)
    if service is None:
        db = getattr(request.app.state, "mongo_database", None)
        linkage_svc = get_booking_linkage_service(request)
        email_svc = get_email_delivery_service(request)
        service = MmbRequestService(
            database=db,
            linkage_service=linkage_svc,
            email_service=email_svc,
        )
        request.app.state.mmb_request_service = service
    return service


def get_staff_request_service(request: Request):
    from rosapenna_backend.services.staff_requests import StaffRequestService
    service = getattr(request.app.state, "staff_request_service", None)
    if service is None:
        db = getattr(request.app.state, "mongo_database", None)
        linkage_svc = get_booking_linkage_service(request)
        email_svc = get_email_delivery_service(request)
        operation_engine = get_booking_operation_engine(request)
        staff_audit = getattr(request.app.state, "staff_audit_log", None)
        service = StaffRequestService(
            database=db,
            linkage_service=linkage_svc,
            email_service=email_svc,
            operation_engine=operation_engine,
            staff_audit_log=staff_audit,
        )
        request.app.state.staff_request_service = service
    return service


def get_revised_confirmation_service(request: Request):
    from rosapenna_backend.services.revised_confirmations import RevisedConfirmationService
    service = getattr(request.app.state, "revised_confirmation_service", None)
    if service is None:
        db = getattr(request.app.state, "mongo_database", None)
        linkage_svc = get_booking_linkage_service(request)
        email_svc = get_email_delivery_service(request)
        service = RevisedConfirmationService(
            database=db,
            linkage_service=linkage_svc,
            email_service=email_svc,
        )
        request.app.state.revised_confirmation_service = service
    return service





