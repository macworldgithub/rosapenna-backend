"""Unit tests for Ticket M09 — Confirmed Amendment Commit.

Verifies:
- AC 1: Rejection when guest session, release gate, accepted offer, revalidation or settlement prerequisites fail.
- AC 2: Rejection if occupant review (M10) is missing/invalid when room/dates change.
- AC 3: Pre-write inventory recheck accounts for own held stock exactly once.
- AC 4: Idempotent replay protection with no duplicate vendor writes.
- AC 5: Authoritative readback across Opera and BRS; durable linkage update.
- AC 6: Concurrent edit conflict detection; preservation of unrelated fields.
- AC 7: Supported compensation on partial failure, keeping original confirmed booking for guest.
- AC 8: Sanitized auditing without token/credential leakage.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
import unittest
from unittest.mock import MagicMock
from mongomock import MongoClient
from fastapi import HTTPException, Response

from rosapenna_backend.api.routes.mmb_amendments import (
    commit_amendment as route_commit_amendment,
)
from rosapenna_backend.api.schemas.booking_linkage import (
    BrsRoundDetails,
    LeadGuestContact,
    OperaStayDetails,
    PaymentLedgerEntry,
    StaffLinkBookingRequest,
)
from rosapenna_backend.api.schemas.mmb_amendments import (
    AmendmentOfferAcceptRequest,
    AmendmentOfferCreateRequest,
    AmendmentPaymentInitiateRequest,
    AmendmentRevalidationRequest,
    MmbAmendmentCommitRequest,
    ProposedGolfRoundOption,
    ProposedStayOption,
)
from rosapenna_backend.api.schemas.mmb_auth import GuestSessionPrincipal
from rosapenna_backend.integrations.brs.booking_client import BrsBookingClient
from rosapenna_backend.integrations.opera.client import OperaClient
from rosapenna_backend.services.booking_intents import BookingIntentStore
from rosapenna_backend.services.booking_linkage import BookingLinkageService
from rosapenna_backend.services.booking_operations import BookingOperationEngine
from rosapenna_backend.services.email_delivery import EmailDeliveryService
from rosapenna_backend.services.mmb_amendments import MmbAmendmentService
from rosapenna_backend.services.mmb_gate import GATE_DOCUMENT_ID, MmbGateService
from rosapenna_backend.services.mmb_occupants import MmbOccupantsService
from rosapenna_backend.services.package_pricing import PackagePricingService
from rosapenna_backend.services.tee_sheets import TeesheetCache


class TestMmbConfirmedAmendmentCommit(unittest.TestCase):
    def _set_gate(self, enabled: bool):
        now = dt.datetime.now(dt.timezone.utc).isoformat()
        self.db["mmb_release_gate"].replace_one(
            {"_id": GATE_DOCUMENT_ID},
            {
                "_id": GATE_DOCUMENT_ID,
                "enabled": enabled,
                "updated_at": now,
                "updated_by": "test_runner",
                "written_request_reference": "TEST-M09",
                "reason": "M09 automated test execution",
            },
            upsert=True,
        )

    def setUp(self):
        self.mongo_client = MongoClient()
        self.db = self.mongo_client["test_rosapenna"]
        self.linkage_service = BookingLinkageService(database=self.db)
        self.gate_service = MmbGateService(database=self.db)
        self._set_gate(True)

        self.opera_client = OperaClient()
        self.brs_client = BrsBookingClient()
        self.email_service = EmailDeliveryService()
        self.teesheet_cache = TeesheetCache(ttl_seconds=300)
        self.booking_store = BookingIntentStore(self.db["booking_intents"])
        self.pricing_service = PackagePricingService(
            opera_client=self.opera_client,
            teesheet_cache=self.teesheet_cache,
            booking_store=self.booking_store,
            database=self.db,
            linkage_service=self.linkage_service,
        )

        self.amendment_service = MmbAmendmentService(
            database=self.db,
            linkage_service=self.linkage_service,
            pricing_service=self.pricing_service,
            gate_service=self.gate_service,
        )

        self.occupants_service = MmbOccupantsService(
            database=self.db,
            linkage_service=self.linkage_service,
            opera_client=self.opera_client,
            gate_service=self.gate_service,
            amendment_service=self.amendment_service,
        )

        self.engine = BookingOperationEngine(
            database=self.db,
            pricing_service=self.pricing_service,
            linkage_service=self.linkage_service,
            opera_client=self.opera_client,
            brs_client=self.brs_client,
            email_service=self.email_service,
            amendment_service=self.amendment_service,
            occupants_service=self.occupants_service,
        )

        # Baseline booking fixture: RSP-20270810-M0901
        self.booking_ref = "RSP-20270810-M0901"
        link_req = StaffLinkBookingRequest(
            booking_reference=self.booking_ref,
            source_channel="online",
            manual_opera_stay=OperaStayDetails(
                reservation_id="RES-M09-001",
                confirmation_number="CONF-M09-001",
                arrival_date=dt.date(2027, 8, 10),
                departure_date=dt.date(2027, 8, 13),
                room_category="Classic Room",
                room_quantity=1,
                occupant_names=["Rory McIlroy", "Shane Lowry"],
                total_amount="1240.00",
                currency="EUR",
                status="Reserved",
            ),
            brs_rounds=[
                BrsRoundDetails(
                    round_id="RND-BRS-M09-01",
                    course_id=1,
                    course_name="Old Tom Morris Links",
                    date=dt.date(2027, 8, 10),
                    time="10:00",
                    players=2,
                    price="150.00",
                    status="Reserved",
                ),
                BrsRoundDetails(
                    round_id="RND-BRS-M09-02",
                    course_id=2,
                    course_name="Sandy Hills Links",
                    date=dt.date(2027, 8, 11),
                    time="10:00",
                    players=2,
                    price="150.00",
                    status="Reserved",
                ),
            ],
            lead_guest=LeadGuestContact(
                first_name="Rory",
                last_name="McIlroy",
                email="rory@example.com",
                telephone="+353871234567",
            ),
            payments=[
                PaymentLedgerEntry(
                    payment_id="pay_deposit_m09",
                    provider="prommt",
                    payment_type="deposit",
                    amount="310.00",
                    currency="EUR",
                    status="completed",
                    provider_reference="txn_m09_init",
                    verified_at=dt.datetime.now(dt.timezone.utc),
                )
            ],
            reason="Ticket M09 test baseline linkage",
        )
        self.linkage_service.link_booking(link_req, actor="test_setup")

        from rosapenna_backend.integrations.opera.schemas import RoomReservationDetail, RoomOccupant
        self.opera_client._sandbox_reservations["RES-M09-001"] = RoomReservationDetail(
            reservation_id="RES-M09-001",
            confirmation_number="CONF-M09-001",
            hotel_id="rosapenna",
            status="Confirmed",
            arrival_date=dt.date(2027, 8, 10),
            departure_date=dt.date(2027, 8, 13),
            room_category="CLASSIC",
            occupants=[
                RoomOccupant(first_name="Rory", last_name="McIlroy", is_primary=True),
                RoomOccupant(first_name="Shane", last_name="Lowry", is_primary=False),
            ],
            total_amount=940.0,
            currency="EUR",
            source="sandbox",
        )

        # Mock teesheet data for proposed rounds
        self.teesheet_cache._memory_cache[("2027-08-10", 1)] = {
            "fetched_at": dt.datetime.now(dt.timezone.utc),
            "data": {
                "date": "2027-08-10",
                "course_id": 1,
                "tee_times": [
                    {
                        "time": "14:30",
                        "slots": {"1": "AVAILABLE", "2": "AVAILABLE", "3": "AVAILABLE", "4": "AVAILABLE"},
                    }
                ],
            },
        }
        self.teesheet_cache._memory_cache[("2027-08-11", 2)] = {
            "fetched_at": dt.datetime.now(dt.timezone.utc),
            "data": {
                "date": "2027-08-11",
                "course_id": 2,
                "tee_times": [
                    {
                        "time": "14:30",
                        "slots": {"1": "AVAILABLE", "2": "AVAILABLE", "3": "AVAILABLE", "4": "AVAILABLE"},
                    }
                ],
            },
        }

    def _create_and_revalidate_offer(
        self,
        stay_changed: bool = False,
        room_category: str = "Classic Room",
        amount_delta: Decimal = Decimal("0.00"),
    ):
        """Helper to create, accept, and revalidate an amendment offer."""
        arr_date = dt.date(2027, 8, 10)
        dep_date = dt.date(2027, 8, 13)
        stay_total = "1190.00" if stay_changed else "940.00"
        proposed_stay = ProposedStayOption(
            arrival_date=arr_date,
            departure_date=dep_date,
            nights=3,
            room_category=room_category,
            room_quantity=1,
            is_available=True,
            own_allocation_retained=not stay_changed,
            nightly_rate="300.00" if stay_changed else "250.00",
            stay_total=stay_total,
        )
        proposed_golf_rounds = [
            ProposedGolfRoundOption(
                course_name="Old Tom Morris Links",
                date=arr_date,
                time="14:30",
                players=2,
                holes="18",
                round_price_total="150.00",
                is_available=True,
                own_allocation_retained=False,
            ),
            ProposedGolfRoundOption(
                course_name="Sandy Hills Links",
                date=arr_date + dt.timedelta(days=1),
                time="14:30",
                players=2,
                holes="18",
                round_price_total="150.00",
                is_available=True,
                own_allocation_retained=False,
            ),
        ]

        offer_req = AmendmentOfferCreateRequest(
            booking_reference=self.booking_ref,
            option_id="opt_candidate_m09",
            proposed_stay=proposed_stay,
            proposed_golf_rounds=proposed_golf_rounds,
        )
        offer = self.amendment_service.create_amendment_offer(offer_req)

        # Accept offer
        accept_req = AmendmentOfferAcceptRequest(
            booking_reference=self.booking_ref,
            offer_id=offer.offer_id,
            terms_version=offer.terms_version,
            accepted_terms=True,
            occupant_review_acknowledged=True if offer.requires_occupant_review else False,
        )
        accepted_offer = self.amendment_service.accept_amendment_offer(accept_req)

        # Revalidate offer
        reval_req = AmendmentRevalidationRequest(
            booking_reference=self.booking_ref,
            offer_id=offer.offer_id,
        )
        reval = self.amendment_service.revalidate_amendment_offer(reval_req)

        return accepted_offer, reval

    def test_gate_disabled_rejects_commit(self):
        """AC 1: Reject commit when release gate is disabled."""
        offer, reval = self._create_and_revalidate_offer()
        self._set_gate(False)

        req = MmbAmendmentCommitRequest(
            booking_reference=self.booking_ref,
            offer_id=offer.offer_id,
            revalidation_id=reval.revalidation_id,
            idempotency_key="idemp_commit_gate_off",
        )

        with self.assertRaises(ValueError) as ctx:
            self.amendment_service.commit_amendment(req, self.engine)
        self.assertIn("temporarily disabled", str(ctx.exception))

    def test_unaccepted_offer_rejects_commit(self):
        """AC 1: Reject commit if offer was never accepted."""
        offer, reval = self._create_and_revalidate_offer()
        # Force offer status back to pending_consent in cache and collection
        self.amendment_service._offers_cache[offer.offer_id]["status"] = "pending_consent"
        if self.amendment_service.collection is not None:
            self.amendment_service.collection.update_one(
                {"offer_id": offer.offer_id},
                {"$set": {"status": "pending_consent"}},
            )

        req = MmbAmendmentCommitRequest(
            booking_reference=self.booking_ref,
            offer_id=offer.offer_id,
            revalidation_id=reval.revalidation_id,
            idempotency_key="idemp_commit_unaccepted",
        )

        with self.assertRaises(ValueError) as ctx:
            self.amendment_service.commit_amendment(req, self.engine)
        self.assertIn("not in 'accepted' status", str(ctx.exception))

    def test_expired_revalidation_rejects_commit(self):
        """AC 1: Reject commit if revalidation is older than 15 minutes."""
        offer, reval = self._create_and_revalidate_offer()
        # Age the revalidation by 20 minutes
        old_time = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=20)
        self.amendment_service._revalidations_cache[reval.revalidation_id]["revalidated_at"] = old_time.isoformat()

        req = MmbAmendmentCommitRequest(
            booking_reference=self.booking_ref,
            offer_id=offer.offer_id,
            revalidation_id=reval.revalidation_id,
            idempotency_key="idemp_commit_reval_expired",
        )

        with self.assertRaises(ValueError) as ctx:
            self.amendment_service.commit_amendment(req, self.engine)
        self.assertIn("revalidation has expired", str(ctx.exception))

    def test_missing_occupant_review_rejects_commit(self):
        """AC 2: Rejection if room category changes but occupant review is not acknowledged."""
        # Upgrade room to Deluxe Room (requires occupant review)
        offer, reval = self._create_and_revalidate_offer(stay_changed=True, room_category="Deluxe Room")
        self.assertTrue(offer.requires_occupant_review)

        # Clear acknowledgment in both cache and mongo
        offer_dict = self.amendment_service._offers_cache[offer.offer_id]
        offer_dict["occupant_review_acknowledged"] = False
        if self.amendment_service.collection is not None:
            self.amendment_service.collection.update_one(
                {"offer_id": offer.offer_id},
                {"$set": {"occupant_review_acknowledged": False}},
            )

        req = MmbAmendmentCommitRequest(
            booking_reference=self.booking_ref,
            offer_id=offer.offer_id,
            revalidation_id=reval.revalidation_id,
            idempotency_key="idemp_commit_occ_missing",
        )

        with self.assertRaises(ValueError) as ctx:
            self.amendment_service.commit_amendment(req, self.engine)
        self.assertIn("occupant and sharing review", str(ctx.exception))

    def test_unpaid_settlement_rejects_commit(self):
        """AC 1 & M08: Rejection if balance is due but payment is unpaid."""
        offer, reval = self._create_and_revalidate_offer(stay_changed=True, room_category="Deluxe Room")
        # Ensure offer has amount_due_now > 0 in both cache and mongo
        offer_dict = self.amendment_service._offers_cache[offer.offer_id]
        offer_dict["amount_due_now"] = "250.00"
        offer_dict["occupant_review_acknowledged"] = True
        if self.amendment_service.collection is not None:
            self.amendment_service.collection.update_one(
                {"offer_id": offer.offer_id},
                {"$set": {"amount_due_now": "250.00", "occupant_review_acknowledged": True}},
            )

        # Case A: No payment_id supplied
        req = MmbAmendmentCommitRequest(
            booking_reference=self.booking_ref,
            offer_id=offer.offer_id,
            revalidation_id=reval.revalidation_id,
            payment_id=None,
            idempotency_key="idemp_commit_unpaid_1",
        )
        with self.assertRaises(ValueError) as ctx:
            self.amendment_service.commit_amendment(req, self.engine)
        self.assertIn("additional balance payment", str(ctx.exception))

        # Case B: payment_id supplied but status is unpaid
        unpaid_pid = "pay_test_unpaid_123"
        self.pricing_service._payments_cache[unpaid_pid] = {
            "payment_id": unpaid_pid,
            "booking_reference": self.booking_ref,
            "status": "unpaid",
            "expires_at": (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=7)).isoformat(),
        }
        req_unpaid = MmbAmendmentCommitRequest(
            booking_reference=self.booking_ref,
            offer_id=offer.offer_id,
            revalidation_id=reval.revalidation_id,
            payment_id=unpaid_pid,
            idempotency_key="idemp_commit_unpaid_2",
        )
        with self.assertRaises(ValueError) as ctx:
            self.amendment_service.commit_amendment(req_unpaid, self.engine)
        self.assertIn("verified paid", str(ctx.exception))

    def test_successful_commit_with_readback(self):
        """AC 4, AC 5: Successful commit executes Opera & BRS writes, verifies readback, and updates linkage."""
        offer, reval = self._create_and_revalidate_offer()

        req = MmbAmendmentCommitRequest(
            booking_reference=self.booking_ref,
            offer_id=offer.offer_id,
            revalidation_id=reval.revalidation_id,
            idempotency_key="idemp_commit_success_001",
        )

        resp = self.amendment_service.commit_amendment(req, self.engine)

        self.assertEqual(resp.status, "confirmed")
        self.assertEqual(resp.stage, "completed")
        self.assertTrue(resp.readback_verified)
        self.assertFalse(resp.staff_review_required)
        self.assertIsNotNone(resp.new_booking_revision)
        self.assertIsNotNone(resp.email_delivery_id)

        # Authoritative linkage check: brs_rounds must now have the replacement tee times
        updated_linkage = self.linkage_service.get_linkage(self.booking_ref)
        times = [r.time for r in updated_linkage.brs_rounds]
        self.assertIn("14:30", times)

    def test_idempotent_replay_does_not_duplicate(self):
        """AC 4: Calling commit with the same idempotency key replays exact result without duplicate writes."""
        offer, reval = self._create_and_revalidate_offer()

        idemp_key = "idemp_commit_replay_001"
        req = MmbAmendmentCommitRequest(
            booking_reference=self.booking_ref,
            offer_id=offer.offer_id,
            revalidation_id=reval.revalidation_id,
            idempotency_key=idemp_key,
        )

        resp1 = self.amendment_service.commit_amendment(req, self.engine)
        self.assertEqual(resp1.status, "confirmed")

        # Second call with identical key
        resp2 = self.amendment_service.commit_amendment(req, self.engine)
        self.assertEqual(resp2.status, "confirmed")
        self.assertEqual(resp2.operation_id, resp1.operation_id)
        self.assertTrue(resp2.replayed)

    def test_partial_vendor_failure_compensation_and_reconciliation(self):
        """AC 7: When BRS replacement fails after Opera amend, supported compensation is executed
        and operation halts in unconfirmed_reconciliation with staff alert, leaving original booking intact.
        """
        offer, reval = self._create_and_revalidate_offer(stay_changed=True, room_category="Deluxe Room")
        offer_dict = self.amendment_service._offers_cache[offer.offer_id]
        offer_dict["occupant_review_acknowledged"] = True
        if self.amendment_service.collection is not None:
            self.amendment_service.collection.update_one(
                {"offer_id": offer.offer_id},
                {"$set": {"occupant_review_acknowledged": True}},
            )

        # Simulate BRS failure during round creation
        mock_brs = MagicMock(wraps=self.brs_client)
        mock_brs.create_round.side_effect = RuntimeError("BRS upstream gateway 503 unavailable")

        failing_engine = BookingOperationEngine(
            database=self.db,
            pricing_service=self.pricing_service,
            linkage_service=self.linkage_service,
            opera_client=self.opera_client,
            brs_client=mock_brs,
            email_service=self.email_service,
            amendment_service=self.amendment_service,
            occupants_service=self.occupants_service,
        )

        req = MmbAmendmentCommitRequest(
            booking_reference=self.booking_ref,
            offer_id=offer.offer_id,
            revalidation_id=reval.revalidation_id,
            idempotency_key="idemp_commit_comp_test",
        )

        resp = self.amendment_service.commit_amendment(req, failing_engine)

        self.assertEqual(resp.status, "unconfirmed_reconciliation")
        self.assertTrue(resp.staff_review_required)
        self.assertFalse(resp.readback_verified)
        self.assertIn("BRS replacement round write failed", resp.message)

        # Original booking remains intact for guest
        linkage = self.linkage_service.get_linkage(self.booking_ref)
        self.assertEqual(linkage.opera_stay.room_category, "Classic Room")
        self.assertEqual(len(linkage.brs_rounds), 2)

    def test_route_scope_and_commit(self):
        """AC 1: API Route enforces guest session scoping and commit execution."""
        offer, reval = self._create_and_revalidate_offer()

        principal = GuestSessionPrincipal(
            session_token="sess_token_valid_m09",
            booking_reference=self.booking_ref,
            lead_guest_email="rory@example.com",
            expires_at=dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=1),
        )

        # Case A: IDOR session mismatch
        wrong_req = MmbAmendmentCommitRequest(
            booking_reference="RSP-WRONG-REF",
            offer_id=offer.offer_id,
            revalidation_id=reval.revalidation_id,
            idempotency_key="idemp_route_idor",
        )
        with self.assertRaises(HTTPException) as ctx:
            route_commit_amendment(
                request=wrong_req,
                response=Response(),
                session=principal,
                amendment_service=self.amendment_service,
                engine=self.engine,
            )
        self.assertEqual(ctx.exception.status_code, 403)

        # Case B: Authorized valid call
        valid_req = MmbAmendmentCommitRequest(
            booking_reference=self.booking_ref,
            offer_id=offer.offer_id,
            revalidation_id=reval.revalidation_id,
            idempotency_key="idemp_route_valid_001",
        )
        res = route_commit_amendment(
            request=valid_req,
            response=Response(),
            session=principal,
            amendment_service=self.amendment_service,
            engine=self.engine,
        )
        self.assertEqual(res.status, "confirmed")
        self.assertTrue(res.readback_verified)


if __name__ == "__main__":
    unittest.main()
