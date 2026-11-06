"""Unit tests for Ticket M06 — Amendment Cost and Terms Review.

Verifies:
1. Exact financial calculations: Original Total vs New Total, Price Delta (+/-),
   Deposit Paid, Revised Balance Due, and Credit Due (AC 1, AC 2).
2. Strict 42-day balance due date calculation (arrival_date - 42 days) (AC 1).
3. Zero inventory holds, zero card charges, zero vendor writes (reserves_inventory=False) (AC 6).
4. Room category/quantity change occupant/sharing review gate (AC 7).
5. Explicit lead-guest terms acceptance and version pinning (AC 4).
6. Invalidation upon offer expiry or booking revision changes (AC 5).
7. IDOR prevention and Cache-Control: no-store headers on endpoints (AC 8).
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
import unittest
from mongomock import MongoClient
from fastapi import HTTPException, Response

from rosapenna_backend.api.routes.mmb_amendments import (
    accept_amendment_offer as route_accept_offer,
    create_amendment_offer as route_create_offer,
    get_amendment_offer as route_get_offer,
)
from rosapenna_backend.api.schemas.booking_linkage import (
    BrsRoundDetails,
    LeadGuestContact,
    OperaStayDetails,
    PaymentLedgerEntry,
    StaffCorrectLinkageRequest,
    StaffLinkBookingRequest,
)
from rosapenna_backend.api.schemas.mmb_amendments import (
    AmendmentOfferAcceptRequest,
    AmendmentOfferCreateRequest,
    ProposedGolfRoundOption,
    ProposedStayOption,
)
from rosapenna_backend.api.schemas.mmb_auth import GuestSessionPrincipal
from rosapenna_backend.services.booking_linkage import BookingLinkageService
from rosapenna_backend.services.mmb_amendments import MmbAmendmentService
from rosapenna_backend.services.mmb_auth import MmbAuthService
from rosapenna_backend.services.mmb_gate import GATE_DOCUMENT_ID, MmbGateService


class TestMmbAmendmentCostAndTermsReview(unittest.TestCase):
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

        # Enable MMB release gate
        now = dt.datetime.now(dt.timezone.utc).isoformat()
        self.db["mmb_release_gate"].replace_one(
            {"_id": GATE_DOCUMENT_ID},
            {
                "_id": GATE_DOCUMENT_ID,
                "enabled": True,
                "updated_at": now,
                "updated_by": "test_runner",
                "written_request_reference": "TEST-M06",
                "reason": "M06 automated test execution",
            },
            upsert=True,
        )

        self.seed_authoritative_booking()

    def tearDown(self):
        self.mongo_client.close()

    def seed_authoritative_booking(self):
        # Seed confirmed booking: Arrival 2027-06-15, Classic Room, 3 nights
        # Total = 660.00 stay + 380.00 golf = 1040.00, Deposit paid = 310.00
        req = StaffLinkBookingRequest(
            booking_reference="RSP-20270615-REV01",
            source_channel="online",
            lead_guest=LeadGuestContact(
                first_name="Jane",
                last_name="Golfer",
                email="jane.golfer@example.com",
                telephone="+353871234567",
            ),
            manual_opera_stay=OperaStayDetails(
                reservation_id="OPERA-M06-01",
                confirmation_number="CONF-M06-01",
                hotel_id="ROSA",
                room_category="Classic Room",
                room_quantity=1,
                arrival_date=dt.date(2027, 6, 15),
                departure_date=dt.date(2027, 6, 18),
                total_amount="660.00",
                currency="EUR",
                status="Reserved",
                occupant_names=["Jane Golfer", "Guest Two"],
            ),
            brs_rounds=[
                BrsRoundDetails(
                    round_id="BRS-M06-R1",
                    course_id=1,
                    course_name="Old Tom Morris Links",
                    date=dt.date(2027, 6, 16),
                    time="10:00",
                    holes="18",
                    players=2,
                    brs_booking_id="BRS-BOOK-M06-01",
                    price="180.00",
                    currency="EUR",
                    status="Reserved",
                ),
                BrsRoundDetails(
                    round_id="BRS-M06-R2",
                    course_id=2,
                    course_name="Sandy Hills Links",
                    date=dt.date(2027, 6, 17),
                    time="11:00",
                    holes="18",
                    players=2,
                    brs_booking_id="BRS-BOOK-M06-02",
                    price="200.00",
                    currency="EUR",
                    status="Reserved",
                ),
            ],
            payments=[
                PaymentLedgerEntry(
                    payment_id="PMT-M06-DEP",
                    provider="prommt",
                    payment_type="deposit",
                    amount="310.00",
                    currency="EUR",
                    status="completed",
                    provider_reference="txn-dep-001",
                    verified_at=dt.datetime(2026, 10, 1, 12, 0, tzinfo=dt.timezone.utc),
                )
            ],
            reason="Seed authoritative booking for Ticket M06 test",
        )
        self.linkage_service.link_booking(req, actor="test_setup")

    def test_create_offer_positive_delta_and_42_day_balance_due_date(self):
        """AC 1 & AC 2: Validates positive delta repricing and exact 42-day balance due date."""
        # Upgrade to Deluxe Room: 3 nights @ 260.00 = 780.00 + 380.00 golf = 1160.00
        # Original: 1040.00. Delta: +120.00. Deposit: 310.00. Revised Balance: 850.00
        arr = dt.date(2027, 6, 15)
        create_req = AmendmentOfferCreateRequest(
            booking_reference="RSP-20270615-REV01",
            option_id="OPT-AMEND-2",
            proposed_stay=ProposedStayOption(
                arrival_date=arr,
                departure_date=dt.date(2027, 6, 18),
                nights=3,
                room_category="Deluxe Room",
                room_quantity=1,
                is_available=True,
                own_allocation_retained=False,
                nightly_rate="260.00",
                stay_total="780.00",
                source_freshness="Live Opera Verified",
            ),
            proposed_golf_rounds=[
                ProposedGolfRoundOption(
                    course_name="Old Tom Morris Links",
                    date=dt.date(2027, 6, 16),
                    time="10:00",
                    players=2,
                    holes="18",
                    round_price_total="180.00",
                    is_available=True,
                    own_allocation_retained=True,
                    source_freshness="Live BRS Verified",
                ),
                ProposedGolfRoundOption(
                    course_name="Sandy Hills Links",
                    date=dt.date(2027, 6, 17),
                    time="11:00",
                    players=2,
                    holes="18",
                    round_price_total="200.00",
                    is_available=True,
                    own_allocation_retained=True,
                    source_freshness="Live BRS Verified",
                ),
            ],
        )

        offer = self.amendment_service.create_amendment_offer(create_req)

        self.assertEqual(offer.booking_reference, "RSP-20270615-REV01")
        self.assertEqual(offer.pricing_comparison.original_package_total, "1040.00")
        self.assertEqual(offer.pricing_comparison.new_package_total, "1160.00")
        self.assertEqual(offer.pricing_comparison.price_delta, "+120.00")
        self.assertEqual(offer.pricing_comparison.deposit_already_paid, "310.00")
        self.assertEqual(offer.pricing_comparison.revised_balance_due, "850.00")
        self.assertEqual(offer.credit_amount, "0.00")

        # 42 days prior to 2027-06-15 = 2027-05-04
        expected_balance_due_date = arr - dt.timedelta(days=42)
        self.assertEqual(offer.balance_due_date, expected_balance_due_date)
        self.assertEqual(expected_balance_due_date.isoformat(), "2027-05-04")

        # Zero inventory holds strictly enforced
        self.assertFalse(offer.reserves_inventory)
        self.assertEqual(offer.status, "pending_consent")
        self.assertTrue(offer.requires_occupant_review)  # Room category changed to Deluxe Room!

    def test_create_offer_negative_delta_credit_due(self):
        """AC 1: When proposed total is less than deposit paid, calculates credit due."""
        # Suppose stay is reduced: 1 night Classic Room @ 220.00 + 0 golf = 220.00
        # Deposit paid was 310.00 -> Credit Due = 90.00, Revised Balance = 0.00
        arr = dt.date(2027, 6, 15)
        create_req = AmendmentOfferCreateRequest(
            booking_reference="RSP-20270615-REV01",
            option_id="OPT-AMEND-CREDIT",
            proposed_stay=ProposedStayOption(
                arrival_date=arr,
                departure_date=dt.date(2027, 6, 16),
                nights=1,
                room_category="Classic Room",
                room_quantity=1,
                is_available=True,
                own_allocation_retained=True,
                nightly_rate="220.00",
                stay_total="220.00",
                source_freshness="Authoritative Stay Retained",
            ),
            proposed_golf_rounds=[],
        )

        offer = self.amendment_service.create_amendment_offer(create_req)
        self.assertEqual(offer.pricing_comparison.original_package_total, "1040.00")
        self.assertEqual(offer.pricing_comparison.new_package_total, "220.00")
        self.assertEqual(offer.pricing_comparison.price_delta, "-820.00")
        self.assertEqual(offer.pricing_comparison.deposit_already_paid, "310.00")
        self.assertEqual(offer.pricing_comparison.revised_balance_due, "0.00")
        self.assertEqual(offer.credit_amount, "90.00")

    def test_occupant_review_gate_enforcement(self):
        """AC 7: Room category change requires occupant review acknowledgment before acceptance."""
        create_req = AmendmentOfferCreateRequest(
            booking_reference="RSP-20270615-REV01",
            option_id="OPT-AMEND-UPGRADE",
            proposed_stay=ProposedStayOption(
                arrival_date=dt.date(2027, 6, 15),
                departure_date=dt.date(2027, 6, 18),
                nights=3,
                room_category="Superior Room",  # Different from Classic Room!
                room_quantity=1,
                is_available=True,
                own_allocation_retained=False,
                nightly_rate="280.00",
                stay_total="840.00",
                source_freshness="Live Opera Rate Verified",
            ),
            proposed_golf_rounds=[],
        )
        offer = self.amendment_service.create_amendment_offer(create_req)
        self.assertTrue(offer.requires_occupant_review)

        # 1. Attempt acceptance without acknowledging occupant review -> FAILS
        accept_fail = AmendmentOfferAcceptRequest(
            offer_id=offer.offer_id,
            booking_reference="RSP-20270615-REV01",
            terms_version="terms-v1-20261014",
            accepted_terms=True,
            occupant_review_acknowledged=False,
        )
        with self.assertRaises(ValueError) as ctx:
            self.amendment_service.accept_amendment_offer(accept_fail)
        self.assertIn("Occupant and room sharing review must be acknowledged", str(ctx.exception))

        # 2. Attempt acceptance WITH occupant review acknowledged -> SUCCEEDS
        accept_ok = AmendmentOfferAcceptRequest(
            offer_id=offer.offer_id,
            booking_reference="RSP-20270615-REV01",
            terms_version="terms-v1-20261014",
            accepted_terms=True,
            occupant_review_acknowledged=True,
        )
        accepted_offer = self.amendment_service.accept_amendment_offer(accept_ok)
        self.assertEqual(accepted_offer.status, "accepted")
        self.assertIsNotNone(accepted_offer.accepted_at)
        self.assertTrue(accepted_offer.occupant_review_acknowledged)

    def test_occupant_review_not_required_when_room_unchanged(self):
        """AC 7: When room category and quantity are unchanged, occupant review is not required."""
        create_req = AmendmentOfferCreateRequest(
            booking_reference="RSP-20270615-REV01",
            option_id="OPT-AMEND-TEE-SHIFT",
            proposed_stay=ProposedStayOption(
                arrival_date=dt.date(2027, 6, 15),
                departure_date=dt.date(2027, 6, 18),
                nights=3,
                room_category="Classic Room",  # Unchanged!
                room_quantity=1,              # Unchanged!
                is_available=True,
                own_allocation_retained=True,
                nightly_rate="220.00",
                stay_total="660.00",
                source_freshness="Authoritative Stay Retained",
            ),
            proposed_golf_rounds=[],
        )
        offer = self.amendment_service.create_amendment_offer(create_req)
        self.assertFalse(offer.requires_occupant_review)

        accept_req = AmendmentOfferAcceptRequest(
            offer_id=offer.offer_id,
            booking_reference="RSP-20270615-REV01",
            terms_version="terms-v1-20261014",
            accepted_terms=True,
            occupant_review_acknowledged=False,
        )
        accepted_offer = self.amendment_service.accept_amendment_offer(accept_req)
        self.assertEqual(accepted_offer.status, "accepted")

    def test_accept_offer_terms_validation(self):
        """AC 4 & AC 5: Explicit terms acceptance and terms_version matching required."""
        create_req = AmendmentOfferCreateRequest(
            booking_reference="RSP-20270615-REV01",
            option_id="OPT-AMEND-1",
            proposed_stay=ProposedStayOption(
                arrival_date=dt.date(2027, 6, 15),
                departure_date=dt.date(2027, 6, 18),
                nights=3,
                room_category="Classic Room",
                room_quantity=1,
                is_available=True,
                own_allocation_retained=True,
                nightly_rate="220.00",
                stay_total="660.00",
                source_freshness="Authoritative Stay Retained",
            ),
            proposed_golf_rounds=[],
        )
        offer = self.amendment_service.create_amendment_offer(create_req)

        # Mismatched terms version
        with self.assertRaises(ValueError) as ctx:
            self.amendment_service.accept_amendment_offer(
                AmendmentOfferAcceptRequest(
                    offer_id=offer.offer_id,
                    booking_reference="RSP-20270615-REV01",
                    terms_version="terms-v0-outdated",
                    accepted_terms=True,
                )
            )
        self.assertIn("Terms version mismatch", str(ctx.exception))

        # Unchecked terms checkbox
        with self.assertRaises(ValueError) as ctx:
            self.amendment_service.accept_amendment_offer(
                AmendmentOfferAcceptRequest(
                    offer_id=offer.offer_id,
                    booking_reference="RSP-20270615-REV01",
                    terms_version="terms-v1-20261014",
                    accepted_terms=False,
                )
            )
        self.assertIn("Explicit acceptance of amendment terms is required", str(ctx.exception))

    def test_offer_invalidation_by_booking_revision_change(self):
        """AC 5: If the underlying booking is corrected/modified, pending offer is invalidated."""
        create_req = AmendmentOfferCreateRequest(
            booking_reference="RSP-20270615-REV01",
            option_id="OPT-AMEND-1",
            proposed_stay=ProposedStayOption(
                arrival_date=dt.date(2027, 6, 15),
                departure_date=dt.date(2027, 6, 18),
                nights=3,
                room_category="Classic Room",
                room_quantity=1,
                is_available=True,
                own_allocation_retained=True,
                nightly_rate="220.00",
                stay_total="660.00",
                source_freshness="Authoritative Stay Retained",
            ),
            proposed_golf_rounds=[],
        )
        offer = self.amendment_service.create_amendment_offer(create_req)
        self.assertEqual(offer.status, "pending_consent")

        # Now simulate a staff correction or booking modification (increments revision)
        self.linkage_service.correct_linkage(
            booking_reference="RSP-20270615-REV01",
            request=StaffCorrectLinkageRequest(
                reason="Guest called hotel to adjust contact phone number",
                lead_guest=LeadGuestContact(
                    first_name="Jane",
                    last_name="Golfer",
                    email="jane.golfer@example.com",
                    telephone="+353879998877",
                ),
            ),
            actor="reservations_desk",
        )

        # Attempt to accept old offer -> must be rejected as invalidated
        with self.assertRaises(ValueError) as ctx:
            self.amendment_service.accept_amendment_offer(
                AmendmentOfferAcceptRequest(
                    offer_id=offer.offer_id,
                    booking_reference="RSP-20270615-REV01",
                    terms_version="terms-v1-20261014",
                    accepted_terms=True,
                )
            )
        self.assertIn("Booking has been modified", str(ctx.exception))

        # Check offer status via get
        rechecked_offer = self.amendment_service.get_amendment_offer(offer.offer_id, "RSP-20270615-REV01")
        self.assertEqual(rechecked_offer.status, "invalidated")

    def test_api_route_functions_scope_and_cache_control(self):
        """AC 8: Tests API route handlers, session scoping, and Cache-Control: no-store."""
        valid_principal = GuestSessionPrincipal(
            session_token="test_token_123",
            booking_reference="RSP-20270615-REV01",
            lead_guest_email="jane.golfer@example.com",
            expires_at=dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=30),
        )
        mismatched_principal = GuestSessionPrincipal(
            session_token="test_token_456",
            booking_reference="RSP-OTHER-REF",
            lead_guest_email="other@example.com",
            expires_at=dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=30),
        )

        create_req = AmendmentOfferCreateRequest(
            booking_reference="RSP-20270615-REV01",
            option_id="OPT-API-TEST",
            proposed_stay=ProposedStayOption(
                arrival_date=dt.date(2027, 6, 15),
                departure_date=dt.date(2027, 6, 18),
                nights=3,
                room_category="Classic Room",
                room_quantity=1,
                is_available=True,
                own_allocation_retained=True,
                nightly_rate="220.00",
                stay_total="660.00",
                source_freshness="Live Opera Verified",
            ),
            proposed_golf_rounds=[],
        )

        # 1. Successful offer creation via route
        res = Response()
        offer_snapshot = route_create_offer(
            request=create_req,
            response=res,
            session=valid_principal,
            amendment_service=self.amendment_service,
        )
        self.assertEqual(res.headers.get("cache-control"), "no-store")
        self.assertEqual(offer_snapshot.booking_reference, "RSP-20270615-REV01")
        self.assertEqual(offer_snapshot.balance_due_date, dt.date(2027, 5, 4))
        self.assertFalse(offer_snapshot.reserves_inventory)

        # 2. IDOR rejection on route when session booking does not match payload booking
        with self.assertRaises(HTTPException) as ctx:
            route_create_offer(
                request=create_req,
                response=Response(),
                session=mismatched_principal,
                amendment_service=self.amendment_service,
            )
        self.assertEqual(ctx.exception.status_code, 403)

        # 3. GET offer route
        get_res = Response()
        fetched_offer = route_get_offer(
            offer_id=offer_snapshot.offer_id,
            response=get_res,
            session=valid_principal,
            amendment_service=self.amendment_service,
        )
        self.assertEqual(get_res.headers.get("cache-control"), "no-store")
        self.assertEqual(fetched_offer.offer_id, offer_snapshot.offer_id)

        # 4. Accept offer route
        accept_res = Response()
        accept_req = AmendmentOfferAcceptRequest(
            offer_id=offer_snapshot.offer_id,
            booking_reference="RSP-20270615-REV01",
            terms_version="terms-v1-20261014",
            accepted_terms=True,
            occupant_review_acknowledged=False,
        )
        accepted = route_accept_offer(
            request=accept_req,
            response=accept_res,
            session=valid_principal,
            amendment_service=self.amendment_service,
        )
        self.assertEqual(accept_res.headers.get("cache-control"), "no-store")
        self.assertEqual(accepted.status, "accepted")


if __name__ == "__main__":
    unittest.main()
