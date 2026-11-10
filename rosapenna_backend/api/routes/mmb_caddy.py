"""FastAPI router for Ticket M12 — Per-Round Caddy Request Updates."""

import logging
from typing import Optional
from fastapi import APIRouter, Depends, Header, HTTPException, Response

from rosapenna_backend.api.dependencies import (
    get_mmb_caddy_service,
    require_guest_booking_scope,
    require_mmb_enabled,
)
from rosapenna_backend.api.schemas.mmb_auth import GuestSessionPrincipal
from rosapenna_backend.api.schemas.mmb_caddy import (
    BookingCaddyRosterResponse,
    RoundCaddyUpdateRequest,
    RoundCaddyUpdateResponse,
)
from rosapenna_backend.services.mmb_caddy import (
    MmbCaddyConflictError,
    MmbCaddyError,
    MmbCaddyIntegrationError,
    MmbCaddyNotFoundError,
    MmbCaddyService,
)

logger = logging.getLogger("booking_api")

router = APIRouter(
    prefix="/mmb/caddy",
    tags=["Manage My Booking — Per-Round Caddy Request Updates (Ticket M12)"],
    dependencies=[Depends(require_mmb_enabled)],
)


@router.get(
    "/{booking_reference}",
    response_model=BookingCaddyRosterResponse,
    summary="Get confirmed golf rounds with active caddy request status and on-day fees",
)
def get_caddy_roster(
    booking_reference: str,
    response: Response,
    session: GuestSessionPrincipal = Depends(require_guest_booking_scope),
    service: MmbCaddyService = Depends(get_mmb_caddy_service),
) -> BookingCaddyRosterResponse:
    """Retrieve all golf rounds on an authenticated booking with caddy request roster.

    Enforces:
    - M01 release gate check (HTTP 503 fail-closed when MMB is OFF).
    - M03 guest session authentication & IDOR booking scoping.
    - Cache-Control: no-store header.
    """
    response.headers["Cache-Control"] = "no-store"
    try:
        return service.get_caddy_roster(booking_reference)
    except MmbCaddyNotFoundError as exc:
        raise HTTPException(status_code=404, detail=exc.message) from exc
    except MmbCaddyError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
    except Exception as exc:
        logger.error("Failed to retrieve caddy roster for %s: %s", booking_reference, exc)
        raise HTTPException(status_code=500, detail="Unable to retrieve caddy roster.") from exc


@router.put(
    "/{booking_reference}/rounds/{round_id}",
    response_model=RoundCaddyUpdateResponse,
    summary="Update caddy request for a specific confirmed golf round",
)
def update_round_caddy(
    booking_reference: str,
    round_id: str,
    request: RoundCaddyUpdateRequest,
    response: Response,
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
    session: GuestSessionPrincipal = Depends(require_guest_booking_scope),
    service: MmbCaddyService = Depends(get_mmb_caddy_service),
) -> RoundCaddyUpdateResponse:
    """Atomically update caddy request for a confirmed round and synchronize with BRS.

    Enforces:
    - M01 release gate check (HTTP 503 fail-closed when MMB is OFF).
    - M03 guest session authentication & IDOR booking scoping.
    - Idempotency-Key header requirement and replay protection.
    - Round & player scope: caddies_requested <= round.players (max 4).
    - Concurrency control via expected_revision.
    - Zero online charge: caddies are strictly requests payable on the day (€90/bag).
    - Narrow BRS write & post-write readback verification.
    - Cache-Control: no-store header.
    """
    response.headers["Cache-Control"] = "no-store"

    if not idempotency_key or len(idempotency_key.strip()) < 8:
        raise HTTPException(
            status_code=400,
            detail="A valid Idempotency-Key header (minimum 8 characters) is required for caddy updates.",
        )

    try:
        actor = f"guest_{session.lead_guest_email or session.booking_reference}"
        return service.update_round_caddy(
            booking_reference=booking_reference,
            round_id=round_id,
            request=request,
            idempotency_key=idempotency_key,
            actor=actor,
        )
    except MmbCaddyConflictError as exc:
        raise HTTPException(status_code=409, detail=exc.message) from exc
    except MmbCaddyNotFoundError as exc:
        raise HTTPException(status_code=404, detail=exc.message) from exc
    except MmbCaddyIntegrationError as exc:
        raise HTTPException(status_code=502, detail=exc.message) from exc
    except MmbCaddyError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
    except Exception as exc:
        logger.error("Failed to update round caddy for %s round %s: %s", booking_reference, round_id, exc)
        raise HTTPException(status_code=500, detail="Internal error updating caddy request.") from exc
