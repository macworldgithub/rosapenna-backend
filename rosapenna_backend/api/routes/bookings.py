"""Booking automation and in-memory session routes."""

from fastapi import APIRouter, Depends, HTTPException

from rosapenna_backend.api.dependencies import get_session_manager, get_settings
from rosapenna_backend.api.schemas.bookings import BookingRequest, BookingResponse, SessionStatusResponse
from rosapenna_backend.api.security import COMMON_DEPS
from rosapenna_backend.config import Settings
from rosapenna_backend.services.booking_sessions import BookingSessionManager

router = APIRouter(dependencies=COMMON_DEPS)


@router.post(
    "/bookings",
    response_model=BookingResponse,
    tags=["bookings"],
    summary="Start a booking automation run",
    description=(
        "Opens a real Chromium browser, navigates the tee sheet, fills all form fields, "
        "and optionally fills Stripe card details and clicks Pay.\n\n"
        "Supply `card_details` to automate card entry. Set `auto_pay: true` to also "
        "click Pay — leave it false (default) to review before paying.\n\n"
        "Blocks up to `BOOKING_BOOKING_FILL_TIMEOUT_SECONDS` (default 90s); returns "
        "`status: 'running'` if still in progress — poll `GET /bookings/{id}`."
    ),
    response_description="Session ID, status, and step-by-step log.",
    responses={429: {"description": "At capacity or rate limit exceeded."}, 401: {"description": "Missing or invalid X-API-Key."}},
)
def create_booking(
    req: BookingRequest,
    manager: BookingSessionManager = Depends(get_session_manager),
    settings: Settings = Depends(get_settings),
):
    session = manager.start_session(req)
    if session is None:
        raise HTTPException(
            status_code=429,
            detail=f"At capacity ({settings.max_concurrent_sessions} concurrent sessions). Try again shortly.",
        )

    finished_in_time = session.ready_event.wait(timeout=settings.booking_fill_timeout_seconds)
    if not finished_in_time:
        return BookingResponse(
            session_id=session.session_id,
            status="running",
            message="Still working — check status via GET /bookings/{session_id}.",
            log=session.log,
        )
    if session.status == "error":
        return BookingResponse(
            session_id=session.session_id,
            status="error",
            message=session.error or "Unknown error during booking automation.",
            log=session.log,
        )
    return BookingResponse(
        session_id=session.session_id,
        status="filled",
        message=(
            "Form filled successfully. "
            + (
                "Card details entered and Pay clicked automatically."
                if req.card_details and req.auto_pay
                else "Card details entered — click Pay manually to complete."
                if req.card_details
                else "Browser left open — enter card details and click Pay manually."
            )
        ),
        log=session.log,
    )


@router.get(
    "/bookings/{session_id}",
    response_model=SessionStatusResponse,
    tags=["bookings"],
    summary="Poll a booking session's status",
    description="Fetch the current status and log for a session created via POST /bookings.",
    response_description="Status, creation time, log, and error if any.",
    responses={404: {"description": "Session not found."}, 401: {"description": "Missing or invalid X-API-Key."}},
)
def get_booking_status(session_id: str, manager: BookingSessionManager = Depends(get_session_manager)):
    session = manager.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    return SessionStatusResponse(
        session_id=session_id, status=session.status, created_at=session.created_at,
        log=session.log, error=session.error,
    )


@router.post(
    "/bookings/{session_id}/close",
    tags=["bookings"],
    summary="Close a booking session's browser",
    description="Signals the background thread to close the Chromium window. Call once payment is done or abandoned.",
    response_description="Acknowledgement that the close request was received.",
    responses={404: {"description": "Session not found."}, 401: {"description": "Missing or invalid X-API-Key."}},
)
def close_booking(session_id: str, manager: BookingSessionManager = Depends(get_session_manager)):
    session = manager.close_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    return {"session_id": session_id, "message": "Close requested; browser will shut down shortly."}


\n