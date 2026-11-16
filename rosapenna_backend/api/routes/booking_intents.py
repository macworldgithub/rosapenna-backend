"""Protected API routes for persisted booking-intent drafts."""

from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Path as ApiPath
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from rosapenna_backend.api.dependencies import (
    get_booking_intent_store,
    get_booking_operation_engine,
    get_package_pricing_service,
)
from rosapenna_backend.api.schemas.booking_intents import (
    BookingIntentAction,
    BookingIntentCommitRequest,
    BookingIntentCreate,
    BookingIntentReacceptRequest,
)
from rosapenna_backend.api.schemas.booking_operations import ExecuteBookingOperationRequest
from rosapenna_backend.api.security import COMMON_DEPS
from rosapenna_backend.services.booking_intents import BookingIntentStore, IntentConflict, IntentNotFound
from rosapenna_backend.services.booking_operations import BookingOperationEngine
from rosapenna_backend.services.package_pricing import PackagePricingService, compute_quote_hash

router = APIRouter(dependencies=COMMON_DEPS)


@router.post(
    "/booking-intents", tags=["booking intents"],
    summary="Create or retrieve an idempotent online booking draft",
    responses={401: {"description": "Missing or invalid API key."}, 409: {"description": "Idempotency conflict."}},
)
def create_booking_intent(
    request: BookingIntentCreate,
    idempotency_key: UUID = Header(..., alias="Idempotency-Key"),
    store: BookingIntentStore = Depends(get_booking_intent_store),
):
    try:
        record, created = store.create(idempotency_key, request)
    except IntentConflict as exc:
        raise HTTPException(409, str(exc)) from None
    return JSONResponse(status_code=201 if created else 200, content=jsonable_encoder({**record, "created": created}))


@router.get(
    "/booking-intents/{intent_id}", tags=["booking intents"],
    summary="Read one saved booking draft and its action history",
    responses={401: {"description": "Missing or invalid API key."}, 404: {"description": "Booking intent not found."}},
)
def get_booking_intent(
    intent_id: str = ApiPath(pattern=r"^bki_[0-9a-f]{32}$"),
    store: BookingIntentStore = Depends(get_booking_intent_store),
):
    try:
        return store.get(intent_id)
    except IntentNotFound:
        raise HTTPException(404, "Booking intent was not found") from None


@router.get(
    "/booking-intents/by-reference/{reference}", tags=["booking intents"],
    summary="Lookup unified booking by booking reference or Opera reservation ID",
    responses={401: {"description": "Missing or invalid API key."}, 404: {"description": "Booking record not found."}},
)
def get_booking_by_reference(
    reference: str,
    store: BookingIntentStore = Depends(get_booking_intent_store),
):
    try:
        return store.get_by_reference(reference)
    except IntentNotFound:
        raise HTTPException(404, "Booking record was not found") from None


@router.post(
    "/booking-intents/{intent_id}/actions", tags=["booking intents"],
    summary="Append an evidence-backed booking draft action with revision checking",
    responses={
        401: {"description": "Missing or invalid API key."},
        404: {"description": "Booking intent not found."},
        409: {"description": "Stale revision or invalid stage transition."},
    },
)
def append_booking_intent_action(
    request: BookingIntentAction,
    intent_id: str = ApiPath(pattern=r"^bki_[0-9a-f]{32}$"),
    store: BookingIntentStore = Depends(get_booking_intent_store),
):
    try:
        record, added = store.append_action(intent_id, request)
    except IntentNotFound:
        raise HTTPException(404, "Booking intent was not found") from None
    except IntentConflict as exc:
        raise HTTPException(409, str(exc)) from None
    return {**record, "action_added": added}


@router.post(
    "/booking-intents/{intent_id}/commit",
    tags=["booking intents"],
    summary="Safely commit or finalize an intent after verified deposit (Ticket N06)",
    responses={
        404: {"description": "Booking intent not found."},
        409: {"description": "Revision or terms conflict."},
    },
)
def commit_booking_intent(
    request: BookingIntentCommitRequest,
    intent_id: str = ApiPath(pattern=r"^bki_[0-9a-f]{32}$"),
    store: BookingIntentStore = Depends(get_booking_intent_store),
    pricing_service: PackagePricingService = Depends(get_package_pricing_service),
    operation_engine: BookingOperationEngine = Depends(get_booking_operation_engine),
):
    try:
        intent = store.get(intent_id)
    except IntentNotFound:
        raise HTTPException(404, "Booking intent was not found") from None

    if intent["revision"] != request.expected_revision:
        raise HTTPException(409, "Booking intent revision changed; reload it before committing")

    quote = pricing_service.get_quote(request.quote_id)
    if not quote:
        raise HTTPException(404, f"Quote '{request.quote_id}' was not found.")
    current_hash = compute_quote_hash(quote)
    if request.quote_hash and request.quote_hash != current_hash:
        raise HTTPException(409, "Quote content hash does not match current server offer.")

    # Locate verified payment
    payment_id = None
    for p in intent.get("links", {}).get("payments", []):
        if p.get("status") in ("completed", "verified_paid"):
            payment_id = p.get("payment_id")
            break

    if not payment_id and pricing_service.payments_collection is not None:
        p_doc = pricing_service.payments_collection.find_one({"booking_intent_id": intent_id, "status": "verified_paid"})
        if p_doc:
            payment_id = p_doc.get("payment_id")

    if not payment_id:
        # Check by pricing service cache / simulator
        for pid, pdoc in getattr(pricing_service, "_payments_cache", {}).items():
            if pdoc.get("booking_intent_id") == intent_id and pdoc.get("status") == "verified_paid":
                payment_id = pid
                break

    if not payment_id:
        return JSONResponse(
            status_code=200,
            content={
                "status": "pending_payment",
                "message": "Deposit payment has not been verified. Inventory remains unreserved.",
                "stage": intent.get("stage", "draft"),
                "payment_state": intent.get("payment_state", "unpaid"),
            },
        )

    # Coordinate safe execution via BookingOperationEngine
    cmd_key = request.command_key or f"commit_{intent_id}_{request.expected_revision}"
    op_req = ExecuteBookingOperationRequest(
        operation_type="new_booking",
        booking_reference=intent.get("booking_reference"),
        quote_id=request.quote_id,
        payment_id=payment_id,
        idempotency_key=cmd_key,
    )
    op_resp = operation_engine.execute_operation(op_req)

    # Synchronize intent stage
    now_dt = store.collection.database.client.server_info() if hasattr(store.collection, "database") else None
    if op_resp.status == "confirmed":
        store.collection.update_one(
            {"_id": intent_id},
            {"$set": {"stage": "confirmed", "reservation_state": "confirmed"}},
        )
    elif op_resp.status == "unconfirmed_reconciliation":
        store.collection.update_one(
            {"_id": intent_id},
            {"$set": {"stage": "unconfirmed_reconciliation", "reservation_state": "unconfirmed_reconciliation"}},
        )

    return op_resp.model_dump(mode="json")


@router.post(
    "/booking-intents/{intent_id}/reaccept-offer",
    tags=["booking intents"],
    summary="Re-accept an updated server quote after price or inventory change",
    responses={
        404: {"description": "Booking intent not found."},
        409: {"description": "Stale revision or quote mismatch."},
    },
)
def reaccept_offer(
    request: BookingIntentReacceptRequest,
    intent_id: str = ApiPath(pattern=r"^bki_[0-9a-f]{32}$"),
    store: BookingIntentStore = Depends(get_booking_intent_store),
    pricing_service: PackagePricingService = Depends(get_package_pricing_service),
):
    try:
        intent = store.get(intent_id)
    except IntentNotFound:
        raise HTTPException(404, "Booking intent was not found") from None

    if intent["revision"] != request.expected_revision:
        raise HTTPException(409, "Booking intent revision changed; reload it before reaccepting")

    quote = pricing_service.get_quote(request.quote_id)
    if not quote or quote.status != "available":
        raise HTTPException(409, "The requested package quote is no longer available.")
    if pricing_service.is_quote_expired(quote):
        raise HTTPException(409, "The requested package quote has expired.")

    expected_hash = compute_quote_hash(quote)
    if expected_hash != request.quote_hash:
        raise HTTPException(409, "Quote content hash mismatch with server offer.")

    # Apply reaccepted quote
    new_rev = intent["revision"] + 1
    store.collection.update_one(
        {"_id": intent_id, "revision": request.expected_revision},
        {
            "$set": {
                "revision": new_rev,
                "quote.version": new_rev,
                "quote.live_revalidated": {
                    "quote_id": quote.quote_id,
                    "quote_hash": expected_hash,
                    "amount": str(quote.total_package_price),
                    "deposit_amount": str(quote.deposit_amount),
                    "currency": quote.currency or "EUR",
                },
            }
        },
    )
    return store.get(intent_id)
