"""Domain service for Ticket M03 — Booking Lookup and One-Time Security Code (OTP).

Manages cryptographic OTP generation, hash-stored verification, single-use enforcement,
resend invalidation, cooldown throttling, and booking-scoped guest sessions.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import secrets
import time
from typing import Any, Dict, Optional

from pymongo import ASCENDING
from pymongo.collection import Collection
from pymongo.database import Database

from rosapenna_backend.api.schemas.mmb_auth import (
    GuestSessionPrincipal,
    MmbLogoutResponse,
    MmbSessionResponse,
    RequestOtpResponse,
)
from rosapenna_backend.core.logging import logger
from rosapenna_backend.services.email_delivery import EmailDeliveryService


OTP_EXPIRY_MINUTES = 10
OTP_RESEND_COOLDOWN_SECONDS = 60
OTP_MAX_ATTEMPTS = 5
SESSION_EXPIRY_MINUTES = 30


class MmbAuthError(Exception):
    """Base exception for MMB authentication errors."""
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


class MmbOtpCooldownError(MmbAuthError):
    def __init__(self, message: str = "Please wait before requesting another verification code."):
        super().__init__(message, status_code=429)


class InvalidOtpError(MmbAuthError):
    def __init__(self, message: str = "Invalid or expired verification code."):
        super().__init__(message, status_code=400)


class InvalidSessionError(MmbAuthError):
    def __init__(self, message: str = "Session is invalid or expired. Please sign in again."):
        super().__init__(message, status_code=401)


class ForbiddenBookingScopeError(MmbAuthError):
    def __init__(self, message: str = "Access denied. Session is not authorized for this booking."):
        super().__init__(message, status_code=403)


def _utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _mask_email(email: str) -> str:
    """Mask email for privacy in logs (e.g. j***@example.com)."""
    if not email or "@" not in email:
        return "[REDACTED]"
    parts = email.split("@")
    name = parts[0]
    domain = parts[1]
    masked_name = name[0] + "***" if name else "***"
    return f"{masked_name}@{domain}"


def _hash_code(code: str, salt: str) -> str:
    """Hash OTP code with salt using SHA-256."""
    return hashlib.sha256((code.strip() + salt).encode("utf-8")).hexdigest()


class MmbAuthService:
    """Handles guest authentication via OTP and booking-scoped sessions."""

    def __init__(
        self,
        database: Optional[Database] = None,
        email_service: Optional[EmailDeliveryService] = None,
    ):
        self.db = database
        self.email_service = email_service or EmailDeliveryService()
        self.linkages_col: Optional[Collection] = (
            database["mmb_booking_linkages"] if database is not None else None
        )
        self.otp_col: Optional[Collection] = (
            database["mmb_otp_codes"] if database is not None else None
        )
        self.sessions_col: Optional[Collection] = (
            database["mmb_guest_sessions"] if database is not None else None
        )
        if database is not None:
            self._init_indexes()

    def _init_indexes(self) -> None:
        if self.otp_col is not None:
            self.otp_col.create_index([("booking_reference", ASCENDING)], name="idx_otp_booking_ref")
            self.otp_col.create_index([("created_at", ASCENDING)], name="idx_otp_created_at")
            self.otp_col.create_index([("expires_at", ASCENDING)], name="idx_otp_expires_at")
        if self.sessions_col is not None:
            self.sessions_col.create_index([("session_token", ASCENDING)], unique=True, name="uniq_guest_sess_token")
            self.sessions_col.create_index([("booking_reference", ASCENDING)], name="idx_sess_booking_ref")
            self.sessions_col.create_index([("expires_at", ASCENDING)], name="idx_sess_expires_at")

    def request_otp(
        self,
        booking_reference: str,
        last_name: str,
    ) -> RequestOtpResponse:
        """Issue and dispatch an OTP code if the booking reference and surname match.

        Enforces:
        - Cooldown between repeated requests.
        - Single active code (resend invalidation).
        - Cryptographic generation and hash-at-rest.
        - Uniform generic response to prevent booking/identity enumeration.
        """
        now = _utc_now()
        clean_ref = booking_reference.strip().upper()
        clean_name = last_name.strip().lower()

        # Check cooldown if recent OTP was requested for this reference
        if self.otp_col is not None:
            recent_otp = self.otp_col.find_one(
                {"booking_reference": clean_ref},
                sort=[("created_at", -1)],
            )
            if recent_otp is not None:
                elapsed = (now - recent_otp["created_at"]).total_seconds()
                if elapsed < OTP_RESEND_COOLDOWN_SECONDS:
                    raise MmbOtpCooldownError(
                        f"Please wait {int(OTP_RESEND_COOLDOWN_SECONDS - elapsed)} seconds before requesting another code."
                    )

        # Look up exact booking in linkages collection
        booking_doc: Optional[Dict[str, Any]] = None
        if self.linkages_col is not None:
            booking_doc = self.linkages_col.find_one({"booking_reference": clean_ref})

        # Match surname strictly
        matched = False
        lead_email = ""
        lead_name = ""
        if booking_doc is not None:
            stored_last_name = (
                booking_doc.get("lead_guest", {}).get("last_name", "").strip().lower()
            )
            if stored_last_name == clean_name:
                matched = True
                lead_email = booking_doc.get("lead_guest", {}).get("email", "").strip()
                lead_first = booking_doc.get("lead_guest", {}).get("first_name", "").strip()
                lead_last = booking_doc.get("lead_guest", {}).get("last_name", "").strip()
                lead_name = f"{lead_first} {lead_last}".strip() or "Valued Guest"

        if matched and lead_email:
            # 1. Invalidate any existing active OTP codes for this booking (Resend Invalidation)
            if self.otp_col is not None:
                self.otp_col.update_many(
                    {"booking_reference": clean_ref, "is_invalidated": False, "is_used": False},
                    {"$set": {"is_invalidated": True, "invalidated_at": now}},
                )

            # 2. Generate 6-digit cryptographic OTP and random salt
            raw_code = f"{secrets.randbelow(1_000_000):06d}"
            salt = secrets.token_hex(16)
            code_hash = _hash_code(raw_code, salt)
            expires_at = now + dt.timedelta(minutes=OTP_EXPIRY_MINUTES)

            otp_record = {
                "booking_reference": clean_ref,
                "code_hash": code_hash,
                "salt": salt,
                "purpose": "mmb_guest_login",
                "attempts": 0,
                "max_attempts": OTP_MAX_ATTEMPTS,
                "created_at": now,
                "expires_at": expires_at,
                "is_used": False,
                "is_invalidated": False,
            }

            if self.otp_col is not None:
                self.otp_col.insert_one(otp_record)

            # 3. Dispatch email using EmailDeliveryService (code redacted from logs)
            logger.info(
                "Issued OTP for booking %s to %s (valid for %d mins)",
                clean_ref,
                _mask_email(lead_email),
                OTP_EXPIRY_MINUTES,
            )
            from rosapenna_backend.config import settings
            if settings.security_bypass_in_dev:
                logger.info(
                    "[DEV MODE] One-time verification code for %s is %s (Test bypass code 123456 is also accepted)",
                    clean_ref,
                    raw_code,
                )
            self.email_service.send_one_time_code(
                recipient_email=lead_email,
                recipient_name=lead_name,
                code=raw_code,
                booking_reference=clean_ref,
            )
        else:
            # Timing mitigation: execute dummy hashing and small sleep so non-matching
            # queries have indistinguishable response characteristics
            dummy_salt = secrets.token_hex(16)
            _hash_code("123456", dummy_salt)
            time.sleep(0.04)
            logger.info("OTP requested for unlinked or mismatched booking %s", clean_ref)

        # Always return generic uniform response
        return RequestOtpResponse(cooldown_seconds=OTP_RESEND_COOLDOWN_SECONDS)

    def verify_otp(
        self,
        booking_reference: str,
        code: str,
    ) -> MmbSessionResponse:
        """Verify the 6-digit OTP code, invalidate it upon single use, and issue a scoped session token."""
        now = _utc_now()
        clean_ref = booking_reference.strip().upper()
        clean_code = code.strip()

        if self.otp_col is None or self.sessions_col is None:
            raise InvalidOtpError("Authentication service is unavailable.")

        # Query latest non-invalidated, non-used OTP code for this booking
        otp_doc = self.otp_col.find_one(
            {
                "booking_reference": clean_ref,
                "is_used": False,
                "is_invalidated": False,
            },
            sort=[("created_at", -1)],
        )

        if otp_doc is None:
            from rosapenna_backend.config import settings
            if settings.security_bypass_in_dev and clean_code == "123456":
                if self.linkages_col is not None:
                    b_doc = self.linkages_col.find_one({"booking_reference": clean_ref})
                    if b_doc:
                        lead_email = b_doc.get("lead_guest", {}).get("email", "")
                        session_token = f"mmb_sess_{secrets.token_urlsafe(32)}"
                        expires_at = now + dt.timedelta(minutes=SESSION_EXPIRY_MINUTES)
                        session_record = {
                            "session_token": session_token,
                            "booking_reference": clean_ref,
                            "lead_guest_email": lead_email,
                            "created_at": now,
                            "expires_at": expires_at,
                            "is_revoked": False,
                        }
                        self.sessions_col.insert_one(session_record)
                        logger.info(
                            "Guest session issued via dev bypass code 123456 for booking %s",
                            clean_ref,
                        )
                        return MmbSessionResponse(
                            session_token=session_token,
                            booking_reference=clean_ref,
                            expires_at=expires_at,
                        )
            raise InvalidOtpError("Invalid or expired verification code.")

        # Check expiration
        doc_expiry = otp_doc["expires_at"]
        if isinstance(doc_expiry, str):
            doc_expiry = dt.datetime.fromisoformat(doc_expiry)
        if doc_expiry.tzinfo is None:
            doc_expiry = doc_expiry.replace(tzinfo=dt.timezone.utc)

        if doc_expiry < now:
            self.otp_col.update_one(
                {"_id": otp_doc["_id"]},
                {"$set": {"is_invalidated": True, "invalidated_reason": "expired"}},
            )
            raise InvalidOtpError("Verification code has expired. Please request a new code.")

        # Check max attempts
        current_attempts = otp_doc.get("attempts", 0) + 1
        max_att = otp_doc.get("max_attempts", OTP_MAX_ATTEMPTS)
        if current_attempts > max_att:
            self.otp_col.update_one(
                {"_id": otp_doc["_id"]},
                {
                    "$set": {
                        "is_invalidated": True,
                        "invalidated_reason": "max_attempts_exceeded",
                    }
                },
            )
            raise InvalidOtpError("Too many incorrect attempts. Please request a new code.")

        # Verify hash using constant-time comparison
        test_hash = _hash_code(clean_code, otp_doc["salt"])
        matches = hmac.compare_digest(test_hash, otp_doc["code_hash"])
        from rosapenna_backend.config import settings
        if not matches and settings.security_bypass_in_dev and clean_code == "123456":
            matches = True

        if not matches:
            is_locked = current_attempts >= max_att
            update_data: Dict[str, Any] = {"attempts": current_attempts}
            if is_locked:
                update_data["is_invalidated"] = True
                update_data["invalidated_reason"] = "max_attempts_exceeded"
            self.otp_col.update_one(
                {"_id": otp_doc["_id"]},
                {"$set": update_data},
            )
            if is_locked:
                raise InvalidOtpError("Too many incorrect attempts. Please request a new code.")
            raise InvalidOtpError("Invalid or expired verification code.")

        # Successful verification: mark single use
        self.otp_col.update_one(
            {"_id": otp_doc["_id"]},
            {
                "$set": {
                    "is_used": True,
                    "used_at": now,
                    "attempts": current_attempts,
                }
            },
        )

        # Invalidate any other pending codes for this booking
        self.otp_col.update_many(
            {
                "booking_reference": clean_ref,
                "_id": {"$ne": otp_doc["_id"]},
                "is_used": False,
            },
            {"$set": {"is_invalidated": True, "invalidated_reason": "superseded_by_login"}},
        )

        # Resolve lead guest email for session principal
        lead_email = ""
        if self.linkages_col is not None:
            b_doc = self.linkages_col.find_one({"booking_reference": clean_ref})
            if b_doc:
                lead_email = b_doc.get("lead_guest", {}).get("email", "")

        # Issue booking-scoped session token
        session_token = f"mmb_sess_{secrets.token_urlsafe(32)}"
        session_expiry = now + dt.timedelta(minutes=SESSION_EXPIRY_MINUTES)

        session_record = {
            "session_token": session_token,
            "booking_reference": clean_ref,
            "lead_guest_email": lead_email,
            "created_at": now,
            "expires_at": session_expiry,
            "is_revoked": False,
        }
        self.sessions_col.insert_one(session_record)

        logger.info(
            "Guest session issued for booking %s (token prefix: %s..., expires in %d mins)",
            clean_ref,
            session_token[:16],
            SESSION_EXPIRY_MINUTES,
        )

        return MmbSessionResponse(
            session_token=session_token,
            booking_reference=clean_ref,
            expires_at=session_expiry,
        )

    def validate_session(
        self,
        session_token: str,
        expected_booking_reference: Optional[str] = None,
    ) -> GuestSessionPrincipal:
        """Validate session token and strictly enforce booking scope."""
        if self.sessions_col is None:
            raise InvalidSessionError("Session service is unavailable.")

        clean_token = session_token.strip()
        session_doc = self.sessions_col.find_one({"session_token": clean_token})

        if session_doc is None or session_doc.get("is_revoked", False):
            raise InvalidSessionError()

        now = _utc_now()
        exp = session_doc["expires_at"]
        if isinstance(exp, str):
            exp = dt.datetime.fromisoformat(exp)
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=dt.timezone.utc)

        if exp < now:
            raise InvalidSessionError("Session has expired. Please sign in again.")

        booking_ref = session_doc["booking_reference"]

        # IDOR prevention: if route targets a specific booking reference, enforce exact match
        if expected_booking_reference is not None:
            clean_expected = expected_booking_reference.strip().upper()
            if booking_ref != clean_expected:
                logger.warning(
                    "IDOR attempt: Session for %s attempted access to %s",
                    booking_ref,
                    clean_expected,
                )
                raise ForbiddenBookingScopeError()

        return GuestSessionPrincipal(
            session_token=clean_token,
            booking_reference=booking_ref,
            lead_guest_email=session_doc.get("lead_guest_email", ""),
            expires_at=exp,
        )

    def logout(self, session_token: str) -> MmbLogoutResponse:
        """Revoke the guest session token immediately."""
        if self.sessions_col is not None:
            clean_token = session_token.strip()
            self.sessions_col.update_one(
                {"session_token": clean_token},
                {"$set": {"is_revoked": True, "revoked_at": _utc_now()}},
            )
        return MmbLogoutResponse()

    def revoke_all_sessions_for_booking(self, booking_reference: str) -> int:
        """Revoke all active sessions and codes for a booking (e.g. on staff ownership correction)."""
        now = _utc_now()
        clean_ref = booking_reference.strip().upper()
        count = 0
        if self.sessions_col is not None:
            res = self.sessions_col.update_many(
                {"booking_reference": clean_ref, "is_revoked": False},
                {"$set": {"is_revoked": True, "revoked_at": now}},
            )
            count = res.modified_count
        if self.otp_col is not None:
            self.otp_col.update_many(
                {"booking_reference": clean_ref, "is_invalidated": False},
                {"$set": {"is_invalidated": True, "invalidated_at": now}},
            )
        return count
