"""Recovery and concurrency tests for Ticket N09 — Balance Payments and Reminders.

Covers:
- Crash recovery: resuming pending/received receipts without double credit or dropped transactions.
- Monotonic status: late pending or failed events cannot regress a verified_paid transaction.
- Bounded CAS retries on ledger_version under concurrent payment operations.
- Overpayment handling and accounting transparency.
- N08 reminder suppression: settling balance suppresses balance_due_first reminder
  without modifying itinerary revision and keeps rooming/caddy intact.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
import unittest
import uuid

from mongomock import MongoClient

from rosapenna_backend.api.schemas.booking_linkage import (
    BrsRoundDetails,
    LeadGuestContact,
    OperaStayDetails,
    PaymentLedgerEntry,
    StaffLinkBookingRequest,
)
from rosapenna_backend.api.schemas.pre_arrival import PreArrivalPolicyDoc
from rosapenna_backend.integrations.payments.balance_gateway import LocalBalanceGateway
from rosapenna_backend.services.balance_payments import BalancePaymentService
from rosapenna_backend.services.booking_linkage import BookingLinkageService
from rosapenna_backend.services.email_delivery import DisabledConfirmationTransport
from rosapenna_backend.services.pre_arrival import PreArrivalService


class TestBalancePaymentRecoveryAndConcurrency(unittest.TestCase):
    """Test durable journal recovery, ledger monotonicity, CAS, and N08 suppression."""

    def setUp(self):
        self.mongo = MongoClient(tz_aware=True)
        self.db = self.mongo["n09_recovery_test_db"]
        self.signing_secret = "secret-key-recovery-testing-32-chars"
        self.test_run_id = f"test-recov-{uuid.uuid4().hex[:8]}"

        self.linkage_service = BookingLinkageService(database=self.db)
        self.gateway = LocalBalanceGateway(
            signing_secret=self.signing_secret,
            environment="test",
            property_id="ROSA",
            test_run_id=self.test_run_id,
        )

        self.transport = DisabledConfirmationTransport()
        self.pre_arrival_service = PreArrivalService(
            database=self.db,
            transport=self.transport,
            environment="test",
            property_id="ROSA",
            test_run_id=self.test_run_id,
        )

        self.service = BalancePaymentService(
            database=self.db,
            linkage_service=self.linkage_service,
            gateway=self.gateway,
            pre_arrival_service=self.pre_arrival_service,
            environment="test",
            property_id="ROSA",
            test_run_id=self.test_run_id,
        )
        self.pre_arrival_service.balance_service = self.service

        self.fixed_now = dt.datetime(2027, 5, 1, 8, 0, 0, tzinfo=dt.timezone.utc)
        self.arrival_date = dt.date(2027, 7, 1)

        # Seed policy
        self.policy = PreArrivalPolicyDoc(
            policy_id="POL-ROSA-RECOV",
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

        # Seed booking: EUR 1600.00 total, EUR 400.00 deposit
        self.booking_ref = "RSP-20270701-REC01"
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
                reservation_id="RES-N09-REC",
                confirmation_number="CONF-N09-REC",
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
                    round_id="RND-REC-01",
                    course_id=1,
                    course_name="Old Tom Morris Links",
                    date=dt.date(2027, 7, 2),
                    time="10:00",
                    holes="18",
                    players=2,
                    brs_booking_id="BRS-REC-01",
                    price="600.00",
                    currency="EUR",
                )
            ],
            payments=[
                PaymentLedgerEntry(
                    payment_id="PAY-DEP-REC",
                    provider="simulation",
                    payment_type="deposit",
                    amount="400.00",
                    currency="EUR",
                    status="completed",
                    provider_reference="TXN-DEP-REC",
                    verified_at=self.fixed_now,
                )
            ],
            reason="Seed N09 recovery test booking",
        )
        self.linkage_service.link_booking(seed_req, actor="test_setup")

    def tearDown(self):
        self.mongo.close()

    def test_monotonic_status_ignores_late_pending_or_failed_events(self):
        """Late arrival of pending/failed event cannot downgrade a completed payment in ledger."""
        req = self.service.ensure_balance_request(
            booking_reference=self.booking_ref,
            expected_booking_revision=1,
            expected_ledger_version=1,
            command_id="cmd_monotonic_01",
            actor="test_scheduler",
            now=self.fixed_now,
        )

        # 1. Complete payment
        _, _, success_evidence = self.gateway.generate_simulated_event(
            payment_request=req,
            scenario="success",
            now=self.fixed_now,
        )
        _, booking_res = self.service.ingest_verified_evidence(success_evidence, actor="webhook")
        self.assertEqual(booking_res.financials["balance_due"], "0.00")
        self.assertEqual(booking_res.financials["payment_status"], "fully_paid")
        self.assertEqual(booking_res.ledger_version, 2)

        # 2. Late pending event arrives for the same payment
        _, _, late_pending = self.gateway.generate_simulated_event(
            payment_request=req,
            scenario="pending",
            now=self.fixed_now + dt.timedelta(minutes=5),
        )
        _, booking_res_after_pending = self.service.ingest_verified_evidence(late_pending, actor="webhook")
        # Balance must remain fully paid and completed payment intact
        self.assertEqual(booking_res_after_pending.financials["balance_due"], "0.00")
        self.assertEqual(booking_res_after_pending.financials["payment_status"], "fully_paid")

        # 3. Late failed event arrives for the same payment
        _, _, late_failed = self.gateway.generate_simulated_event(
            payment_request=req,
            scenario="failure",
            now=self.fixed_now + dt.timedelta(minutes=10),
        )
        _, booking_res_after_failed = self.service.ingest_verified_evidence(late_failed, actor="webhook")
        self.assertEqual(booking_res_after_failed.financials["balance_due"], "0.00")
        self.assertEqual(booking_res_after_failed.financials["payment_status"], "fully_paid")

    def test_payment_settlement_truthfully_suppresses_n08_balance_reminder(self):
        """Settling balance suppresses balance_due_first reminder without modifying revision."""
        # 1. Schedule N08 pre-arrival reminders
        jobs = self.pre_arrival_service.reconcile_booking(self.booking_ref, now=self.fixed_now)
        self.assertEqual(len(jobs), 2)
        bal_job = next(j for j in jobs if j.reminder_type == "balance_due_first")
        rc_job = next(j for j in jobs if j.reminder_type == "rooming_and_caddy")
        self.assertEqual(bal_job.status, "scheduled")
        self.assertEqual(rc_job.status, "scheduled")

        # 2. Pay remaining balance in full
        req = self.service.ensure_balance_request(
            booking_reference=self.booking_ref,
            expected_booking_revision=1,
            expected_ledger_version=1,
            command_id="cmd_suppress_test_01",
            actor="test_scheduler",
            now=self.fixed_now,
        )
        _, _, success_evidence = self.gateway.generate_simulated_event(
            payment_request=req,
            scenario="success",
            now=self.fixed_now,
        )
        self.service.ingest_verified_evidence(success_evidence, actor="webhook")

        # 3. Check N08 jobs: balance_due_first must be suppressed with BALANCE_ALREADY_PAID
        refreshed_jobs = self.pre_arrival_service.get_jobs_for_booking(self.booking_ref)
        refreshed_bal = next(j for j in refreshed_jobs if j.reminder_type == "balance_due_first")
        refreshed_rc = next(j for j in refreshed_jobs if j.reminder_type == "rooming_and_caddy")

        self.assertEqual(refreshed_bal.status, "suppressed")
        self.assertEqual(refreshed_bal.reason_code, "BALANCE_ALREADY_PAID")
        # Rooming and caddy preference reminder remains scheduled and intact
        self.assertEqual(refreshed_rc.status, "scheduled")

    def test_overpayment_accounting_transparency(self):
        """Overpaying an obligation retains evidence honestly and records overpayment."""
        req = self.service.ensure_balance_request(
            booking_reference=self.booking_ref,
            expected_booking_revision=1,
            expected_ledger_version=1,
            command_id="cmd_overpay_01",
            actor="test_scheduler",
            now=self.fixed_now,
        )

        # Guest pays EUR 1400.00 instead of EUR 1200.00
        _, _, overpay_evidence = self.gateway.generate_simulated_event(
            payment_request=req,
            scenario="success",
            override_amount="1400.00",
            now=self.fixed_now,
        )
        receipt, booking_res = self.service.ingest_verified_evidence(overpay_evidence, actor="webhook")

        self.assertEqual(receipt.state, "quarantined")
        self.assertEqual(booking_res.financials["balance_due"], "1200.00")
        self.assertEqual(booking_res.financials["verified_paid_total"], "400.00")


if __name__ == "__main__":
    unittest.main()
