"""Ticket N06 hardening: durable booking work for verified payment events.

A verified deposit records booking_work = pending in the same update. A failed commit
leaves it pending and answers 202. Recovery runs owed work once. All providers, the booking
engine and storage are mocks or mongomock. No network call is possible.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import json
import unittest
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from fastapi.responses import JSONResponse
from mongomock import MongoClient

from rosapenna_backend.api.routes.simulation import SimulatePaymentRequestBody, simulate_payment

# Checkout owner hash for a well-formed test capability token (the route receives this value).
OWNER_HASH = hashlib.sha256(('A' * 43).encode()).hexdigest()
from rosapenna_backend.config import Settings
from rosapenna_backend.integrations.payments.simulation import PaymentSimulator
from rosapenna_backend.integrations.prommt.schemas import PrommtWebhookPayload
from rosapenna_backend.integrations.prommt.schemas import PrommtPaymentDetailsResponse
from rosapenna_backend.services.package_pricing import PackagePricingService
from tests import test_prommt as prommt_tests


class FakeBookingEngine:
    """Counts real executions per idempotency key, like the real engine's replay guard."""

    def __init__(self, fail: bool = False):
        self.fail = fail
        self.executed: dict[str, int] = {}
        self.completed: set[str] = set()

    def execute_operation(self, request):
        key = request.idempotency_key
        if key in self.completed:
            return SimpleNamespace(status="confirmed")
        if self.fail:
            raise RuntimeError("booking backend unavailable")
        self.executed[key] = self.executed.get(key, 0) + 1
        self.completed.add(key)
        return SimpleNamespace(status="confirmed")


class TestPrommtBookingWorkEvents(unittest.TestCase):
    def setUp(self):
        # Reuse the routed Prommt fixture (link, signed webhook, mocks) without re-running its tests.
        self.h = prommt_tests.PrommtRoutedPaymentTests(methodName="deliver")
        self.h.setUp()
        self.addCleanup(self.h.tearDown)
        self.payment_id = self.h.link.payment_id
        self.service = self.h.service
        self.engine = FakeBookingEngine(fail=True)
        self.h.engine = self.engine

    def stored(self):
        return self.h.stored()

    def deliver_verified(self, event=None):
        with patch.object(self.h.client, "get_test_payment_details", return_value=self.h.details):
            return self.h.deliver(event or self.h.event)

    def test_paid_event_sets_pending_in_same_update_as_verified_paid(self):
        with patch.object(self.h.client, "get_test_payment_details", return_value=self.h.details):
            self.service.apply_prommt_test_payment(PrommtWebhookPayload.model_validate(self.h.event))
        saved = self.stored()
        self.assertEqual(saved["status"], "verified_paid")
        self.assertEqual(saved["booking_work"], "pending")
        self.assertIn("booking_work_updated_at", saved)
        self.assertEqual(saved["booking_idempotency_key"], f"prommt_test_{self.payment_id}")

    def test_commit_failure_leaves_pending_and_answers_202(self):
        response = self.deliver_verified()
        self.assertIsInstance(response, JSONResponse)
        self.assertEqual(response.status_code, 202)
        self.assertEqual(json.loads(response.body), {"status": "accepted_pending_booking"})
        saved = self.stored()
        self.assertEqual((saved["status"], saved["booking_work"]), ("verified_paid", "pending"))

    def test_find_and_process_completes_pending_work_once(self):
        self.deliver_verified()
        self.assertEqual(self.service.find_pending_booking_work(), [self.payment_id])

        self.engine.fail = False
        seen = []

        def commit(payment):
            seen.append((payment["payment_id"], payment["booking_idempotency_key"]))
            return self.engine.execute_operation(
                SimpleNamespace(idempotency_key=payment["booking_idempotency_key"])
            ).status

        completed = self.service.process_pending_booking_work(commit)
        self.assertEqual(completed, [self.payment_id])
        self.assertEqual(seen, [(self.payment_id, f"prommt_test_{self.payment_id}")])
        self.assertEqual(self.stored()["booking_work"], "done")
        self.assertEqual(self.service.find_pending_booking_work(), [])

    def test_second_recovery_run_does_nothing(self):
        self.deliver_verified()
        self.engine.fail = False
        calls = []

        def commit(payment):
            calls.append(payment["payment_id"])
            return "confirmed"

        self.service.process_pending_booking_work(commit)
        self.assertEqual(self.service.process_pending_booking_work(commit), [])
        self.assertEqual(calls, [self.payment_id])

    def test_unverified_payment_never_gets_pending_work(self):
        self.assertNotIn("booking_work", self.stored())
        self.assertEqual(self.service.find_pending_booking_work(), [])
        calls = []
        self.assertEqual(self.service.process_pending_booking_work(lambda p: calls.append(p) or "confirmed"), [])
        self.assertEqual(calls, [])

    def test_failed_provider_status_never_gets_pending_work(self):
        failed = PrommtPaymentDetailsResponse(
            status="FAILED", amount=Decimal("310.00"), currency="EUR",
            externalId=self.h.link.booking_reference, transactionId="txn-f11",
        )
        with patch.object(self.h.client, "get_test_payment_details", return_value=failed):
            with self.assertRaises(ValueError):
                self.service.apply_prommt_test_payment(PrommtWebhookPayload.model_validate(self.h.event))
        saved = self.stored()
        self.assertNotEqual(saved["status"], "verified_paid")
        self.assertNotIn("booking_work", saved)
        self.assertEqual(self.service.find_pending_booking_work(), [])

    def test_duplicate_delivery_creates_no_second_operation_or_credit(self):
        self.engine.fail = False
        first = self.deliver_verified()
        self.assertEqual(first, {"status": "processed", "booking_status": "confirmed"})
        before = self.stored()

        second = self.deliver_verified()
        self.assertEqual(second, {"status": "processed", "booking_status": "confirmed"})
        after = self.stored()

        key = f"prommt_test_{self.payment_id}"
        self.assertEqual(self.engine.executed, {key: 1})
        self.assertEqual(after["prommt_event_id"], before["prommt_event_id"])
        self.assertEqual(after["transaction_id"], before["transaction_id"])
        self.assertEqual(after["booking_work"], "done")

    def test_duplicate_after_failed_commit_retries_same_key_only(self):
        self.assertEqual(self.deliver_verified().status_code, 202)
        self.engine.fail = False
        self.assertEqual(self.deliver_verified(), {"status": "processed", "booking_status": "confirmed"})
        self.assertEqual(self.engine.executed, {f"prommt_test_{self.payment_id}": 1})
        self.assertEqual(self.stored()["booking_work"], "done")


class TestSimulationBookingWork(unittest.TestCase):
    def setUp(self):
        self.run_id = "test-run-n06-hardening"
        self.mongo = MongoClient()
        self.db = self.mongo["rosapenna_test_n06_hardening_events"]
        self.simulator = PaymentSimulator(database=self.db, default_secret="test-secret-signing-key-12345")
        self.settings = Settings(environment="test", payment_mode="simulation", simulation_enabled=True,
                                 simulation_run_id=self.run_id)
        self.service = PackagePricingService(
            opera_client=MagicMock(), teesheet_cache=MagicMock(), booking_store=MagicMock(),
            database=self.db,
            prommt_client=MagicMock(),
            settings=SimpleNamespace(environment="test", payment_mode="simulation", simulation_enabled=True,
                                     security_bypass_in_dev=False, host="127.0.0.1", simulation_run_id=self.run_id),
        )
        req = self.simulator.create_or_get_payment_request(
            booking_intent_id="bki_n06_events", booking_reference="RSP-N06-EVENTS",
            quote_id="QTE-N06-EVENTS", quote_version=1, quote_hash="hash_n06_events",
            amount="100.00", environment="test", simulation_run_id=self.run_id,
        )
        self.payment_id = req.payment_id
        self.db["package_payments"].insert_one({
            "payment_id": self.payment_id, "status": "unpaid", "evidence_source": "simulation", "owner_hash": OWNER_HASH,
            "is_test": True, "simulation_run_id": self.run_id, "purpose": "deposit",
            "expires_at": (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1)).isoformat(),
            "booking_reference": "RSP-N06-EVENTS", "quote_id": "QTE-N06-EVENTS", "amount": "100.00",
            "currency": "EUR",
        })

    def tearDown(self):
        self.mongo.close()

    def simulate(self, engine, scenario="success"):
        return asyncio.run(simulate_payment(
            self.payment_id, SimulatePaymentRequestBody(scenario=scenario),
            self.settings, self.simulator, self.service, engine, owner_hash=OWNER_HASH,
        ))

    def test_simulation_paid_sets_pending_and_failed_commit_answers_202(self):
        engine = FakeBookingEngine(fail=True)
        response = self.simulate(engine)
        self.assertIsInstance(response, JSONResponse)
        self.assertEqual(response.status_code, 202)
        saved = self.db["package_payments"].find_one({"payment_id": self.payment_id}, {"_id": 0})
        self.assertEqual((saved["status"], saved["booking_work"]), ("verified_paid", "pending"))
        self.assertEqual(saved["booking_idempotency_key"], f"sim_commit_{self.payment_id}")

        engine.fail = False
        keys = []
        completed = self.service.process_pending_booking_work(
            lambda p: keys.append(p["booking_idempotency_key"]) or
            engine.execute_operation(SimpleNamespace(idempotency_key=p["booking_idempotency_key"])).status
        )
        self.assertEqual(completed, [self.payment_id])
        self.assertEqual(keys, [f"sim_commit_{self.payment_id}"])
        self.assertEqual(self.service.find_pending_booking_work(), [])
        self.assertEqual(engine.executed, {f"sim_commit_{self.payment_id}": 1})

    def test_simulation_failure_scenario_never_gets_pending_work(self):
        engine = FakeBookingEngine()
        response = self.simulate(engine, scenario="failure")
        self.assertEqual(response["payment_status"], "failed")
        saved = self.db["package_payments"].find_one({"payment_id": self.payment_id}, {"_id": 0})
        self.assertNotIn("booking_work", saved)
        self.assertEqual(self.service.find_pending_booking_work(), [])
        self.assertEqual(engine.executed, {})


class TestFindPendingLimit(unittest.TestCase):
    def test_limit_is_clamped_to_1_and_100(self):
        mongo = MongoClient()
        self.addCleanup(mongo.close)
        db = mongo["rosapenna_test_n06_limit"]
        for i in range(3):
            db["package_payments"].insert_one({
                "payment_id": f"pay_{i}", "status": "verified_paid", "booking_work": "pending",
                "booking_work_updated_at": dt.datetime(2026, 1, 1 + i, tzinfo=dt.timezone.utc).isoformat(),
            })
        service = PackagePricingService(
            opera_client=MagicMock(), teesheet_cache=MagicMock(), booking_store=MagicMock(),
            database=db, prommt_client=MagicMock(),
            settings=SimpleNamespace(environment="test", payment_mode="simulation"),
        )
        self.assertEqual(service.find_pending_booking_work(limit=0), ["pay_0"])
        self.assertEqual(service.find_pending_booking_work(limit=500), ["pay_0", "pay_1", "pay_2"])
        self.assertEqual(service.find_pending_booking_work(limit=2), ["pay_0", "pay_1"])


if __name__ == "__main__":
    unittest.main()
