"""Process-local booking session registry and lifecycle."""

import threading
import time
import uuid
from typing import Dict, List, Optional

from rosapenna_backend.api.schemas.bookings import BookingRequest
from rosapenna_backend.config import Settings, settings
from rosapenna_backend.core.logging import logger
from rosapenna_backend.services.booking_automation import BookingSession


class BookingSessionManager:
    def __init__(self, app_settings: Settings = settings):
        self.settings = app_settings
        self.sessions: Dict[str, BookingSession] = {}
        self.lock = threading.Lock()
        self.semaphore = threading.Semaphore(app_settings.max_concurrent_sessions)
        self._reaper_stop = threading.Event()
        self._reaper_thread: Optional[threading.Thread] = None

    def start(self) -> None:
        self._reaper_stop.clear()
        self._reaper_thread = threading.Thread(target=self._reap_loop, name="booking-session-reaper", daemon=True)
        self._reaper_thread.start()

    def _reap_loop(self) -> None:
        while not self._reaper_stop.wait(self.settings.session_reap_interval_seconds):
            try:
                now = time.time()
                with self.lock:
                    stale_ids = [
                        sid for sid, session in self.sessions.items()
                        if session.status in ("closed", "error")
                        and now - session.created_at > self.settings.session_ttl_seconds
                    ]
                    for session_id in stale_ids:
                        del self.sessions[session_id]
                if stale_ids:
                    logger.info("Reaped %d stale session(s): %s", len(stale_ids), stale_ids)
            except Exception as exc:
                logger.warning("Session reaper iteration failed: %s", type(exc).__name__)

    def start_session(self, request: BookingRequest) -> Optional[BookingSession]:
        session_id = str(uuid.uuid4())
        session = BookingSession(session_id, request, self.semaphore)
        if not session.start():
            return None
        with self.lock:
            self.sessions[session_id] = session
        return session

    def get(self, session_id: str) -> Optional[BookingSession]:
        with self.lock:
            return self.sessions.get(session_id)

    def close_session(self, session_id: str) -> Optional[BookingSession]:
        session = self.get(session_id)
        if session is not None:
            session.request_close()
        return session

    def active_count(self) -> int:
        with self.lock:
            return sum(1 for session in self.sessions.values() if session.status == "running")

    def list_sessions(self) -> List[dict]:
        with self.lock:
            return [
                {"session_id": sid, "status": session.status, "created_at": session.created_at, "error": session.error}
                for sid, session in self.sessions.items()
            ]

    def close(self) -> None:
        self._reaper_stop.set()
        if self._reaper_thread is not None:
            self._reaper_thread.join(timeout=2)
        with self.lock:
            sessions = list(self.sessions.values())
        for session in sessions:
            if session.status in ("running", "filled"):
                session.request_close()
        deadline = time.time() + 10
        for session in sessions:
            session._thread.join(max(0, deadline - time.time()))
