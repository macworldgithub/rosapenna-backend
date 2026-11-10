"""FastAPI routes for revised confirmations, updated PDF itineraries, and pre-arrival reminders (Ticket M15)."""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Path, Response

from rosapenna_backend.api.dependencies import (
    get_revised_confirmation_service,
    require_guest_booking_scope,
    require_mmb_enabled,
)
from rosapenna_backend.api.schemas.revised_confirmations import (
    DispatchReminderRequest,
    DispatchReminderResponse,
    RevisedConfirmationSendRequest,
    RevisedConfirmationSendResponse,
    ScheduledReminderListResponse,
)
from rosapenna_backend.services.booking_linkage import LinkageNotFoundError
from rosapenna_backend.services.revised_confirmations import RevisedConfirmationService

router = APIRouter(
    prefix="/mmb/confirmations",
    tags=["Manage My Booking - Revised Confirmations & Reminders"],
    dependencies=[Depends(require_mmb_enabled)],
)


@router.get(
    "/{booking_reference}/pdf",
    summary="Download updated binary PDF itinerary for the current revision (Ticket M15)",
    responses={
        401: {"description": "Missing or invalid guest session token."},
        403: {"description": "Access denied; session not authorized for this booking."},
        404: {"description": "Booking record was not found."},
        503: {"description": "Manage My Booking module is currently offline."},
        200: {"content": {"application/pdf": {}}, "description": "Binary PDF itinerary document."},
    },
)
def download_revised_itinerary_pdf(
    booking_reference: str = Path(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9\-]+$"),
    service: RevisedConfirmationService = Depends(get_revised_confirmation_service),
    _authorized_session: Any = Depends(require_guest_booking_scope),
) -> Response:
    """Compile and stream the authoritative PDF itinerary matching the booking's current revision."""
    try:
        pdf_bytes, filename = service.generate_revised_itinerary_pdf(booking_reference)
    except LinkageNotFoundError:
        raise HTTPException(status_code=404, detail="Booking record was not found") from None
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to generate revised PDF itinerary: {exc}") from None

    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Content-Length": str(len(pdf_bytes)),
            "Cache-Control": "no-store, no-cache, must-revalidate",
        },
    )


@router.post(
    "/{booking_reference}/send",
    response_model=RevisedConfirmationSendResponse,
    summary="Dispatch official revised booking confirmation email with updated PDF (Ticket M15)",
)
def send_revised_confirmation_email(
    payload: RevisedConfirmationSendRequest,
    booking_reference: str = Path(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9\-]+$"),
    service: RevisedConfirmationService = Depends(get_revised_confirmation_service),
    _authorized_session: Any = Depends(require_guest_booking_scope),
    idempotency_key: Optional[str] = Header(None, alias="X-Idempotency-Key"),
) -> RevisedConfirmationSendResponse:
    """Send official revised booking confirmation to lead guest and reservations copy."""
    idemp = payload.idempotency_key or idempotency_key
    try:
        snapshot = service.get_canonical_booking_snapshot(booking_reference)
        rev = snapshot.get("revision_number", 1)
        delivery = service.send_revised_confirmation(
            booking_reference=booking_reference,
            recipient_override=payload.recipient_override,
            idempotency_key=idemp,
        )
        return RevisedConfirmationSendResponse(
            booking_reference=booking_reference.upper(),
            revision_number=rev,
            delivery_result=delivery,
        )
    except LinkageNotFoundError:
        raise HTTPException(status_code=404, detail="Booking record was not found") from None


@router.get(
    "/{booking_reference}/reminders",
    response_model=ScheduledReminderListResponse,
    summary="List all scheduled, canceled, and dispatched pre-arrival communication milestones (Ticket M15)",
)
def get_booking_reminders(
    booking_reference: str = Path(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9\-]+$"),
    service: RevisedConfirmationService = Depends(get_revised_confirmation_service),
    _authorized_session: Any = Depends(require_guest_booking_scope),
) -> ScheduledReminderListResponse:
    """Fetch all pre-arrival communication milestones for a booking."""
    try:
        return service.get_scheduled_reminders(booking_reference)
    except LinkageNotFoundError:
        raise HTTPException(status_code=404, detail="Booking record was not found") from None


@router.post(
    "/{booking_reference}/reminders/{reminder_id}/dispatch",
    response_model=DispatchReminderResponse,
    summary="Truthfully evaluate and dispatch a due pre-arrival reminder (Ticket M15)",
)
def dispatch_booking_reminder(
    payload: DispatchReminderRequest,
    booking_reference: str = Path(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9\-]+$"),
    reminder_id: str = Path(min_length=1, max_length=100),
    service: RevisedConfirmationService = Depends(get_revised_confirmation_service),
    _authorized_session: Any = Depends(require_guest_booking_scope),
    idempotency_key: Optional[str] = Header(None, alias="X-Idempotency-Key"),
) -> DispatchReminderResponse:
    """Evaluate booking state and dispatch pre-arrival reminder, or suppress if conditions no longer apply."""
    idemp = payload.idempotency_key or idempotency_key
    try:
        return service.dispatch_reminder(
            booking_reference=booking_reference,
            reminder_id=reminder_id,
            idempotency_key=idemp,
        )
    except LinkageNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
