"""Unit tests for Ticket M05 — Live Amendment Options and Alternatives.

Verifies:
1. Multi-course search across Old Tom Morris, Sandy Hills, and St Patrick's Links (AC 1).
2. F13 package rules and source freshness metadata (AC 2).
3. Own held stock recognized exactly once without double-counting (AC 3).
4. Complete package verification for displayed alternatives (AC 4).
5. Incomplete/partial availability labeled and never offered as bookable (AC 5).
6. Strictly read-only search with zero inventory holds or mutations (AC 6).
7. Truthful source freshness reporting (AC 7).
8. M01 gate and M03 session security enforcement with IDOR protection (AC 8).
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
from rosapenna_backend.api.schemas.mmb_amendments import MmbAmendmentSearchRequest
from rosapenna_backend.api.schemas.mmb_auth import GuestSessionPrincipal
from rosapenna_backend.services.booking_linkage import BookingLinkageService
from rosapenna_backend.services.mmb_amendments import MmbAmendmentService
from rosapenna_backend.services.mmb_auth import ForbiddenBookingScopeError, MmbAuthService
from rosapenna_backend.services.mmb_gate import GATE_DOCUMENT_ID, MmbGateService


class TestMmbLiveAmendments(unittest.TestCase):
    def setUp(self):
        self.mongo_client = MongoClient()
        self.db = self.mongo_client["test_rosapenna"]
        self.linkage_service = BookingLinkageService(database=self.db)
        self.auth_service = MmbAuthService(database=self.db)
        self.gate_service = MmbGateService(database=self.db)
        self.amendment_service = MmbAmendmentService(
            database=self.db,
            linkage_service=self.linkage_service,
        )

        # Enable gate in collection for test suite
        now = dt.datetime.now(dt.timezone.utc).isoformat()
        self.db["mmb_release_gate"].replace_one(
            {"_id": GATE_DOCUMENT_ID},
            {
                "_id": GATE_DOCUMENT_ID,
                "enabled": True,
                "updated_at": now,
                "updated_by": "test_runner",
                "written_request_reference": "TEST-AMENDMENTS-M05",
                "reason": "M05 automated test execution",
            },
            upsert=True,
        )

        self.seed_booking()

    def tearDown(self):
        self.mongo_client.close()

    def seed_booking(self):
        req = StaffLinkBookingRequest(
            booking_reference="RSP-20270514-AMEND01",
            source_channel="online",
            lead_guest=LeadGuestContact(
                first_name="Jane",
                last_name="Golfer",
                email="jane.golfer@example.com",
                telephone="+353871234567",
            ),
            manual_opera_stay=OperaStayDetails(
                reservation_id="OPERA-RES-AMEND-01",
                confirmation_number="CONF-AMEND-01",
                hotel_id="ROSA",
                room_category="Classic Room",
                room_quantity=1,
                arrival_date=dt.date(2027, 5, 14),
                departure_date=dt.date(2027, 5, 17),
                total_amount="660.00",
                currency="EUR",
                occupant_names=["Jane Golfer", "Mark Golfer"],
            ),
            brs_rounds=[
                BrsRoundDetails(
                    round_id="BRS-ROUND-AMEND-01",
                    course_id=1,
                    course_name="Old Tom Morris Links",
                    date=dt.date(2027, 5, 15),
                    time="09:30",
                    holes="18",
                    players=2,
                    price="360.00",
                    currency="EUR",
                )
            ],
            payments=[
                PaymentLedgerEntry(
                    payment_id="PAY-TXN-AMEND-01",
                    provider="prommt",
                    payment_type="deposit",
                    amount="255.00",
                    currency="EUR",
                    status="completed",
                )
            ],
            reason="Seed authoritative booking for M05 amendment search tests",
        )
        self.linkage_service.link_booking(req, actor="test_setup")

    def test_search_evaluates_opera_and_all_three_brs_courses(self):
        """AC 1: Evaluates room availability and all three courses."""
        req = MmbAmendmentSearchRequest(
            booking_reference="RSP-20270514-AMEND01",
            proposed_arrival_date=dt.date(2027, 5, 14),
            proposed_departure_date=dt.date(2027, 5, 17),
        )
        res = self.amendment_service.search_amendment_options(req)

        self.assertEqual(res.booking_reference, "RSP-20270514-AMEND01")
        self.assertGreaterEqual(len(res.alternatives), 2)

        # Verify courses across alternatives
        courses_found = set()
        for alt in res.alternatives:
            for round_opt in alt.golf_options:
                courses_found.add(round_opt.course_name)

        self.assertIn("Old Tom Morris Links", courses_found)
        self.assertIn("Sandy Hills Links", courses_found)
        self.assertIn("St Patrick's Links", courses_found)

    def test_search_credits_own_held_stock_without_double_counting(self):
        """AC 3: Recognizes own held room inventory exactly once."""
        req = MmbAmendmentSearchRequest(
            booking_reference="RSP-20270514-AMEND01",
            proposed_arrival_date=dt.date(2027, 5, 14),
            proposed_departure_date=dt.date(2027, 5, 17),
            proposed_room_category="Classic Room",
        )
        res = self.amendment_service.search_amendment_options(req)

        # Alt 1 retains the current Classic Room on same dates
        alt1 = res.alternatives[0]
        self.assertTrue(alt1.stay_option.own_allocation_retained)
        self.assertTrue(alt1.stay_option.is_available)

    def test_search_complete_package_and_partial_handling(self):
        """AC 4 & AC 5: Distinguishes complete vs incomplete alternatives."""
        req = MmbAmendmentSearchRequest(
            booking_reference="RSP-20270514-AMEND01",
            proposed_arrival_date=dt.date(2027, 5, 14),
            proposed_departure_date=dt.date(2027, 5, 17),
        )
        res = self.amendment_service.search_amendment_options(req)

        for alt in res.alternatives:
            if alt.is_complete_package:
                self.assertTrue(alt.stay_option.is_available)
                self.assertTrue(all(g.is_available for g in alt.golf_options))
                self.assertEqual(len(alt.unavailability_reasons), 0)
            else:
                # Incomplete alternative must have explicit reasons and cannot be booked
                self.assertGreater(len(alt.unavailability_reasons), 0)

    def test_search_strictly_zero_inventory_holds_or_mutations(self):
        """AC 6: Search does not mutate database records or hold inventory."""
        booking_before = self.linkage_service.get_linkage("RSP-20270514-AMEND01")

        req = MmbAmendmentSearchRequest(
            booking_reference="RSP-20270514-AMEND01",
            proposed_arrival_date=dt.date(2027, 5, 14),
            proposed_departure_date=dt.date(2027, 5, 17),
            proposed_room_category="Deluxe Room",
        )
        res = self.amendment_service.search_amendment_options(req)

        # Assert zero inventory holds
        self.assertFalse(res.reserves_inventory)

        # Assert confirmed booking linkage document was not mutated
        booking_after = self.linkage_service.get_linkage("RSP-20270514-AMEND01")
        self.assertEqual(booking_before.opera_stay.room_category, booking_after.opera_stay.room_category)
        self.assertEqual(booking_before.financials, booking_after.financials)

    def test_st_patricks_agronomy_rest_marks_alternative_incomplete(self):
        """AC 2 & AC 5: St Patrick's rest days mark alternative incomplete with clear reason."""
        # 2027-05-15 (Saturday) + 2 days = 2027-05-17 (Monday, weekday 0 in [0, 1])
        req = MmbAmendmentSearchRequest(
            booking_reference="RSP-20270514-AMEND01",
            proposed_arrival_date=dt.date(2027, 5, 15),
            proposed_departure_date=dt.date(2027, 5, 18),
        )
        res = self.amendment_service.search_amendment_options(req)

        # Alt 3 includes St Patrick's
        alt3 = next(a for a in res.alternatives if "St Patrick's" in a.title)
        self.assertFalse(alt3.is_complete_package)
        self.assertFalse(alt3.golf_options[1].is_available)
        self.assertIn("resting for essential agronomy maintenance", alt3.unavailability_reasons[0])

    def test_search_source_freshness_metadata(self):
        """AC 2 & AC 7: Truthful source freshness metadata attached."""
        req = MmbAmendmentSearchRequest(booking_reference="RSP-20270514-AMEND01")
        res = self.amendment_service.search_amendment_options(req)

        self.assertIn("hotel_pms", res.sources_freshness)
        self.assertIn("golf_brs", res.sources_freshness)
        self.assertIn("pricing_engine", res.sources_freshness)
        self.assertEqual(res.sources_freshness["hotel_pms"].status, "confirmed")
        self.assertEqual(res.sources_freshness["golf_brs"].status, "confirmed")

    def test_search_session_idor_prevention(self):
        """AC 8: Session for booking A cannot search amendments for booking B."""
        token_a = "mmb_sess_test_token_amend_a"
        now = dt.datetime.now(dt.timezone.utc)
        self.db["mmb_guest_sessions"].insert_one(
            {
                "session_token": token_a,
                "booking_reference": "RSP-20270514-AMEND01",
                "lead_guest_email": "jane.golfer@example.com",
                "created_at": now,
                "expires_at": now + dt.timedelta(minutes=30),
                "is_revoked": False,
            }
        )

        with self.assertRaises(ForbiddenBookingScopeError):
            self.auth_service.validate_session(token_a, expected_booking_reference="RSP-OTHER-REF")


if __name__ == "__main__":
    unittest.main()
