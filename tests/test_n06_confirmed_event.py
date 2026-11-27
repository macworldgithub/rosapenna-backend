"""N06-09: a verified new booking saves one durable confirmed_event for N07 and sends no email."""

from __future__ import annotations

from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from mongomock import MongoClient

from rosapenna_backend.api.schemas.booking_operations import ExecuteBookingOperationRequest
from rosapenna_backend.api.schemas.communications import ConfirmedEvent
from rosapenna_backend.api.schemas.packages import PackagePaymentLinkRequest
from rosapenna_backend.config import Settings
from rosapenna_backend.integrations.brs.booking_client import BrsBookingClient
from rosapenna_backend.integrations.opera.client import OperaClient
from rosapenna_backend.integrations.payments.simulation import PaymentSimulator
from rosapenna_backend.services.booking_confirmations import (
    BookingConfirmationCoordinator,
    ConfirmationRuntimeContext,
)
from rosapenna_backend.services.booking_intents import BookingIntentStore
from rosapenna_backend.services.booking_linkage import BookingLinkageService
from rosapenna_backend.services.booking_operations import BookingOperationEngine
from rosapenna_backend.services.email_delivery import RecordingConfirmationTransport
from rosapenna_backend.services.package_pricing import PackagePricingService
from tests.test_n06_commit import make_mock_available_quote

RUN_ID = "run-n06-09-confirmed-event"
SECRET = "test-sim-secret-key-n0609"


class TestN06ConfirmedEvent(unittest.TestCase):
    def setUp(self):
        self.db = MongoClient()["rosapenna_test_n06_09"]
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
        # Unavailable mail adapter: any send call would fail and be recorded.
        self.mail = MagicMock()
        self.mail.settings.allow_live_send = False
        self.mail.send_booking_confirmation.side_effect = RuntimeError("mail adapter unavailable")
        self.engine = self._engine()
        self.tmp = tempfile.mkdtemp(prefix="n06_09_")
        self.recording_dir = Path(self.tmp) / "recordings"
        self.recording_dir.mkdir()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _engine(self) -> BookingOperationEngine:
        return BookingOperationEngine(
            database=self.db,
            pricing_service=self.pricing,
            linkage_service=self.linkage,
            opera_client=self.opera_client,
            brs_client=self.brs_client,
            email_service=self.mail,
        )

    def _paid_request(self, key: str = "n06-09-commit") -> ExecuteBookingOperationRequest:
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

    def _coordinator(self) -> BookingConfirmationCoordinator:
        transport = RecordingConfirmationTransport(recording_dir=self.recording_dir)
        return BookingConfirmationCoordinator(
            database=self.db,
            transport=transport,
            runtime_context=ConfirmationRuntimeContext(
                mode="recording", environment="test", run_id=RUN_ID,
                recording_dir=str(transport.recording_dir),
            ),
        )

    def _send_calls(self) -> list:
        return [call for call in self.mail.mock_calls if "send" in call[0]]

    def _events(self) -> list:
        return list(self.db["booking_operations"].find({"confirmed_event": {"$ne": None}}))

    def test_verified_booking_records_one_event_that_n07_accepts(self):
        result = self.engine.execute_operation(self._paid_request())
        self.assertEqual(result.status, "confirmed")

        docs = self._events()
        self.assertEqual(len(docs), 1)
        doc = docs[0]
        self.assertEqual(doc["stage"], "completed")
        self.assertIsNone(doc.get("confirmation"))
        event = ConfirmedEvent.model_validate(doc["confirmed_event"])
        self.assertEqual(event.event_id, f"EVT-{result.operation_id}")
        self.assertEqual(event.occurred_at, doc["completed_at"])
        self.assertEqual((event.environment, event.evidence_source, event.is_test, event.simulation_run_id),
                         ("test", "simulation", True, RUN_ID))
        financials = event.snapshot.financials
        self.assertEqual(
            (financials.total_amount, financials.deposit_paid, financials.verified_paid_total, financials.balance_due),
            ("1140.00", "285.00", "285.00", "855.00"),
        )
        self.assertEqual(event.snapshot_hash, event.snapshot.compute_hash())
        self.assertEqual(len(event.snapshot.golf_rounds), 2)

        record = self._coordinator().prepare_confirmation(result.operation_id, event.event_id)
        self.assertEqual(record.event_id, event.event_id)
        self.assertEqual(record.snapshot_hash, event.snapshot_hash)
        self.assertEqual(record.overall_status, "prepared")
        self.assertEqual(list(self.recording_dir.iterdir()), [])

    def test_paid_booking_flows_to_one_guest_and_hotel_confirmation(self):
        result = self.engine.execute_operation(self._paid_request())
        self.assertEqual(result.status, "confirmed")
        coordinator = self._coordinator()
        self.assertEqual(coordinator.find_pending_operations(), [result.operation_id])

        status = coordinator.process_confirmed_operation(result.operation_id)
        self.assertEqual(status.overall_status, "recorded_test")
        self.assertEqual((status.guest_delivery.status, status.hotel_delivery.status), ("recorded_test", "recorded_test"))
        self.assertEqual(len(coordinator.transport.recorded_submissions), 2)

        again = coordinator.process_confirmed_operation(result.operation_id)
        self.assertEqual(again.overall_status, "recorded_test")
        self.assertEqual(len(coordinator.transport.recorded_submissions), 2)
        self.assertEqual(coordinator.find_pending_operations(), [])
        doc = self.db["booking_operations"].find_one({"operation_id": result.operation_id})
        self.assertEqual(doc["stage"], "completed")
        self.assertEqual(self._send_calls(), [])

    def test_commit_makes_zero_mail_calls(self):
        result = self.engine.execute_operation(self._paid_request())
        self.assertEqual(result.status, "confirmed")
        self.assertIsNone(result.email_delivery_id)
        self.assertEqual(self._send_calls(), [])
        self.mail.send_booking_confirmation.assert_not_called()
        self.mail.send_revised_booking_confirmation.assert_not_called()

    def test_replay_keeps_one_unchanged_event(self):
        request = self._paid_request()
        first = self.engine.execute_operation(request)
        saved = self._events()[0]["confirmed_event"]

        replay = self.engine.execute_operation(request)
        other_key = self.engine.execute_operation(request.model_copy(update={"idempotency_key": "other-key"}))
        record = self.engine.get_operation(first.operation_id)
        rewrite = self.engine._commit_completed_with_event(record, {**saved, "event_id": "EVT-SECOND"})

        self.assertTrue(replay.replayed)
        self.assertEqual(other_key.operation_id, first.operation_id)
        self.assertFalse(rewrite)
        docs = self._events()
        self.assertEqual(len(docs), 1)
        self.assertEqual(docs[0]["confirmed_event"], saved)
        self.assertEqual(self._send_calls(), [])

    def test_failed_golf_write_creates_no_event(self):
        with patch.object(self.brs_client, "create_round", side_effect=RuntimeError("golf unavailable")):
            result = self.engine.execute_operation(self._paid_request())
        self.assertEqual(result.status, "unconfirmed_reconciliation")
        self.assertEqual(self._events(), [])
        self.assertEqual(self._send_calls(), [])

    def test_missing_payment_fact_goes_to_staff_review_without_event(self):
        request = self._paid_request()
        real_get = self.pricing.get_payment

        def without_receipt(payment_id):
            doc = dict(real_get(payment_id))
            doc.pop("transaction_id", None)
            return doc

        with patch.object(self.pricing, "get_payment", side_effect=without_receipt):
            result = self.engine.execute_operation(request)

        self.assertEqual(result.status, "unconfirmed_reconciliation")
        operation = self.engine.get_operation(result.operation_id)
        self.assertEqual(operation.staff_alert.failed_stage, "confirmed_event")
        self.assertIsNone(operation.completed_at)
        self.assertEqual(self._events(), [])

    def test_event_survives_restart(self):
        request = self._paid_request()
        first = self.engine.execute_operation(request)
        saved = self._events()[0]["confirmed_event"]

        restarted = self._engine()
        self.assertEqual(restarted.get_operation(first.operation_id).confirmed_event, saved)
        replay = restarted.execute_operation(request)
        self.assertTrue(replay.replayed)
        self.assertEqual(self._events()[0]["confirmed_event"], saved)
        record = self._coordinator().prepare_confirmation(first.operation_id, saved["event_id"])
        self.assertEqual(record.snapshot_hash, saved["snapshot_hash"])


if __name__ == "__main__":
    unittest.main()
