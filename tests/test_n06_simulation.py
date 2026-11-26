"""Unit and integration tests for Ticket N06 Payment Simulation and Evidence Engine."""

from __future__ import annotations

import datetime as dt
import asyncio
import hashlib
import json
import unittest
from unittest.mock import MagicMock
from decimal import Decimal

from mongomock import MongoClient
from fastapi import HTTPException

from rosapenna_backend.api.routes.simulation import SimulatePaymentRequestBody, simulate_payment
from rosapenna_backend.config import Settings

from rosapenna_backend.integrations.payments.simulation import (
    ALLOWED_SIMULATION_SCENARIOS,
    PaymentSimulator,
    SimulationEventEnvelope,
    sign_simulation_payload,
    verify_simulation_signature,
)


class TestN06PaymentSimulation(unittest.TestCase):
    def setUp(self):
        self.mongo_client = MongoClient()
        self.db = self.mongo_client["rosapenna_test_n06_sim"]
        self.secret = "test-secret-signing-key-12345"
        self.simulator = PaymentSimulator(database=self.db, default_secret=self.secret)
        self.run_id = "test-run-uuid-001"

    def test_create_scoped_simulation_request(self):
        """Creates an offline payment request with exact bindings and local URL."""
        req = self.simulator.create_or_get_payment_request(
            booking_intent_id="bki_test1234567890abcdef",
            booking_reference="RSP-20270514-ABC123",
            quote_id="QTE-2027-001",
            quote_version=1,
            quote_hash="hash_abc123",
            amount="247.50",
            currency="EUR",
            environment="test",
            simulation_run_id=self.run_id,
        )

        self.assertTrue(req.payment_id.startswith("pay_sim_"))
        self.assertEqual(req.status, "unpaid")
        self.assertEqual(req.amount, "247.50")
        self.assertEqual(req.currency, "EUR")
        self.assertEqual(req.evidence_source, "simulation")
        self.assertTrue(req.is_test)
        self.assertEqual(req.url, f"/dev/payments/{req.payment_id}")
        self.assertNotIn("prommt.com", req.url)

    def test_idempotent_request_retrieval(self):
        """Repeated creation for same intent, purpose, and quote version returns existing request."""
        req1 = self.simulator.create_or_get_payment_request(
            booking_intent_id="bki_test1234567890abcdef",
            booking_reference="RSP-20270514-ABC123",
            quote_id="QTE-2027-001",
            quote_version=1,
            quote_hash="hash_abc123",
            amount="247.50",
            environment="test",
        )
        req2 = self.simulator.create_or_get_payment_request(
            booking_intent_id="bki_test1234567890abcdef",
            booking_reference="RSP-20270514-ABC123",
            quote_id="QTE-2027-001",
            quote_version=1,
            quote_hash="hash_abc123",
            amount="247.50",
            environment="test",
        )
        self.assertEqual(req1.payment_id, req2.payment_id)

    def test_hmac_signature_verification_over_raw_bytes(self):
        """Validates constant-time signature over raw bytes and detects tampering."""
        raw_payload = b'{"amount":"247.50","event_id":"evt_001","payment_id":"pay_001"}'
        sig = sign_simulation_payload(raw_payload, self.secret)

        self.assertTrue(verify_simulation_signature(raw_payload, sig, self.secret))
        # Tampered bytes
        tampered_bytes = b'{"amount":"247.51","event_id":"evt_001","payment_id":"pay_001"}'
        self.assertFalse(verify_simulation_signature(tampered_bytes, sig, self.secret))
        # Tampered signature
        self.assertFalse(verify_simulation_signature(raw_payload, sig[:-2] + "00", self.secret))
        # Wrong secret
        self.assertFalse(verify_simulation_signature(raw_payload, sig, "wrong-secret"))

    def test_simulation_event_success_transition(self):
        """Simulating success transitions payment to verified_paid with durable evidence."""
        req = self.simulator.create_or_get_payment_request(
            booking_intent_id="bki_test1234567890abcdef",
            booking_reference="RSP-20270514-ABC123",
            quote_id="QTE-2027-001",
            quote_version=1,
            quote_hash="hash_abc123",
            amount="247.50",
            environment="test",
            simulation_run_id=self.run_id,
        )

        envelope, signature, raw_bytes = self.simulator.generate_simulated_event(
            payment_id=req.payment_id,
            scenario="success",
            secret=self.secret,
            simulation_run_id=self.run_id,
        )

        processed = self.simulator.process_simulation_event(
            raw_bytes=raw_bytes,
            signature=signature,
            secret=self.secret,
            allowed_run_id=self.run_id,
        )

        self.assertEqual(processed.status, "verified_paid")
        self.assertIsNotNone(processed.paid_at)
        self.assertIsNotNone(processed.transaction_id)
        self.assertEqual(processed.evidence_source, "simulation")
        self.assertTrue(processed.is_test)

    def test_simulation_route_rejects_other_run_and_non_test_payment(self):
        req = self.simulator.create_or_get_payment_request(
            booking_intent_id="bki_test_route_scope", booking_reference="RSP-TEST-SCOPE",
            quote_id="QTE-SCOPE", quote_version=1, quote_hash="hash_scope",
            amount="100.00", environment="test", simulation_run_id=self.run_id,
        )
        settings = Settings(environment="test", payment_mode="simulation", simulation_enabled=True,
                            simulation_run_id=self.run_id)
        pricing = MagicMock()
        engine = MagicMock()
        for overrides in (
            {"evidence_source": "provider", "is_test": False, "simulation_run_id": self.run_id},
            {"evidence_source": "simulation", "is_test": True, "simulation_run_id": "other-run"},
        ):
            with self.subTest(overrides=overrides):
                pricing.get_payment.return_value = overrides
                with self.assertRaises(HTTPException) as raised:
                    asyncio.run(simulate_payment(req.payment_id, SimulatePaymentRequestBody(scenario="success"),
                                                 settings, self.simulator, pricing, engine))
                self.assertEqual(raised.exception.status_code, 404)
                self.assertEqual(self.simulator.get_payment_request(req.payment_id).status, "unpaid")
                engine.execute_operation.assert_not_called()

    def test_late_failure_does_not_replace_success_transaction(self):
        req = self.simulator.create_or_get_payment_request(
            booking_intent_id="bki_test_late_failure", booking_reference="RSP-TEST-LATE",
            quote_id="QTE-LATE", quote_version=1, quote_hash="hash_late",
            amount="100.00", environment="test", simulation_run_id=self.run_id,
        )
        _, signature, raw = self.simulator.generate_simulated_event(
            req.payment_id, "success", self.secret, simulation_run_id=self.run_id
        )
        original = self.simulator.process_simulation_event(raw, signature, self.secret, self.run_id)
        settings = Settings(environment="test", payment_mode="simulation", simulation_enabled=True,
                            simulation_run_id=self.run_id, simulation_signing_secret=self.secret)
        payment = {"evidence_source": "simulation", "is_test": True,
                   "simulation_run_id": self.run_id, "transaction_id": original.transaction_id,
                   "paid_at": original.paid_at}
        pricing = MagicMock()
        pricing.get_payment.return_value = payment
        pricing.payments_collection = None
        pricing._payments_cache = {}
        engine = MagicMock()
        engine.execute_operation.return_value.model_dump.return_value = {}
        asyncio.run(simulate_payment(req.payment_id, SimulatePaymentRequestBody(scenario="failure"),
                                     settings, self.simulator, pricing, engine))
        self.assertEqual(payment["transaction_id"], original.transaction_id)
        self.assertEqual(payment["paid_at"], original.paid_at)
        self.assertEqual(payment["status"], "verified_paid")

    def test_signed_event_requires_matching_run_request_and_test_source(self):
        req = self.simulator.create_or_get_payment_request(
            booking_intent_id="bki_test_run_binding",
            booking_reference="RSP-TEST-RUN",
            quote_id="QTE-RUN",
            quote_version=1,
            quote_hash="hash_run",
            amount="247.50",
            environment="test",
            simulation_run_id=self.run_id,
        )
        envelope, _, _ = self.simulator.generate_simulated_event(
            req.payment_id, "success", self.secret, simulation_run_id=self.run_id
        )
        for field, value in (
            ("simulation_run_id", None),
            ("simulation_run_id", "another-run"),
            ("request_id", "req_sim_wrong"),
            ("evidence_source", "provider"),
            ("is_test", False),
            ("status", "failed"),
        ):
            with self.subTest(field=field, value=value):
                event = envelope.model_dump(mode="json")
                event[field] = value
                raw = json.dumps(event, sort_keys=True).encode("utf-8")
                signature = sign_simulation_payload(raw, self.secret)
                with self.assertRaises(ValueError):
                    self.simulator.process_simulation_event(raw, signature, self.secret, self.run_id)
                self.assertEqual(self.simulator.get_payment_request(req.payment_id).status, "unpaid")

        # Even a correctly signed event cannot use a payment from another run.
        self.db["simulation_payments"].update_one(
            {"payment_id": req.payment_id}, {"$set": {"simulation_run_id": "another-run"}}
        )
        raw = json.dumps(envelope.model_dump(mode="json"), sort_keys=True).encode("utf-8")
        with self.assertRaises(ValueError):
            self.simulator.process_simulation_event(raw, sign_simulation_payload(raw, self.secret), self.secret, self.run_id)
        self.assertEqual(self.simulator.get_payment_request(req.payment_id).status, "unpaid")

    def test_replay_protection_timestamp_skew(self):
        """Rejects simulation events where timestamp is skewed beyond 300s tolerance."""
        req = self.simulator.create_or_get_payment_request(
            booking_intent_id="bki_test1234567890abcdef",
            booking_reference="RSP-20270514-ABC123",
            quote_id="QTE-2027-001",
            quote_version=1,
            quote_hash="hash_abc123",
            amount="247.50",
            environment="test",
        )

        envelope, _, _ = self.simulator.generate_simulated_event(
            payment_id=req.payment_id,
            scenario="success",
            secret=self.secret,
        )
        # Artificially skew timestamp back by 400 seconds
        stale_time = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=400)).isoformat()
        envelope.occurred_at = stale_time

        stale_raw = json.dumps(envelope.model_dump(mode="json"), sort_keys=True).encode("utf-8")
        stale_sig = sign_simulation_payload(stale_raw, self.secret)

        with self.assertRaises(ValueError) as ctx:
            self.simulator.process_simulation_event(
                raw_bytes=stale_raw,
                signature=stale_sig,
                secret=self.secret,
            )
        self.assertIn("timestamp skewed", str(ctx.exception).lower())

    def test_correlation_checks_intent_and_amount(self):
        """Rejects event if amount or intent does not correlate with payment request."""
        req = self.simulator.create_or_get_payment_request(
            booking_intent_id="bki_intent_A",
            booking_reference="RSP-20270514-ABC123",
            quote_id="QTE-2027-001",
            quote_version=1,
            quote_hash="hash_abc123",
            amount="247.50",
            environment="test",
        )

        envelope, _, _ = self.simulator.generate_simulated_event(
            payment_id=req.payment_id,
            scenario="success",
            secret=self.secret,
        )

        # Alter intent
        envelope.intent_id = "bki_different_intent"
        raw = json.dumps(envelope.model_dump(mode="json"), sort_keys=True).encode("utf-8")
        sig = sign_simulation_payload(raw, self.secret)

        with self.assertRaises(ValueError) as ctx:
            self.simulator.process_simulation_event(raw, sig, self.secret)
        self.assertIn("intent mismatch", str(ctx.exception).lower())

        # Alter amount
        envelope.intent_id = "bki_intent_A"
        envelope.amount = "10.00"
        raw2 = json.dumps(envelope.model_dump(mode="json"), sort_keys=True).encode("utf-8")
        sig2 = sign_simulation_payload(raw2, self.secret)

        with self.assertRaises(ValueError) as ctx2:
            self.simulator.process_simulation_event(raw2, sig2, self.secret)
        self.assertIn("amount mismatch", str(ctx2.exception).lower())

    def test_idempotent_event_replay_and_conflict(self):
        """Replaying identical event returns current state; replaying same event_id with different payload raises conflict."""
        req = self.simulator.create_or_get_payment_request(
            booking_intent_id="bki_test1234567890abcdef",
            booking_reference="RSP-20270514-ABC123",
            quote_id="QTE-2027-001",
            quote_version=1,
            quote_hash="hash_abc123",
            amount="247.50",
            environment="test",
        )

        envelope, signature, raw_bytes = self.simulator.generate_simulated_event(
            payment_id=req.payment_id,
            scenario="success",
            secret=self.secret,
        )

        p1 = self.simulator.process_simulation_event(raw_bytes, signature, self.secret)
        self.assertEqual(p1.status, "verified_paid")

        # Identical replay returns same
        p2 = self.simulator.process_simulation_event(raw_bytes, signature, self.secret)
        self.assertEqual(p2.status, "verified_paid")

        # Reusing same event_id with different transaction_id payload raises conflict
        envelope.transaction_id = "txn_different_attempt"
        conflict_raw = json.dumps(envelope.model_dump(mode="json"), sort_keys=True).encode("utf-8")
        conflict_sig = sign_simulation_payload(conflict_raw, self.secret)

        with self.assertRaises(ValueError) as ctx:
            self.simulator.process_simulation_event(conflict_raw, conflict_sig, self.secret)
        self.assertIn("conflict", str(ctx.exception).lower())

    def test_verified_paid_cannot_regress_to_failed(self):
        """Out of order failure or pending event never overwrites an already verified_paid state."""
        req = self.simulator.create_or_get_payment_request(
            booking_intent_id="bki_test1234567890abcdef",
            booking_reference="RSP-20270514-ABC123",
            quote_id="QTE-2027-001",
            quote_version=1,
            quote_hash="hash_abc123",
            amount="247.50",
            environment="test",
        )

        env_success, sig_success, raw_success = self.simulator.generate_simulated_event(
            payment_id=req.payment_id,
            scenario="success",
            secret=self.secret,
        )
        self.simulator.process_simulation_event(raw_success, sig_success, self.secret)

        env_fail, sig_fail, raw_fail = self.simulator.generate_simulated_event(
            payment_id=req.payment_id,
            scenario="failure",
            secret=self.secret,
        )
        p_after = self.simulator.process_simulation_event(raw_fail, sig_fail, self.secret)
        self.assertEqual(p_after.status, "verified_paid")

    def test_brs_live_writes_blocked_by_production_policy(self):
        """Live BRS write attempts are strictly blocked with BrsProductionPolicyError."""
        from rosapenna_backend.integrations.brs.booking_client import (
            BrsBookingClient,
            BrsProductionPolicyError,
        )
        from rosapenna_backend.api.schemas.brs_booking import CreateBrsRoundRequest

        client = BrsBookingClient()
        client.settings.allow_live_writes = True
        req = CreateBrsRoundRequest(
            booking_intent_id="bki_test_policy_001",
            idempotency_key="idemp_brs_policy_001",
            course_id=3,
            date=dt.date(2027, 5, 14),
            time="10:00",
            players=2,
            price="180.00",
            currency="EUR",
        )
        with self.assertRaises(BrsProductionPolicyError) as ctx:
            client.create_round(req, booking_reference="RSP-TEST-POLICY")
        self.assertIn("verified website booking and its player list", str(ctx.exception))

    def test_prommt_live_outbound_guard_and_local_urls(self):
        """Prommt client returns local dev links and guards outbound HTTP calls in test mode."""
        from rosapenna_backend.integrations.prommt import PrommtClient, PrommtError
        from rosapenna_backend.integrations.prommt.schemas import PrommtCreatePaymentRequest

        client = PrommtClient()
        client.settings.allow_live_calls = False
        # Ensure simulated links are local relative paths, not pointing to prommt.com
        link = client.get_payment_link("pmt_test_123")
        self.assertTrue(link.startswith("/dev/payments/"))
        self.assertNotIn("prommt.com", link)

        # Attempting live call in test profile must be rejected
        live_client = PrommtClient()
        live_client.settings.allow_live_calls = True
        req = PrommtCreatePaymentRequest(
            amount=100.0,
            currency="EUR",
            channel="LINK",
            payerEmail="test@example.com",
            description="Test",
        )
        with self.assertRaises(PrommtError) as ctx:
            live_client.create_payment_request(req)
        self.assertIn("strictly forbidden", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
