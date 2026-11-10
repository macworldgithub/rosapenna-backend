"""API routes for Ticket M16 \u2014 Staff History, Exception Tracking, and Safe Recovery.

Provides authorized staff with complete audit history, actionable exception views
(paid-but-unconfirmed, partial writes, notification failures), and safe operation recovery retries.
"""

from __future__ import annotations

from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Path as ApiPath, Query, Response, status

from rosapenna_backend.api.dependencies import get_mmb_history_service
from rosapenna_backend.api.schemas.mmb_history import (
    RecoveryExceptionItem,
    StaffActionHistoryResponse,
    StaffRecoveryExceptionsListResponse,
    StaffRecoveryRetryRequest,
    StaffRecoveryRetryResponse,
)
from rosapenna_backend.api.staff_security import StaffPrincipal, require_staff_roles
from rosapenna_backend.services.mmb_history import MmbHistoryError, MmbHistoryNotFoundError, MmbHistoryService

router = APIRouter(
    tags=["staff-recovery"],
)


@router.get(
    "/staff/history/{booking_reference}",
    response_model=StaffActionHistoryResponse,
    summary="Get complete technical action history and audit log for a booking",
    description="Requires an authorized staff token ('reservations_agent', 'reservations_manager', or 'site_admin').",
    responses={
        401: {"description": "Missing or invalid staff token."},
        403: {"description": "Forbidden \u2014 insufficient role."},
        404: {"description": "Booking record not found."},
    },
)
def get_staff_action_history(
    response: Response,
    booking_reference: str = ApiPath(
        min_length=5,
        max_length=50,
        pattern=r"^[A-Za-z0-9\-]+$",
        description="Authoritative booking reference.",
    ),
    principal: StaffPrincipal = Depends(
        require_staff_roles("reservations_agent", "reservations_manager", "site_admin")
    ),
    service: MmbHistoryService = Depends(get_mmb_history_service),
) -> StaffActionHistoryResponse:
    """Retrieve time-ordered technical action events and staff audit trail for a booking."""
    response.headers["Cache-Control"] = "no-store"
    try:
        return service.get_staff_history(booking_reference, principal)
    except MmbHistoryNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=exc.message) from None
    except MmbHistoryError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from None
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Unable to load staff action history: {exc}",
        ) from None


@router.get(
    "/staff/recovery/exceptions",
    response_model=StaffRecoveryExceptionsListResponse,
    summary="List actionable MMB exceptions across bookings",
    description="Lists unconfirmed reconciliations, partial writes, unmatched payments, and notification failures.",
    responses={
        401: {"description": "Missing or invalid staff token."},
        403: {"description": "Forbidden \u2014 insufficient role."},
    },
)
def list_recovery_exceptions(
    response: Response,
    status_filter: Optional[str] = Query(default=None, alias="status"),
    principal: StaffPrincipal = Depends(
        require_staff_roles("reservations_agent", "reservations_manager", "site_admin")
    ),
    service: MmbHistoryService = Depends(get_mmb_history_service),
) -> StaffRecoveryExceptionsListResponse:
    """List all actionable exceptions across bookings requiring staff review or recovery."""
    response.headers["Cache-Control"] = "no-store"
    try:
        return service.list_recovery_exceptions(status_filter=status_filter, principal=principal)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Unable to list recovery exceptions: {exc}",
        ) from None


@router.post(
    "/staff/recovery/operations/{operation_id}/retry",
    response_model=StaffRecoveryRetryResponse,
    summary="Safely retry execution of a halted booking operation",
    description="Resumes existing operation ID after rechecking current versions and readbacks; prevents duplicates.",
    responses={
        401: {"description": "Missing or invalid staff token."},
        403: {"description": "Forbidden \u2014 insufficient role."},
        404: {"description": "Operation record not found."},
        409: {"description": "Conflict or not in unconfirmed_reconciliation stage."},
    },
)
def retry_recovery_operation(
    response: Response,
    request: StaffRecoveryRetryRequest,
    operation_id: str = ApiPath(min_length=1, max_length=100),
    principal: StaffPrincipal = Depends(
        require_staff_roles("reservations_agent", "reservations_manager", "site_admin")
    ),
    service: MmbHistoryService = Depends(get_mmb_history_service),
) -> StaffRecoveryRetryResponse:
    """Execute safe recovery retry on a halted booking operation maintaining operation identity."""
    response.headers["Cache-Control"] = "no-store"
    try:
        return service.retry_recovery_operation(
            operation_id=operation_id,
            request=request,
            principal=principal,
        )
    except MmbHistoryNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=exc.message) from None
    except MmbHistoryError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from None
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Recovery retry failed: {exc}",
        ) from None
