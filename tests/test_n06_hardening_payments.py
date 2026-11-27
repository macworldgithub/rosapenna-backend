"""Hardening tests for package payment-link creation: no duplicate or extra payable deposit attempts."""

from __future__ import annotations

import datetime as dt
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock

from mongomock import MongoClient

from rosapenna_backend.api.schemas.packages import PackagePaymentLinkRequest
from rosapenna_backend.config import Settings
from rosapenna_backend.integrations.payments.simulation import PaymentSimulator
from rosapenna_backend.services.booking_intents import BookingIntentStore
from rosapenna_backend.services.package_pricing import PackagePricingService
from tests.test_n06_commit import make_mock_available_quote
from tests.test_packages import free_sheet, make_mock_opera_avail


class N06PaymentLinkHardeningTests(unittest.TestCase):
    def setUp(self):
        self.db = MongoClient()["rosapenna_test_n06_hardening"]
        self.settings = Settings(
            environment="test",
            payment_mode="simulation",
            simulation_enabled=True,
            simulation_signing_secret="test-sim-secret-key-12345",
            simulation_run_id="run-n06-hardening-test-01",
        )
        self.opera = MagicMock()
        self.opera.get_room_availability.return_value = make_mock_opera_avail()
        self.teesheet = MagicMock()
        self.teesheet.get.return_value = {
            "status": "success",
            "data": {"tee_times": free_sheet("14:00")["data"]["tee_times"] + free_sheet("10:00")["data"]["tee_times"]},
        }
        self.simulator = PaymentSimulator(database=self.db, default_secret=self.settings.simulation_signing_secret)
        self.service = PackagePricingService(
            opera_client=self.opera,
            teesheet_cache=self.teesheet,
            booking_store=BookingIntentStore(self.db["booking_intents"]),
            database=self.db,
            payment_simulator=self.simulator,
            settings=self.settings,
        )
        self.quote = make_mock_available_quote()
        self.service._cache_and_return_quote(self.quote)

    def _request(self) -> PackagePaymentLinkRequest:
        return PackagePaymentLinkRequest(
            quote_id=self.quote.quote_id,
            lead_first_name="Jane",
            lead_last_name="Golfer",
            lead_email="jane@example.invalid",
            lead_phone="+353871234567",
            accepted_terms=True,
        )

    def _deposit_docs(self) -> list:
        return list(self.db["package_payments"].find({"quote_id": self.quote.quote_id, "purpose": "deposit"}))

    def _mark_paid(self, payment_id: str) -> None:
        _, signature, raw = self.simulator.generate_simulated_event(
            payment_id=payment_id,
            scenario="success",
            secret=self.settings.simulation_signing_secret,
            simulation_run_id=self.settings.simulation_run_id,
        )
        self.simulator.process_simulation_event(
            raw, signature, self.settings.simulation_signing_secret,
            allowed_run_id=self.settings.simulation_run_id,
        )

    def _expire_attempts(self) -> None:
        past = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=1)).isoformat()
        self.db["package_payments"].update_many({"quote_id": self.quote.quote_id}, {"$set": {"expires_at": past}})
        for doc in self.service._payments_cache.values():
            if doc.get("quote_id") == self.quote.quote_id:
                doc["expires_at"] = past

    def test_retry_returns_same_link_and_creates_one_document(self):
        first = self.service.create_payment_link(self._request())
        second = self.service.create_payment_link(self._request())

        self.assertEqual(first.payment_id, second.payment_id)
        self.assertEqual(first.payment_url, second.payment_url)
        self.assertEqual(len(self._deposit_docs()), 1)

    def test_new_attempt_blocked_after_verified_deposit(self):
        first = self.service.create_payment_link(self._request())
        self._mark_paid(first.payment_id)

        with self.assertRaisesRegex(ValueError, "This booking already has a verified deposit. No new payment was created."):
            self.service.create_payment_link(self._request())
        self.assertEqual(len(self._deposit_docs()), 1)

    def test_new_attempt_allowed_after_expiry(self):
        first = self.service.create_payment_link(self._request())
        self._expire_attempts()

        second = self.service.create_payment_link(self._request())

        self.assertNotEqual(first.payment_id, second.payment_id)
        self.assertEqual(len(self._deposit_docs()), 2)

    def test_parallel_calls_create_exactly_one_document(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.service.create_payment_link(self._request()), range(2)))

        self.assertEqual(results[0].payment_id, results[1].payment_id)
        self.assertEqual(len(self._deposit_docs()), 1)


if __name__ == "__main__":
    unittest.main()
