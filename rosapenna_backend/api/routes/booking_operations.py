"""API routes for Ticket F14 — Safe Booking Operation Engine."""

from __future__ import annotations

from typing import List
from fastapi import APIRouter, Depends, HTTPException, status

from rosapenna_backend.api.dependencies import get_booking_operation_engine
from rosapenna_backend.api.schemas.booking_operations import (
    BookingOperationRecord,
    ExecuteBookingOperationRequest,
    ExecuteBookingOperationResponse,
)
from rosapenna_backend.api.security import COMMON_DEPS
from rosapenna_backend.services.booking_operations import BookingOperationEngine

router = APIRouter(
    prefix="/operations/bookings",
    tags=["booking-operations"],
    dependencies=COMMON_DEPS,
)


@router.post(
    "/commit",
    response_model=ExecuteBookingOperationResponse,
    status_code=status.HTTP_200_OK,
    summary="Safely commit or resume a booking or amendment operation",
    description="Coordinates payment verification, post-payment inventory and pricing revalidation, Opera/BRS writes with readback, and reconciliation alerts.",
)
def commit_booking_operation(
    request: ExecuteBookingOperationRequest,
    engine: BookingOperationEngine = Depends(get_booking_operation_engine),
):
    try:
        return engine.execute_operation(request)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Operation failed with unexpected error: {exc}",
        )


@router.get(
    "/{operation_id}",
    response_model=BookingOperationRecord,
    summary="Retrieve state and audit trail for a booking operation",
)
def get_operation_details(
    operation_id: str,
    engine: BookingOperationEngine = Depends(get_booking_operation_engine),
):
    record = engine.get_operation(operation_id)
    if not record:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Booking operation '{operation_id}' was not found.",
        )
    return record


@router.get(
    "/by-reference/{booking_reference}",
    response_model=List[BookingOperationRecord],
    summary="List all operations for a booking reference",
)
def list_operations_for_booking(
    booking_reference: str,
    engine: BookingOperationEngine = Depends(get_booking_operation_engine),
):
    return engine.get_operations_for_booking(booking_reference)
