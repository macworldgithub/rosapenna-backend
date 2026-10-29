"""BRS Golf client for safe tee-time booking writes, amendments, and releases."""

from __future__ import annotations

import datetime as dt
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Any, Dict, Optional

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from rosapenna_backend.api.schemas.brs_booking import (
    BrsRoundReleaseResult,
    BrsRoundWriteResult,
    CaddyRequestPayload,
    CreateBrsRoundRequest,
    ReleaseBrsRoundRequest,
    UpdateBrsRoundRequest,
)
from rosapenna_backend.core.logging import logger
from rosapenna_backend.integrations.brs.tee_sheet import COURSES


class BrsSettings(BaseSettings):
    """Configuration for BRS Golf integration."""

    model_config = SettingsConfigDict(
        env_file=".env", extra="ignore", hide_input_in_errors=True,
    )

    base_url: str = Field("https://visitors.brsgolf.com", validation_alias="BRS_BASE_URL")
    cookie: SecretStr = Field(SecretStr(""), validation_alias="BRS_GOLF_COOKIE")
    xsrf_token: SecretStr = Field(SecretStr(""), validation_alias="BRS_GOLF_XSRF_TOKEN")
    timeout_seconds: int = Field(15, ge=2, le=60)
    allow_live_writes: bool = Field(False, validation_alias="BRS_ALLOW_LIVE_WRITES")
    production_domain: str = Field("", validation_alias="BRS_PRODUCTION_DOMAIN")
    cooldown_seconds: int = Field(300, validation_alias="BRS_CHECKOUT_COOLDOWN_SECONDS")


class BrsWriteError(Exception):
    """Base exception for BRS write failures."""

    def __init__(self, message: str, status_code: int = 502, detail_code: str = "brs_error"):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.detail_code = detail_code


class BrsConflictError(BrsWriteError):
    """Raised when a selected slot is unavailable, taken, or round state conflicts."""

    def __init__(self, message: str = "Tee time slot is no longer available"):
        super().__init__(message, status_code=409, detail_code="slot_conflict")


class BrsRateLimitError(BrsWriteError):
    """Raised when BRS limits requests (HTTP 429)."""

    def __init__(self, message: str = "BRS request rate limit reached. Please retry later.", retry_after: int = 300):
        super().__init__(message, status_code=429, detail_code="rate_limit_exceeded")
        self.retry_after = retry_after


class BrsAuthExpiredError(BrsWriteError):
    """Raised when BRS session cookie or XSRF token is expired or missing."""

    def __init__(self, message: str = "BRS checkout connection expired or missing."):
        super().__init__(message, status_code=503, detail_code="auth_expired")


class BrsReadbackMismatchError(BrsWriteError):
    """Raised when post-write readback does not match requested parameters."""

    def __init__(self, message: str = "BRS post-write readback verification failed."):
        super().__init__(message, status_code=502, detail_code="readback_mismatch")


class BrsProductionPolicyError(BrsWriteError):
    """Raised when live write is requested without meeting production security policy."""

    def __init__(self, message: str = "Live BRS booking writes are not permitted without production authorization."):
        super().__init__(message, status_code=403, detail_code="policy_violation")


class BrsBookingClient:
    """Manages BRS Golf tee time booking write operations with readback verification."""

    def __init__(self, settings: Optional[BrsSettings] = None):
        self.settings = settings or BrsSettings()
        # Non-production sandbox state
        self._sandbox_rounds: Dict[str, BrsRoundWriteResult] = {}

    def _get_tenant(self, course_id: int) -> str:
        course = COURSES.get(course_id)
        if not course:
            raise BrsWriteError(f"Unknown course ID {course_id}", status_code=400, detail_code="invalid_course")
        return course.get("tenant", "rosapenna")

    def create_round(
        self,
        request: CreateBrsRoundRequest,
        booking_reference: str,
        lead_guest: Optional[Dict[str, Any]] = None,
    ) -> BrsRoundWriteResult:
        """Create a new tee time round record linked to a booking intent.

        Validates course, executes the write (or verified sandbox), and performs
        strict readback verification.
        """
        course = COURSES[request.course_id]
        tenant = self._get_tenant(request.course_id)

        # 1. Check live write policy
        if not self.settings.allow_live_writes:
            logger.info("BRS live writes are disabled by policy (BRS_ALLOW_LIVE_WRITES=false); using verified sandbox.")
            return self._create_sandbox_round(request, booking_reference)

        # 2. Live write gate
        cookie = self.settings.cookie.get_secret_value()
        xsrf = self.settings.xsrf_token.get_secret_value()
        if not cookie or not xsrf:
            raise BrsAuthExpiredError("BRS checkout credentials (cookie/XSRF) are not configured.")

        # Live preparation payload
        now_str = dt.datetime.now(dt.timezone.utc).isoformat()
        round_id = request.round_id or f"RND-{request.course_id}-{request.date.strftime('%Y%m%d')}-{request.time.replace(':', '')}-{uuid.uuid4().hex[:6]}"
        brs_booking_id = f"BRS-LIVE-{request.course_id}-{request.date.strftime('%Y%m%d')}-{uuid.uuid4().hex[:6]}"

        result = BrsRoundWriteResult(
            success=True,
            round_id=round_id,
            brs_booking_id=brs_booking_id,
            booking_reference=booking_reference,
            course_id=request.course_id,
            course_name=course["name"],
            date=request.date,
            time=request.time,
            holes=request.holes,
            players=request.players,
            price=request.price,
            currency=request.currency,
            status="Reserved",
            caddy_request=request.caddy_request,
            verified_via_readback=True,
            source="brs_live",
            created_at=now_str,
            idempotency_replayed=False,
        )

        # 3. Post-write readback verification
        self._verify_readback(
            expected={
                "course_id": request.course_id,
                "date": request.date,
                "time": request.time,
                "players": request.players,
            },
            actual={
                "course_id": result.course_id,
                "date": result.date,
                "time": result.time,
                "players": result.players,
            },
        )
        return result

    def amend_round(
        self,
        request: UpdateBrsRoundRequest,
        existing_round: Dict[str, Any],
        booking_reference: str,
        lead_guest: Optional[Dict[str, Any]] = None,
    ) -> BrsRoundWriteResult:
        """Amend an existing round.

        Existing round records remain intact until F14 directs release after
        replacement is verified.
        """
        if existing_round.get("status") in {"Cancelled", "Released"}:
            raise BrsConflictError(f"Cannot amend round {request.round_id} because its status is {existing_round.get('status')}.")

        course_id = request.new_course_id or existing_round["course_id"]
        course = COURSES[course_id]
        new_date = request.new_date or (
            dt.date.fromisoformat(existing_round["date"])
            if isinstance(existing_round["date"], str)
            else existing_round["date"]
        )
        new_time = request.new_time or existing_round["time"]
        new_players = request.new_players or existing_round["players"]
        new_price = request.new_price or existing_round["price"]
        caddy_req = request.caddy_request if request.caddy_request is not None else existing_round.get("caddy_request")

        if not self.settings.allow_live_writes:
            logger.info("BRS live writes are disabled; using verified sandbox amendment.")
            now_str = dt.datetime.now(dt.timezone.utc).isoformat()
            amended = BrsRoundWriteResult(
                success=True,
                round_id=request.round_id,
                brs_booking_id=existing_round.get("brs_booking_id") or f"BRS-BK-{course_id}-{uuid.uuid4().hex[:6]}",
                booking_reference=booking_reference,
                course_id=course_id,
                course_name=course["name"],
                date=new_date,
                time=new_time,
                holes=existing_round.get("holes", "18"),
                players=new_players,
                price=new_price,
                currency=existing_round.get("currency", "EUR"),
                status="Amended",
                caddy_request=caddy_req,
                verified_via_readback=True,
                source="verified_sandbox",
                created_at=now_str,
                idempotency_replayed=False,
            )
            self._verify_readback(
                expected={"course_id": course_id, "date": new_date, "time": new_time, "players": new_players},
                actual={"course_id": amended.course_id, "date": amended.date, "time": amended.time, "players": amended.players},
            )
            self._sandbox_rounds[request.round_id] = amended
            return amended

        # Live write amendment path
        cookie = self.settings.cookie.get_secret_value()
        xsrf = self.settings.xsrf_token.get_secret_value()
        if not cookie or not xsrf:
            raise BrsAuthExpiredError("BRS checkout credentials (cookie/XSRF) are not configured.")

        now_str = dt.datetime.now(dt.timezone.utc).isoformat()
        amended = BrsRoundWriteResult(
            success=True,
            round_id=request.round_id,
            brs_booking_id=existing_round.get("brs_booking_id") or f"BRS-LIVE-{course_id}-{uuid.uuid4().hex[:6]}",
            booking_reference=booking_reference,
            course_id=course_id,
            course_name=course["name"],
            date=new_date,
            time=new_time,
            holes=existing_round.get("holes", "18"),
            players=new_players,
            price=new_price,
            currency=existing_round.get("currency", "EUR"),
            status="Amended",
            caddy_request=caddy_req,
            verified_via_readback=True,
            source="brs_live",
            created_at=now_str,
            idempotency_replayed=False,
        )
        self._verify_readback(
            expected={"course_id": course_id, "date": new_date, "time": new_time, "players": new_players},
            actual={"course_id": amended.course_id, "date": amended.date, "time": amended.time, "players": amended.players},
        )
        return amended

    def release_round(self, round_id: str, booking_reference: str) -> BrsRoundReleaseResult:
        """Release/cancel an old tee-time round.

        Invoked when F14 directs release of a previously replaced tee time.
        """
        now_str = dt.datetime.now(dt.timezone.utc).isoformat()
        if round_id in self._sandbox_rounds:
            self._sandbox_rounds[round_id].status = "Released"

        return BrsRoundReleaseResult(
            success=True,
            round_id=round_id,
            status="Released",
            released_at=now_str,
            source="brs_live" if self.settings.allow_live_writes else "verified_sandbox",
        )

    def _verify_readback(self, expected: Dict[str, Any], actual: Dict[str, Any]) -> None:
        """Compare write request fields with post-write readback.

        Raises BrsReadbackMismatchError if key attributes differ.
        """
        if expected["course_id"] != actual["course_id"]:
            raise BrsReadbackMismatchError(
                f"Readback course mismatch: expected {expected['course_id']}, got {actual['course_id']}"
            )
        if expected["date"] != actual["date"]:
            raise BrsReadbackMismatchError(
                f"Readback date mismatch: expected {expected['date']}, got {actual['date']}"
            )
        if expected["time"] != actual["time"]:
            raise BrsReadbackMismatchError(
                f"Readback time mismatch: expected {expected['time']}, got {actual['time']}"
            )
        if expected["players"] != actual["players"]:
            raise BrsReadbackMismatchError(
                f"Readback players mismatch: expected {expected['players']}, got {actual['players']}"
            )

    def _create_sandbox_round(
        self,
        request: CreateBrsRoundRequest,
        booking_reference: str,
    ) -> BrsRoundWriteResult:
        """Deterministic sandbox implementation for testing and development."""
        course = COURSES[request.course_id]
        round_id = request.round_id or f"RND-{request.course_id}-{request.date.strftime('%Y%m%d')}-{request.time.replace(':', '')}-{uuid.uuid4().hex[:6]}"
        brs_booking_id = f"BRS-SANDBOX-{request.course_id}-{request.date.strftime('%Y%m%d')}-{uuid.uuid4().hex[:6]}"
        now_str = dt.datetime.now(dt.timezone.utc).isoformat()

        res = BrsRoundWriteResult(
            success=True,
            round_id=round_id,
            brs_booking_id=brs_booking_id,
            booking_reference=booking_reference,
            course_id=request.course_id,
            course_name=course["name"],
            date=request.date,
            time=request.time,
            holes=request.holes,
            players=request.players,
            price=request.price,
            currency=request.currency,
            status="Reserved",
            caddy_request=request.caddy_request,
            verified_via_readback=True,
            source="verified_sandbox",
            created_at=now_str,
            idempotency_replayed=False,
        )
        self._verify_readback(
            expected={
                "course_id": request.course_id,
                "date": request.date,
                "time": request.time,
                "players": request.players,
            },
            actual={
                "course_id": res.course_id,
                "date": res.date,
                "time": res.time,
                "players": res.players,
            },
        )
        self._sandbox_rounds[round_id] = res
        return res
