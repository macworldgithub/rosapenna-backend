"""FastAPI router for Ticket M10 — Room Occupants, Sharing, and Moves."""

import logging
from fastapi import APIRouter, Depends, HTTPException, Response

from rosapenna_backend.api.dependencies import (
    get_mmb_occupants_service,
    require_guest_booking_scope,
    require_mmb_enabled,
)
from rosapenna_backend.api.schemas.mmb_auth import GuestSessionPrincipal
from rosapenna_backend.api.schemas.mmb_occupants import (
    OccupantRosterResponse,
    OccupantUpdateRequest,
    OccupantUpdateResponse,
)
from rosapenna_backend.services.mmb_occupants import (
    ConcurrentEditConflictError,
    MmbOccupantsService,
    OccupantValidationError,
)

logger = logging.getLogger("booking_api")

router = APIRouter(
    prefix="/mmb/occupants",
    tags=["Manage My Booking — Room Occupants & Sharing (Ticket M10)"],
    dependencies=[Depends(require_mmb_enabled)],
)


@router.get(
    "/{booking_reference}",
    response_model=OccupantRosterResponse,
    summary="Get confirmed occupant roster and room assignments for a booking",
)
def get_occupant_roster(
    booking_reference: str,
    response: Response,
    session: GuestSessionPrincipal = Depends(require_guest_booking_scope),
    service: MmbOccupantsService = Depends(get_mmb_occupants_service),
) -> OccupantRosterResponse:
    """Retrieve the authoritative room occupant list, booked room groups, and sharing requests.

    Enforces:
    - M01 release gate check (HTTP 503 fail-closed when MMB is OFF).
    - M03 guest session authentication & IDOR booking scoping.
    - Zero physical room number allocation by guest controls.
    - Cache-Control: no-store header.
    """
    response.headers["Cache-Control"] = "no-store"
    try:
        return service.get_roster(booking_reference)
    except OccupantValidationError as exc:
        raise HTTPException(status_code=400, detail=exc.message) from exc
    except Exception as exc:
        logger.error("Failed to retrieve occupant roster for %s: %s", booking_reference, exc)
        raise HTTPException(status_code=500, detail="Unable to retrieve occupant roster.") from exc


@router.put(
    "/{booking_reference}",
    response_model=OccupantUpdateResponse,
    summary="Update room occupants, room moves, and sharing preferences",
)
def update_occupants(
    booking_reference: str,
    request: OccupantUpdateRequest,
    response: Response,
    session: GuestSessionPrincipal = Depends(require_guest_booking_scope),
    service: MmbOccupantsService = Depends(get_mmb_occupants_service),
) -> OccupantUpdateResponse:
    """Update room occupant names, sharing preferences, and room moves within booked rooms.

    Enforces:
    - M01 release gate check (HTTP 503 fail-closed when MMB is OFF).
    - M03 guest session authentication & IDOR booking scoping.
    - AC 1: Full name, booked room group, and effective date validation.
    - AC 2: Zero physical room number allocation (unassigned physical room notice).
    - AC 4: Authorized Opera reservation write updating occupant and sharing fields only.
    - AC 5: Readback verification before reporting confirmed; pending staff review on failure.
    - AC 6: Revision / read-before-write concurrent edit detection.
    - AC 7: Idempotency support.
    - AC 8: Occupant updates within booked rooms confirmed without changing inventory/rates.
    - Cache-Control: no-store header.
    """
    response.headers["Cache-Control"] = "no-store"
    if request.booking_reference.strip().upper() != booking_reference.strip().upper():
        raise HTTPException(
            status_code=400,
            detail="Payload booking reference does not match route path.",
        )

    try:
        return service.update_occupants(
            booking_reference=booking_reference,
            request=request,
            actor=f"guest_{session.guest_id or session.booking_reference}",
        )
    except ConcurrentEditConflictError as exc:
        raise HTTPException(status_code=409, detail=exc.message) from exc
    except OccupantValidationError as exc:
        raise HTTPException(status_code=400, detail=exc.message) from exc
    except Exception as exc:
        logger.error("Occupant update failed for booking %s: %s", booking_reference, exc)
        raise HTTPException(status_code=500, detail="Internal error updating room occupants.") from exc


@router.post(
    "/{booking_reference}/review-for-amendment",
    response_model=OccupantUpdateResponse,
    summary="Review and acknowledge occupant assignments for an amendment offer (AC 3)",
)
def review_amendment_occupants(
    booking_reference: str,
    request: OccupantUpdateRequest,
    response: Response,
    session: GuestSessionPrincipal = Depends(require_guest_booking_scope),
    service: MmbOccupantsService = Depends(get_mmb_occupants_service),
) -> OccupantUpdateResponse:
    """Acknowledge reviewed occupant assignments for an amendment offer before M09 commit.

    Produces an immutable OccupantReviewVersion linked to the amendment offer.
    """
    response.headers["Cache-Control"] = "no-store"
    if not request.amendment_offer_id:
        raise HTTPException(
            status_code=400,
            detail="amendment_offer_id is required for amendment occupant review.",
        )

    try:
        return service.review_amendment_occupants(
            booking_reference=booking_reference,
            offer_id=request.amendment_offer_id,
            request=request,
            actor=f"guest_{session.guest_id or session.booking_reference}",
        )
    except OccupantValidationError as exc:
        raise HTTPException(status_code=400, detail=exc.message) from exc
    except Exception as exc:
        logger.error("Amendment occupant review failed for booking %s: %s", booking_reference, exc)
        raise HTTPException(status_code=500, detail="Internal error reviewing occupants for amendment.") from exc
