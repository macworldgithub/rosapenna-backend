"""API routes for Ticket M05 & M06 — Live Amendment Options and Cost/Terms Review."""

from fastapi import APIRouter, Depends, HTTPException, Response, status

from rosapenna_backend.api.dependencies import (
    get_booking_operation_engine,
    get_current_guest_session,
    get_mmb_amendment_service,
    require_guest_booking_scope,
    require_mmb_enabled,
)
from rosapenna_backend.api.schemas.mmb_amendments import (
    AmendmentPaymentInitiateRequest,
    AmendmentPaymentInitiateResponse,
    AmendmentRevalidationRequest,
    AmendmentRevalidationResponse,
    AmendmentOfferAcceptRequest,
    AmendmentOfferCreateRequest,
    AmendmentOfferSnapshot,
    MmbAmendmentCommitRequest,
    MmbAmendmentCommitResponse,
    MmbAmendmentSearchRequest,
    MmbAmendmentSearchResponse,
)
from rosapenna_backend.api.schemas.mmb_auth import GuestSessionPrincipal
from rosapenna_backend.api.security import rate_limit
from rosapenna_backend.services.booking_linkage import LinkageNotFoundError
from rosapenna_backend.services.mmb_amendments import MmbAmendmentService

router = APIRouter(prefix="/mmb/amendments", tags=["mmb amendments"])


@router.post(
    "/search",
    response_model=MmbAmendmentSearchResponse,
    summary="Search live Stay & Play amendment alternatives for an authenticated booking",
    description=(
        "Evaluates candidate room availability and BRS tee sheets across all three courses. "
        "Credits this booking's own held resources exactly once without double counting. "
        "Strictly read-only; reserves no inventory (reserves_inventory=False)."
    ),
    dependencies=[Depends(rate_limit), Depends(require_mmb_enabled)],
)
def search_live_amendments(
    request: MmbAmendmentSearchRequest,
    response: Response,
    session: GuestSessionPrincipal = Depends(get_current_guest_session),
    amendment_service: MmbAmendmentService = Depends(get_mmb_amendment_service),
) -> MmbAmendmentSearchResponse:
    require_guest_booking_scope(request.booking_reference, session)
    response.headers["Cache-Control"] = "no-store"

    try:
        return amendment_service.search_amendment_options(request)
    except LinkageNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Booking reference '{request.booking_reference}' was not found.",
        )


@router.post(
    "/offer",
    response_model=AmendmentOfferSnapshot,
    summary="Create an immutable amendment offer snapshot for cost and terms review",
    description=(
        "Captures candidate stay and golf alternatives into an immutable snapshot. "
        "Reconciles F11 verified payment ledger, computes exact delta, revised balance, "
        "and strictly enforces 42-day balance due date and occupant review gates."
    ),
    dependencies=[Depends(rate_limit), Depends(require_mmb_enabled)],
)
def create_amendment_offer(
    request: AmendmentOfferCreateRequest,
    response: Response,
    session: GuestSessionPrincipal = Depends(get_current_guest_session),
    amendment_service: MmbAmendmentService = Depends(get_mmb_amendment_service),
) -> AmendmentOfferSnapshot:
    require_guest_booking_scope(request.booking_reference, session)
    response.headers["Cache-Control"] = "no-store"

    try:
        return amendment_service.create_amendment_offer(request)
    except LinkageNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Booking reference '{request.booking_reference}' was not found.",
        )


@router.get(
    "/offer/{offer_id}",
    response_model=AmendmentOfferSnapshot,
    summary="Fetch an existing amendment offer snapshot",
    dependencies=[Depends(rate_limit), Depends(require_mmb_enabled)],
)
def get_amendment_offer(
    offer_id: str,
    response: Response,
    session: GuestSessionPrincipal = Depends(get_current_guest_session),
    amendment_service: MmbAmendmentService = Depends(get_mmb_amendment_service),
) -> AmendmentOfferSnapshot:
    response.headers["Cache-Control"] = "no-store"

    try:
        return amendment_service.get_amendment_offer(offer_id, session.booking_reference)
    except LinkageNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Amendment offer '{offer_id}' was not found.",
        )


@router.post(
    "/offer/accept",
    response_model=AmendmentOfferSnapshot,
    summary="Accept an amendment offer with explicit guest consent and occupant review acknowledgment",
    dependencies=[Depends(rate_limit), Depends(require_mmb_enabled)],
)
def accept_amendment_offer(
    request: AmendmentOfferAcceptRequest,
    response: Response,
    session: GuestSessionPrincipal = Depends(get_current_guest_session),
    amendment_service: MmbAmendmentService = Depends(get_mmb_amendment_service),
) -> AmendmentOfferSnapshot:
    require_guest_booking_scope(request.booking_reference, session)
    response.headers["Cache-Control"] = "no-store"

    try:
        return amendment_service.accept_amendment_offer(request)
    except LinkageNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Offer or booking was not found.",
        )
    except ValueError as exc:
        msg = str(exc)
        if "modified" in msg or "invalidated" in msg:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=msg)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=msg)


@router.post(
    "/revalidate",
    response_model=AmendmentRevalidationResponse,
    summary="Atomically revalidate an accepted amendment offer before payment link creation or commit",
    description=(
        "Rechecks release gate, active guest session, booking revision, room availability, "
        "BRS tee times, rates, and F11 payment ledger. If any component changed, returns a "
        "refreshed offer requiring explicit re-acceptance. Reserves zero inventory."
    ),
    dependencies=[Depends(rate_limit), Depends(require_mmb_enabled)],
)
def revalidate_amendment_offer(
    request: AmendmentRevalidationRequest,
    response: Response,
    session: GuestSessionPrincipal = Depends(get_current_guest_session),
    amendment_service: MmbAmendmentService = Depends(get_mmb_amendment_service),
) -> AmendmentRevalidationResponse:
    require_guest_booking_scope(request.booking_reference, session)
    response.headers["Cache-Control"] = "no-store"

    return amendment_service.revalidate_amendment_offer(request)

@router.post(
    "/settlement/initiate",
    response_model=AmendmentPaymentInitiateResponse,
    summary="Initiate payment or credit settlement for a revalidated amendment offer (Ticket M08)",
    description=(
        "Handles the payment position from the revalidated offer: creates scoped Prommt payment link "
        "if amount is due, authorizes zero-due commit path if balance is zero, or records resort credit "
        "without automated refund. Client input cannot alter financial amounts or currency."
    ),
    dependencies=[Depends(rate_limit), Depends(require_mmb_enabled)],
)
def initiate_amendment_settlement(
    request: AmendmentPaymentInitiateRequest,
    response: Response,
    session: GuestSessionPrincipal = Depends(get_current_guest_session),
    amendment_service: MmbAmendmentService = Depends(get_mmb_amendment_service),
) -> AmendmentPaymentInitiateResponse:
    require_guest_booking_scope(request.booking_reference, session)
    response.headers["Cache-Control"] = "no-store"

    try:
        return amendment_service.initiate_amendment_settlement(request)
    except LinkageNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Booking reference '{request.booking_reference}' was not found.",
        )
    except ValueError as exc:
        msg = str(exc)
        if "not found" in msg.lower():
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=msg)
        if "revision mismatch" in msg.lower() or "modified" in msg.lower() or "expired" in msg.lower():
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=msg)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=msg)


@router.get(
    "/settlement/payment/{payment_id}",
    summary="Check status of an amendment payment (Ticket M08)",
    dependencies=[Depends(rate_limit), Depends(require_mmb_enabled)],
)
def get_amendment_payment_status(
    payment_id: str,
    response: Response,
    session: GuestSessionPrincipal = Depends(get_current_guest_session),
    amendment_service: MmbAmendmentService = Depends(get_mmb_amendment_service),
):
    response.headers["Cache-Control"] = "no-store"
    payment = amendment_service.get_amendment_payment(payment_id, session.booking_reference)
    if not payment:
        raise HTTPException(status_code=404, detail="Payment record not found.")
    return payment


@router.post(
    "/commit",
    response_model=MmbAmendmentCommitResponse,
    summary="Commit confirmed amendment through coordinated vendor writes with readback (Ticket M09)",
    description=(
        "Executes Opera stay amendment and replacement BRS round writes. "
        "Strictly verifies post-write readbacks before releasing original rounds. "
        "On any failure, executes supported compensation or halts to unconfirmed reconciliation "
        "alerting staff, leaving original booking intact for the guest."
    ),
    dependencies=[Depends(rate_limit), Depends(require_mmb_enabled)],
)
def commit_amendment(
    request: MmbAmendmentCommitRequest,
    response: Response,
    session: GuestSessionPrincipal = Depends(get_current_guest_session),
    amendment_service: MmbAmendmentService = Depends(get_mmb_amendment_service),
    engine = Depends(get_booking_operation_engine),
) -> MmbAmendmentCommitResponse:
    require_guest_booking_scope(request.booking_reference, session)
    response.headers["Cache-Control"] = "no-store"

    try:
        return amendment_service.commit_amendment(request, engine=engine)
    except LinkageNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Booking reference '{request.booking_reference}' was not found.",
        )
    except ValueError as exc:
        msg = str(exc)
        if "not found" in msg.lower():
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=msg)
        if "revision mismatch" in msg.lower() or "modified" in msg.lower() or "expired" in msg.lower() or "invalidated" in msg.lower():
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=msg)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=msg)

