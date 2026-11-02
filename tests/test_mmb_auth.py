"""Unit and integration tests for Ticket M03 — Booking Lookup and One-Time Security Code (OTP)."""

import datetime as dt
import unittest
from unittest.mock import MagicMock
from fastapi import HTTPException
from mongomock import MongoClient

from rosapenna_backend.api.dependencies import (
    get_current_guest_session,
    require_guest_booking_scope,
    require_mmb_enabled,
)
from rosapenna_backend.api.routes.mmb_auth import (
    get_authenticated_booking_summary,
    get_session_profile,
    logout_guest_session,
    request_one_time_code,
    verify_one_time_code,
)
from rosapenna_backend.api.schemas.booking_linkage import (
    LeadGuestContact,
    OperaStayDetails,
    StaffLinkBookingRequest,
)
from rosapenna_backend.api.schemas.mmb_auth import (
    GuestSessionPrincipal,
    RequestOtpRequest,
    VerifyOtpRequest,
)
from rosapenna_backend.config import Settings
from rosapenna_backend.integrations.opera.client import OperaClient
from rosapenna_backend.services.booking_linkage import BookingLinkageService
from rosapenna_backend.services.email_delivery import EmailDeliveryService
from rosapenna_backend.services.mmb_auth import (
    ForbiddenBookingScopeError,
    InvalidOtpError,
    InvalidSessionError,
    MmbAuthService,
    MmbOtpCooldownError,
)
from rosapenna_backend.services.mmb_gate import MmbGateService


class TestMmbAuthService(unittest.TestCase):
    """Test OTP generation, hash-at-rest, single-use, resend invalidation, cooldown, and sessions."""

    def setUp(self):
        self.mongo = MongoClient(tz_aware=True)
        self.db = self.mongo["test_mmb_auth_db"]
        self.email_service = EmailDeliveryService()
        self.linkage_service = BookingLinkageService(
            database=self.db,
            opera_client=OperaClient(),
        )
        self.auth_service = MmbAuthService(
            database=self.db,
            email_service=self.email_service,
        )

        # Seed sample linked booking
        link_req = StaffLinkBookingRequest(
            booking_reference="RSP-20270514-AUTH01",
            source_channel="online",
            lead_guest=LeadGuestContact(
                first_name="Jane",
                last_name="Golfer",
                email="jane.golfer@example.com",
                telephone="+353871234567",
            ),
            manual_opera_stay=OperaStayDetails(
                reservation_id="RES-AUTH-01",
                confirmation_number="CONF-AUTH-01",
                hotel_id="ROSA",
                room_category="DELUXE",
                room_quantity=1,
                arrival_date=dt.date(2027, 5, 14),
                departure_date=dt.date(2027, 5, 17),
                total_amount="990.00",
                currency="EUR",
                occupant_names=["Jane Golfer"],
            ),
            brs_rounds=[],
            payments=[],
            reason="Seed booking for auth tests",
        )
        self.linkage_service.link_booking(link_req, "staff_tester")

    def tearDown(self):
        self.mongo.close()

    def test_request_otp_matching_booking_sends_email(self):
        """Requesting OTP for an exact matching reference and surname sends email with 6-digit code."""
        res = self.auth_service.request_otp("RSP-20270514-AUTH01", "Golfer")
        self.assertTrue(res.success)
        self.assertIn("If the booking details match our records", res.message)

        # Verify email was dispatched to the stored email address
        dispatched = self.email_service._dispatched_emails
        self.assertEqual(len(dispatched), 1)
        self.assertEqual(dispatched[0]["recipient"], "jane.golfer@example.com")
        self.assertEqual(dispatched[0]["template"], "one_time_code")

        # Verify OTP record in database is hashed (never stored in plaintext)
        otp_doc = self.db["mmb_otp_codes"].find_one({"booking_reference": "RSP-20270514-AUTH01"})
        self.assertIsNotNone(otp_doc)
        self.assertNotIn("123456", str(otp_doc))
        self.assertIn("code_hash", otp_doc)
        self.assertIn("salt", otp_doc)
        self.assertFalse(otp_doc["is_used"])
        self.assertFalse(otp_doc["is_invalidated"])

    def test_request_otp_non_matching_returns_generic_response(self):
        """Non-matching booking reference or surname returns identical generic response with no email sent."""
        res = self.auth_service.request_otp("RSP-NONEXISTENT", "Golfer")
        self.assertTrue(res.success)
        self.assertIn("If the booking details match our records", res.message)

        # No email sent
        self.assertEqual(len(self.email_service._dispatched_emails), 0)

        # Wrong surname also sends no email
        res_wrong_name = self.auth_service.request_otp("RSP-20270514-AUTH01", "WrongName")
        self.assertTrue(res_wrong_name.success)
        self.assertEqual(len(self.email_service._dispatched_emails), 0)

    def test_request_otp_cooldown_throttling(self):
        """Repeated OTP requests within the 60-second cooldown window are throttled."""
        self.auth_service.request_otp("RSP-20270514-AUTH01", "Golfer")

        # Immediate repeat should trigger cooldown
        with self.assertRaises(MmbOtpCooldownError):
            self.auth_service.request_otp("RSP-20270514-AUTH01", "Golfer")

    def test_request_otp_resend_invalidates_prior_code(self):
        """Requesting a new OTP code invalidates any previous active code for that booking."""
        self.auth_service.request_otp("RSP-20270514-AUTH01", "Golfer")
        first_doc = self.db["mmb_otp_codes"].find_one({"booking_reference": "RSP-20270514-AUTH01"})

        # Simulate time passing past cooldown
        self.db["mmb_otp_codes"].update_one(
            {"_id": first_doc["_id"]},
            {"$set": {"created_at": dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=65)}},
        )

        # Request second code
        self.auth_service.request_otp("RSP-20270514-AUTH01", "Golfer")
        first_doc_reloaded = self.db["mmb_otp_codes"].find_one({"_id": first_doc["_id"]})
        self.assertTrue(first_doc_reloaded["is_invalidated"])

    def test_verify_otp_success_and_single_use(self):
        """Correct OTP code issues a session token and marks code consumed (single-use)."""
        self.auth_service.request_otp("RSP-20270514-AUTH01", "Golfer")

        # Extract dispatched code from sandbox email log
        dispatched_html = self.email_service._dispatched_emails[0]
        # In sandbox, we can extract the code sent to test verification
        # The service sent code; let's test directly with matching code
        otp_doc = self.db["mmb_otp_codes"].find_one({"booking_reference": "RSP-20270514-AUTH01"})

        # Let's seed a known code hash to verify cleanly
        import hashlib
        known_code = "654321"
        known_salt = "testsalt1234"
        known_hash = hashlib.sha256((known_code + known_salt).encode("utf-8")).hexdigest()
        self.db["mmb_otp_codes"].update_one(
            {"_id": otp_doc["_id"]},
            {"$set": {"code_hash": known_hash, "salt": known_salt}},
        )

        session = self.auth_service.verify_otp("RSP-20270514-AUTH01", known_code)
        self.assertEqual(session.booking_reference, "RSP-20270514-AUTH01")
        self.assertTrue(session.session_token.startswith("mmb_sess_"))
        self.assertGreater(session.expires_at, dt.datetime.now(dt.timezone.utc))

        # Check code is marked used
        updated_doc = self.db["mmb_otp_codes"].find_one({"_id": otp_doc["_id"]})
        self.assertTrue(updated_doc["is_used"])

        # Replay attempt fails (single use)
        with self.assertRaises(InvalidOtpError):
            self.auth_service.verify_otp("RSP-20270514-AUTH01", known_code)

    def test_verify_otp_incorrect_code_and_max_attempts(self):
        """Incorrect code increments attempts and permanently invalidates after 5 failures."""
        self.auth_service.request_otp("RSP-20270514-AUTH01", "Golfer")
        otp_doc = self.db["mmb_otp_codes"].find_one({"booking_reference": "RSP-20270514-AUTH01"})

        # 4 incorrect attempts
        for _ in range(4):
            with self.assertRaises(InvalidOtpError):
                self.auth_service.verify_otp("RSP-20270514-AUTH01", "000000")

        doc_after_4 = self.db["mmb_otp_codes"].find_one({"_id": otp_doc["_id"]})
        self.assertEqual(doc_after_4["attempts"], 4)
        self.assertFalse(doc_after_4["is_invalidated"])

        # 5th incorrect attempt invalidates code
        with self.assertRaises(InvalidOtpError) as cm:
            self.auth_service.verify_otp("RSP-20270514-AUTH01", "000000")
        self.assertIn("Too many incorrect attempts", str(cm.exception))

        doc_after_5 = self.db["mmb_otp_codes"].find_one({"_id": otp_doc["_id"]})
        self.assertTrue(doc_after_5["is_invalidated"])

    def test_verify_otp_expired_code_rejected(self):
        """Expired code (>10 minutes) is rejected and invalidated."""
        self.auth_service.request_otp("RSP-20270514-AUTH01", "Golfer")
        # Backdate expiration
        past = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=15)
        self.db["mmb_otp_codes"].update_one(
            {"booking_reference": "RSP-20270514-AUTH01"},
            {"$set": {"expires_at": past}},
        )

        with self.assertRaises(InvalidOtpError) as cm:
            self.auth_service.verify_otp("RSP-20270514-AUTH01", "123456")
        self.assertIn("expired", str(cm.exception).lower())

    def test_session_validation_and_idor_protection(self):
        """Session token authorizes exact booking reference and forbids accessing other bookings."""
        # 1. Create session manually in db
        token = "mmb_sess_test_token_123"
        now = dt.datetime.now(dt.timezone.utc)
        self.db["mmb_guest_sessions"].insert_one({
            "session_token": token,
            "booking_reference": "RSP-20270514-AUTH01",
            "lead_guest_email": "jane.golfer@example.com",
            "created_at": now,
            "expires_at": now + dt.timedelta(minutes=30),
            "is_revoked": False,
        })

        # Exact matching booking ref passes
        principal = self.auth_service.validate_session(token, "RSP-20270514-AUTH01")
        self.assertEqual(principal.booking_reference, "RSP-20270514-AUTH01")

        # Different booking ref raises ForbiddenBookingScopeError (IDOR Prevention)
        with self.assertRaises(ForbiddenBookingScopeError):
            self.auth_service.validate_session(token, "RSP-DIFFERENT-BOOKING")

    def test_session_expiry_and_revocation(self):
        """Expired or revoked session raises InvalidSessionError."""
        token = "mmb_sess_exp_token"
        past = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=1)
        self.db["mmb_guest_sessions"].insert_one({
            "session_token": token,
            "booking_reference": "RSP-20270514-AUTH01",
            "lead_guest_email": "jane.golfer@example.com",
            "created_at": past - dt.timedelta(minutes=30),
            "expires_at": past,
            "is_revoked": False,
        })

        with self.assertRaises(InvalidSessionError):
            self.auth_service.validate_session(token)

        # Logout revokes active session
        valid_token = "mmb_sess_valid_token"
        future = dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=30)
        self.db["mmb_guest_sessions"].insert_one({
            "session_token": valid_token,
            "booking_reference": "RSP-20270514-AUTH01",
            "lead_guest_email": "jane.golfer@example.com",
            "created_at": dt.datetime.now(dt.timezone.utc),
            "expires_at": future,
            "is_revoked": False,
        })
        self.auth_service.logout(valid_token)
        with self.assertRaises(InvalidSessionError):
            self.auth_service.validate_session(valid_token)


class TestMmbAuthRoutes(unittest.TestCase):
    """Test API route endpoints and MMB release gate integration."""

    def setUp(self):
        self.mongo = MongoClient(tz_aware=True)
        self.db = self.mongo["test_mmb_routes_db"]
        self.settings = Settings(
            environment="production",
            api_key="M@cWorld123!!",
            staff_api_key="M@cWorld123!!",
            security_bypass_in_dev=False,
        )
        self.email_service = EmailDeliveryService()
        self.linkage_service = BookingLinkageService(
            database=self.db,
            opera_client=OperaClient(),
        )
        self.auth_service = MmbAuthService(
            database=self.db,
            email_service=self.email_service,
        )
        self.mmb_gate_service = MmbGateService(
            database=self.db,
            settings=self.settings,
        )

        # Seed booking
        link_req = StaffLinkBookingRequest(
            booking_reference="RSP-20270514-ROUTE01",
            source_channel="online",
            lead_guest=LeadGuestContact(
                first_name="Jane",
                last_name="Golfer",
                email="jane.golfer@example.com",
                telephone="+353871234567",
            ),
            manual_opera_stay=OperaStayDetails(
                reservation_id="RES-ROUTE-01",
                confirmation_number="CONF-ROUTE-01",
                hotel_id="ROSA",
                room_category="DELUXE",
                room_quantity=1,
                arrival_date=dt.date(2027, 5, 14),
                departure_date=dt.date(2027, 5, 17),
                total_amount="990.00",
                currency="EUR",
                occupant_names=["Jane Golfer"],
            ),
            brs_rounds=[],
            payments=[],
            reason="Route test booking",
        )
        self.linkage_service.link_booking(link_req, "staff_tester")

    def tearDown(self):
        self.mongo.close()

    def test_routes_gated_when_mmb_is_disabled(self):
        """When MMB release gate is OFF (default), require_mmb_enabled blocks OTP requests with HTTP 503."""
        mock_request = MagicMock()
        mock_request.app.state.mmb_gate_service = self.mmb_gate_service
        with self.assertRaises(HTTPException) as cm:
            require_mmb_enabled(mock_request)
        self.assertEqual(cm.exception.status_code, 503)

    def test_routes_work_when_mmb_is_enabled(self):
        """When MMB is enabled, request-otp, verify-otp, authenticated booking lookup, and logout succeed."""
        # Enable gate
        self.db["mmb_release_gate"].update_one(
            {"_id": "mmb_release_gate"},
            {"$set": {"enabled": True, "updated_at": dt.datetime.now(dt.timezone.utc).isoformat()}},
            upsert=True,
        )

        # 1. Request OTP via route
        req = RequestOtpRequest(booking_reference="RSP-20270514-ROUTE01", last_name="Golfer")
        res = request_one_time_code(req, self.auth_service)
        self.assertTrue(res.success)

        # 2. Extract code from DB to simulate user entering email code
        import hashlib
        known_code = "789123"
        salt = "routesalt123"
        code_hash = hashlib.sha256((known_code + salt).encode("utf-8")).hexdigest()
        self.db["mmb_otp_codes"].update_one(
            {"booking_reference": "RSP-20270514-ROUTE01"},
            {"$set": {"code_hash": code_hash, "salt": salt}},
        )

        # 3. Verify OTP via route
        verify_req = VerifyOtpRequest(booking_reference="RSP-20270514-ROUTE01", code=known_code)
        session_res = verify_one_time_code(verify_req, self.auth_service)
        self.assertEqual(session_res.booking_reference, "RSP-20270514-ROUTE01")
        self.assertTrue(session_res.session_token.startswith("mmb_sess_"))

        # 4. Authenticated booking summary via route
        principal = GuestSessionPrincipal(
            session_token=session_res.session_token,
            booking_reference="RSP-20270514-ROUTE01",
            lead_guest_email="jane.golfer@example.com",
            expires_at=session_res.expires_at,
        )
        booking_summary = get_authenticated_booking_summary(
            "RSP-20270514-ROUTE01",
            principal,
            self.linkage_service,
        )
        self.assertEqual(booking_summary.booking_reference, "RSP-20270514-ROUTE01")
        self.assertEqual(booking_summary.lead_guest_name, "Jane Golfer")

        # 5. IDOR test: trying to access different booking fails with 403
        with self.assertRaises(HTTPException) as cm:
            require_guest_booking_scope("RSP-OTHER-999", principal)
        self.assertEqual(cm.exception.status_code, 403)

        # 6. Logout route
        logout_res = logout_guest_session(principal, self.auth_service)
        self.assertTrue(logout_res.success)


if __name__ == "__main__":
    unittest.main()
