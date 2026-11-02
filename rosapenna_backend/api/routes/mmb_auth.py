"""API routes for Ticket M03 — Booking Lookup and One-Time Security Code (OTP)."""

from fastapi import APIRouter, Depends, HTTPException, status

from rosapenna_backend.api.dependencies import (
    get_booking_linkage_service,
    get_current_guest_session,
    get_mmb_auth_service,
    require_guest_booking_scope,
    require_mmb_enabled,
)
from rosapenna_backend.api.schemas.booking_linkage import GuestSafeBookingSummary
from rosapenna_backend.api.schemas.mmb_auth import (
    GuestSessionPrincipal,
    MmbLogoutResponse,
    MmbSessionResponse,
    RequestOtpRequest,
    RequestOtpResponse,
    VerifyOtpRequest,
)
from rosapenna_backend.api.security import rate_limit
from rosapenna_backend.services.booking_linkage import (
    BookingLinkageService,
    LinkageNotFoundError,
)
from rosapenna_backend.services.mmb_auth import (
    ForbiddenBookingScopeError,
    InvalidOtpError,
    InvalidSessionError,
    MmbAuthError,
    MmbAuthService,
    MmbOtpCooldownError,
)

router = APIRouter(prefix="/mmb", tags=["mmb authentication"])


@router.post(
    "/auth/request-otp",
    response_model=RequestOtpResponse,
    summary="Request a 6-digit one-time verification code for booking access",
    description=(
        "Enforces MMB release gate and rate limiting. Cryptographically generates an OTP "
        "and dispatches it exclusively to the lead guest email on file. Never accepts a caller-supplied email. "
        "Returns a generic response to prevent identity or booking existence enumeration."
    ),
    dependencies=[Depends(rate_limit), Depends(require_mmb_enabled)],
)
def request_one_time_code(
    request: RequestOtpRequest,
    auth_service: MmbAuthService = Depends(get_mmb_auth_service),
) -> RequestOtpResponse:
    try:
        return auth_service.request_otp(
            booking_reference=request.booking_reference,
            last_name=request.last_name,
        )
    except MmbOtpCooldownError as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=exc.message,
            headers={"Retry-After": "60"},
        )
    except MmbAuthError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message)


@router.post(
    "/auth/verify-otp",
    response_model=MmbSessionResponse,
    summary="Verify 6-digit OTP code and issue a booking-scoped session token",
    description=(
        "Validates the hashed code in constant time. Enforces single-use consumption, "
        "10-minute expiry, and maximum attempt limits. On success, issues a short-lived bearer session token."
    ),
    dependencies=[Depends(rate_limit), Depends(require_mmb_enabled)],
)
def verify_one_time_code(
    request: VerifyOtpRequest,
    auth_service: MmbAuthService = Depends(get_mmb_auth_service),
) -> MmbSessionResponse:
    try:
        return auth_service.verify_otp(
            booking_reference=request.booking_reference,
            code=request.code,
        )
    except InvalidOtpError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=exc.message)
    except MmbAuthError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message)


@router.post(
    "/auth/logout",
    response_model=MmbLogoutResponse,
    summary="Revoke guest session and invalidate pending codes",
    dependencies=[Depends(require_mmb_enabled)],
)
def logout_guest_session(
    session: GuestSessionPrincipal = Depends(get_current_guest_session),
    auth_service: MmbAuthService = Depends(get_mmb_auth_service),
) -> MmbLogoutResponse:
    return auth_service.logout(session.session_token)


@router.get(
    "/auth/me",
    response_model=GuestSessionPrincipal,
    summary="Get current authenticated guest session details",
    dependencies=[Depends(require_mmb_enabled)],
)
def get_session_profile(
    session: GuestSessionPrincipal = Depends(get_current_guest_session),
) -> GuestSessionPrincipal:
    return session


@router.get(
    "/booking/{booking_reference}",
    response_model=GuestSafeBookingSummary,
    summary="Get verified guest safe booking summary",
    description="Requires authenticated guest session and strictly enforces booking scope (IDOR prevention).",
    dependencies=[Depends(require_mmb_enabled)],
)
def get_authenticated_booking_summary(
    booking_reference: str,
    session: GuestSessionPrincipal = Depends(get_current_guest_session),
    linkage_service: BookingLinkageService = Depends(get_booking_linkage_service),
) -> GuestSafeBookingSummary:
    require_guest_booking_scope(booking_reference, session)

    try:
        # Retrieve linkage record and sanitize
        linkage = linkage_service.get_linkage(booking_reference)
        # Use guest_lookup with the verified lead surname to generate safe summary
        return linkage_service.guest_lookup(
            booking_reference=booking_reference,
            last_name=linkage.lead_guest.last_name,
        )
    except LinkageNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No booking was found matching the provided reference.",
        )
