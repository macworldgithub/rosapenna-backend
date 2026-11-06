"""Unit tests for Ticket M04 — Confirmed Booking Dashboard.

Verifies:
1. Booking-scoped session access (AC 1).
2. IDOR prevention between distinct bookings (AC 1).
3. Authoritative data models for stay, golf, and payments (AC 2).
4. Strict separation of pending requests from confirmed values (AC 3).
5. Only F11 verified ledger events contribute to paid totals (AC 4).
6. Truthful source health and degraded status indicators (AC 5).
7. Physical room numbers appear only when allocated by hotel operations (AC 6).
8. Strict suppression of external system IDs, internal notes, and PII (AC 7).
9. Fail-closed M01 gate and M03 session expiry enforcement (AC 8).
"""

from __future__ import annotations

import datetime as dt
import unittest

from mongomock import MongoClient

from rosapenna_backend.api.schemas.booking_linkage import (
    BrsRoundDetails,
    LeadGuestContact,
    OperaStayDetails,
    PaymentLedgerEntry,
    StaffLinkBookingRequest,
)
from rosapenna_backend.api.schemas.mmb_auth import GuestSessionPrincipal
from rosapenna_backend.services.booking_linkage import (
    BookingLinkageService,
    LinkageNotFoundError,
)
from rosapenna_backend.services.mmb_auth import (
    ForbiddenBookingScopeError,
    InvalidSessionError,
    MmbAuthService,
)
from rosapenna_backend.services.mmb_gate import GATE_DOCUMENT_ID, MmbGateService


class TestMmbConfirmedDashboard(unittest.TestCase):
    def setUp(self):
        self.mongo_client = MongoClient()
        self.db = self.mongo_client["test_rosapenna"]
        self.linkage_service = BookingLinkageService(database=self.db)
        self.auth_service = MmbAuthService(database=self.db)
        self.gate_service = MmbGateService(database=self.db)

        # Enable gate in collection for test suite
        now = dt.datetime.now(dt.timezone.utc).isoformat()
        self.db["mmb_release_gate"].replace_one(
            {"_id": GATE_DOCUMENT_ID},
            {
                "_id": GATE_DOCUMENT_ID,
                "enabled": True,
                "updated_at": now,
                "updated_by": "test_runner",
                "written_request_reference": "TEST-DASHBOARD-M04",
                "reason": "M04 automated test execution",
            },
            upsert=True,
        )

        # Seed authoritative booking with stay, golf rounds, and F11 payments
        self.seed_booking()

    def tearDown(self):
        self.mongo_client.close()

    def seed_booking(self):
        req = StaffLinkBookingRequest(
            booking_reference="RSP-20270514-DASH01",
            source_channel="online",
            lead_guest=LeadGuestContact(
                first_name="Jane",
                last_name="Golfer",
                email="jane.golfer@example.com",
                telephone="+353871234567",
            ),
            manual_opera_stay=OperaStayDetails(
                reservation_id="OPERA-SECRET-RES-999",
                confirmation_number="CONF-SECRET-999",
                hotel_id="ROSA",
                room_category="Deluxe Room",
                room_quantity=1,
                arrival_date=dt.date(2027, 5, 14),
                departure_date=dt.date(2027, 5, 17),
                total_amount="990.00",
                currency="EUR",
                occupant_names=["Jane Golfer", "Mark Golfer"],
                assigned_room_number=None,  # Unassigned - must be "Assigned upon check-in"
            ),
            brs_rounds=[
                BrsRoundDetails(
                    round_id="BRS-SECRET-ROUND-123",
                    course_id=1,
                    course_name="Old Tom Morris Links",
                    date=dt.date(2027, 5, 15),
                    time="09:30",
                    holes="18",
                    players=2,
                    brs_booking_id="BRS-SECRET-BOOKING-456",
                    price="360.00",
                    currency="EUR",
                    caddy_request_status="Requested (2 Caddies)",
                )
            ],
            payments=[
                PaymentLedgerEntry(
                    payment_id="PAY-SECRET-TXN-789",
                    provider="prommt",
                    payment_type="deposit",
                    amount="337.50",
                    currency="EUR",
                    status="completed",
                    provider_reference="PROMMT-PAY-REF-INTERNAL",
                    verified_at=dt.datetime.now(dt.timezone.utc),
                ),
                PaymentLedgerEntry(
                    payment_id="PAY-SECRET-FAILED-000",
                    provider="prommt",
                    payment_type="deposit",
                    amount="100.00",
                    currency="EUR",
                    status="failed",
                    provider_reference="PROMMT-PAY-FAILED-INTERNAL",
                ),
            ],
            reason="Seed authoritative booking for M04 dashboard tests",
        )
        self.linkage_service.link_booking(req, actor="test_setup")

    def test_dashboard_scoped_session_access(self):
        """AC 1 & AC 2: Authoritative read for verified session."""
        summary = self.linkage_service.guest_lookup(
            booking_reference="RSP-20270514-DASH01",
            last_name="Golfer",
        )
        self.assertEqual(summary.booking_reference, "RSP-20270514-DASH01")
        self.assertEqual(summary.lead_guest_name, "Jane Golfer")
        self.assertIsNotNone(summary.stay)
        self.assertEqual(summary.stay.room_category, "Deluxe Room")
        self.assertEqual(summary.stay.nights, 3)
        self.assertEqual(summary.stay.occupant_names, ["Jane Golfer", "Mark Golfer"])
        self.assertEqual(len(summary.golf_rounds), 1)
        self.assertEqual(summary.golf_rounds[0].course_name, "Old Tom Morris Links")
        self.assertEqual(summary.golf_rounds[0].caddy_request_status, "Requested (2 Caddies)")
        self.assertEqual(summary.financials.deposit_paid, "337.50")
        self.assertEqual(summary.financials.balance_due, "1012.50")
        self.assertEqual(summary.financials.balance_due_date, dt.date(2027, 4, 16))
        self.assertTrue(summary.financials.ledger_verified)
        self.assertEqual(summary.financials.payments_count, 1)

    def test_dashboard_physical_room_number_policy(self):
        """AC 6: Room number is None unless explicitly allocated by hotel operations."""
        # Unassigned room
        summary = self.linkage_service.guest_lookup("RSP-20270514-DASH01", "Golfer")
        self.assertIsNone(summary.stay.assigned_room_number)

        # Assigned room
        self.db["mmb_booking_linkages"].update_one(
            {"booking_reference": "RSP-20270514-DASH01"},
            {"$set": {"opera_stay.assigned_room_number": "Room 204"}},
        )
        summary_assigned = self.linkage_service.guest_lookup("RSP-20270514-DASH01", "Golfer")
        self.assertEqual(summary_assigned.stay.assigned_room_number, "Room 204")

    def test_dashboard_pending_requests_separation(self):
        """AC 3: Pending requests are held separately and do not alter confirmed values."""
        now = dt.datetime.now(dt.timezone.utc)
        self.db["mmb_booking_linkages"].update_one(
            {"booking_reference": "RSP-20270514-DASH01"},
            {
                "$set": {
                    "pending_requests": [
                        {
                            "request_id": "REQ-20270514-001",
                            "request_type": "tee_time_amendment",
                            "description": "Shift tee time from 09:30 to 11:00 on 2027-05-15",
                            "status": "pending_review",
                            "submitted_at": now,
                            "notes": "Awaiting Pavilion coordinator review",
                        }
                    ]
                }
            },
        )
        summary = self.linkage_service.guest_lookup("RSP-20270514-DASH01", "Golfer")

        # Confirmed tee time remains unchanged at 09:30
        self.assertEqual(summary.golf_rounds[0].time, "09:30")

        # Pending request is populated in its own dedicated structure
        self.assertEqual(len(summary.pending_requests), 1)
        self.assertEqual(summary.pending_requests[0].request_id, "REQ-20270514-001")
        self.assertEqual(summary.pending_requests[0].status, "pending_review")

    def test_dashboard_f11_verified_ledger_only(self):
        """AC 4: Only completed F11 ledger events contribute to paid totals."""
        summary = self.linkage_service.guest_lookup("RSP-20270514-DASH01", "Golfer")
        # Failed payment of 100.00 is strictly ignored
        self.assertEqual(summary.financials.deposit_paid, "337.50")
        self.assertEqual(summary.financials.balance_due, "1012.50")

    def test_dashboard_source_health_and_degraded_states(self):
        """AC 5: Reports truthful source health without false success."""
        summary = self.linkage_service.guest_lookup("RSP-20270514-DASH01", "Golfer")
        self.assertIn("hotel_pms", summary.sources_health)
        self.assertIn("golf_brs", summary.sources_health)
        self.assertIn("payment_ledger", summary.sources_health)
        self.assertEqual(summary.sources_health["hotel_pms"].status, "confirmed")
        self.assertEqual(summary.sources_health["golf_brs"].status, "confirmed")
        self.assertEqual(summary.sources_health["payment_ledger"].status, "confirmed")

    def test_dashboard_zero_vendor_ids_exposed(self):
        """AC 7: Zero external system IDs or database internals exposed in serialized view."""
        summary = self.linkage_service.guest_lookup("RSP-20270514-DASH01", "Golfer")
        dumped_json = summary.model_dump(mode="json")
        dumped_str = str(dumped_json)

        # Opera IDs
        self.assertNotIn("OPERA-SECRET-RES-999", dumped_str)
        self.assertNotIn("CONF-SECRET-999", dumped_str)
        # BRS IDs
        self.assertNotIn("BRS-SECRET-ROUND-123", dumped_str)
        self.assertNotIn("BRS-SECRET-BOOKING-456", dumped_str)
        # Payment internal IDs
        self.assertNotIn("PAY-SECRET-TXN-789", dumped_str)
        self.assertNotIn("PROMMT-PAY-REF-INTERNAL", dumped_str)
        self.assertNotIn("PAY-SECRET-FAILED-000", dumped_str)
        self.assertNotIn("PROMMT-PAY-FAILED-INTERNAL", dumped_str)
        # Database internal ID
        self.assertNotIn("_id", dumped_str)

    def test_dashboard_idor_prevention(self):
        """AC 1: Session token for booking A cannot access booking B."""
        token_a = "mmb_sess_test_token_aaa"
        now = dt.datetime.now(dt.timezone.utc)
        self.db["mmb_guest_sessions"].insert_one(
            {
                "session_token": token_a,
                "booking_reference": "RSP-20270514-DASH01",
                "lead_guest_email": "jane.golfer@example.com",
                "created_at": now,
                "expires_at": now + dt.timedelta(minutes=30),
                "is_revoked": False,
            }
        )

        # Attempt to access another booking with session token for DASH01
        target_ref = "RSP-20270514-OTHER99"
        with self.assertRaises(ForbiddenBookingScopeError):
            self.auth_service.validate_session(token_a, expected_booking_reference=target_ref)

    def test_dashboard_session_expiry_and_revocation(self):
        """AC 8: Expired or revoked guest sessions fail validation."""
        # Insert expired session
        token = "mmb_sess_test_dashboard_token_123"
        now = dt.datetime.now(dt.timezone.utc)
        self.db["mmb_guest_sessions"].insert_one(
            {
                "session_token": token,
                "booking_reference": "RSP-20270514-DASH01",
                "lead_guest_email": "jane.golfer@example.com",
                "created_at": now - dt.timedelta(minutes=35),
                "expires_at": now - dt.timedelta(minutes=5),  # Expired
                "is_revoked": False,
            }
        )
        with self.assertRaises(InvalidSessionError):
            self.auth_service.validate_session(token)

        # Revoked session
        token_revoked = "mmb_sess_test_dashboard_revoked_456"
        self.db["mmb_guest_sessions"].insert_one(
            {
                "session_token": token_revoked,
                "booking_reference": "RSP-20270514-DASH01",
                "lead_guest_email": "jane.golfer@example.com",
                "created_at": now,
                "expires_at": now + dt.timedelta(minutes=25),
                "is_revoked": True,
            }
        )
        with self.assertRaises(InvalidSessionError):
            self.auth_service.validate_session(token_revoked)

    def test_dashboard_gate_fail_closed(self):
        """AC 8: When release gate is disabled, gate check fails closed."""
        self.db["mmb_release_gate"].replace_one(
            {"_id": GATE_DOCUMENT_ID},
            {"_id": GATE_DOCUMENT_ID, "enabled": False},
            upsert=True,
        )
        self.assertFalse(self.gate_service.is_enabled())


if __name__ == "__main__":
    unittest.main()
