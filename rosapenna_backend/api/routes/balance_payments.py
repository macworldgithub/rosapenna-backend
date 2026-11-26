"""Staff API routes for Ticket N09 — Balance Payments and Reminders."""

from __future__ import annotations

from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, Query, Response, status

from rosapenna_backend.api.dependencies import get_balance_payment_service, get_settings
from rosapenna_backend.api.schemas.balance_payments import (
    BalancePaymentRequestDoc,
    BalanceSummaryResponse,
    StaffBalanceAttentionItem,
    StaffCreateBalanceRequest,
    StaffReconcileBalanceRequest,
)
from rosapenna_backend.api.staff_security import StaffPrincipal, require_staff_roles
from rosapenna_backend.config import Settings
from rosapenna_backend.services.balance_payments import (
    BalanceAlreadyPaidError,
    BalanceConflictError,
    BalanceNotFoundError,
    BalancePaymentError,
    BalancePaymentService,
)

router = APIRouter(prefix="/staff/balance-payments", tags=["Staff Balance Payments"])


STAFF_ROLES = require_staff_roles("reservations", "finance", "manager", "admin")


def _check_staff_api_enabled(settings: Settings = Depends(get_settings)) -> None:
    if not settings.balance_staff_api_enabled:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Staff balance payment API is disabled by policy",
            headers={"Cache-Control": "no-store"},
        )


def authorize_staff_balance_scope(
    principal: StaffPrincipal,
    booking_reference: str,
    action: str,
    test_run_id: Optional[str] = None,
) -> None:
    """Require exact booking and active test-run scope before staff access."""
    clean_ref = booking_reference.strip().upper()
    required_scopes = {f"booking:{clean_ref}:{action}"}
    if test_run_id:
        required_scopes.add(f"run:{test_run_id}:balance")
    if not required_scopes.issubset(principal.scopes):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Staff principal lacks required scopes: {sorted(required_scopes - principal.scopes)}",
            headers={"Cache-Control": "no-store"},
        )


@router.get(
    "/bookings/{booking_reference}",
    response_model=BalanceSummaryResponse,
    summary="Get verified balance summary for a booking",
    dependencies=[Depends(_check_staff_api_enabled)],
)
def get_staff_balance_summary(
    booking_reference: str,
    response: Response,
    property_id: Optional[str] = Query(default=None),
    principal: StaffPrincipal = Depends(STAFF_ROLES),
    settings: Settings = Depends(get_settings),
    service: BalancePaymentService = Depends(get_balance_payment_service),
) -> BalanceSummaryResponse:
    response.headers["Cache-Control"] = "no-store"
    authorize_staff_balance_scope(principal, booking_reference, action="read", test_run_id=settings.balance_payment_test_run_id)
    try:
        return service.get_balance_summary(booking_reference=booking_reference, property_id=property_id)
    except BalanceNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.post(
    "/bookings/{booking_reference}/request",
    response_model=BalancePaymentRequestDoc,
    summary="Ensure a bound balance payment link request for an existing confirmed booking",
    dependencies=[Depends(_check_staff_api_enabled)],
)
def create_staff_balance_request(
    booking_reference: str,
    payload: StaffCreateBalanceRequest,
    response: Response,
    principal: StaffPrincipal = Depends(STAFF_ROLES),
    settings: Settings = Depends(get_settings),
    service: BalancePaymentService = Depends(get_balance_payment_service),
) -> BalancePaymentRequestDoc:
    response.headers["Cache-Control"] = "no-store"
    authorize_staff_balance_scope(principal, booking_reference, action="request", test_run_id=settings.balance_payment_test_run_id)
    try:
        return service.ensure_balance_request(
            booking_reference=booking_reference,
            expected_booking_revision=payload.expected_booking_revision,
            expected_ledger_version=payload.expected_ledger_version,
            command_id=payload.command_id,
            actor=principal.subject,
        )
    except BalanceNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except BalanceAlreadyPaidError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except BalanceConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.post(
    "/requests/{payment_id}/reconcile",
    response_model=BalancePaymentRequestDoc,
    summary="Reconcile an unknown, failed, or disputed balance payment request",
    dependencies=[Depends(_check_staff_api_enabled)],
)
def reconcile_staff_balance_request(
    payment_id: str,
    payload: StaffReconcileBalanceRequest,
    response: Response,
    principal: StaffPrincipal = Depends(STAFF_ROLES),
    settings: Settings = Depends(get_settings),
    service: BalancePaymentService = Depends(get_balance_payment_service),
) -> BalancePaymentRequestDoc:
    response.headers["Cache-Control"] = "no-store"
    clean_id = payment_id.strip()
    req_doc = service.requests_col.find_one({"payment_id": clean_id}) if service.requests_col is not None else None
    ref = req_doc.get("booking_reference") if req_doc else clean_id
    authorize_staff_balance_scope(principal, ref, action="reconcile", test_run_id=settings.balance_payment_test_run_id)
    try:
        return service.reconcile_request_state(
            payment_id=payment_id,
            action=payload.action,
            reason=payload.reason,
            actor=principal.subject,
        )
    except BalanceNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.get(
    "/attention",
    response_model=List[StaffBalanceAttentionItem],
    summary="List bookings and balance requests requiring staff attention",
    dependencies=[Depends(_check_staff_api_enabled)],
)
def list_staff_balance_attention(
    response: Response,
    property_id: Optional[str] = Query(default=None),
    limit: int = Query(default=50, ge=1, le=100),
    cursor: Optional[str] = Query(default=None),
    principal: StaffPrincipal = Depends(STAFF_ROLES),
    settings: Settings = Depends(get_settings),
    service: BalancePaymentService = Depends(get_balance_payment_service),
) -> List[StaffBalanceAttentionItem]:
    response.headers["Cache-Control"] = "no-store"
    required = {f"run:{settings.balance_payment_test_run_id}:balance"} if settings.balance_payment_test_run_id else set()
    required.add("staff:balance:attention")
    if not required.issubset(principal.scopes):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Staff principal lacks required scopes: {sorted(required - principal.scopes)}",
            headers={"Cache-Control": "no-store"},
        )
    if property_id is not None and property_id.strip().upper() != service.property_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Staff attention is limited to the configured property.",
            headers={"Cache-Control": "no-store"},
        )
    return service.get_attention_list(limit=limit, cursor=cursor)
