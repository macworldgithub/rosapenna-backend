"""Room availability and reservation routes powered by Oracle Opera Cloud / OHIP."""

import datetime as dt

from fastapi import APIRouter, Depends, HTTPException, Query

from rosapenna_backend.api.dependencies import get_booking_intent_store, get_opera_client
from rosapenna_backend.api.schemas.booking_intents import OperaReservationPayload
from rosapenna_backend.api.security import COMMON_DEPS
from rosapenna_backend.integrations.opera.client import (
    OperaClient,
    OperaConflictError,
    OperaIntegrationError,
    OperaReadbackMismatchError,
)
from rosapenna_backend.integrations.opera.schemas import (
    CreateReservationRequest,
    ReservationWriteResult,
    RoomAvailabilityResponse,
    RoomReservationDetail,
    UpdateReservationRequest,
)
from rosapenna_backend.services.booking_intents import (
    BookingIntentStore,
    IntentConflict,
    IntentNotFound,
)

router = APIRouter(prefix="/rooms", tags=["rooms"], dependencies=COMMON_DEPS)


@router.get(
    "/availability",
    response_model=RoomAvailabilityResponse,
    summary="Check room category availability and rates",
    description="Queries Opera Cloud via OHIP for room category availability, counts, and rate plans.",
)
def get_room_availability(
    arrival_date: dt.date = Query(..., alias="arrival_date", description="Check-in date (YYYY-MM-DD)"),
    departure_date: dt.date = Query(..., alias="departure_date", description="Check-out date (YYYY-MM-DD)"),
    adults: int = Query(2, ge=1, le=10, description="Number of adult guests"),
    children: int = Query(0, ge=0, le=10, description="Number of children"),
    client: OperaClient = Depends(get_opera_client),
):
    if departure_date <= arrival_date:
        raise HTTPException(status_code=400, detail="departure_date must be after arrival_date")

    try:
        return client.get_room_availability(
            arrival_date=arrival_date,
            departure_date=departure_date,
            adults=adults,
            children=children,
        )
    except OperaIntegrationError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from None


@router.get(
    "/reservations/{reservation_id}",
    response_model=RoomReservationDetail,
    summary="Read an existing Opera Cloud room reservation",
    description="Retrieves reservation details including stay dates, room type, occupants, and physical room allocation.",
)
def get_room_reservation(
    reservation_id: str,
    client: OperaClient = Depends(get_opera_client),
):
    try:
        return client.get_reservation(reservation_id)
    except OperaIntegrationError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from None


@router.post(
    "/reservations",
    response_model=ReservationWriteResult,
    summary="Create a room reservation in Opera Cloud",
    description="Creates a reservation for a verified booking intent with idempotency guard, safety checks, and post-write readback.",
)
def create_room_reservation(
    request: CreateReservationRequest,
    client: OperaClient = Depends(get_opera_client),
    booking_store: BookingIntentStore = Depends(get_booking_intent_store),
):
    # 1. Verify existence of booking intent
    try:
        intent = booking_store.get(request.booking_intent_id)
    except IntentNotFound:
        raise HTTPException(status_code=404, detail="Booking intent not found.")

    # 2. Idempotency check: if intent already links an Opera reservation, replay it
    existing_opera = intent.get("links", {}).get("opera_reservation")
    if existing_opera:
        return ReservationWriteResult(
            reservation_id=existing_opera["reservation_id"],
            confirmation_number=existing_opera.get("confirmation_number") or existing_opera["reservation_id"],
            status=existing_opera.get("status", "Reserved"),
            hotel_id=existing_opera.get("hotel_id", "ROSA"),
            arrival_date=dt.date.fromisoformat(existing_opera["arrival_date"]) if isinstance(existing_opera["arrival_date"], str) else existing_opera["arrival_date"],
            departure_date=dt.date.fromisoformat(existing_opera["departure_date"]) if isinstance(existing_opera["departure_date"], str) else existing_opera["departure_date"],
            room_category=existing_opera["room_category"],
            room_quantity=existing_opera.get("room_quantity", 1),
            room_group_id=existing_opera.get("room_group_id"),
            occupants=request.occupants,
            sharing_assignments=request.sharing_assignments,
            total_amount=float(existing_opera["total_amount"]),
            currency=existing_opera.get("currency", "EUR"),
            source="idempotent_replay",
            verified_via_readback=True,
            idempotency_replayed=True,
        )

    # 3. Execute reservation write to Opera (or verified sandbox)
    try:
        result = client.create_reservation(request)
    except OperaReadbackMismatchError as exc:
        try:
            booking_store.mark_recovery_required(
                intent_id=request.booking_intent_id,
                expected_revision=intent["revision"],
                detail_code="upstream_response_error",
            )
        except Exception:
            pass
        raise HTTPException(status_code=502, detail=f"Opera post-write readback mismatch: {exc.message}. Intent marked for recovery.")
    except OperaConflictError as exc:
        raise HTTPException(status_code=409, detail=exc.message)
    except OperaIntegrationError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message)

    # 4. Link authoritative Opera reservation to MongoDB booking intent
    opera_payload = OperaReservationPayload(
        reservation_id=result.reservation_id,
        confirmation_number=result.confirmation_number,
        hotel_id=result.hotel_id,
        room_category=result.room_category,
        room_quantity=result.room_quantity,
        room_group_id=result.room_group_id,
        arrival_date=result.arrival_date,
        departure_date=result.departure_date,
        total_amount=f"{result.total_amount:.2f}",
        currency=result.currency,
        status=result.status,
    )

    try:
        booking_store.link_opera_reservation(
            intent_id=request.booking_intent_id,
            expected_revision=intent["revision"],
            opera_payload=opera_payload,
        )
    except IntentConflict as exc:
        raise HTTPException(status_code=409, detail=f"Booking intent revision conflicted: {str(exc)}")

    return result


@router.put(
    "/reservations/{reservation_id}",
    response_model=ReservationWriteResult,
    summary="Amend an existing Opera Cloud room reservation",
    description="Updates stay dates, category, or occupants on an existing Opera reservation with conflict detection.",
)
def amend_room_reservation(
    reservation_id: str,
    request: UpdateReservationRequest,
    client: OperaClient = Depends(get_opera_client),
    booking_store: BookingIntentStore = Depends(get_booking_intent_store),
):
    if request.reservation_id != reservation_id:
        raise HTTPException(status_code=400, detail="Path reservation_id must match body reservation_id.")

    try:
        result = client.amend_reservation(request)
    except OperaConflictError as exc:
        raise HTTPException(status_code=409, detail=exc.message)
    except OperaReadbackMismatchError as exc:
        raise HTTPException(status_code=502, detail=exc.message)
    except OperaIntegrationError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message)

    # If linked booking intent exists, update its linked Opera payload
    try:
        intent = booking_store.get_by_reference(reservation_id)
        current_opera = intent.get("links", {}).get("opera_reservation")
        if current_opera:
            updated_payload = OperaReservationPayload(
                reservation_id=result.reservation_id,
                confirmation_number=result.confirmation_number,
                hotel_id=result.hotel_id,
                room_category=result.room_category,
                room_quantity=result.room_quantity,
                room_group_id=result.room_group_id,
                arrival_date=result.arrival_date,
                departure_date=result.departure_date,
                total_amount=f"{result.total_amount:.2f}",
                currency=result.currency,
                status=result.status,
            )
            booking_store.link_opera_reservation(
                intent_id=intent["booking_intent_id"],
                expected_revision=intent["revision"],
                opera_payload=updated_payload,
            )
    except (IntentNotFound, IntentConflict):
        pass

    return result
