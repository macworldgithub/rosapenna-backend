"""Unit and integration tests for Ticket M13 — Special and Cancellation Requests."""

import datetime as dt
import unittest
from fastapi import HTTPException
from mongomock import MongoClient

from rosapenna_backend.api.dependencies import require_guest_booking_scope
from rosapenna_backend.api.schemas.mmb_auth import GuestSessionPrincipal
from rosapenna_backend.api.schemas.mmb_requests import (
    GuestSpecialRequestCreate,
)
from rosapenna_backend.services.booking_linkage import BookingLinkageService
from rosapenna_backend.services.email_delivery import EmailDeliveryService
from rosapenna_backend.services.mmb_requests import (
    MmbRequestConflictError,
    MmbRequestError,
    MmbRequestNotFoundError,
    MmbRequestService,
)


class TestMmbRequestsM13(unittest.TestCase):
    """Test suite covering M13 special and cancellation request updates."""

    def setUp(self):
        self.mongo = MongoClient(tz_aware=True)
        self.db = self.mongo["test_mmb_m13_db"]
        self.email_service = EmailDeliveryService()
        self.linkage_service = BookingLinkageService(database=self.db)
        self.service = MmbRequestService(
            database=self.db,
            linkage_service=self.linkage_service,
            email_service=self.email_service,
        )

        # Seed confirmed booking linkage
        self.db.mmb_booking_linkages.insert_one({
            "booking_reference": "RSP-20270810-M1301",
            "source_channel": "online",
            "status": "linked",
            "lead_guest": {
                "first_name": "Niamh",
                "last_name": "O'Connell",
                "email": "niamh@example.com",
                "telephone": "+353 74 915 5000",
            },
            "opera_stay": {
                "reservation_id": "OPERA-M1301",
                "confirmation_number": "CONF-M1301",
                "room_category": "DELUXE",
                "room_quantity": 1,
                "arrival_date": "2027-08-10",
                "departure_date": "2027-08-13",
                "total_amount": "1200.00",
                "status": "Confirmed",
                "occupant_names": ["Niamh O'Connell"],
                "assigned_room_number": None,
            },
            "brs_rounds": [
                {
                    "round_id": "RND-OLDTOM-001",
                    "course_id": 1,
                    "course_name": "Old Tom Morris Links",
                    "date": "2027-08-11",
                    "time": "10:00",
                    "holes": "18",
                    "players": 2,
                    "price": "160.00",
                    "currency": "EUR",
                    "status": "Confirmed",
                    "brs_booking_id": "BRS-1301",
                }
            ],
            "payments": [
                {
                    "payment_id": "PAY-1301",
                    "provider": "prommt",
                    "payment_type": "deposit",
                    "amount": "300.00",
                    "currency": "EUR",
                    "status": "completed",
                    "verified_at": dt.datetime(2026, 11, 1, 10, 5, tzinfo=dt.timezone.utc),
                }
            ],
            "pending_requests": [],
            "audit_trail": [],
            "revision": 1,
            "created_at": dt.datetime(2026, 11, 1, 10, 0, tzinfo=dt.timezone.utc),
            "updated_at": dt.datetime(2026, 11, 1, 10, 0, tzinfo=dt.timezone.utc),
        })

    def test_submit_special_request_success(self):
        """Special request persists with unique ID, pending status, and 0 mutation to inventory/ledger."""
        req = GuestSpecialRequestCreate(
            booking_reference="RSP-20270810-M1301",
            request_type="special_request",
            category="dietary",
            request_text="1 guest is coeliac, please prepare gluten-free breakfast options.",
            desired_room_category="Quiet room on ground floor",
            expected_revision=1,
        )

        res = self.service.submit_request(
            booking_reference="RSP-20270810-M1301",
            request=req,
            idempotency_key="idemp_key_req_001_success",
            actor="guest_niamh@example.com",
        )

        self.assertEqual(res.booking_reference, "RSP-20270810-M1301")
        self.assertTrue(res.request_id.startswith("REQ-"))
        self.assertEqual(res.request_type, "special_request")
        self.assertEqual(res.category, "dietary")
        self.assertEqual(res.status, "pending_review")
        self.assertFalse(res.inventory_mutated)
        self.assertFalse(res.payment_mutated)
        self.assertFalse(res.replayed)
        self.assertEqual(res.booking_revision, 2)
        self.assertIn("pending staff review", res.message)

        # Verify DB linkage: confirmed stay and rounds are unchanged
        linkage = self.db.mmb_booking_linkages.find_one({"booking_reference": "RSP-20270810-M1301"})
        self.assertEqual(linkage["revision"], 2)
        self.assertEqual(len(linkage["pending_requests"]), 1)
        stored_req = linkage["pending_requests"][0]
        self.assertEqual(stored_req["request_id"], res.request_id)
        self.assertEqual(stored_req["status"], "pending_review")
        self.assertEqual(stored_req["description"], "1 guest is coeliac, please prepare gluten-free breakfast options.")

        # Crucially: rooms, golf, and payments were NOT mutated
        self.assertEqual(linkage["opera_stay"]["total_amount"], "1200.00")
        self.assertEqual(linkage["brs_rounds"][0]["status"], "Confirmed")
        self.assertEqual(linkage["payments"][0]["status"], "completed")

    def test_cancellation_request_does_not_auto_cancel_or_refund(self):
        """M13 AC 6: Cancellation request is recorded as pending review only, without auto-cancellation or refund."""
        req = GuestSpecialRequestCreate(
            booking_reference="RSP-20270810-M1301",
            request_type="cancellation_request",
            category="cancellation_inquiry",
            request_text="Family illness requires us to cancel our upcoming trip. Please review cancellation policy.",
            expected_revision=1,
        )

        res = self.service.submit_request(
            booking_reference="RSP-20270810-M1301",
            request=req,
            idempotency_key="idemp_key_cancel_001",
            actor="guest_niamh@example.com",
        )

        self.assertEqual(res.request_type, "cancellation_request")
        self.assertEqual(res.status, "pending_review")
        self.assertFalse(res.inventory_mutated)
        self.assertFalse(res.payment_mutated)

        # Authoritative booking linkage remains fully confirmed
        linkage = self.db.mmb_booking_linkages.find_one({"booking_reference": "RSP-20270810-M1301"})
        self.assertEqual(linkage["status"], "linked")
        self.assertEqual(linkage["opera_stay"]["status"], "Confirmed")
        self.assertEqual(len(linkage["payments"]), 1)
        self.assertEqual(linkage["payments"][0]["status"], "completed")  # No refund applied

    def test_xss_sanitization_in_acknowledgement_email(self):
        """User input containing HTML tags is escaped in email acknowledgement."""
        xss_text = "<script>alert('xss')</script><b>Emergency</b>"
        booking_mock = {
            "booking_reference": "RSP-20270810-M1301",
            "lead_guest": {"first_name": "Test<img src=x>", "last_name": "User", "email": "test@example.com"},
        }
        res = self.email_service.send_special_request_acknowledgement(
            booking=booking_mock,
            request_text=xss_text,
            category="general_inquiry",
        )
        self.assertTrue(res.success)
        # Check that simulated email output contains escaped characters, not raw tags
        last_email = self.email_service._dispatched_emails[-1]
        self.assertNotIn("<script>", last_email["html_preview"])
        self.assertIn("&lt;script&gt;", last_email["html_preview"])

    def test_idempotency_replay_and_conflict(self):
        """M13 AC 2: Same idempotency key replays cached response; differing payload raises 409."""
        req = GuestSpecialRequestCreate(
            booking_reference="RSP-20270810-M1301",
            request_type="special_request",
            category="room_preference",
            request_text="High floor room requested please.",
            expected_revision=1,
        )

        # First submission
        res1 = self.service.submit_request(
            booking_reference="RSP-20270810-M1301",
            request=req,
            idempotency_key="idemp_key_replay_m13",
            actor="guest_niamh@example.com",
        )
        self.assertFalse(res1.replayed)

        # Replay identical
        res2 = self.service.submit_request(
            booking_reference="RSP-20270810-M1301",
            request=req,
            idempotency_key="idemp_key_replay_m13",
            actor="guest_niamh@example.com",
        )
        self.assertTrue(res2.replayed)
        self.assertEqual(res2.request_id, res1.request_id)

        # Differing payload with same key
        diff_req = GuestSpecialRequestCreate(
            booking_reference="RSP-20270810-M1301",
            request_type="special_request",
            category="accessibility",  # Changed
            request_text="High floor room requested please.",
            expected_revision=1,
        )
        with self.assertRaises(MmbRequestConflictError):
            self.service.submit_request(
                booking_reference="RSP-20270810-M1301",
                request=diff_req,
                idempotency_key="idemp_key_replay_m13",
                actor="guest_niamh@example.com",
            )

    def test_optimistic_concurrency_conflict(self):
        """Outdated expected_revision raises 409 conflict."""
        req = GuestSpecialRequestCreate(
            booking_reference="RSP-20270810-M1301",
            request_type="special_request",
            category="other",
            request_text="Please arrange taxi from airport.",
            expected_revision=99,
        )
        with self.assertRaises(MmbRequestConflictError):
            self.service.submit_request(
                booking_reference="RSP-20270810-M1301",
                request=req,
                idempotency_key="idemp_key_concurrency_m13",
                actor="guest_niamh@example.com",
            )

    def test_idor_session_scoping(self):
        """Cross-booking submission is denied with HTTP 403."""
        session = GuestSessionPrincipal(
            session_token="test_token_m13",
            booking_reference="RSP-20270810-M1301",
            lead_guest_email="niamh@example.com",
            expires_at=dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=1),
        )
        with self.assertRaises(HTTPException) as ctx:
            require_guest_booking_scope(
                booking_reference="RSP-DIFFERENT-BOOKING",
                session=session,
            )
        self.assertEqual(ctx.exception.status_code, 403)


if __name__ == "__main__":
    unittest.main()
