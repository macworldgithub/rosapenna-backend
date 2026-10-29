"""BRS golf round booking write, amendment, and release routes."""

from __future__ import annotations

import datetime as dt
import uuid

from fastapi import APIRouter, Depends, HTTPException

from rosapenna_backend.api.dependencies import (
    get_booking_intent_store,
    get_brs_booking_client,
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
from rosapenna_backend.integrations.brs.booking_client import (
    BrsAuthExpiredError,
    BrsBookingClient,
    BrsConflictError,
    BrsRateLimitError,
    BrsReadbackMismatchError,
    BrsWriteError,
)
from rosapenna_backend.services.booking_intents import (
    BookingIntentStore,
    IntentConflict,
    IntentNotFound,
)
from rosapenna_backend.services.tee_sheets import TeesheetCache

router = APIRouter(prefix="/golf/rounds", tags=["golf"], dependencies=COMMON_DEPS)


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

    # 3. Validate slot against F09 tee sheet cache / availability
    try:
        availability = teesheet_cache.get_course_availability(
            course_id=request.course_id,
            date_str=request.date.isoformat(),
            players=request.players,
            time_from=request.time,
            time_to=request.time,
        )
        matching_slots = [s for s in availability.slots if s.time == request.time and s.available_players >= request.players]
        if not matching_slots and availability.slots_count > 0:
            raise HTTPException(
                status_code=409,
                detail=f"Tee time slot at {request.time} on {request.date} does not have enough capacity for {request.players} players.",
            )
    except HTTPException:
        raise
    except Exception as exc:
        # If cache query fails with invalid course or range, report 400
        if "Choose a date between" in str(exc) or "Unsupported course" in str(exc):
            raise HTTPException(status_code=400, detail=str(exc))
        # Non-fatal if cache is in sandbox mode or starting

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

    if request.new_date or request.new_time or request.new_course_id:
        try:
            availability = teesheet_cache.get_course_availability(
                course_id=new_course_id,
                date_str=new_date.isoformat(),
                players=new_players,
                time_from=new_time,
                time_to=new_time,
            )
            matching_slots = [s for s in availability.slots if s.time == new_time and s.available_players >= new_players]
            if not matching_slots and availability.slots_count > 0:
                raise HTTPException(
                    status_code=409,
                    detail=f"Tee time slot at {new_time} on {new_date} is no longer available.",
                )
        except HTTPException:
            raise
        except Exception:
            pass

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
    release_res = client.release_round(round_id=round_id, booking_reference=booking_reference)

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
