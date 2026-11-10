"""API routes for Ticket M16 \u2014 Guest Action History Timeline.

Provides authenticated guests with a privacy-filtered, chronological view
of material actions, acceptances, requests, payments, and confirmations.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Path as ApiPath, Response, status

from rosapenna_backend.api.dependencies import (
    get_mmb_history_service,
    require_guest_booking_scope,
    require_mmb_enabled,
)
from rosapenna_backend.api.schemas.mmb_auth import GuestSessionPrincipal
from rosapenna_backend.api.schemas.mmb_history import GuestActionHistoryResponse
from rosapenna_backend.services.mmb_history import MmbHistoryError, MmbHistoryNotFoundError, MmbHistoryService

router = APIRouter(
    prefix="/mmb/history",
    tags=["mmb-history"],
    dependencies=[Depends(require_mmb_enabled)],
)


@router.get(
    "/{booking_reference}",
    response_model=GuestActionHistoryResponse,
    summary="Get privacy-filtered action history timeline for a booking",
    description="Requires an authenticated guest session matching the booking reference (IDOR prevention).",
    responses={
        401: {"description": "Missing or invalid guest session token."},
        403: {"description": "Forbidden \u2014 session does not match booking reference."},
        404: {"description": "Booking record not found."},
        503: {"description": "Manage My Booking is disabled."},
    },
)
def get_guest_action_history(
    response: Response,
    booking_reference: str = ApiPath(
        min_length=5,
        max_length=50,
        pattern=r"^[A-Za-z0-9\-]+$",
        description="Authoritative booking reference.",
    ),
    session: GuestSessionPrincipal = Depends(require_guest_booking_scope),
    service: MmbHistoryService = Depends(get_mmb_history_service),
) -> GuestActionHistoryResponse:
    """Retrieve time-ordered action events strictly filtered for the authenticated guest."""
    response.headers["Cache-Control"] = "no-store"
    try:
        return service.get_guest_history(booking_reference)
    except MmbHistoryNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=exc.message) from None
    except MmbHistoryError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from None
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Unable to load booking action history: {exc}",
        ) from None
