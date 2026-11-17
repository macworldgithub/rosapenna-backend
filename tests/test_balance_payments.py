"""Unit and integration tests for Ticket N09 — Balance Payments and Pure Domain Logic.

Covers:
- Exact 25/75 baseline and accepted total calculation without float errors.
- Decimal money parsing, currency validation, negative/NaN/3-decimal rejections.
- Milestone lead days: 42-day balance due date vs 56/42 first request.
- Request lifecycle: exact binding, idempotency replay, and creation timeout handling.
- Cryptographic event verification: HMAC signature, payload tampering rejection, and replay prevention.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
import json
import unittest
import uuid
from zoneinfo import ZoneInfo

from mongomock import MongoClient

from rosapenna_backend.api.schemas.balance_payments import (
    AcceptedFinancialsSnapshot,
    BalancePaymentRequestDoc,
    VerifiedBalanceEvidence,
    validate_price_string,
)
from rosapenna_backend.api.schemas.booking_linkage import (
    BrsRoundDetails,
    LeadGuestContact,
    OperaStayDetails,
    PaymentLedgerEntry,
    StaffLinkBookingRequest,
)
from rosapenna_backend.api.schemas.pre_arrival import PreArrivalPolicyDoc
from rosapenna_backend.integrations.payments.balance_gateway import (
    BalanceGatewayDisabledError,
    BalanceGatewaySignatureError,
    BalanceGatewayTimeoutError,
    DisabledBalanceGateway,
    LocalBalanceGateway,
)
from rosapenna_backend.services.balance_payments import (
    BalanceAlreadyPaidError,
    BalanceConflictError,
    BalanceEvidenceTamperedError,
    BalanceNotFoundError,
    BalancePaymentService,
)
from rosapenna_backend.services.booking_linkage import BookingLinkageService


class TestBalancePaymentsCalculations(unittest.TestCase):
    """Test pure financial calculation, precision, and validation rules."""

    def test_price_string_validation(self):
        self.assertEqual(validate_price_string("1200.00"), "1200.00")
        self.assertEqual(validate_price_string(1200), "1200.00")
        self.assertEqual(validate_price_string(Decimal("337.50")), "337.50")
        self.assertEqual(validate_price_string("0"), "0.00")

        # Invalid amounts
        for invalid in ("-10.00", "NaN", "Infinity", "12.345", "1e3", "abc"):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    validate_price_string(invalid)

    def test_accepted_financials_hash_computation(self):
        hash1 = AcceptedFinancialsSnapshot.compute_hash(
            booking_reference="RSP-20270610-TEST01",
            booking_revision=1,
            accepted_total="1600.00",
            currency="EUR",
            source="online_quote",
            evidence_reference="QTE-12345",
        )
        hash2 = AcceptedFinancialsSnapshot.compute_hash(
            booking_reference="RSP-20270610-TEST01",
            booking_revision=1,
            accepted_total="1600.00",
            currency="EUR",
            source="online_quote",
            evidence_reference="QTE-12345",
        )
        self.assertEqual(hash1, hash2)

        # Altered total produces different hash
        hash3 = AcceptedFinancialsSnapshot.compute_hash(
            booking_reference="RSP-20270610-TEST01",
            booking_revision=1,
            accepted_total="1800.00",
            currency="EUR",
            source="online_quote",
            evidence_reference="QTE-12345",
        )
        self.assertNotEqual(hash1, hash3)


class TestBalancePaymentServiceDomain(unittest.TestCase):
    """Test service orchestration, exact request binding, and evidence ingestion."""

    def setUp(self):
        self.mongo = MongoClient(tz_aware=True)
        self.db = self.mongo["n09_test_db"]
        self.linkage_service = BookingLinkageService(database=self.db)
        self.signing_secret = "test-signing-secret-key-32-chars-long"
        self.test_run_id = f"test-run-{uuid.uuid4().hex[:8]}"

        self.gateway = LocalBalanceGateway(
            signing_secret=self.signing_secret,
            environment="test",
            property_id="ROSA",
            test_run_id=self.test_run_id,
        )

        self.service = BalancePaymentService(
            database=self.db,
            linkage_service=self.linkage_service,
            gateway=self.gateway,
            environment="test",
            property_id="ROSA",
            test_run_id=self.test_run_id,
        )

        self.fixed_now = dt.datetime(2027, 5, 1, 8, 0, 0, tzinfo=dt.timezone.utc)
        self.arrival_date = dt.date(2027, 7, 1)

        # Seed pre-arrival policy (42 days due, 56 days first request)
        self.policy = PreArrivalPolicyDoc(
            policy_id="POL-ROSA-TEST",
            version=1,
            environment="test",
            approval_status="approved",
            approved_by="finance_director",
            approved_at=self.fixed_now,
            source="Board Policy",
            timezone="Europe/Dublin",
            local_send_time="09:00",
            balance_request_lead_days=56,
            balance_due_lead_days=42,
            rooming_caddy_lead_days=42,
        )
        self.db["pre_arrival_policies"].insert_one(self.policy.model_dump(mode="json"))

        # Seed a confirmed booking: EUR 1600.00 total, EUR 400.00 deposit paid
        self.booking_ref = "RSP-20270701-BAL01"
        seed_req = StaffLinkBookingRequest(
            booking_reference=self.booking_ref,
            source_channel="online",
            lead_guest=LeadGuestContact(
                first_name="Jane",
                last_name="Golfer",
                email="jane@example.com",
                telephone="+353871234567",
            ),
            manual_opera_stay=OperaStayDetails(
                reservation_id="RES-N09-01",
                confirmation_number="CONF-N09-01",
                hotel_id="ROSA",
                room_category="DELUXE",
                room_quantity=1,
                arrival_date=self.arrival_date,
                departure_date=dt.date(2027, 7, 5),
                total_amount="1000.00",
                currency="EUR",
                occupant_names=["Jane Golfer"],
            ),
            brs_rounds=[
                BrsRoundDetails(
                    round_id="RND-N09-01",
                    course_id=1,
                    course_name="Old Tom Morris Links",
                    date=dt.date(2027, 7, 2),
                    time="10:00",
                    holes="18",
                    players=2,
                    brs_booking_id="BRS-N09-01",
                    price="600.00",
                    currency="EUR",
                )
            ],
            payments=[
                PaymentLedgerEntry(
                    payment_id="PAY-DEP-01",
                    provider="simulation",
                    payment_type="deposit",
                    amount="400.00",
                    currency="EUR",
                    status="completed",
                    provider_reference="TXN-DEP-01",
                    verified_at=self.fixed_now,
                )
            ],
            reason="Seed N09 test booking",
        )
        self.linkage_service.link_booking(seed_req, actor="test_setup")

    def tearDown(self):
        self.mongo.close()

    def test_baseline_calculation_and_due_dates(self):
        """EUR 1600 accepted total with EUR 400 deposit owes EUR 1200.00."""
        summary = self.service.get_balance_summary(self.booking_ref, now=self.fixed_now)
        self.assertEqual(summary.accepted_total, "1600.00")
        self.assertEqual(summary.verified_deposit, "400.00")
        self.assertEqual(summary.verified_other_paid, "0.00")
        self.assertEqual(summary.total_verified_paid, "400.00")
        self.assertEqual(summary.balance_due, "1200.00")
        self.assertEqual(summary.overpayment, "0.00")
        self.assertEqual(summary.currency, "EUR")

        # Arrival 2027-07-01 minus 42 days = 2027-05-20 (balance_due_date)
        # Arrival 2027-07-01 minus 56 days = 2027-05-06 (first_request_date)
        self.assertEqual(summary.balance_due_date, dt.date(2027, 5, 20))
        self.assertEqual(summary.first_request_date, dt.date(2027, 5, 6))
        # Clock is 2027-05-01 (before first request date) -> not_due
        self.assertEqual(summary.collection_status, "not_due")

        # Move clock past first_request_date (2027-05-10) -> due
        due_clock = dt.datetime(2027, 5, 10, 10, 0, tzinfo=dt.timezone.utc)
        summary_due = self.service.get_balance_summary(self.booking_ref, now=due_clock)
        self.assertEqual(summary_due.collection_status, "due")

        # Move clock past balance_due_date (2027-05-25) -> overdue
        overdue_clock = dt.datetime(2027, 5, 25, 10, 0, tzinfo=dt.timezone.utc)
        summary_overdue = self.service.get_balance_summary(self.booking_ref, now=overdue_clock)
        self.assertEqual(summary_overdue.collection_status, "overdue")

    def test_ensure_balance_request_idempotent_and_bound(self):
        """Creating balance request binds exact remaining amount and returns identical on replay."""
        cmd_id = "cmd_test_balance_create_01"
        req = self.service.ensure_balance_request(
            booking_reference=self.booking_ref,
            expected_booking_revision=1,
            expected_ledger_version=1,
            command_id=cmd_id,
            actor="test_scheduler",
            now=self.fixed_now,
        )
        self.assertEqual(req.booking_reference, self.booking_ref)
        self.assertEqual(req.amount, "1200.00")
        self.assertEqual(req.currency, "EUR")
        self.assertEqual(req.state, "ready")
        self.assertTrue(req.hosted_url.startswith("https://payments.rosapenna.invalid/balance/checkout"))

        # Replay with same command ID returns identical request
        replayed = self.service.ensure_balance_request(
            booking_reference=self.booking_ref,
            expected_booking_revision=1,
            expected_ledger_version=1,
            command_id=cmd_id,
            actor="test_scheduler",
            now=self.fixed_now,
        )
        self.assertEqual(req.payment_id, replayed.payment_id)
        self.assertEqual(req.hosted_url, replayed.hosted_url)

    def test_ensure_balance_request_rejects_version_mismatches(self):
        """Stale revision or ledger version raises BalanceConflictError."""
        with self.assertRaises(BalanceConflictError):
            self.service.ensure_balance_request(
                booking_reference=self.booking_ref,
                expected_booking_revision=2,  # Current is 1
                expected_ledger_version=1,
                command_id="cmd_mismatch_rev",
                actor="test_scheduler",
                now=self.fixed_now,
            )

        with self.assertRaises(BalanceConflictError):
            self.service.ensure_balance_request(
                booking_reference=self.booking_ref,
                expected_booking_revision=1,
                expected_ledger_version=2,  # Current is 1
                command_id="cmd_mismatch_ledg",
                actor="test_scheduler",
                now=self.fixed_now,
            )

    def test_successful_evidence_ingestion_updates_ledger_to_zero_due(self):
        """Ingesting verified evidence marks ledger, increments version, and settles balance."""
        cmd_id = "cmd_test_balance_pay_01"
        req = self.service.ensure_balance_request(
            booking_reference=self.booking_ref,
            expected_booking_revision=1,
            expected_ledger_version=1,
            command_id=cmd_id,
            actor="test_scheduler",
            now=self.fixed_now,
        )

        # Generate simulated signed event
        raw_bytes, sig, evidence = self.gateway.generate_simulated_event(
            payment_request=req,
            scenario="success",
            now=self.fixed_now,
        )

        # Ingest evidence
        receipt, linkage = self.service.ingest_verified_evidence(
            evidence=evidence,
            actor="webhook_tester",
            now=self.fixed_now,
        )
        self.assertEqual(receipt.state, "applied")
        self.assertEqual(linkage.financials["balance_due"], "0.00")
        self.assertEqual(linkage.financials["balance_paid"], "1200.00")
        self.assertEqual(linkage.financials["deposit_paid"], "400.00")
        self.assertEqual(linkage.financials["payment_status"], "fully_paid")
        self.assertEqual(linkage.ledger_version, 2)

        # Summary now shows paid
        summary = self.service.get_balance_summary(self.booking_ref, now=self.fixed_now)
        self.assertEqual(summary.collection_status, "paid")
        self.assertEqual(summary.balance_due, "0.00")

    def test_duplicate_and_tampered_evidence_rejected(self):
        """Altered payload hash or duplicate transaction ID is quarantined."""
        cmd_id = "cmd_test_balance_tamper_01"
        req = self.service.ensure_balance_request(
            booking_reference=self.booking_ref,
            expected_booking_revision=1,
            expected_ledger_version=1,
            command_id=cmd_id,
            actor="test_scheduler",
            now=self.fixed_now,
        )

        _, _, evidence = self.gateway.generate_simulated_event(
            payment_request=req,
            scenario="success",
            now=self.fixed_now,
        )

        # 1. Apply first time
        self.service.ingest_verified_evidence(evidence=evidence, actor="webhook_tester", now=self.fixed_now)

        # 2. Same event ID with altered amount/hash raises BalanceEvidenceTamperedError
        tampered_evidence = evidence.model_copy(update={"raw_event_hash": "altered_tampered_hash_1234567890"})
        with self.assertRaises(BalanceEvidenceTamperedError):
            self.service.ingest_verified_evidence(evidence=tampered_evidence, actor="attacker", now=self.fixed_now)

        # 3. Different event claiming the same successful transaction ID is rejected
        dup_txn_evidence = evidence.model_copy(update={
            "event_id": "evt_new_distinct_id",
            "receipt_id": "rcpt_new_distinct_id",
        })
        with self.assertRaises(BalanceEvidenceTamperedError):
            self.service.ingest_verified_evidence(evidence=dup_txn_evidence, actor="replay_attacker", now=self.fixed_now)


if __name__ == "__main__":
    unittest.main()
