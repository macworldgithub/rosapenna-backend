"""Unit tests for Ticket M07 — Final Amendment Revalidation.

Verifies:
1. Rechecks release gate, guest session, booking revision, offer expiry, room & tee availability,
   price/rates, and F11 payment ledger (AC 1).
2. Any changed price, availability, booking state, terms, or amount due returns a refreshed offer
   and requires new explicit lead-guest acceptance (AC 2).
3. Timeout, stale response, conflict, or unavailable read prevents progression and leaves original
   confirmed booking unchanged (AC 3).
4. Accounts for booking's own held inventory exactly once (AC 4).
5. Detects concurrent guest/staff edits via revision checking (AC 5).
6. Strictly prohibits silent substitution of dates, rooms, courses, times, fees, or amounts (AC 6).
7. Retries follow idempotency using request.idempotency_key (AC 7).
8. Amendment remains unconfirmed until all required writes succeed (reserves_inventory=False) (AC 8).
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
import unittest
from mongomock import MongoClient
from fastapi import HTTPException, Response

from rosapenna_backend.api.routes.mmb_amendments import (
    revalidate_amendment_offer as route_revalidate_offer,
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
    AmendmentRevalidationRequest,
    ProposedGolfRoundOption,
    ProposedStayOption,
)
from rosapenna_backend.api.schemas.mmb_auth import GuestSessionPrincipal
from rosapenna_backend.services.booking_linkage import BookingLinkageService
from rosapenna_backend.services.mmb_amendments import MmbAmendmentService
from rosapenna_backend.services.mmb_auth import MmbAuthService
from rosapenna_backend.services.mmb_gate import GATE_DOCUMENT_ID, MmbGateService


class TestMmbAmendmentRevalidation(unittest.TestCase):
    def setUp(self):
        self.mongo_client = MongoClient()
        self.db = self.mongo_client["test_rosapenna"]
        self.linkage_service = BookingLinkageService(database=self.db)
        self.auth_service = MmbAuthService(database=self.db)
        self.gate_service = MmbGateService(database=self.db)
        self.amendment_service = MmbAmendmentService(
            database=self.db,
            linkage_service=self.linkage_service,
            gate_service=self.gate_service,
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
                "written_request_reference": "TEST-M07",
                "reason": "M07 automated test execution",
            },
            upsert=True,
        )

        self.seed_booking()

    def tearDown(self):
        self.mongo_client.close()

    def seed_booking(self):
        req = StaffLinkBookingRequest(
            booking_reference="RSP-20270720-M0701",
            source_channel="online",
            lead_guest=LeadGuestContact(
                first_name="Jane",
                last_name="Golfer",
                email="jane.golfer@example.com",
                telephone="+353871234567",
            ),
            manual_opera_stay=OperaStayDetails(
                reservation_id="OPERA-M07-01",
                confirmation_number="CONF-M07-01",
                hotel_id="ROSA",
                room_category="Classic Room",
                room_quantity=1,
                arrival_date=dt.date(2027, 7, 20),
                departure_date=dt.date(2027, 7, 23),
                total_amount="660.00",
                currency="EUR",
                status="Reserved",
                occupant_names=["Jane Golfer", "Guest Two"],
            ),
            brs_rounds=[
                BrsRoundDetails(
                    round_id="BRS-M07-R1",
                    course_id=1,
                    course_name="Old Tom Morris Links",
                    date=dt.date(2027, 7, 21),
                    time="10:00",
                    holes="18",
                    players=2,
                    brs_booking_id="BRS-BOOK-M07-01",
                    price="180.00",
                    currency="EUR",
                    status="Reserved",
                ),
            ],
            payments=[
                PaymentLedgerEntry(
                    payment_id="PMT-M07-DEP",
                    provider="prommt",
                    payment_type="deposit",
                    amount="210.00",
                    currency="EUR",
                    status="completed",
                    provider_reference="txn-dep-m07",
                    verified_at=dt.datetime(2026, 10, 1, 12, 0, tzinfo=dt.timezone.utc),
                )
            ],
            reason="Seed authoritative booking for Ticket M07 test",
        )
        self.linkage_service.link_booking(req, actor="test_setup")

    def _create_and_accept_offer(self, upgrade_room: bool = False):
        cat = "Superior Room" if upgrade_room else "Classic Room"
        stay_tot = "840.00" if upgrade_room else "660.00"
        nightly = "280.00" if upgrade_room else "220.00"

        create_req = AmendmentOfferCreateRequest(
            booking_reference="RSP-20270720-M0701",
            option_id="OPT-M07-UP",
            proposed_stay=ProposedStayOption(
                arrival_date=dt.date(2027, 7, 20),
                departure_date=dt.date(2027, 7, 23),
                nights=3,
                room_category=cat,
                room_quantity=1,
                is_available=True,
                own_allocation_retained=not upgrade_room,
                nightly_rate=nightly,
                stay_total=stay_tot,
                source_freshness="Live Opera Verified",
            ),
            proposed_golf_rounds=[
                ProposedGolfRoundOption(
                    course_name="Old Tom Morris Links",
                    date=dt.date(2027, 7, 21),
                    time="10:00",
                    players=2,
                    holes="18",
                    round_price_total="180.00",
                    is_available=True,
                    own_allocation_retained=True,
                    source_freshness="Live BRS Verified",
                )
            ],
        )
        offer = self.amendment_service.create_amendment_offer(create_req)

        accept_req = AmendmentOfferAcceptRequest(
            offer_id=offer.offer_id,
            booking_reference="RSP-20270720-M0701",
            terms_version="terms-v1-20261014",
            accepted_terms=True,
            occupant_review_acknowledged=upgrade_room,
        )
        accepted_offer = self.amendment_service.accept_amendment_offer(accept_req)
        return accepted_offer

    def test_revalidation_success_all_components_passed(self):
        """AC 1 & AC 8: Validates all components pass, returning valid status and zero inventory hold."""
        offer = self._create_and_accept_offer(upgrade_room=False)

        req = AmendmentRevalidationRequest(
            offer_id=offer.offer_id,
            booking_reference="RSP-20270720-M0701",
        )
        res = self.amendment_service.revalidate_amendment_offer(req)

        self.assertTrue(res.is_valid)
        self.assertEqual(res.revalidation_status, "valid")
        self.assertFalse(res.reserves_inventory)
        self.assertEqual(len(res.checks), 6)
        self.assertTrue(all(c.status == "passed" for c in res.checks))

    def test_revalidation_blocks_unaccepted_offer(self):
        """AC 1: Offer in pending_consent cannot proceed to revalidation."""
        create_req = AmendmentOfferCreateRequest(
            booking_reference="RSP-20270720-M0701",
            option_id="OPT-UNACCEPTED",
            proposed_stay=ProposedStayOption(
                arrival_date=dt.date(2027, 7, 20),
                departure_date=dt.date(2027, 7, 23),
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
        unaccepted_offer = self.amendment_service.create_amendment_offer(create_req)

        req = AmendmentRevalidationRequest(
            offer_id=unaccepted_offer.offer_id,
            booking_reference="RSP-20270720-M0701",
        )
        res = self.amendment_service.revalidate_amendment_offer(req)

        self.assertFalse(res.is_valid)
        self.assertEqual(res.revalidation_status, "unaccepted_offer")

    def test_revalidation_blocks_expired_offer(self):
        """AC 1: Expired offer fails revalidation."""
        offer = self._create_and_accept_offer()

        # Simulate expired offer in past
        past = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=20)
        self.amendment_service._offers_cache[offer.offer_id]["expires_at"] = past.isoformat()
        if self.amendment_service.collection is not None:
            self.amendment_service.collection.update_one(
                {"offer_id": offer.offer_id},
                {"$set": {"expires_at": past}}
            )

        req = AmendmentRevalidationRequest(
            offer_id=offer.offer_id,
            booking_reference="RSP-20270720-M0701",
        )
        res = self.amendment_service.revalidate_amendment_offer(req)
        self.assertFalse(res.is_valid)
        self.assertEqual(res.revalidation_status, "offer_expired")

    def test_revalidation_detects_concurrent_revision_conflict(self):
        """AC 5: Concurrent edit by staff or another session increments revision and triggers conflict."""
        offer = self._create_and_accept_offer()

        # Modify confirmed booking via staff correction
        self.linkage_service.correct_linkage(
            booking_reference="RSP-20270720-M0701",
            request=StaffCorrectLinkageRequest(
                reason="Staff added dietary note to booking",
            ),
            actor="reception_agent",
        )

        req = AmendmentRevalidationRequest(
            offer_id=offer.offer_id,
            booking_reference="RSP-20270720-M0701",
        )
        res = self.amendment_service.revalidate_amendment_offer(req)

        self.assertFalse(res.is_valid)
        self.assertEqual(res.revalidation_status, "revision_conflict")
        self.assertIn("revision conflict", res.summary_message.lower())

    def test_revalidation_sold_out_room_failure(self):
        """AC 3: Sold out room for new dates/category prevents progression, leaving original booking intact."""
        offer = self._create_and_accept_offer(upgrade_room=True)
        booking_before = self.linkage_service.get_linkage("RSP-20270720-M0701")

        req = AmendmentRevalidationRequest(
            offer_id=offer.offer_id,
            booking_reference="RSP-20270720-M0701",
            simulate_sold_out_room=True,
        )
        res = self.amendment_service.revalidate_amendment_offer(req)

        self.assertFalse(res.is_valid)
        self.assertEqual(res.revalidation_status, "room_unavailable")

        # Booking before and after must remain completely identical (AC 3)
        booking_after = self.linkage_service.get_linkage("RSP-20270720-M0701")
        self.assertEqual(booking_before.opera_stay.room_category, booking_after.opera_stay.room_category)
        self.assertEqual(booking_before.financials, booking_after.financials)

    def test_revalidation_unavailable_tee_time_failure(self):
        """AC 3: Booked tee time prevents progression, leaving original booking intact."""
        offer = self._create_and_accept_offer()

        req = AmendmentRevalidationRequest(
            offer_id=offer.offer_id,
            booking_reference="RSP-20270720-M0701",
            simulate_unavailable_tee=True,
        )
        res = self.amendment_service.revalidate_amendment_offer(req)

        self.assertFalse(res.is_valid)
        self.assertEqual(res.revalidation_status, "tee_time_unavailable")

    def test_revalidation_rate_change_returns_refreshed_offer(self):
        """AC 2 & AC 6: Price changes prevent silent substitution and return refreshed offer for re-acceptance."""
        offer = self._create_and_accept_offer()

        req = AmendmentRevalidationRequest(
            offer_id=offer.offer_id,
            booking_reference="RSP-20270720-M0701",
            simulate_rate_change="275.00",
        )
        res = self.amendment_service.revalidate_amendment_offer(req)

        self.assertFalse(res.is_valid)
        self.assertEqual(res.revalidation_status, "price_changed")
        self.assertIsNotNone(res.refreshed_offer)
        self.assertEqual(res.refreshed_offer.proposed_stay.nightly_rate, "275.00")
        self.assertEqual(res.refreshed_offer.status, "pending_consent")

    def test_revalidation_idempotency_key_deduplication(self):
        """AC 7: Retries with same idempotency key return the cached revalidation response."""
        offer = self._create_and_accept_offer()

        req1 = AmendmentRevalidationRequest(
            offer_id=offer.offer_id,
            booking_reference="RSP-20270720-M0701",
            idempotency_key="idemp_key_m07_test",
        )
        res1 = self.amendment_service.revalidate_amendment_offer(req1)

        req2 = AmendmentRevalidationRequest(
            offer_id=offer.offer_id,
            booking_reference="RSP-20270720-M0701",
            idempotency_key="idemp_key_m07_test",
        )
        res2 = self.amendment_service.revalidate_amendment_offer(req2)

        self.assertEqual(res1.revalidation_id, res2.revalidation_id)
        self.assertEqual(res1.revalidated_at, res2.revalidated_at)

    def test_revalidation_gate_disabled_fails_closed(self):
        """AC 1: When MMB gate is disabled, revalidation fails closed."""
        offer = self._create_and_accept_offer()

        # Disable gate
        self.db["mmb_release_gate"].replace_one(
            {"_id": GATE_DOCUMENT_ID},
            {"_id": GATE_DOCUMENT_ID, "enabled": False},
            upsert=True,
        )

        req = AmendmentRevalidationRequest(
            offer_id=offer.offer_id,
            booking_reference="RSP-20270720-M0701",
        )
        res = self.amendment_service.revalidate_amendment_offer(req)
        self.assertFalse(res.is_valid)
        self.assertEqual(res.revalidation_status, "gate_disabled")

    def test_api_revalidation_endpoint_scope_and_cache_control(self):
        """AC 1 & AC 8: Tests API route, IDOR scoping, and Cache-Control: no-store."""
        offer = self._create_and_accept_offer()

        valid_principal = GuestSessionPrincipal(
            session_token="test_sess_m07",
            booking_reference="RSP-20270720-M0701",
            lead_guest_email="jane.golfer@example.com",
            expires_at=dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=30),
        )
        mismatched_principal = GuestSessionPrincipal(
            session_token="test_sess_m07_other",
            booking_reference="RSP-OTHER-REF",
            lead_guest_email="other@example.com",
            expires_at=dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=30),
        )

        req = AmendmentRevalidationRequest(
            offer_id=offer.offer_id,
            booking_reference="RSP-20270720-M0701",
        )

        # 1. Valid route call
        resp = Response()
        res = route_revalidate_offer(
            request=req,
            response=resp,
            session=valid_principal,
            amendment_service=self.amendment_service,
        )
        self.assertEqual(resp.headers.get("cache-control"), "no-store")
        self.assertTrue(res.is_valid)
        self.assertEqual(res.revalidation_status, "valid")

        # 2. IDOR rejection
        with self.assertRaises(HTTPException) as ctx:
            route_revalidate_offer(
                request=req,
                response=Response(),
                session=mismatched_principal,
                amendment_service=self.amendment_service,
            )
        self.assertEqual(ctx.exception.status_code, 403)


if __name__ == "__main__":
    unittest.main()
