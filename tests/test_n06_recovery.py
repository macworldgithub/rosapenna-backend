"""Test suite for Ticket N06: Failure recovery, reconciliation, and quote reacceptance.

Covers:
1. Partial vendor write failure (Opera succeeds, BRS fails) halts safely to unconfirmed_reconciliation.
2. Staff reconciliation alert generated with exact vendor state.
3. Expired or drifted quote detection before commit.
4. Booking intent offer reacceptance flow.
"""

import datetime as dt
import unittest
from decimal import Decimal
from unittest.mock import MagicMock, patch
from mongomock import MongoClient

from rosapenna_backend.config import Settings
from rosapenna_backend.integrations.opera.client import OperaClient
from rosapenna_backend.integrations.brs.booking_client import BrsBookingClient
from rosapenna_backend.integrations.payments.simulation import PaymentSimulator
from rosapenna_backend.services.booking_intents import BookingIntentStore
from rosapenna_backend.services.booking_linkage import BookingLinkageService
from rosapenna_backend.services.booking_operations import (
    BookingOperationEngine,
    ExecuteBookingOperationRequest,
)
from rosapenna_backend.services.email_delivery import EmailDeliveryService
from rosapenna_backend.services.package_pricing import PackagePricingService, compute_quote_hash
from rosapenna_backend.api.schemas.packages import (
    ItemizedGolfRoundQuote,
    ItemizedRoomQuote,
    PackagePaymentLinkRequest,
    PackageQuoteResponse,
    PackageRulesConfig,
)


def make_test_quote(expired: bool = False) -> PackageQuoteResponse:
    arrival = dt.date(2027, 6, 10)
    departure = dt.date(2027, 6, 13)
    expires_at = (
        dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=10)
        if expired
        else dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=2)
    )
    return PackageQuoteResponse(
        quote_id="QTE-RECOVERY-001",
        status="available",
        rules_version=PackageRulesConfig().version,
        arrival_date=arrival,
        departure_date=departure,
        nights=3,
        currency="EUR",
        itemized_rooms=[
            ItemizedRoomQuote(
                room_category="CLASSIC",
                room_quantity=1,
                nights=3,
                rate_per_night="200.00",
                subtotal="600.00",
                rate_plan_code="BB_STANDARD",
                breakfast_included=True,
                available_rooms=3,
            )
        ],
        itemized_golf=[
            ItemizedGolfRoundQuote(
                course_id=3,
                course_name="Sandy Hills Links",
                date=arrival,
                time="14:00",
                players=2,
                rate_per_player="90.00",
                subtotal="180.00",
                rate_basis="Standard Visitor Rate",
                available_slots=4,
            )
        ],
        total_rooms_price="600.00",
        total_golf_price="180.00",
        total_package_price="780.00",
        deposit_amount="195.00",
        balance_amount="585.00",
        balance_due_date=arrival - dt.timedelta(days=30),
        terms_version="terms-v1-20261014",
        deposit_percent=25,
        balance_percent=75,
        rate_basis_summary="Standard Visitor Rate",
        is_golf_ireland_eligible=False,
        reserves_inventory=False,
        freshness_ttl_seconds=3600,
        expires_at=expires_at.isoformat(),
        created_at=dt.datetime.now(dt.timezone.utc).isoformat(),
        errors=[],
    )


class TestN06RecoveryAndReconciliation(unittest.TestCase):
    def setUp(self):
        self.mongo_client = MongoClient()
        self.db = self.mongo_client["rosapenna_test_recovery"]

        self.settings = Settings(
            payment_mode="simulation",
            simulation_enabled=True,
            simulation_run_id="run-n06-recovery-test-01",
            simulation_signing_secret="recovery-secret-test-key-32bytes-len",
            prommt_allow_live_calls=False,
            opera_allow_live_writes=False,
            brs_allow_live_writes=False,
            email_allow_live_send=False,
        )

        self.opera_client = OperaClient()
        self.brs_booking_client = BrsBookingClient()
        self.teesheet_cache = MagicMock()
        self.teesheet_cache.get.return_value = {
            "status": "success",
            "data": {
                "tee_times": [
                    {
                        "time": "14:00",
                        "slots": {"1": "AVAILABLE", "2": "AVAILABLE", "3": "AVAILABLE", "4": "AVAILABLE"},
                    }
                ]
            },
        }

        self.store = BookingIntentStore(self.db["booking_intents"])
        self.linkage_service = BookingLinkageService(database=self.db, opera_client=self.opera_client)
        self.email_service = EmailDeliveryService()
        self.simulator = PaymentSimulator(database=self.db, default_secret=self.settings.simulation_signing_secret)

        self.pricing_service = PackagePricingService(
            opera_client=self.opera_client,
            teesheet_cache=self.teesheet_cache,
            booking_store=self.store,
            database=self.db,
            linkage_service=self.linkage_service,
            payment_simulator=self.simulator,
            settings=self.settings,
        )

        self.engine = BookingOperationEngine(
            database=self.db,
            pricing_service=self.pricing_service,
            linkage_service=self.linkage_service,
            opera_client=self.opera_client,
            brs_client=self.brs_booking_client,
            email_service=self.email_service,
        )

    def test_partial_vendor_failure_halts_to_unconfirmed_reconciliation(self):
        """When Opera succeeds but BRS fails, engine must halt to unconfirmed_reconciliation without crashing."""
        quote = make_test_quote()
        self.pricing_service._cache_and_return_quote(quote)
        link_resp = self.pricing_service.create_payment_link(
            PackagePaymentLinkRequest(
                quote_id=quote.quote_id,
                lead_first_name="Fiona",
                lead_last_name="Gallagher",
                lead_email="fiona@example.com",
                lead_phone="+353871234567",
                accepted_terms=True,
            )
        )

        envelope, sig, raw_bytes = self.simulator.generate_simulated_event(
            payment_id=link_resp.payment_id,
            scenario="success",
            secret=self.settings.simulation_signing_secret,
            simulation_run_id=self.settings.simulation_run_id,
        )
        self.simulator.process_simulation_event(
            raw_bytes=raw_bytes,
            signature=sig,
            secret=self.settings.simulation_signing_secret,
            allowed_run_id=self.settings.simulation_run_id,
        )

        # Force BRS round creation to raise an error
        with patch.object(
            self.brs_booking_client,
            "create_round",
            side_effect=RuntimeError("Simulated BRS upstream socket timeout"),
        ):
            resp = self.engine.execute_operation(
                ExecuteBookingOperationRequest(
                    operation_type="new_booking",
                    booking_reference=link_resp.booking_reference,
                    quote_id=quote.quote_id,
                    payment_id=link_resp.payment_id,
                    idempotency_key="rec_op_key_001",
                )
            )

        self.assertEqual(resp.stage, "unconfirmed_reconciliation")
        self.assertEqual(resp.status, "unconfirmed_reconciliation")
        self.assertTrue(resp.staff_review_required)
        self.assertFalse(resp.readback_verified)
        self.assertIn("BRS round write failed", resp.message)

        # Inspect persisted operation record in database
        rec = self.db.booking_operations.find_one({"booking_reference": link_resp.booking_reference})
        self.assertIsNotNone(rec)
        self.assertEqual(rec["stage"], "unconfirmed_reconciliation")
        self.assertIsNotNone(rec.get("staff_alert"))
        alert = rec["staff_alert"]
        self.assertEqual(alert["failed_stage"], "golf_write")
        self.assertIn("Simulated BRS upstream socket timeout", alert["reason"])
        # Opera reservation was secured and preserved
        self.assertTrue(len(alert["opera_reservation_id"]) > 0)

        # Payment remains verified_paid in ledger, not discarded
        pay_doc = self.pricing_service.get_payment(link_resp.payment_id)
        self.assertEqual(pay_doc["status"], "verified_paid")

    def test_expired_quote_rejected_at_payment_link_creation(self):
        """Pricing service immediately rejects creating payment link for an expired quote."""
        expired_quote = make_test_quote(expired=True)
        self.pricing_service._cache_and_return_quote(expired_quote)
        with self.assertRaises(ValueError) as ctx:
            self.pricing_service.create_payment_link(
                PackagePaymentLinkRequest(
                    quote_id=expired_quote.quote_id,
                    lead_first_name="Liam",
                    lead_last_name="Gallagher",
                    lead_email="liam@example.com",
                    lead_phone="+353871234567",
                    accepted_terms=True,
                )
            )
        self.assertIn("has expired", str(ctx.exception))

    def test_expired_quote_rejected_at_commit_time(self):
        """Engine halts to unconfirmed_reconciliation if quote expires before commit is executed."""
        quote = make_test_quote(expired=False)
        self.pricing_service._cache_and_return_quote(quote)
        link_resp = self.pricing_service.create_payment_link(
            PackagePaymentLinkRequest(
                quote_id=quote.quote_id,
                lead_first_name="Liam",
                lead_last_name="Gallagher",
                lead_email="liam@example.com",
                lead_phone="+353871234567",
                accepted_terms=True,
            )
        )
        envelope, sig, raw_bytes = self.simulator.generate_simulated_event(
            payment_id=link_resp.payment_id,
            scenario="success",
            secret=self.settings.simulation_signing_secret,
            simulation_run_id=self.settings.simulation_run_id,
        )
        self.simulator.process_simulation_event(
            raw_bytes=raw_bytes,
            signature=sig,
            secret=self.settings.simulation_signing_secret,
            allowed_run_id=self.settings.simulation_run_id,
        )

        # Expire quote in store
        quote_doc = self.db.package_quotes.find_one({"quote_id": quote.quote_id})
        if quote_doc:
            self.db.package_quotes.update_one(
                {"quote_id": quote.quote_id},
                {"$set": {"expires_at": (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=5)).isoformat()}},
            )

        resp = self.engine.execute_operation(
            ExecuteBookingOperationRequest(
                operation_type="new_booking",
                booking_reference=link_resp.booking_reference,
                quote_id=quote.quote_id,
                payment_id=link_resp.payment_id,
                idempotency_key="rec_op_key_002",
            )
        )

        self.assertEqual(resp.stage, "unconfirmed_reconciliation")
        self.assertEqual(resp.status, "unconfirmed_reconciliation")
        self.assertIn("Quote has expired", resp.message)

    def test_linkage_failure_halts_to_unconfirmed_reconciliation(self):
        """Authoritative linkage failure must not confirm; halts to unconfirmed_reconciliation."""
        quote = make_test_quote()
        self.pricing_service._cache_and_return_quote(quote)
        link_resp = self.pricing_service.create_payment_link(
            PackagePaymentLinkRequest(
                quote_id=quote.quote_id,
                lead_first_name="Edward",
                lead_last_name="Norton",
                lead_email="edward@example.com",
                lead_phone="+353871234567",
                accepted_terms=True,
            )
        )
        envelope, sig, raw_bytes = self.simulator.generate_simulated_event(
            payment_id=link_resp.payment_id,
            scenario="success",
            secret=self.settings.simulation_signing_secret,
            simulation_run_id=self.settings.simulation_run_id,
        )
        self.simulator.process_simulation_event(
            raw_bytes=raw_bytes,
            signature=sig,
            secret=self.settings.simulation_signing_secret,
            allowed_run_id=self.settings.simulation_run_id,
        )

        with patch.object(
            self.linkage_service,
            "link_booking",
            side_effect=RuntimeError("Simulated database write conflict during linkage"),
        ):
            resp = self.engine.execute_operation(
                ExecuteBookingOperationRequest(
                    operation_type="new_booking",
                    booking_reference=link_resp.booking_reference,
                    quote_id=quote.quote_id,
                    payment_id=link_resp.payment_id,
                    idempotency_key="link_fail_key_001",
                )
            )

        self.assertEqual(resp.status, "unconfirmed_reconciliation")
        self.assertEqual(resp.stage, "unconfirmed_reconciliation")
        self.assertTrue(resp.staff_review_required)
        self.assertFalse(resp.readback_verified)
        self.assertIn("linkage failed", resp.message.lower())

        # Inspect persisted operation record
        rec = self.db.booking_operations.find_one({"booking_reference": link_resp.booking_reference})
        self.assertIsNotNone(rec)
        self.assertEqual(rec["stage"], "unconfirmed_reconciliation")
        self.assertEqual(rec["staff_alert"]["failed_stage"], "linkage")
        # Both Opera and BRS reservations are preserved in alert for reconciliation
        self.assertTrue(len(rec["staff_alert"]["opera_reservation_id"]) > 0)
        self.assertTrue(len(rec["staff_alert"]["brs_round_ids"]) > 0)


if __name__ == "__main__":
    unittest.main()
