"""Protected API routes for persisted booking-intent drafts."""

from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Path as ApiPath
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from rosapenna_backend.api.dependencies import get_booking_intent_store
from rosapenna_backend.api.schemas.booking_intents import BookingIntentAction, BookingIntentCreate
from rosapenna_backend.api.security import COMMON_DEPS
from rosapenna_backend.services.booking_intents import BookingIntentStore, IntentConflict, IntentNotFound

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
