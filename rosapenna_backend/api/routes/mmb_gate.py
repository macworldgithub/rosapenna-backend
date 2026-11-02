"""API routes for Manage My Booking (MMB) release control and feature gating (Ticket M01)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from rosapenna_backend.api.dependencies import (
    get_mmb_gate_service,
    get_staff_audit_log,
    require_mmb_enabled,
)
from rosapenna_backend.api.schemas.mmb_gate import (
    MmbStatusResponse,
    StaffMmbGateStatusResponse,
    StaffMmbGateUpdateRequest,
)
from rosapenna_backend.api.security import COMMON_DEPS
from rosapenna_backend.api.staff_security import StaffPrincipal, require_staff_roles
from rosapenna_backend.services.mmb_gate import MmbGateService
from rosapenna_backend.services.staff_audit import StaffAuditLog

router = APIRouter(tags=["manage my booking release control"])


@router.get(
    "/mmb/status",
    response_model=MmbStatusResponse,
    summary="Get public Manage My Booking portal status",
    description="Returns whether the guest Manage My Booking self-service portal is currently enabled or disabled.",
    dependencies=COMMON_DEPS,
)
def get_mmb_status(
    gate_service: MmbGateService = Depends(get_mmb_gate_service),
):
    status = gate_service.get_public_status()
    return MmbStatusResponse(**status)


@router.get(
    "/staff/mmb/gate",
    response_model=StaffMmbGateStatusResponse,
    summary="Get administrative MMB release gate status and audit history",
    description="Requires an authorized staff token ('site_admin' or 'reservations_manager').",
    dependencies=COMMON_DEPS,
)
def get_staff_mmb_gate(
    principal: StaffPrincipal = Depends(require_staff_roles("site_admin", "reservations_manager")),
    gate_service: MmbGateService = Depends(get_mmb_gate_service),
):
    status = gate_service.get_staff_status()
    return StaffMmbGateStatusResponse(**status)


@router.post(
    "/staff/mmb/gate",
    response_model=StaffMmbGateStatusResponse,
    summary="Update Manage My Booking release gate state",
    description="Requires written request reference, reason, and authorized staff token ('site_admin' or 'reservations_manager').",
    dependencies=COMMON_DEPS,
)
def update_staff_mmb_gate(
    request: StaffMmbGateUpdateRequest,
    principal: StaffPrincipal = Depends(require_staff_roles("site_admin", "reservations_manager")),
    gate_service: MmbGateService = Depends(get_mmb_gate_service),
    audit: StaffAuditLog = Depends(get_staff_audit_log),
):
    try:
        updated = gate_service.update_gate(
            request=request,
            principal=principal,
            audit=audit,
        )
        return StaffMmbGateStatusResponse(**updated)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None


@router.get(
    "/mmb/booking/{booking_ref}",
    summary="Protected guest booking inspection endpoint (requires MMB active)",
    description="Demonstrates server-side MMB gate enforcement. Rejects with HTTP 503 when MMB is disabled.",
    dependencies=[*COMMON_DEPS, Depends(require_mmb_enabled)],
)
def get_guest_mmb_booking(
    booking_ref: str,
):
    # This route is safely gated behind require_mmb_enabled.
    # When MMB is enabled, downstream tickets (M03/M04) will supply full verified booking lookup.
    return {
        "booking_reference": booking_ref,
        "mmb_access": "granted",
        "status": "active",
    }
