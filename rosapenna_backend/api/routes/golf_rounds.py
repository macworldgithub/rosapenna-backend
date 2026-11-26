"""BRS golf round booking write, amendment, and release routes."""

from __future__ import annotations

import datetime as dt
import uuid

from fastapi import APIRouter, Depends, HTTPException

from rosapenna_backend.api.dependencies import (
    get_booking_intent_store,
    get_brs_booking_client,
    get_settings,
    get_teesheet_cache,
)
from rosapenna_backend.api.schemas.booking_intents import BrsRoundPayload
from rosapenna_backend.api.schemas.brs_booking import (
    BrsRoundReleaseResult,
    BrsRoundWriteResult,
    CreateBrsRoundRequest,
    ReleaseBrsRoundRequest,
    UpdateBrsRoundRequest,
)
from rosapenna_backend.api.security import COMMON_DEPS
from rosapenna_backend.config import Settings
from rosapenna_backend.integrations.brs.booking_client import (
    BrsAuthExpiredError,
    BrsBookingClient,
    BrsConflictError,
    BrsRateLimitError,
    BrsReadbackMismatchError,
    BrsWriteError,
)
from rosapenna_backend.integrations.brs.tee_sheet import BrsUnsupportedQueryError, tee_time_is_empty
from rosapenna_backend.services.booking_intents import (
    BookingIntentStore,
    IntentConflict,
    IntentNotFound,
)
from rosapenna_backend.services.tee_sheets import TeesheetCache

def require_local_test_rounds(settings: Settings = Depends(get_settings)) -> None:
    # Guest checkout creates linked rounds through the verified payment operation.
    if (settings.environment not in {"development", "test"}
            or not settings.simulation_enabled
            or settings.payment_mode != "simulation"
            or not settings.simulation_run_id):
        raise HTTPException(status_code=404, detail="Direct golf round changes are unavailable.")


router = APIRouter(prefix="/golf/rounds", tags=["golf"], dependencies=[
    *COMMON_DEPS, Depends(require_local_test_rounds),
])


def _require_empty_tee_time(cache: TeesheetCache, course_id: int, day: dt.date, time: str, players: int) -> None:
    try:
        sheet = cache.get(day, course_id, force_refresh=True)
        is_empty = tee_time_is_empty(sheet, time, players)
    except BrsUnsupportedQueryError:
        raise HTTPException(status_code=400, detail="Unsupported date or course.") from None
    except Exception:
        raise HTTPException(status_code=503, detail="Could not check the tee time. No booking was changed.") from None
    if not is_empty:
        raise HTTPException(status_code=409, detail="The tee time is occupied or unavailable. Select a different time.")


@router.post(
    "",
    response_model=BrsRoundWriteResult,
    summary="Create a BRS golf round linked to a booking intent",
    description="Validates slot availability against F09, writes to BRS with post-write readback, and links round in F05.",
)
def create_golf_round(
    request: CreateBrsRoundRequest,
    client: BrsBookingClient = Depends(get_brs_booking_client),
    booking_store: BookingIntentStore = Depends(get_booking_intent_store),
    teesheet_cache: TeesheetCache = Depends(get_teesheet_cache),
):
    # 1. Verify existence of booking intent
    try:
        intent = booking_store.get(request.booking_intent_id)
    except IntentNotFound:
        raise HTTPException(status_code=404, detail="Booking intent not found.")

    booking_reference = intent.get("booking_reference") or f"RSP-{request.date.strftime('%Y%m%d')}-000001"
    existing_rounds = intent.get("links", {}).get("brs_rounds", [])

    # 2. Idempotency check: if intent already links this round_id, replay it
    target_round_id = request.round_id or f"RND-{request.course_id}-{request.date.strftime('%Y%m%d')}-{request.time.replace(':', '')}"
    for r in existing_rounds:
        if r.get("round_id") == target_round_id or (
            r.get("course_id") == request.course_id
            and r.get("date") == request.date.isoformat()
            and r.get("time") == request.time
        ):
            return BrsRoundWriteResult(
                round_id=r["round_id"],
                brs_booking_id=r.get("brs_booking_id") or f"BRS-{r['round_id']}",
                booking_reference=booking_reference,
                course_id=r["course_id"],
                course_name=r["course_name"],
                date=dt.date.fromisoformat(r["date"]) if isinstance(r["date"], str) else r["date"],
                time=r["time"],
                holes=r.get("holes", "18"),
                players=r["players"],
                price=r["price"],
                currency=r.get("currency", "EUR"),
                status=r.get("status", "Reserved"),
                caddy_request=r.get("caddy_request"),
                verified_via_readback=True,
                source="idempotent_replay",
                created_at=dt.datetime.now(dt.timezone.utc).isoformat(),
                idempotency_replayed=True,
            )

    _require_empty_tee_time(teesheet_cache, request.course_id, request.date, request.time, request.players)

    # 4. Execute round write via BRS client (or verified sandbox)
    lead_guest = intent.get("lead_guest")
    try:
        result = client.create_round(
            request=request,
            booking_reference=booking_reference,
            lead_guest=lead_guest,
        )
    except BrsReadbackMismatchError as exc:
        try:
            booking_store.mark_recovery_required(
                intent_id=request.booking_intent_id,
                expected_revision=intent["revision"],
                detail_code="upstream_response_error",
            )
        except Exception:
            pass
        raise HTTPException(
            status_code=502,
            detail="BRS post-write readback verification failed. Booking marked for recovery.",
        ) from None
    except BrsConflictError as exc:
        raise HTTPException(status_code=409, detail=exc.message) from None
    except BrsRateLimitError as exc:
        raise HTTPException(
            status_code=429,
            detail=exc.message,
            headers={"Retry-After": str(exc.retry_after)},
        ) from None
    except BrsAuthExpiredError as exc:
        raise HTTPException(status_code=503, detail=exc.message) from None
    except BrsWriteError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from None

    # 5. Link round atomically into BookingIntentStore
    payload = BrsRoundPayload(
        round_id=result.round_id,
        course_id=result.course_id,
        course_name=result.course_name,
        date=result.date,
        time=result.time,
        holes=result.holes,
        players=result.players,
        brs_booking_id=result.brs_booking_id,
        booking_reference=booking_reference,
        price=result.price,
        currency=result.currency,
        status=result.status,
        caddy_request=result.caddy_request,
    )
    try:
        booking_store.link_brs_round(
            intent_id=request.booking_intent_id,
            action_id=uuid.uuid4(),
            expected_revision=intent["revision"],
            brs_payload=payload,
        )
    except IntentConflict:
        # Re-fetch intent to inspect updated links
        refreshed = booking_store.get(request.booking_intent_id)
        existing = [
            r for r in refreshed.get("links", {}).get("brs_rounds", [])
            if r.get("round_id") == result.round_id
        ]
        if existing:
            result.idempotency_replayed = True
            return result
        raise HTTPException(status_code=409, detail="Booking intent was modified concurrently.")

    return result


@router.put(
    "/{round_id}",
    response_model=BrsRoundWriteResult,
    summary="Amend an existing BRS golf round",
    description="Updates course, date, time, player count, or caddy requests. Preserves original round intact until release.",
)
def amend_golf_round(
    round_id: str,
    request: UpdateBrsRoundRequest,
    client: BrsBookingClient = Depends(get_brs_booking_client),
    booking_store: BookingIntentStore = Depends(get_booking_intent_store),
    teesheet_cache: TeesheetCache = Depends(get_teesheet_cache),
):
    if round_id != request.round_id:
        raise HTTPException(status_code=400, detail="Path round_id must match body round_id.")

    # 1. Verify existence of booking intent
    try:
        intent = booking_store.get(request.booking_intent_id)
    except IntentNotFound:
        raise HTTPException(status_code=404, detail="Booking intent not found.")

    booking_reference = intent.get("booking_reference") or f"RSP-UPDATE-000001"
    existing_rounds = intent.get("links", {}).get("brs_rounds", [])
    target = next((r for r in existing_rounds if r.get("round_id") == round_id), None)
    if not target:
        raise HTTPException(status_code=404, detail=f"Round {round_id} not found on this booking.")

    if target.get("status") in {"Cancelled", "Released"}:
        raise HTTPException(status_code=409, detail=f"Cannot amend round {round_id} because its status is {target.get('status')}.")

    # 2. If new slot provided, validate capacity against F09
    new_course_id = request.new_course_id or target["course_id"]
    new_date = request.new_date or (
        dt.date.fromisoformat(target["date"]) if isinstance(target["date"], str) else target["date"]
    )
    new_time = request.new_time or target["time"]
    new_players = request.new_players or target["players"]

    if (new_course_id, str(new_date), new_time) != (target["course_id"], str(target["date"]), target["time"]):
        _require_empty_tee_time(teesheet_cache, new_course_id, new_date, new_time, new_players)

    # 3. Execute amendment on BRS client
    lead_guest = intent.get("lead_guest")
    try:
        result = client.amend_round(
            request=request,
            existing_round=target,
            booking_reference=booking_reference,
            lead_guest=lead_guest,
        )
    except BrsReadbackMismatchError:
        try:
            booking_store.mark_recovery_required(
                intent_id=request.booking_intent_id,
                expected_revision=intent["revision"],
                detail_code="upstream_response_error",
            )
        except Exception:
            pass
        raise HTTPException(
            status_code=502,
            detail="BRS amendment readback verification failed. Booking marked for recovery.",
        ) from None
    except BrsConflictError as exc:
        raise HTTPException(status_code=409, detail=exc.message) from None
    except BrsRateLimitError as exc:
        raise HTTPException(
            status_code=429,
            detail=exc.message,
            headers={"Retry-After": str(exc.retry_after)},
        ) from None
    except BrsAuthExpiredError as exc:
        raise HTTPException(status_code=503, detail=exc.message) from None
    except BrsWriteError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from None

    # 4. Atomically update round in BookingIntentStore
    payload = BrsRoundPayload(
        round_id=result.round_id,
        course_id=result.course_id,
        course_name=result.course_name,
        date=result.date,
        time=result.time,
        holes=result.holes,
        players=result.players,
        brs_booking_id=result.brs_booking_id,
        booking_reference=booking_reference,
        price=result.price,
        currency=result.currency,
        status=result.status,
        caddy_request=result.caddy_request,
    )
    try:
        booking_store.link_brs_round(
            intent_id=request.booking_intent_id,
            action_id=uuid.uuid4(),
            expected_revision=intent["revision"],
            brs_payload=payload,
        )
    except IntentConflict:
        refreshed = booking_store.get(request.booking_intent_id)
        existing = [
            r for r in refreshed.get("links", {}).get("brs_rounds", [])
            if r.get("round_id") == result.round_id
        ]
        if existing:
            result.idempotency_replayed = True
            return result
        raise HTTPException(status_code=409, detail="Booking intent was modified concurrently.")

    return result


@router.delete(
    "/{round_id}",
    response_model=BrsRoundReleaseResult,
    summary="Release/cancel a BRS round",
    description="Coordinates release of an old tee time after replacement is verified (F14).",
)
def release_golf_round(
    round_id: str,
    request: ReleaseBrsRoundRequest,
    client: BrsBookingClient = Depends(get_brs_booking_client),
    booking_store: BookingIntentStore = Depends(get_booking_intent_store),
):
    if round_id != request.round_id:
        raise HTTPException(status_code=400, detail="Path round_id must match body round_id.")

    try:
        intent = booking_store.get(request.booking_intent_id)
    except IntentNotFound:
        raise HTTPException(status_code=404, detail="Booking intent not found.")

    booking_reference = intent.get("booking_reference") or "RSP-RELEASE-000001"
    existing_rounds = intent.get("links", {}).get("brs_rounds", [])
    target = next((r for r in existing_rounds if r.get("round_id") == round_id), None)
    if not target:
        raise HTTPException(status_code=404, detail=f"Round {round_id} not found on this booking.")

    if target.get("status") in {"Cancelled", "Released"}:
        return BrsRoundReleaseResult(
            success=True,
            round_id=round_id,
            status="Released",
            released_at=dt.datetime.now(dt.timezone.utc).isoformat(),
            source="verified_sandbox",
        )

    # Execute release
    try:
        release_res = client.release_round(round_id=round_id, booking_reference=booking_reference)
    except BrsWriteError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from None

    # Update round status to Released in BookingIntentStore
    updated_payload = BrsRoundPayload(
        round_id=target["round_id"],
        course_id=target["course_id"],
        course_name=target["course_name"],
        date=dt.date.fromisoformat(target["date"]) if isinstance(target["date"], str) else target["date"],
        time=target["time"],
        holes=target.get("holes", "18"),
        players=target["players"],
        brs_booking_id=target.get("brs_booking_id"),
        booking_reference=booking_reference,
        price=target["price"],
        currency=target.get("currency", "EUR"),
        status="Released",
        caddy_request=target.get("caddy_request"),
    )
    try:
        booking_store.link_brs_round(
            intent_id=request.booking_intent_id,
            action_id=uuid.uuid4(),
            expected_revision=intent["revision"],
            brs_payload=updated_payload,
        )
    except IntentConflict:
        pass

    return release_res
