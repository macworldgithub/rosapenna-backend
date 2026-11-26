"""Role-protected staff operations with audit logging."""

from datetime import date, time
from threading import Lock
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from playwright.sync_api import Error as PlaywrightError
from pydantic import BaseModel, ConfigDict, Field
from pymongo.errors import PyMongoError

from rosapenna_backend.api.dependencies import (
    get_session_manager,
    get_settings,
    get_staff_audit_log,
    get_teesheet_cache,
)
from rosapenna_backend.api.security import rate_limit, verify_api_key
from rosapenna_backend.api.staff_security import StaffPrincipal, require_staff_roles
from rosapenna_backend.config import Settings
from rosapenna_backend.core.logging import logger
from rosapenna_backend.integrations.brs.booking_client import BrsConflictError
from rosapenna_backend.integrations.brs.staff_booking_ui import preview_staff_tee_time
from rosapenna_backend.services.booking_sessions import BookingSessionManager
from rosapenna_backend.services.staff_audit import StaffAuditLog
from rosapenna_backend.services.tee_sheets import TeesheetCache

router = APIRouter(dependencies=[Depends(rate_limit), Depends(verify_api_key)])


@router.get(
    "/sessions",
    tags=["staff operations"],
    summary="List active and recent booking sessions",
    description="Requires an authorized staff token; records an immutable audit log entry.",
    responses={
        401: {"description": "Missing or invalid staff token."},
        503: {"description": "Staff authentication is not configured."},
    },
)
def list_sessions(
    principal: StaffPrincipal = Depends(require_staff_roles("site_admin")),
    audit: StaffAuditLog = Depends(get_staff_audit_log),
    manager: BookingSessionManager = Depends(get_session_manager),
):
    sessions = manager.list_sessions()
    audit.record(principal, "sessions.list", "booking_sessions", "success")
    return sessions


@router.post(
    "/teesheets/refresh",
    tags=["staff operations"],
    summary="Run the daily BRS tee-sheet refresh",
    description="Requires an authorized staff token; records an immutable audit log entry.",
    responses={
        401: {"description": "Missing or invalid staff token."},
        503: {"description": "Staff authentication is not configured."},
    },
)
def refresh_teesheets(
    retry_failed: bool = False,
    principal: StaffPrincipal = Depends(require_staff_roles("site_admin")),
    audit: StaffAuditLog = Depends(get_staff_audit_log),
    cache: TeesheetCache = Depends(get_teesheet_cache),
):
    audit.record(principal, "teesheets.refresh", "tee_sheet_cache", "requested")
    try:
        result = cache.refresh(retry_failed=retry_failed)
    except PyMongoError:
        try:
            audit.record(principal, "teesheets.refresh", "tee_sheet_cache", "failed")
        except Exception as exc:
            logger.warning("Staff refresh failure audit event could not be recorded (%s)", type(exc).__name__)
        raise
    except Exception as exc:
        try:
            audit.record(principal, "teesheets.refresh", "tee_sheet_cache", "failed")
        except Exception as audit_exc:
            logger.warning("Staff refresh failure audit event could not be recorded (%s)", type(audit_exc).__name__)
        logger.error("Staff tee-sheet refresh failed (%s)", type(exc).__name__)
        raise HTTPException(502, "The tee-sheet refresh could not be started") from None

    try:
        audit.record(principal, "teesheets.refresh", "tee_sheet_cache", "responded")
    except Exception as exc:
        # The durable request event was recorded before the side effect.
        logger.warning("Staff refresh response audit event could not be recorded (%s)", type(exc).__name__)
    return result


class StaffBrsPreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    course_id: Literal[1, 3]
    day: date
    tee_time: str = Field(pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")


_preview_lock = Lock()


@router.post("/staff/brs-preview", tags=["staff operations"])
def run_brs_preview(
    body: StaffBrsPreviewRequest,
    principal: StaffPrincipal = Depends(require_staff_roles("site_admin")),
    settings: Settings = Depends(get_settings),
    audit: StaffAuditLog = Depends(get_staff_audit_log),
):
    """Record a staff simulation and inspect one live row without any booking save."""
    if principal.issuer == "local_dev":
        raise HTTPException(401, "Staff authorization token required. Disable development auth bypass.")
    storage_state = settings.staff_brs_preview_storage_state
    if (
        settings.payment_mode.strip().lower() != "disabled"
        or settings.simulation_enabled
        or storage_state is None
        or not storage_state.is_absolute()
        or not storage_state.is_file()
    ):
        raise HTTPException(503, "The staff preview requires disabled payments and configured BRS access.")
    if not _preview_lock.acquire(blocking=False):
        raise HTTPException(429, "Another BRS preview is running. Try again in a moment.")

    resource = f"course:{body.course_id}/date:{body.day.isoformat()}/time:{body.tee_time}"
    try:
        audit.record(principal, "brs.preview", resource, "simulation_requested")
        try:
            add_enabled = preview_staff_tee_time(
                storage_state, course_id=body.course_id, day=body.day,
                at=time.fromisoformat(body.tee_time),
            )
        except (BrsConflictError, PlaywrightError, OSError, ValueError):
            audit.record(principal, "brs.preview", resource, "unverified")
            raise HTTPException(502, "The BRS row could not be verified. Check the staff session and tee time.") from None
        audit.record(principal, "brs.preview", resource, "add_enabled" if add_enabled else "add_unavailable")
        return {"payment": "simulated", "brs_row_checked": True, "add_enabled": add_enabled, "booking_saved": False}
    finally:
        _preview_lock.release()
