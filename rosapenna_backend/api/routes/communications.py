"""Communications and itinerary delivery endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Path as ApiPath, Response

from rosapenna_backend.api.dependencies import get_booking_intent_store, get_email_delivery_service
from rosapenna_backend.api.schemas.communications import (
    EmailDeliveryResult,
    SendEmailRequest,
    SpecialRequestCreate,
)
from rosapenna_backend.api.security import COMMON_DEPS
from rosapenna_backend.services.booking_intents import BookingIntentStore, IntentNotFound
from rosapenna_backend.services.email_delivery import EmailDeliveryService
from rosapenna_backend.services.pdf_itinerary import build_itinerary_pdf

router = APIRouter(dependencies=COMMON_DEPS, prefix="/communications", tags=["communications"])


@router.post(
    "/send",
    response_model=EmailDeliveryResult,
    summary="Dispatch email notification with template routing and attachment handling",
    responses={
        400: {"description": "Invalid template parameters or missing required fields."},
        401: {"description": "Missing or invalid API key."},
        404: {"description": "Booking intent or reference not found."},
    },
)
def send_communication(
    request: SendEmailRequest,
    store: BookingIntentStore = Depends(get_booking_intent_store),
    email_service: EmailDeliveryService = Depends(get_email_delivery_service),
) -> EmailDeliveryResult:
    """Send an email notification according to the requested template."""
    try:
        booking = store.get_by_reference(request.booking_intent_id)
    except IntentNotFound:
        raise HTTPException(status_code=404, detail="Booking record was not found") from None

    lead = booking.get("lead_guest") or {}
    recipient_email = request.recipient_email or lead.get("email")
    recipient_name = request.recipient_name or f"{lead.get('first_name', '')} {lead.get('last_name', '')}".strip() or "Guest"

    if request.template == "booking_confirmation":
        return email_service.send_booking_confirmation(
            booking=booking,
            recipient_override=recipient_email,
            idempotency_key=request.idempotency_key,
        )

    if request.template == "special_request_acknowledgement":
        text = request.special_request_text or "Special request noted."
        return email_service.send_special_request_acknowledgement(
            booking=booking,
            request_text=text,
        )

    if request.template == "one_time_code":
        if not request.otp_code:
            raise HTTPException(status_code=400, detail="otp_code is required for one_time_code template")
        if not recipient_email:
            raise HTTPException(status_code=400, detail="Recipient email is required for one_time_code template")
        ref = booking.get("booking_reference") or booking.get("booking_intent_id", "REF-PENDING")
        return email_service.send_one_time_code(
            recipient_email=recipient_email,
            recipient_name=recipient_name,
            code=request.otp_code,
            booking_reference=ref,
        )

    if request.template == "balance_reminder":
        return email_service.send_balance_reminder(
            booking=booking,
            recipient_override=recipient_email,
        )

    raise HTTPException(status_code=400, detail=f"Unsupported email template: {request.template}")


@router.post(
    "/special-requests",
    summary="Submit a guest special request without mutating inventory or payments",
    responses={
        401: {"description": "Missing or invalid API key."},
        404: {"description": "Booking intent or reference not found."},
    },
)
def create_special_request(
    request: SpecialRequestCreate,
    store: BookingIntentStore = Depends(get_booking_intent_store),
    email_service: EmailDeliveryService = Depends(get_email_delivery_service),
) -> dict:
    """Record guest special request as pending staff review and send acknowledgement."""
    try:
        booking = store.get_by_reference(request.booking_intent_id)
    except IntentNotFound:
        raise HTTPException(status_code=404, detail="Booking record was not found") from None

    delivery = email_service.send_special_request_acknowledgement(
        booking=booking,
        request_text=request.request_text,
        category=request.category,
    )

    ref = booking.get("booking_reference") or request.booking_intent_id
    return {
        "status": "received",
        "booking_reference": ref,
        "category": request.category,
        "request_text": request.request_text,
        "review_status": "pending_staff_review",
        "inventory_mutated": False,
        "payment_mutated": False,
        "delivery_result": delivery.model_dump(mode="json"),
    }


@router.get(
    "/itinerary/{booking_intent_id}/pdf",
    summary="Download rendered PDF itinerary document for a booking",
    responses={
        401: {"description": "Missing or invalid API key."},
        404: {"description": "Booking intent or reference not found."},
        200: {"content": {"application/pdf": {}}, "description": "Binary PDF itinerary document."},
    },
)
def download_itinerary_pdf(
    booking_intent_id: str = ApiPath(min_length=1, max_length=100),
    store: BookingIntentStore = Depends(get_booking_intent_store),
) -> Response:
    """Compile and stream the authoritative PDF itinerary for a booking."""
    try:
        booking = store.get_by_reference(booking_intent_id)
    except IntentNotFound:
        raise HTTPException(status_code=404, detail="Booking record was not found") from None

    pdf_bytes = build_itinerary_pdf(booking)
    ref = booking.get("booking_reference") or booking.get("booking_intent_id", "itinerary")

    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="Rosapenna-Itinerary-{ref}.pdf"',
            "Content-Length": str(len(pdf_bytes)),
        },
    )
