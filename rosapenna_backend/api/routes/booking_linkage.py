"""API routes for Ticket M02 — Online and Offline Booking Linkage and Guest Lookup."""

from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, Query, status

from rosapenna_backend.api.dependencies import (
    get_booking_linkage_service,
    require_mmb_enabled,
)
from rosapenna_backend.api.schemas.booking_linkage import (
    BookingLinkageResponse,
    GuestBookingLookupRequest,
    GuestSafeBookingSummary,
    StaffCorrectLinkageRequest,
    StaffLinkBookingRequest,
)
from rosapenna_backend.api.security import rate_limit, verify_api_key
from rosapenna_backend.api.staff_security import StaffPrincipal, require_staff_roles
from rosapenna_backend.services.booking_linkage import (
    BookingLinkageService,
    LinkageConflictError,
    LinkageError,
    LinkageNotFoundError,
)

router = APIRouter(tags=["booking linkage"])


# ==============================================================================
# Staff-facing endpoints (Protected by Staff Role & API Key)
# ==============================================================================

@router.post(
    "/staff/booking-linkage",
    response_model=BookingLinkageResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create or associate an online/offline booking linkage",
    description="Associates lead guest, Opera stay, BRS rounds, and payment records without vendor mutation.",
    dependencies=[Depends(verify_api_key), Depends(rate_limit)],
)
def staff_link_booking(
    request: StaffLinkBookingRequest,
    principal: StaffPrincipal = Depends(
        require_staff_roles("reservations_agent", "reservations_manager", "site_admin")
    ),
    service: BookingLinkageService = Depends(get_booking_linkage_service),
) -> BookingLinkageResponse:
    actor = f"{principal.subject}@{principal.issuer}"
    try:
        return service.link_booking(request=request, actor=actor)
    except LinkageConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    except LinkageError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message)


@router.post(
    "/staff/booking-linkage/{booking_reference}/correct",
    response_model=BookingLinkageResponse,
    summary="Correct booking linkage components with immutable audit trail",
    description="Allows authorized staff to resolve missing, conflicting, or duplicated components.",
    dependencies=[Depends(verify_api_key), Depends(rate_limit)],
)
def staff_correct_linkage(
    booking_reference: str,
    request: StaffCorrectLinkageRequest,
    principal: StaffPrincipal = Depends(
        require_staff_roles("reservations_manager", "site_admin")
    ),
    service: BookingLinkageService = Depends(get_booking_linkage_service),
) -> BookingLinkageResponse:
    actor = f"{principal.subject}@{principal.issuer}"
    try:
        return service.correct_linkage(
            booking_reference=booking_reference,
            request=request,
            actor=actor,
        )
    except LinkageNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    except LinkageError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message)


@router.get(
    "/staff/booking-linkage",
    response_model=List[BookingLinkageResponse],
    summary="List booking linkages for staff review and reconciliation",
    dependencies=[Depends(verify_api_key), Depends(rate_limit)],
)
def staff_list_linkages(
    linkage_status: Optional[str] = Query(None, alias="status", description="Filter by linkage status"),
    source_channel: Optional[str] = Query(None, description="Filter by source channel (online/phone/email)"),
    limit: int = Query(50, ge=1, le=200),
    principal: StaffPrincipal = Depends(
        require_staff_roles("reservations_agent", "reservations_manager", "site_admin")
    ),
    service: BookingLinkageService = Depends(get_booking_linkage_service),
) -> List[BookingLinkageResponse]:
    return service.list_linkages(
        status=linkage_status,
        source_channel=source_channel,
        limit=limit,
    )


@router.get(
    "/staff/booking-linkage/{booking_reference}",
    response_model=BookingLinkageResponse,
    summary="Get full booking linkage record with audit trail",
    dependencies=[Depends(verify_api_key), Depends(rate_limit)],
)
def staff_get_linkage(
    booking_reference: str,
    principal: StaffPrincipal = Depends(
        require_staff_roles("reservations_agent", "reservations_manager", "site_admin")
    ),
    service: BookingLinkageService = Depends(get_booking_linkage_service),
) -> BookingLinkageResponse:
    try:
        return service.get_linkage(booking_reference)
    except LinkageNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))


# ==============================================================================
# Guest-facing lookup endpoint (Fail-closed behind MMB Release Gate)
# ==============================================================================

@router.post(
    "/mmb/lookup",
    response_model=GuestSafeBookingSummary,
    summary="Guest portal booking lookup by reference and surname",
    description="Enforces MMB release gate. Exposes only safe public fields; suppresses vendor IDs.",
    dependencies=[Depends(rate_limit), Depends(require_mmb_enabled)],
)
def guest_lookup_booking(
    request: GuestBookingLookupRequest,
    service: BookingLinkageService = Depends(get_booking_linkage_service),
) -> GuestSafeBookingSummary:
    try:
        return service.guest_lookup(
            booking_reference=request.booking_reference,
            last_name=request.last_name,
        )
    except LinkageNotFoundError:
        # Generic error message to prevent enumeration or revealing whether reference exists
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No booking was found matching the provided reference and surname.",
        )
