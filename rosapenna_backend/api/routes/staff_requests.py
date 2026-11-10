"""API routes for Ticket M14 — Staff Request Outcomes."""

from __future__ import annotations

from typing import Optional
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response, status

from rosapenna_backend.api.dependencies import get_staff_request_service
from rosapenna_backend.api.schemas.staff_requests import (
    StaffRequestItem,
    StaffRequestListResponse,
    StaffRequestTransitionCreate,
    StaffRequestTransitionResponse,
)
from rosapenna_backend.api.staff_security import StaffPrincipal, require_staff_roles
from rosapenna_backend.services.staff_requests import StaffRequestError, StaffRequestService

router = APIRouter(
    prefix="/staff/requests",
    tags=["staff-requests"],
)


@router.get(
    "",
    response_model=StaffRequestListResponse,
    summary="List special and cancellation requests for staff review",
    description="Requires an authorized staff token ('reservations_agent', 'reservations_manager', or 'site_admin').",
    responses={
        401: {"description": "Missing or invalid staff token."},
        403: {"description": "Forbidden — insufficient role."},
    },
)
def list_staff_requests(
    response: Response,
    status_filter: Optional[str] = Query(default=None, alias="status"),
    request_type_filter: Optional[str] = Query(default=None, alias="request_type"),
    booking_reference: Optional[str] = Query(default=None, alias="booking_reference"),
    principal: StaffPrincipal = Depends(
        require_staff_roles("reservations_agent", "reservations_manager", "site_admin")
    ),
    service: StaffRequestService = Depends(get_staff_request_service),
):
    response.headers["Cache-Control"] = "no-store"
    try:
        return service.list_requests(
            principal=principal,
            status_filter=status_filter,
            request_type_filter=request_type_filter,
            booking_reference=booking_reference,
        )
    except StaffRequestError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to list staff requests: {exc}",
        )


@router.get(
    "/{booking_reference}/{request_id}",
    response_model=StaffRequestItem,
    summary="Get detailed view of a guest request for staff review",
    description="Requires an authorized staff token.",
    responses={
        401: {"description": "Missing or invalid staff token."},
        404: {"description": "Booking or request not found."},
    },
)
def get_staff_request_details(
    booking_reference: str,
    request_id: str,
    response: Response,
    principal: StaffPrincipal = Depends(
        require_staff_roles("reservations_agent", "reservations_manager", "site_admin")
    ),
    service: StaffRequestService = Depends(get_staff_request_service),
):
    response.headers["Cache-Control"] = "no-store"
    try:
        return service.get_request_details(
            principal=principal,
            booking_reference=booking_reference,
            request_id=request_id,
        )
    except StaffRequestError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to retrieve request details: {exc}",
        )


@router.post(
    "/{booking_reference}/{request_id}/transition",
    response_model=StaffRequestTransitionResponse,
    summary="Record staff outcome transition for a special or cancellation request",
    description="Records needs-information, rejected, approved-awaiting-implementation, or completed with audit history and F12 guest dispatch.",
    responses={
        400: {"description": "Validation error or invalid Idempotency-Key."},
        401: {"description": "Missing or invalid staff token."},
        403: {"description": "Forbidden — insufficient role."},
        404: {"description": "Booking or request not found."},
        409: {"description": "Idempotency conflict or concurrent booking revision conflict."},
        422: {"description": "Missing prerequisite F14 execution, vendor readback, or cancellation authorization."},
    },
)
def transition_staff_request(
    booking_reference: str,
    request_id: str,
    payload: StaffRequestTransitionCreate,
    response: Response,
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
    principal: StaffPrincipal = Depends(
        require_staff_roles("reservations_agent", "reservations_manager", "site_admin")
    ),
    service: StaffRequestService = Depends(get_staff_request_service),
):
    response.headers["Cache-Control"] = "no-store"
    try:
        return service.transition_request(
            principal=principal,
            booking_reference=booking_reference,
            request_id=request_id,
            payload=payload,
            idempotency_key=idempotency_key,
        )
    except StaffRequestError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to transition request: {exc}",
        )


@router.post(
    "/{booking_reference}/{request_id}/retry-notification",
    response_model=StaffRequestTransitionResponse,
    summary="Retry F12 outcome notification to guest without altering state",
    description="Retries sending official email notification to lead guest after a prior delivery failure.",
    responses={
        400: {"description": "Invalid request state."},
        401: {"description": "Missing or invalid staff token."},
        404: {"description": "Booking or request not found."},
    },
)
def retry_staff_request_notification(
    booking_reference: str,
    request_id: str,
    response: Response,
    principal: StaffPrincipal = Depends(
        require_staff_roles("reservations_agent", "reservations_manager", "site_admin")
    ),
    service: StaffRequestService = Depends(get_staff_request_service),
):
    response.headers["Cache-Control"] = "no-store"
    try:
        return service.retry_notification(
            principal=principal,
            booking_reference=booking_reference,
            request_id=request_id,
        )
    except StaffRequestError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to retry notification: {exc}",
        )
