"""Staff API routes for Ticket N08 — Pre-Arrival Scheduling and Recovery."""

from __future__ import annotations

from typing import Annotated, Any, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Path, Request, Response, status

from rosapenna_backend.api.dependencies import get_pre_arrival_service, get_settings
from rosapenna_backend.api.schemas.pre_arrival import (
    StaffJobListResponse,
    StaffJobSummary,
    StaffReconcileRequest,
    StaffReconcileResponse,
    StaffRetryJobRequest,
    StaffRetryJobResponse,
)
from rosapenna_backend.api.staff_security import StaffPrincipal, require_staff_roles
from rosapenna_backend.config import Settings
from rosapenna_backend.services.pre_arrival import (
    PreArrivalConflictError,
    PreArrivalNotFoundError,
    PreArrivalPolicyError,
    PreArrivalService,
)

STAFF_ROLES = require_staff_roles("staff_operator", "developer")


def authorize_staff_pre_arrival_scope(
    principal: StaffPrincipal,
    booking_reference: str,
    action: str,
    test_run_id: Optional[str] = None,
) -> None:
    """Require exact booking and active test-run scope before staff access."""
    clean_ref = booking_reference.strip().upper()
    required_scopes = {f"booking:{clean_ref}:{action}"}
    if test_run_id:
        required_scopes.add(f"run:{test_run_id}:pre_arrival")
    if not required_scopes.issubset(principal.scopes):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Staff principal lacks required scopes: {sorted(required_scopes - principal.scopes)}",
            headers={"Cache-Control": "no-store"},
        )


def set_no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"


def check_staff_pre_arrival_gate(settings: Settings = Depends(get_settings)) -> None:
    """Enforce server gate setting before serving staff pre-arrival endpoints."""
    if not settings.pre_arrival_staff_api_enabled:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Staff pre-arrival routes are disabled by server policy.",
            headers={"Cache-Control": "no-store"},
        )


async def check_body_size_cap(request: Request) -> None:
    """Cap request body at 4 KiB to prevent denial-of-service payloads."""
    content_length = request.headers.get("content-length")
    if content_length and int(content_length) > 4096:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="Request body exceeds 4 KiB limit.",
            headers={"Cache-Control": "no-store"},
        )


router = APIRouter(
    prefix="/staff/pre-arrival",
    tags=["Staff Pre-Arrival Scheduling"],
    dependencies=[Depends(set_no_store), Depends(check_staff_pre_arrival_gate)],
)


@router.get(
    "/bookings/{booking_reference}/jobs",
    response_model=StaffJobListResponse,
    summary="List pre-arrival communication jobs for a confirmed booking",
)
def get_booking_jobs(
    booking_reference: str = Path(min_length=5, max_length=50, pattern=r"^[A-Za-z0-9\-]+$"),
    service: PreArrivalService = Depends(get_pre_arrival_service),
    principal: StaffPrincipal = Depends(STAFF_ROLES),
) -> StaffJobListResponse:
    """Scoped read-only status of pre-arrival jobs for a booking (no guest tokens or bodies exposed)."""
    authorize_staff_pre_arrival_scope(principal, booking_reference, "read", service.test_run_id)
    jobs = service.get_jobs_for_booking(booking_reference)
    summaries = [
        StaffJobSummary(
            reminder_id=j.reminder_id,
            booking_reference=j.booking_reference,
            revision_number=j.revision_number,
            reminder_type=j.reminder_type,
            title=j.title,
            scheduled_date=j.scheduled_date,
            due_at=j.due_at,
            status=j.status,
            reason_code=j.reason_code,
            attempt_count=j.attempt_count,
            next_attempt_at=j.next_attempt_at,
            created_at=j.created_at,
            updated_at=j.updated_at,
        )
        for j in jobs
    ]
    return StaffJobListResponse(
        booking_reference=booking_reference.upper(),
        jobs=summaries,
        total=len(summaries),
    )


@router.post(
    "/bookings/{booking_reference}/reconcile",
    response_model=StaffReconcileResponse,
    summary="Reconcile pre-arrival reminders for a booking with audit justification",
    dependencies=[Depends(check_body_size_cap)],
)
def reconcile_booking_reminders(
    payload: StaffReconcileRequest,
    booking_reference: str = Path(min_length=5, max_length=50, pattern=r"^[A-Za-z0-9\-]+$"),
    service: PreArrivalService = Depends(get_pre_arrival_service),
    principal: StaffPrincipal = Depends(STAFF_ROLES),
) -> StaffReconcileResponse:
    """Idempotently re-evaluate schedule against current evidence and policy (does not send mail)."""
    authorize_staff_pre_arrival_scope(principal, booking_reference, "reconcile", service.test_run_id)
    try:
        jobs = service.reconcile_booking(
            booking_reference=booking_reference,
            expected_revision=payload.expected_revision,
            actor=f"staff_{principal.subject}",
        )
    except PreArrivalNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
            headers={"Cache-Control": "no-store"},
        ) from None
    except PreArrivalConflictError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
            headers={"Cache-Control": "no-store"},
        ) from None
    except PreArrivalPolicyError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
            headers={"Cache-Control": "no-store"},
        ) from None

    summaries = [
        StaffJobSummary(
            reminder_id=j.reminder_id,
            booking_reference=j.booking_reference,
            revision_number=j.revision_number,
            reminder_type=j.reminder_type,
            title=j.title,
            scheduled_date=j.scheduled_date,
            due_at=j.due_at,
            status=j.status,
            reason_code=j.reason_code,
            attempt_count=j.attempt_count,
            next_attempt_at=j.next_attempt_at,
            created_at=j.created_at,
            updated_at=j.updated_at,
        )
        for j in jobs
    ]
    return StaffReconcileResponse(
        booking_reference=booking_reference.upper(),
        reconciled_jobs_count=len(summaries),
        jobs=summaries,
    )


@router.post(
    "/jobs/{reminder_id}/retry",
    response_model=StaffRetryJobResponse,
    summary="Safely retry a failed or reconciled unknown pre-arrival communication job",
    dependencies=[Depends(check_body_size_cap)],
)
def retry_job(
    payload: StaffRetryJobRequest,
    reminder_id: str = Path(min_length=5, max_length=100, pattern=r"^[A-Za-z0-9\-_]+$"),
    service: PreArrivalService = Depends(get_pre_arrival_service),
    principal: StaffPrincipal = Depends(STAFF_ROLES),
) -> StaffRetryJobResponse:
    """Re-enqueue a failed or reconciled job for dispatch. Rejects retrying successful submissions."""
    try:
        existing_job = service.get_job_by_id(reminder_id)
    except PreArrivalNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
            headers={"Cache-Control": "no-store"},
        ) from None
    authorize_staff_pre_arrival_scope(principal, existing_job.booking_reference, "retry", service.test_run_id)
    try:
        updated_job = service.retry_job(
            reminder_id=reminder_id,
            expected_revision=payload.expected_revision,
            reason=payload.reason,
            actor=f"staff_{principal.subject}",
            reconciliation_outcome=payload.reconciliation_outcome,
            attempt_id=payload.attempt_id,
        )
    except PreArrivalNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
            headers={"Cache-Control": "no-store"},
        ) from None
    except PreArrivalConflictError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
            headers={"Cache-Control": "no-store"},
        ) from None

    return StaffRetryJobResponse(
        reminder_id=updated_job.reminder_id,
        status=updated_job.status,
        message="Job successfully re-enqueued for dispatch.",
        next_attempt_at=updated_job.next_attempt_at,
    )
