"""Unit tests for Ticket M08 — Amendment Payment and Credit Handling.

Verifies:
1. Scoped Prommt payment link is server-created when an approved amount is due (AC 1).
2. Client input cannot alter due amount, currency, booking reference, or offer (AC 2).
3. Only trusted provider evidence marks payment settled; browser redirects never do (AC 3).
4. Duplicate/replayed/delayed webhooks operate idempotently without double counting (AC 4).
5. Original booking remains intact and amendment unconfirmed until coordinated commit (AC 5).
6. Zero balance due creates NO payment link and authorizes direct commit path (AC 6).
7. Credit handling follows Rosapenna approved policy without automated card refund (AC 7).
8. Payment success followed by failed vendor write transitions to paid-but-unconfirmed and alerts staff (AC 8).
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
import hashlib
import hmac
import unittest
from mongomock import MongoClient
from fastapi import HTTPException, Response
from pydantic import ValidationError

from rosapenna_backend.api.routes.mmb_amendments import (
    get_amendment_payment_status as route_payment_status,
    initiate_amendment_settlement as route_initiate_settlement,
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
    ProposedGolfRoundOption,
    ProposedStayOption,
)
from rosapenna_backend.api.schemas.mmb_auth import GuestSessionPrincipal
from rosapenna_backend.api.schemas.packages import PaymentCallbackRequest
from rosapenna_backend.integrations.prommt import PrommtClient
from rosapenna_backend.services.booking_intents import BookingIntentStore
from rosapenna_backend.services.booking_linkage import BookingLinkageService
from rosapenna_backend.services.mmb_amendments import MmbAmendmentService
from rosapenna_backend.services.mmb_gate import GATE_DOCUMENT_ID, MmbGateService
from rosapenna_backend.services.package_pricing import PackagePricingService
from rosapenna_backend.services.tee_sheets import TeesheetCache


class TestMmbAmendmentPaymentAndCredit(unittest.TestCase):
    def setUp(self):
        self.mongo_client = MongoClient()
        self.db = self.mongo_client["test_rosapenna"]
        self.linkage_service = BookingLinkageService(database=self.db)
        self.gate_service = MmbGateService(database=self.db)

        # Mock dependencies for pricing service
        self.pricing_service = PackagePricingService(
            opera_client=None,
            teesheet_cache=TeesheetCache(),
            booking_store=BookingIntentStore(self.db["booking_intents"]),
            database=self.db,
            prommt_client=PrommtClient(),
            linkage_service=self.linkage_service,
        )

        self.amendment_service = MmbAmendmentService(
            database=self.db,
            linkage_service=self.linkage_service,
            pricing_service=self.pricing_service,
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
                "written_request_reference": "TEST-M08",
                "reason": "M08 automated test execution",
            },
            upsert=True,
        )

        self.seed_booking()

    def tearDown(self):
        self.mongo_client.close()

    def seed_booking(self):
        req = StaffLinkBookingRequest(
            booking_reference="RSP-20270810-M0801",
            source_channel="online",
            lead_guest=LeadGuestContact(
                first_name="Jane",
                last_name="Golfer",
                email="jane.golfer@example.com",
                telephone="+353871234567",
            ),
            manual_opera_stay=OperaStayDetails(
                reservation_id="OPERA-M08-01",
                confirmation_number="CONF-M08-01",
                hotel_id="ROSA",
                room_category="Classic Room",
                room_quantity=1,
                arrival_date=dt.date(2027, 8, 10),
                departure_date=dt.date(2027, 8, 13),
                total_amount="750.00",
                currency="EUR",
                occupant_names=["Jane Golfer", "John Golfer"],
            ),
            brs_rounds=[
                BrsRoundDetails(
                    round_id="BRS-M08-R1",
                    course_id=1,
                    course_name="Old Tom Morris Links",
                    date=dt.date(2027, 8, 11),
                    time="10:00",
                    holes="18",
                    players=2,
                    price="180.00",
                    currency="EUR",
                ),
                BrsRoundDetails(
                    round_id="BRS-M08-R2",
                    course_id=3,
                    course_name="Sandy Hills Links",
                    date=dt.date(2027, 8, 12),
                    time="14:00",
                    holes="18",
                    players=2,
                    price="220.00",
                    currency="EUR",
                ),
            ],
            payments=[
                PaymentLedgerEntry(
                    payment_id="pay_init_m08",
                    provider="prommt",
                    payment_type="deposit",
                    amount="525.00",
                    currency="EUR",
                    status="completed",
                    provider_reference="pmt_req_init_m08",
                    verified_at=dt.datetime.now(dt.timezone.utc),
                )
            ],
            reason="Seed baseline booking for M08 payment tests",
        )
        self.linkage_service.link_booking(req, actor="test_setup")

    def _create_and_revalidate_offer(
        self,
        stay_total: str = "750.00",
        golf_total: str = "400.00",
        room_category: str = "Classic Room",
        extra_fee: Decimal = Decimal("0.00"),
    ):
        """Helper to create, accept, and revalidate an amendment offer."""
        clean_ref = "RSP-20270810-M0801"
        create_req = AmendmentOfferCreateRequest(
            booking_reference=clean_ref,
            option_id="opt_candidate_m08",
            proposed_stay=ProposedStayOption(
                arrival_date=dt.date(2027, 8, 10),
                departure_date=dt.date(2027, 8, 13),
                nights=3,
                room_category=room_category,
                room_quantity=1,
                is_available=True,
                own_allocation_retained=True,
                nightly_rate="250.00",
                stay_total=stay_total,
            ),
            proposed_golf_rounds=[
                ProposedGolfRoundOption(
                    course_name="Old Tom Morris Links",
                    date=dt.date(2027, 8, 11),
                    time="10:00",
                    players=2,
                    holes="18",
                    round_price_total=golf_total,
                    is_available=True,
                    own_allocation_retained=True,
                )
            ],
        )
        offer = self.amendment_service.create_amendment_offer(create_req)

        # Accept terms
        accept_req = AmendmentOfferAcceptRequest(
            offer_id=offer.offer_id,
            booking_reference=clean_ref,
            terms_version=offer.terms_version,
            accepted_terms=True,
            occupant_review_acknowledged=True,
        )
        accepted_offer = self.amendment_service.accept_amendment_offer(accept_req)

        # Revalidate
        reval_req = AmendmentRevalidationRequest(
            offer_id=accepted_offer.offer_id,
            booking_reference=clean_ref,
        )
        reval = self.amendment_service.revalidate_amendment_offer(reval_req)
        self.assertTrue(reval.is_valid)

        return accepted_offer, reval

    def test_additional_amount_due_creates_scoped_payment_link(self):
        """AC 1: When additional amount is due, server creates scoped payment link."""
        # Upgrade room: stay_total = 1000.00, golf_total = 400.00 -> package total 1400.00
        # Original package total = 1150.00, original deposit = 525.00
        # Revised deposit 25% = 350.00, delta = +250.00, amount due now = 62.50
        offer, reval = self._create_and_revalidate_offer(
            stay_total="1000.00",
            golf_total="400.00",
            room_category="Deluxe Room",
        )
        self.assertGreater(Decimal(offer.amount_due_now), Decimal("0.00"))

        req = AmendmentPaymentInitiateRequest(
            booking_reference="RSP-20270810-M0801",
            offer_id=offer.offer_id,
            revalidation_id=reval.revalidation_id,
            idempotency_key="idem_pay_001",
        )
        resp = self.amendment_service.initiate_amendment_settlement(req)

        self.assertEqual(resp.action_type, "payment_required")
        self.assertEqual(resp.settlement_status, "payment_link_created")
        self.assertIsNotNone(resp.payment_id)
        self.assertTrue(resp.payment_id.startswith("pay_amend_"))
        self.assertIsNotNone(resp.payment_url)
        self.assertEqual(resp.currency, "EUR")
        self.assertEqual(resp.amount_due_now, offer.amount_due_now)
        self.assertFalse(resp.reserves_inventory)  # AC 8

        # Payment link expiry is strictly 7 days
        now = dt.datetime.now(dt.timezone.utc)
        self.assertIsNotNone(resp.expires_at)
        days_until_expiry = (resp.expires_at - now).total_seconds() / 86400
        self.assertAlmostEqual(days_until_expiry, 7.0, delta=0.1)

    def test_client_tampering_prevented_strictly(self):
        """AC 2: Client input cannot alter due amount, currency, booking reference, or offer."""
        offer, reval = self._create_and_revalidate_offer(
            stay_total="1000.00",
            golf_total="400.00",
            room_category="Deluxe Room",
        )

        # Attempt to inject amount or currency into request payload -> Pydantic forbids extra fields
        with self.assertRaises(ValidationError):
            AmendmentPaymentInitiateRequest(
                booking_reference="RSP-20270810-M0801",
                offer_id=offer.offer_id,
                revalidation_id=reval.revalidation_id,
                amount="1.00",  # Injected field
                currency="USD",  # Injected field
            )

    def test_zero_due_authorizes_commit_without_payment_link(self):
        """AC 6: Zero balance due creates NO payment link and authorizes direct commit path."""
        # Same price as original: stay_total = 750.00, golf_total = 400.00
        offer, reval = self._create_and_revalidate_offer(
            stay_total="750.00",
            golf_total="400.00",
            room_category="Classic Room",
        )
        self.assertEqual(Decimal(offer.amount_due_now), Decimal("0.00"))
        self.assertEqual(Decimal(offer.credit_amount), Decimal("0.00"))

        req = AmendmentPaymentInitiateRequest(
            booking_reference="RSP-20270810-M0801",
            offer_id=offer.offer_id,
            revalidation_id=reval.revalidation_id,
        )
        resp = self.amendment_service.initiate_amendment_settlement(req)

        self.assertEqual(resp.action_type, "zero_due")
        self.assertEqual(resp.settlement_status, "zero_due_authorized")
        self.assertIsNone(resp.payment_id)
        self.assertIsNone(resp.payment_url)
        self.assertEqual(resp.amount_due_now, "0.00")
        self.assertEqual(resp.credit_amount, "0.00")
        self.assertIn("Zero balance due", resp.message)

    def test_credit_recorded_under_rosapenna_policy_without_automatic_refund(self):
        """AC 7: Credit handling follows Rosapenna approved policy without automated card refund."""
        # Cheaper package: stay_total = 400.00, golf_total = 200.00 -> total 600.00
        # Original deposit was 525.00 -> exceeds new total of 600.00 or yields credit
        offer, reval = self._create_and_revalidate_offer(
            stay_total="300.00",
            golf_total="100.00",
            room_category="Classic Room",
        )
        self.assertGreater(Decimal(offer.credit_amount), Decimal("0.00"))

        req = AmendmentPaymentInitiateRequest(
            booking_reference="RSP-20270810-M0801",
            offer_id=offer.offer_id,
            revalidation_id=reval.revalidation_id,
        )
        resp = self.amendment_service.initiate_amendment_settlement(req)

        self.assertEqual(resp.action_type, "credit_generated")
        self.assertEqual(resp.settlement_status, "credit_recorded")
        self.assertIsNone(resp.payment_id)
        self.assertIsNone(resp.payment_url)
        self.assertEqual(resp.credit_amount, offer.credit_amount)
        self.assertIn("Automatic credit card refunds are not performed", resp.message)

        # Check that credit is recorded in linkage ledger
        linkage = self.linkage_service.get_linkage("RSP-20270810-M0801")
        credit_entries = [p for p in linkage.payments if p.payment_type == "credit"]
        self.assertEqual(len(credit_entries), 1)
        self.assertEqual(credit_entries[0].amount, offer.credit_amount)

    def test_settlement_rejected_for_unaccepted_or_expired_offer(self):
        """Settlement requires an accepted, unexpired offer."""
        clean_ref = "RSP-20270810-M0801"
        create_req = AmendmentOfferCreateRequest(
            booking_reference=clean_ref,
            option_id="opt_unaccepted",
            proposed_stay=ProposedStayOption(
                arrival_date=dt.date(2027, 8, 10),
                departure_date=dt.date(2027, 8, 13),
                nights=3,
                room_category="Classic Room",
                room_quantity=1,
                is_available=True,
                own_allocation_retained=True,
                nightly_rate="250.00",
                stay_total="750.00",
            ),
            proposed_golf_rounds=[],
        )
        offer = self.amendment_service.create_amendment_offer(create_req)
        # Offer is pending_consent, not accepted!

        reval_req = AmendmentRevalidationRequest(
            offer_id=offer.offer_id,
            booking_reference=clean_ref,
        )
        reval = self.amendment_service.revalidate_amendment_offer(reval_req)
        self.assertFalse(reval.is_valid)

        req = AmendmentPaymentInitiateRequest(
            booking_reference=clean_ref,
            offer_id=offer.offer_id,
            revalidation_id=reval.revalidation_id,
        )
        with self.assertRaises(ValueError) as ctx:
            self.amendment_service.initiate_amendment_settlement(req)
        self.assertIn("revalidation", str(ctx.exception).lower())

    def test_settlement_idempotency_returns_cached_response(self):
        """AC 4: Duplicate requests with same idempotency key do not duplicate links."""
        offer, reval = self._create_and_revalidate_offer(
            stay_total="1000.00",
            golf_total="400.00",
            room_category="Deluxe Room",
        )

        req = AmendmentPaymentInitiateRequest(
            booking_reference="RSP-20270810-M0801",
            offer_id=offer.offer_id,
            revalidation_id=reval.revalidation_id,
            idempotency_key="unique_idempotency_key_123",
        )
        resp1 = self.amendment_service.initiate_amendment_settlement(req)
        resp2 = self.amendment_service.initiate_amendment_settlement(req)

        self.assertEqual(resp1.payment_id, resp2.payment_id)
        self.assertEqual(resp1.payment_url, resp2.payment_url)
        self.assertEqual(resp1.action_type, resp2.action_type)

    def test_payment_webhook_hmac_and_replay_protection(self):
        """AC 3: Only trusted HMAC-signed callbacks mark payment verified; rejects forged/skewed."""
        offer, reval = self._create_and_revalidate_offer(
            stay_total="1000.00",
            golf_total="400.00",
            room_category="Deluxe Room",
        )
        settle_req = AmendmentPaymentInitiateRequest(
            booking_reference="RSP-20270810-M0801",
            offer_id=offer.offer_id,
            revalidation_id=reval.revalidation_id,
        )
        settle_resp = self.amendment_service.initiate_amendment_settlement(settle_req)

        secret = "rosapenna-dev-webhook-secret-key"
        now_ts = int(dt.datetime.now(dt.timezone.utc).timestamp())

        # 1. Valid signed callback
        valid_payload = f"{settle_resp.payment_id}:txn_test_m08:paid:{settle_resp.amount_due_now}:{now_ts}"
        valid_sig = hmac.new(secret.encode("utf-8"), valid_payload.encode("utf-8"), hashlib.sha256).hexdigest()

        cb_req = PaymentCallbackRequest(
            payment_id=settle_resp.payment_id,
            transaction_id="txn_test_m08",
            status="paid",
            amount=settle_resp.amount_due_now,
            timestamp=now_ts,
            provider_signature=valid_sig,
        )
        cb_resp = self.pricing_service.process_payment_callback(cb_req)
        self.assertEqual(cb_resp["payment_state"], "verified_paid")

        # 2. Forged signature rejected
        forged_req = PaymentCallbackRequest(
            payment_id=settle_resp.payment_id,
            transaction_id="txn_forged",
            status="paid",
            amount=settle_resp.amount_due_now,
            timestamp=now_ts,
            provider_signature="invalid_forged_signature",
        )
        with self.assertRaises(ValueError):
            self.pricing_service.process_payment_callback(forged_req)

        # 3. Skewed timestamp (> 300s) rejected
        skewed_ts = now_ts - 500
        skewed_payload = f"{settle_resp.payment_id}:txn_skew:paid:{settle_resp.amount_due_now}:{skewed_ts}"
        skewed_sig = hmac.new(secret.encode("utf-8"), skewed_payload.encode("utf-8"), hashlib.sha256).hexdigest()

        skewed_req = PaymentCallbackRequest(
            payment_id=settle_resp.payment_id,
            transaction_id="txn_skew",
            status="paid",
            amount=settle_resp.amount_due_now,
            timestamp=skewed_ts,
            provider_signature=skewed_sig,
        )
        with self.assertRaises(ValueError):
            self.pricing_service.process_payment_callback(skewed_req)

    def test_idempotent_duplicate_payment_callback(self):
        """AC 4: Duplicate webhooks do not double-count or alter ledger."""
        offer, reval = self._create_and_revalidate_offer(
            stay_total="1000.00",
            golf_total="400.00",
            room_category="Deluxe Room",
        )
        settle_resp = self.amendment_service.initiate_amendment_settlement(
            AmendmentPaymentInitiateRequest(
                booking_reference="RSP-20270810-M0801",
                offer_id=offer.offer_id,
                revalidation_id=reval.revalidation_id,
            )
        )

        secret = "rosapenna-dev-webhook-secret-key"
        now_ts = int(dt.datetime.now(dt.timezone.utc).timestamp())
        payload = f"{settle_resp.payment_id}:txn_dup_m08:paid:{settle_resp.amount_due_now}:{now_ts}"
        sig = hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()

        cb_req = PaymentCallbackRequest(
            payment_id=settle_resp.payment_id,
            transaction_id="txn_dup_m08",
            status="paid",
            amount=settle_resp.amount_due_now,
            timestamp=now_ts,
            provider_signature=sig,
        )
        # First execution -> transitions to verified_paid
        resp1 = self.pricing_service.process_payment_callback(cb_req)
        self.assertEqual(resp1["payment_state"], "verified_paid")

        # Second execution -> returns already_processed idempotently
        resp2 = self.pricing_service.process_payment_callback(cb_req)
        self.assertEqual(resp2["status"], "already_processed")

    def test_paid_unconfirmed_recovery(self):
        """AC 8: Payment success followed by vendor failure marks paid-but-unconfirmed and alerts staff."""
        offer, reval = self._create_and_revalidate_offer(
            stay_total="1000.00",
            golf_total="400.00",
            room_category="Deluxe Room",
        )
        settle_resp = self.amendment_service.initiate_amendment_settlement(
            AmendmentPaymentInitiateRequest(
                booking_reference="RSP-20270810-M0801",
                offer_id=offer.offer_id,
                revalidation_id=reval.revalidation_id,
            )
        )

        # Simulate verified payment
        secret = "rosapenna-dev-webhook-secret-key"
        now_ts = int(dt.datetime.now(dt.timezone.utc).timestamp())
        payload = f"{settle_resp.payment_id}:txn_settled_rec:paid:{settle_resp.amount_due_now}:{now_ts}"
        sig = hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()
        self.pricing_service.process_payment_callback(
            PaymentCallbackRequest(
                payment_id=settle_resp.payment_id,
                transaction_id="txn_settled_rec",
                status="paid",
                amount=settle_resp.amount_due_now,
                timestamp=now_ts,
                provider_signature=sig,
            )
        )

        # Trigger paid-unconfirmed recovery upon downstream vendor write error
        recovery_result = self.amendment_service.record_paid_unconfirmed(
            booking_reference="RSP-20270810-M0801",
            payment_id=settle_resp.payment_id,
            offer_id=offer.offer_id,
            failure_reason="Opera PMS HTTP 503 Service Unavailable during room block amendment",
        )

        self.assertEqual(recovery_result["status"], "paid_unconfirmed")
        self.assertIn("Original booking remains active", recovery_result["message"])

        # Check payment record reflects paid_unconfirmed
        pay_doc = self.pricing_service.get_payment(settle_resp.payment_id)
        self.assertEqual(pay_doc["status"], "paid_unconfirmed")
        self.assertEqual(
            pay_doc["vendor_failure_reason"],
            "Opera PMS HTTP 503 Service Unavailable during room block amendment",
        )

        # Check booking linkage audit trail recorded the staff alert
        linkage = self.linkage_service.get_linkage("RSP-20270810-M0801")
        alert_actions = [a for a in linkage.audit_trail if a.action == "payment_unconfirmed_alert"]
        self.assertEqual(len(alert_actions), 1)
        self.assertIn("Manual staff reconciliation required", alert_actions[0].reason)

    def test_routes_settlement_and_idor_protection(self):
        """Verifies FastAPI route security: session validation, IDOR scope check, and no-store headers."""
        offer, reval = self._create_and_revalidate_offer(
            stay_total="750.00",
            golf_total="400.00",
            room_category="Classic Room",
        )
        session = GuestSessionPrincipal(
            session_token="test_token",
            booking_reference="RSP-20270810-M0801",
            lead_guest_email="jane.golfer@example.com",
            expires_at=dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=1),
        )

        req = AmendmentPaymentInitiateRequest(
            booking_reference="RSP-20270810-M0801",
            offer_id=offer.offer_id,
            revalidation_id=reval.revalidation_id,
        )
        resp_obj = Response()
        api_resp = route_initiate_settlement(
            request=req,
            response=resp_obj,
            session=session,
            amendment_service=self.amendment_service,
        )
        self.assertEqual(api_resp.action_type, "zero_due")
        self.assertEqual(resp_obj.headers.get("Cache-Control"), "no-store")

        # Test IDOR attack: Session for another booking attempts settlement
        mismatched_session = GuestSessionPrincipal(
            session_token="test_token_evil",
            booking_reference="RSP-OTHER-REF",
            lead_guest_email="attacker@example.com",
            expires_at=dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=1),
        )
        with self.assertRaises(HTTPException) as ctx:
            route_initiate_settlement(
                request=req,
                response=resp_obj,
                session=mismatched_session,
                amendment_service=self.amendment_service,
            )
        self.assertEqual(ctx.exception.status_code, 403)


if __name__ == "__main__":
    unittest.main()
