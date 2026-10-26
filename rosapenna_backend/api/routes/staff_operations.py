"""Fail-closed, role-protected staff operations pending external OIDC setup."""

from fastapi import APIRouter, Depends, HTTPException
from pymongo.errors import PyMongoError

from rosapenna_backend.api.dependencies import (
    get_session_manager,
    get_staff_audit_log,
    get_teesheet_cache,
)
from rosapenna_backend.api.security import rate_limit, verify_api_key
from rosapenna_backend.api.staff_security import StaffPrincipal, require_staff_roles
from rosapenna_backend.core.logging import logger
from rosapenna_backend.services.booking_sessions import BookingSessionManager
from rosapenna_backend.services.staff_audit import StaffAuditLog
from rosapenna_backend.services.tee_sheets import TeesheetCache

router = APIRouter(dependencies=[Depends(rate_limit), Depends(verify_api_key)])


@router.get(
    "/sessions",
    tags=["staff operations"],
    summary="List active and recent booking sessions",
    description="Requires an external OIDC staff role; unavailable until the provider is configured.",
    responses={
        503: {"description": "External OIDC staff sign-in is not configured."},
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
    description="Requires an external OIDC staff role; unavailable until the provider is configured.",
    responses={
        503: {"description": "External OIDC staff sign-in is not configured."},
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
