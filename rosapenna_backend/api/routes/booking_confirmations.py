"""Protected staff routes for Ticket N07 Booking Confirmations inspection and retries."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Response, status

from rosapenna_backend.api.dependencies import get_confirmation_coordinator, get_settings
from rosapenna_backend.api.schemas.communications import (
    StaffConfirmationRetryRequest,
    StaffConfirmationRetryResponse,
    StaffConfirmationStatusResponse,
)
from rosapenna_backend.api.staff_security import StaffPrincipal, require_staff_roles
from rosapenna_backend.config import Settings
from rosapenna_backend.services.booking_confirmations import (
    BookingConfirmationCoordinator,
    ConfirmationConflictError,
    ConfirmationNotFoundError,
)

def set_no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"


router = APIRouter(
    prefix="/staff/confirmations",
    tags=["staff_confirmations"],
    dependencies=[Depends(set_no_store)],
)

STAFF_ROLES = require_staff_roles("staff_operator", "developer")


def require_confirmation_role(principal: StaffPrincipal) -> None:
    """Require an allowed staff role even when an auth dependency is replaced in tests."""
    if not {"staff_operator", "developer"}.intersection(principal.roles):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="A staff operator role is required.",
            headers={"Cache-Control": "no-store"},
        )


def check_staff_api_gate(settings: Settings = Depends(get_settings)) -> None:
    """Enforce server setting gate before serving staff confirmation endpoints."""
    if not settings.confirmations_staff_api_enabled:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Staff confirmation routes are disabled by server policy.",
            headers={"Cache-Control": "no-store"},
        )


@router.get(
    "/{operation_id}",
    response_model=StaffConfirmationStatusResponse,
    summary="Inspect status of guest and hotel confirmation deliveries",
    dependencies=[Depends(check_staff_api_gate)],
)
def get_confirmation_status(
    operation_id: Annotated[str, Path(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")],
    response: Response,
    principal: StaffPrincipal = Depends(STAFF_ROLES),
    coordinator: BookingConfirmationCoordinator = Depends(get_confirmation_coordinator),
) -> StaffConfirmationStatusResponse:
    """Return masked, safe delivery status of a confirmed booking."""
    response.headers["Cache-Control"] = "no-store"
    require_confirmation_role(principal)
    try:
        coordinator.authorize_operation_scope(operation_id, principal.scopes, "read")
        return coordinator.get_status(operation_id)
    except ConfirmationNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=exc.message,
            headers={"Cache-Control": "no-store"},
        )
    except ConfirmationConflictError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=exc.message,
            headers={"Cache-Control": "no-store"},
        )


@router.post(
    "/{operation_id}/retry",
    response_model=StaffConfirmationRetryResponse,
    summary="Retry delivery to one failed role with operator audit reason",
    dependencies=[Depends(check_staff_api_gate)],
)
def retry_confirmation_delivery(
    operation_id: Annotated[str, Path(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")],
    request: StaffConfirmationRetryRequest,
    response: Response,
    principal: StaffPrincipal = Depends(STAFF_ROLES),
    coordinator: BookingConfirmationCoordinator = Depends(get_confirmation_coordinator),
) -> StaffConfirmationRetryResponse:
    """Execute audited retry for a failed recipient role."""
    response.headers["Cache-Control"] = "no-store"
    require_confirmation_role(principal)
    try:
        coordinator.authorize_operation_scope(operation_id, principal.scopes, "retry")
        delivery = coordinator.dispatch_recipient(
            operation_id=operation_id,
            recipient_role=request.recipient_role,
            expected_event_id=request.expected_event_id,
            expected_attempt_id=request.expected_attempt_id,
            reason=request.reason,
            actor=principal.subject,
        )
        return StaffConfirmationRetryResponse(
            operation_id=operation_id,
            recipient_role=request.recipient_role,
            attempt_id=delivery.attempt_id or "",
            status=delivery.status,
            message="Retry dispatched successfully",
        )
    except ConfirmationNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=exc.message,
            headers={"Cache-Control": "no-store"},
        )
    except ConfirmationConflictError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=exc.message,
            headers={"Cache-Control": "no-store"},
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
            headers={"Cache-Control": "no-store"},
        )
