from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from mongomock import MongoClient

from rosapenna_backend.api.schemas.booking_operations import ExecuteBookingOperationRequest
from rosapenna_backend.api.schemas.packages import PackagePaymentLinkRequest
from rosapenna_backend.config import Settings
from rosapenna_backend.integrations.brs.booking_client import BrsBookingClient
from rosapenna_backend.integrations.opera.client import OperaClient
from rosapenna_backend.integrations.payments.simulation import PaymentSimulator
from rosapenna_backend.services.booking_intents import BookingIntentStore
from rosapenna_backend.services.booking_linkage import BookingLinkageService
from rosapenna_backend.services.booking_operations import BookingOperationEngine, OperationConflictError
from rosapenna_backend.services.package_pricing import PackagePricingService
from tests.test_n06_commit import make_mock_available_quote

RUN_ID = "run-n06-hardening-ops"
SECRET = "test-sim-secret-key-n06-hardening"


class TestN06HardeningOperations(unittest.TestCase):
    def setUp(self):
        self.db = MongoClient()["rosapenna_test_n06_hardening_ops"]
        self.settings = Settings(
            environment="test",
            payment_mode="simulation",
            simulation_enabled=True,
            simulation_signing_secret=SECRET,
            simulation_run_id=RUN_ID,
        )
        self.opera_client = OperaClient()
        self.brs_client = BrsBookingClient()
        teesheets = MagicMock()
        teesheets.get.return_value = {"status": "success", "data": {"tee_times": [
            {"time": slot, "slots": {str(i): {"status": "Available"} for i in range(4)}}
            for slot in ("14:00", "10:00")
        ]}}
        self.linkage = BookingLinkageService(database=self.db, opera_client=self.opera_client)
        self.simulator = PaymentSimulator(database=self.db, default_secret=SECRET)
        self.pricing = PackagePricingService(
            opera_client=self.opera_client,
            teesheet_cache=teesheets,
            booking_store=BookingIntentStore(self.db["booking_intents"]),
            database=self.db,
            linkage_service=self.linkage,
            payment_simulator=self.simulator,
            settings=self.settings,
        )
        self.mail = MagicMock()
        self.mail.settings.allow_live_send = False
        self.mail.send_booking_confirmation.side_effect = RuntimeError("mail adapter unavailable")
        self.engine = self._engine()

    def _engine(self) -> BookingOperationEngine:
        return BookingOperationEngine(
            database=self.db,
            pricing_service=self.pricing,
            linkage_service=self.linkage,
            opera_client=self.opera_client,
            brs_client=self.brs_client,
            email_service=self.mail,
        )

    def _paid_request(self, key: str) -> ExecuteBookingOperationRequest:
        quote = make_mock_available_quote()
        self.pricing._cache_and_return_quote(quote)
        link = self.pricing.create_payment_link(PackagePaymentLinkRequest(
            quote_id=quote.quote_id,
            lead_first_name="Jane",
            lead_last_name="Golfer",
            lead_email="jane@example.invalid",
            lead_phone="+353871234567",
            accepted_terms=True,
        ))
        _, sig, raw = self.simulator.generate_simulated_event(
            payment_id=link.payment_id, scenario="success", secret=SECRET, simulation_run_id=RUN_ID,
        )
        self.simulator.process_simulation_event(
            raw_bytes=raw, signature=sig, secret=SECRET, allowed_run_id=RUN_ID,
        )
        return ExecuteBookingOperationRequest(
            operation_type="new_booking",
            booking_reference=link.booking_reference,
            quote_id=quote.quote_id,
            payment_id=link.payment_id,
            idempotency_key=key,
        )

    def _operation_docs(self) -> list:
        return list(self.db["booking_operations"].find({}, {"_id": 0}))

    def test_provider_failure_after_claim_is_not_silent(self):
        """(a) An unexpected error after the claim keeps a staff alert; the operation is not stuck silently."""
        request = self._paid_request("hardening-after-claim-01")
        with patch.object(self.engine, "_commit_completed_with_event", side_effect=RuntimeError("db down")):
            with self.assertRaises(RuntimeError):
                self.engine.execute_operation(request)

        op = self.engine.get_operations_for_booking(request.booking_reference)[0]
        self.assertIsNotNone(op.staff_alert)
        self.assertEqual(op.staff_alert.failed_stage, "unexpected_error")
        self.assertEqual(self.engine.get_checkout_booking_status(
            request.booking_reference, request.payment_id, request.quote_id), "needs_review")

        # The claim stays set, so a restart does not replay the vendor writes.
        with patch.object(self.opera_client, "create_reservation") as create:
            result = self.engine.execute_operation(request)
        self.assertEqual(result.status, "processing")
        create.assert_not_called()

    def test_step_records_show_saved_good_booking_and_unknown_golf_write(self):
        """(b) Good booking: hotel and golf steps are saved with references. Failing golf: unknown."""
        good = self._paid_request("hardening-steps-good-01")
        result = self.engine.execute_operation(good)
        self.assertEqual(result.status, "confirmed")
        op = self.engine.get_operation(result.operation_id)
        self.assertEqual(op.steps["hotel"]["state"], "saved")
        self.assertEqual(op.steps["hotel"]["vendor_reference"], op.vendor_references["opera_reservation_id"])
        golf_steps = {k: v for k, v in op.steps.items() if k.startswith("golf_")}
        self.assertEqual(len(golf_steps), len(op.vendor_references["brs_round_ids"]))
        for entry in golf_steps.values():
            self.assertEqual(entry["state"], "saved")
            self.assertIn(entry["vendor_reference"], op.vendor_references["brs_round_ids"])


    def test_failing_golf_write_marks_step_unknown(self):
        """(b) A golf write that raises is recorded as unknown with no reference."""
        request = self._paid_request("hardening-steps-bad-02")
        with patch.object(self.brs_client, "create_round", side_effect=RuntimeError("golf unavailable")):
            failed = self.engine.execute_operation(request)
        self.assertEqual(failed.status, "unconfirmed_reconciliation")
        op = self.engine.get_operation(failed.operation_id)
        golf_0 = [v for k, v in op.steps.items() if k.startswith("golf_0_")]
        self.assertEqual(len(golf_0), 1)
        self.assertEqual(golf_0[0]["state"], "unknown")
        self.assertIsNone(golf_0[0]["vendor_reference"])
        self.assertEqual(op.steps["hotel"]["state"], "saved")

    def test_same_key_with_different_fingerprint_conflicts_and_writes_nothing(self):
        """(c) Same idempotency key, changed amount: conflict, and no new operation or vendor write."""
        request = self._paid_request("hardening-fingerprint-01")
        first = self.engine.execute_operation(request)
        self.assertEqual(first.status, "confirmed")
        docs_before = self._operation_docs()

        real_get = self.pricing.get_payment

        def changed_amount(payment_id):
            return {**real_get(payment_id), "amount": "999.00"}

        with patch.object(self.pricing, "get_payment", side_effect=changed_amount), \
                patch.object(self.opera_client, "create_reservation") as create, \
                patch.object(self.brs_client, "create_round") as golf:
            with self.assertRaises(OperationConflictError):
                self.engine.execute_operation(request)
        create.assert_not_called()
        golf.assert_not_called()
        self.assertEqual(self._operation_docs(), docs_before)

    def test_recovery_of_partial_new_booking_lists_steps_and_makes_no_vendor_call(self):
        """(d) Recovery is read-only: it lists steps and calls neither Opera nor BRS."""
        request = self._paid_request("hardening-recovery-01")
        with patch.object(self.brs_client, "create_round", side_effect=RuntimeError("golf unavailable")):
            failed = self.engine.execute_operation(request)
        self.assertEqual(failed.status, "unconfirmed_reconciliation")
        docs_before = self._operation_docs()

        with patch.object(self.opera_client, "create_reservation", wraps=self.opera_client.create_reservation) as opera_calls, \
                patch.object(self.brs_client, "create_round", wraps=self.brs_client.create_round) as brs_calls:
            report = self.engine.recover_operation(failed.operation_id, actor="staff:test")

        opera_calls.assert_not_called()
        brs_calls.assert_not_called()
        self.assertEqual(report["outcome"], "requires_manual_reconciliation")
        self.assertEqual(report["vendor_writes"], 0)
        self.assertIn("hotel", report["skipped_saved_steps"])
        golf_keys = [s["key"] for s in report["steps"] if s["key"].startswith("golf_0_")]
        self.assertEqual(len(golf_keys), 1)
        self.assertIn(golf_keys[0], report["manual_verification_steps"])
        for step in report["steps"]:
            self.assertEqual(set(step), {"key", "state", "vendor_reference", "updated_at"})
        self.assertEqual(self._operation_docs(), docs_before)

    def test_steps_survive_full_record_save(self):
        """(e) A full-document save never erases steps, confirmed_event or confirmation."""
        request = self._paid_request("hardening-survive-01")
        result = self.engine.execute_operation(request)
        self.assertEqual(result.status, "confirmed")

        stale = self.engine.get_operation(result.operation_id)
        stale.steps = {}
        self.engine._save_record(stale)

        doc = self.db["booking_operations"].find_one({"operation_id": result.operation_id})
        self.assertEqual(doc["steps"]["hotel"]["state"], "saved")
        self.assertIsNotNone(doc.get("confirmed_event"))
        self.assertEqual(self.engine.get_operation(result.operation_id).steps["hotel"]["state"], "saved")


if __name__ == "__main__":
    unittest.main()
