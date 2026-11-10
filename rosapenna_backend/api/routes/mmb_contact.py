"""FastAPI router for Ticket M11 — Guest Phone Updates."""

import logging
from typing import Optional
from fastapi import APIRouter, Depends, Header, HTTPException, Response

from rosapenna_backend.api.dependencies import (
    get_mmb_contact_service,
    require_guest_booking_scope,
    require_mmb_enabled,
)
from rosapenna_backend.api.schemas.mmb_auth import GuestSessionPrincipal
from rosapenna_backend.api.schemas.mmb_contact import (
    GuestPhoneReadResponse,
    GuestPhoneUpdateRequest,
    GuestPhoneUpdateResponse,
)
from rosapenna_backend.services.mmb_contact import (
    MmbContactConflictError,
    MmbContactError,
    MmbContactIntegrationError,
    MmbContactNotFoundError,
    MmbContactService,
)

logger = logging.getLogger("booking_api")

router = APIRouter(
    prefix="/mmb/contact",
    tags=["Manage My Booking — Guest Contact Updates (Ticket M11)"],
    dependencies=[Depends(require_mmb_enabled)],
)


@router.get(
    "/{booking_reference}/phone",
    response_model=GuestPhoneReadResponse,
    summary="Get verified lead guest phone number and contact version",
)
def get_guest_phone(
    booking_reference: str,
    response: Response,
    session: GuestSessionPrincipal = Depends(require_guest_booking_scope),
    service: MmbContactService = Depends(get_mmb_contact_service),
) -> GuestPhoneReadResponse:
    """Retrieve the authoritative lead guest telephone for an authenticated booking.

    Enforces:
    - M01 release gate check (HTTP 503 fail-closed when MMB is OFF).
    - M03 guest session authentication & IDOR booking scoping.
    - Cache-Control: no-store header.
    """
    response.headers["Cache-Control"] = "no-store"
    try:
        return service.get_lead_phone(booking_reference)
    except MmbContactNotFoundError as exc:
        raise HTTPException(status_code=404, detail=exc.message) from exc
    except MmbContactError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
    except Exception as exc:
        logger.error("Failed to retrieve guest telephone for %s: %s", booking_reference, exc)
        raise HTTPException(status_code=500, detail="Unable to retrieve guest contact details.") from exc


@router.put(
    "/{booking_reference}/phone",
    response_model=GuestPhoneUpdateResponse,
    summary="Update lead guest phone number on reservation and booking linkage",
)
def update_guest_phone(
    booking_reference: str,
    request: GuestPhoneUpdateRequest,
    response: Response,
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
    session: GuestSessionPrincipal = Depends(require_guest_booking_scope),
    service: MmbContactService = Depends(get_mmb_contact_service),
) -> GuestPhoneUpdateResponse:
    """Update lead guest telephone number on reservation and verify with Opera readback.

    Enforces:
    - M01 release gate check (HTTP 503 fail-closed when MMB is OFF).
    - M03 guest session authentication & IDOR booking scoping.
    - Idempotency-Key header requirement and replay protection.
    - Optimistic concurrency control via expected_revision and contact_version.
    - Lead-guest only: rejects attempts to update ownership or email.
    - Narrow Opera phone update with authoritative post-write readback.
    - Cache-Control: no-store header.
    """
    response.headers["Cache-Control"] = "no-store"

    if not idempotency_key or len(idempotency_key.strip()) < 8:
        raise HTTPException(
            status_code=400,
            detail="A valid Idempotency-Key header (minimum 8 characters) is required for contact updates.",
        )

    try:
        actor = f"guest_{session.lead_guest_email or session.booking_reference}"
        return service.update_lead_phone(
            booking_reference=booking_reference,
            request=request,
            idempotency_key=idempotency_key,
            actor=actor,
        )
    except MmbContactConflictError as exc:
        raise HTTPException(status_code=409, detail=exc.message) from exc
    except MmbContactNotFoundError as exc:
        raise HTTPException(status_code=404, detail=exc.message) from exc
    except MmbContactIntegrationError as exc:
        raise HTTPException(status_code=502, detail=exc.message) from exc
    except MmbContactError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
    except Exception as exc:
        logger.error("Failed to update guest telephone for %s: %s", booking_reference, exc)
        raise HTTPException(status_code=500, detail="Internal error updating guest telephone.") from exc
