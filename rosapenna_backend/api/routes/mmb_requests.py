"""FastAPI router for Ticket M13 — Special and Cancellation Requests."""

import logging
from typing import Optional
from fastapi import APIRouter, Depends, Header, HTTPException, Response

from rosapenna_backend.api.dependencies import (
    get_mmb_request_service,
    require_guest_booking_scope,
    require_mmb_enabled,
)
from rosapenna_backend.api.schemas.mmb_auth import GuestSessionPrincipal
from rosapenna_backend.api.schemas.mmb_requests import (
    GuestRequestsListResponse,
    GuestSpecialRequestCreate,
    GuestSpecialRequestResponse,
)
from rosapenna_backend.services.mmb_requests import (
    MmbRequestConflictError,
    MmbRequestError,
    MmbRequestNotFoundError,
    MmbRequestService,
)

logger = logging.getLogger("booking_api")

router = APIRouter(
    prefix="/mmb/requests",
    tags=["Manage My Booking — Special and Cancellation Requests (Ticket M13)"],
    dependencies=[Depends(require_mmb_enabled)],
)


@router.get(
    "/{booking_reference}",
    response_model=GuestRequestsListResponse,
    summary="Get all submitted special and cancellation requests for an authenticated booking",
)
def get_guest_requests(
    booking_reference: str,
    response: Response,
    session: GuestSessionPrincipal = Depends(require_guest_booking_scope),
    service: MmbRequestService = Depends(get_mmb_request_service),
) -> GuestRequestsListResponse:
    """Retrieve all submitted requests with current review status.

    Enforces:
    - M01 release gate check (HTTP 503 fail-closed when MMB is OFF).
    - M03 guest session authentication & IDOR booking scoping.
    - Cache-Control: no-store header.
    """
    response.headers["Cache-Control"] = "no-store"
    try:
        return service.get_guest_requests(booking_reference)
    except MmbRequestNotFoundError as exc:
        raise HTTPException(status_code=404, detail=exc.message) from exc
    except MmbRequestError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
    except Exception as exc:
        logger.error("Failed to retrieve requests for %s: %s", booking_reference, exc)
        raise HTTPException(status_code=500, detail="Unable to retrieve guest requests.") from exc


@router.post(
    "/{booking_reference}",
    response_model=GuestSpecialRequestResponse,
    summary="Submit a special request or cancellation inquiry without mutating confirmed inventory",
)
def submit_guest_request(
    booking_reference: str,
    request: GuestSpecialRequestCreate,
    response: Response,
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
    session: GuestSessionPrincipal = Depends(require_guest_booking_scope),
    service: MmbRequestService = Depends(get_mmb_request_service),
) -> GuestSpecialRequestResponse:
    """Atomically record a special request or cancellation inquiry.

    Enforces:
    - M01 release gate check (HTTP 503 fail-closed when MMB is OFF).
    - M03 guest session authentication & IDOR booking scoping.
    - Idempotency-Key header requirement and replay protection.
    - Zero mutation to rooms, golf rounds, payment ledger, or inventory.
    - Cancellation inquiry does NOT trigger automatic cancellation or refund.
    - Sanitized F12 reservations email acknowledgement dispatched.
    - Cache-Control: no-store header.
    """
    response.headers["Cache-Control"] = "no-store"

    if not idempotency_key or len(idempotency_key.strip()) < 8:
        raise HTTPException(
            status_code=400,
            detail="A valid Idempotency-Key header (minimum 8 characters) is required for request submission.",
        )

    try:
        actor = f"guest_{session.lead_guest_email or session.booking_reference}"
        return service.submit_request(
            booking_reference=booking_reference,
            request=request,
            idempotency_key=idempotency_key,
            actor=actor,
        )
    except MmbRequestConflictError as exc:
        raise HTTPException(status_code=409, detail=exc.message) from exc
    except MmbRequestNotFoundError as exc:
        raise HTTPException(status_code=404, detail=exc.message) from exc
    except MmbRequestError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
    except Exception as exc:
        logger.error("Failed to submit request for %s: %s", booking_reference, exc)
        raise HTTPException(status_code=500, detail="Internal error recording guest request.") from exc
