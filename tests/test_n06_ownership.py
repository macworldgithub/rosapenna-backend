"""N06-02 checkout ownership: only the browser that created a quote can use its guest routes.

The browser sends its token in X-Checkout-Capability. The backend stores only sha256(token).
Any other caller gets the same 404 as a missing object.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import hmac
import json
import secrets
import unittest
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

from fastapi import HTTPException, Request
from mongomock import MongoClient

from rosapenna_backend.api.dependencies import get_checkout_owner_hash
from rosapenna_backend.api.routes.packages import (
    calculate_package_quote,
    create_package_payment_link,
    get_package_payment,
    get_package_quote,
    handle_prommt_webhook,
    reconcile_prommt_test_payment,
)
from rosapenna_backend.api.routes.simulation import SimulatePaymentRequestBody, get_simulation_payment, simulate_payment
from rosapenna_backend.api.schemas.packages import (
    GolfRoundQuoteRequest,
    PackagePaymentLinkRequest,
    PackageQuoteRequest,
    RoomSelectionQuoteRequest,
)
from rosapenna_backend.integrations.opera.client import OperaClient
from rosapenna_backend.integrations.payments.simulation import PaymentSimulator
from rosapenna_backend.integrations.prommt.client import PrommtClient, PrommtSettings
from rosapenna_backend.integrations.prommt.schemas import (
    PrommtPaymentDetailsResponse,
    PrommtPaymentRequestResponse,
)
from rosapenna_backend.services.booking_intents import BookingIntentStore
from rosapenna_backend.services.package_pricing import PackagePricingService
from rosapenna_backend.services.tee_sheets import TeesheetCache
from tests.test_packages import free_sheet, make_mock_brs_avail, make_mock_opera_avail

TOKEN_A = "A" * 43
TOKEN_B = "B" * 43
HASH_A = hashlib.sha256(TOKEN_A.encode()).hexdigest()
HASH_B = hashlib.sha256(TOKEN_B.encode()).hexdigest()
MALFORMED_TOKENS = ("short", "A" * 42, "A" * 44, "A" * 42 + "+", "A" * 42 + "\n")
NOT_FOUND = (404, "Not found")


def owner(token):
    """Run the real header dependency, as the route wiring does."""
    return get_checkout_owner_hash(token)


def error_of(exc: HTTPException):
    return (exc.status_code, exc.detail)


def quote_request() -> PackageQuoteRequest:
    return PackageQuoteRequest(
        arrival_date=dt.date(2027, 6, 10),
        departure_date=dt.date(2027, 6, 12),
        rooms=[RoomSelectionQuoteRequest(room_category="CLASSIC", room_quantity=1)],
        golf_rounds=[
            GolfRoundQuoteRequest(course_id=1, date=dt.date(2027, 6, 10), time="09:30", players=2),
            GolfRoundQuoteRequest(course_id=3, date=dt.date(2027, 6, 11), time="09:30", players=2),
        ],
    )


def link_request(quote_id: str) -> PackagePaymentLinkRequest:
    return PackagePaymentLinkRequest(
        quote_id=quote_id, lead_first_name="Ann", lead_last_name="Test", lead_email="ann@example.invalid",
        lead_phone="+353871234567", residence_country="IE", accepted_terms=True,
        additional_guests=[{"first_name": "Bob", "last_name": "Test"}],
    )


class OwnershipHelperTests(unittest.TestCase):
    def test_dependency_returns_hash_only_for_well_formed_token(self):
        self.assertEqual(owner(TOKEN_A), HASH_A)
        self.assertNotEqual(owner(TOKEN_A), TOKEN_A)
        self.assertIsNone(owner(None))
        self.assertIsNone(owner(""))
        for token in MALFORMED_TOKENS:
            with self.subTest(token=token):
                self.assertIsNone(owner(token))


class GuestRouteOwnershipTests(unittest.TestCase):
    def setUp(self):
        self.mongo = MongoClient(tz_aware=True)
        self.db = self.mongo["n06_ownership_test_db"]
        mock_opera = MagicMock(spec=OperaClient)
        mock_opera.get_room_availability.return_value = make_mock_opera_avail()
        mock_cache = MagicMock(spec=TeesheetCache)
        mock_cache.get_course_availability.return_value = make_mock_brs_avail(1, "09:30", 2)
        mock_cache.get.return_value = free_sheet("09:30")
        self.secret = secrets.token_urlsafe(32)
        self.resource_id = f"res-{uuid4().hex}"
        self.client = PrommtClient(PrommtSettings(
            PROMMT_MODE="TEST", PROMMT_WEBHOOK_SECRET=self.secret, PROMMT_ALLOW_LIVE_CALLS=False,
        ))
        self.settings = SimpleNamespace(
            environment="test", payment_mode="prommt_test", simulation_enabled=False,
            simulation_run_id=None, security_bypass_in_dev=False,
        )
        self.service = PackagePricingService(
            opera_client=mock_opera, teesheet_cache=mock_cache,
            booking_store=BookingIntentStore(self.db.booking_intents), database=self.db,
            prommt_client=self.client, settings=self.settings,
        )
        self.engine = MagicMock()
        self.engine.execute_operation.return_value.status = "needs_review"
        self.engine.get_checkout_booking_status.return_value = "needs_review"
        self.provider = patch.object(
            self.client, "create_test_checkout_link",
            return_value=PrommtPaymentRequestResponse(resourceId=self.resource_id, url="https://pay.prommt.com/link/t"),
        )
        self.provider_mock = self.provider.start()

    def tearDown(self):
        self.provider.stop()
        self.mongo.close()

    def create_quote(self, token):
        return calculate_package_quote(request=quote_request(), pricing_service=self.service, owner_hash=owner(token))

    def create_link(self, token, quote_id):
        return create_package_payment_link(
            request=link_request(quote_id), pricing_service=self.service, owner_hash=owner(token),
        )

    def read_quote(self, quote_id, token):
        return get_package_quote(quote_id=quote_id, pricing_service=self.service, owner_hash=owner(token))

    def read_payment(self, payment_id, token):
        return get_package_payment(
            payment_id, self.service, self.engine, owner_hash=owner(token),
        )

    def reconcile(self, payment_id, token, details):
        with patch.object(self.client, "get_test_payment_details", return_value=details) as lookup, patch(
            "rosapenna_backend.api.routes.packages.get_booking_operation_engine", return_value=self.engine,
        ):
            result = reconcile_prommt_test_payment(
                payment_id, MagicMock(), self.service, owner_hash=owner(token),
            )
        return result, lookup

    def paid_details(self, payment_id):
        stored = self.db.package_payments.find_one({"payment_id": payment_id})
        return PrommtPaymentDetailsResponse(
            status="PAID_SUCCESS", amount=Decimal(str(stored["amount"])), currency="EUR",
            externalId=stored["booking_reference"], transactionId="txn-owner-1",
        )

    def test_create_quote_requires_well_formed_token(self):
        for token in (None, *MALFORMED_TOKENS):
            with self.subTest(token=token), self.assertRaises(HTTPException) as raised:
                self.create_quote(token)
            self.assertEqual(error_of(raised.exception), (400, "Checkout capability required"))
        self.assertEqual(self.db.package_quotes.count_documents({}), 0)

    def test_quote_read_is_owner_only(self):
        quote = self.create_quote(TOKEN_A)
        self.assertEqual(self.read_quote(quote.quote_id, TOKEN_A).quote_id, quote.quote_id)
        probes = [(quote.quote_id, TOKEN_B), (quote.quote_id, None), ("QTE-missing", TOKEN_A)]
        probes += [(quote.quote_id, token) for token in MALFORMED_TOKENS]
        for quote_id, token in probes:
            with self.subTest(quote_id=quote_id, token=token), self.assertRaises(HTTPException) as raised:
                self.read_quote(quote_id, token)
            self.assertEqual(error_of(raised.exception), NOT_FOUND)

    def test_owner_creates_one_link_and_others_get_not_found(self):
        quote = self.create_quote(TOKEN_A)
        link = self.create_link(TOKEN_A, quote.quote_id)
        self.assertNotIn("owner_hash", link.model_dump())
        self.assertEqual(self.create_link(TOKEN_A, quote.quote_id).payment_id, link.payment_id)
        self.assertEqual(self.provider_mock.call_count, 1)

        for token in (TOKEN_B, None, *MALFORMED_TOKENS):
            with self.subTest(token=token), self.assertRaises(HTTPException) as raised:
                self.create_link(token, quote.quote_id)
            self.assertEqual(error_of(raised.exception), NOT_FOUND)
        self.assertEqual(self.db.package_payments.count_documents({"quote_id": quote.quote_id}), 1)
        self.assertEqual(self.provider_mock.call_count, 1)

    def test_status_is_owner_only(self):
        quote = self.create_quote(TOKEN_A)
        link = self.create_link(TOKEN_A, quote.quote_id)
        status = self.read_payment(link.payment_id, TOKEN_A)
        self.assertEqual(status, {"status": "unpaid", "booking_status": "awaiting_payment"})
        probes = [(link.payment_id, TOKEN_B), (link.payment_id, None), ("pay_missing", TOKEN_A)]
        probes += [(link.payment_id, token) for token in MALFORMED_TOKENS]
        for payment_id, token in probes:
            with self.subTest(payment_id=payment_id, token=token), self.assertRaises(HTTPException) as raised:
                self.read_payment(payment_id, token)
            self.assertEqual(error_of(raised.exception), NOT_FOUND)

    def test_reconcile_is_owner_only(self):
        quote = self.create_quote(TOKEN_A)
        link = self.create_link(TOKEN_A, quote.quote_id)
        details = self.paid_details(link.payment_id)
        for token in (TOKEN_B, None, *MALFORMED_TOKENS):
            with self.subTest(token=token), self.assertRaises(HTTPException) as raised:
                self.reconcile(link.payment_id, token, details)
            self.assertEqual(error_of(raised.exception), NOT_FOUND)
        self.assertEqual(self.db.package_payments.find_one({"payment_id": link.payment_id})["status"], "unpaid")

        result, lookup = self.reconcile(link.payment_id, TOKEN_A, details)
        lookup.assert_called_once()
        self.assertEqual(result, {"status": "processed", "booking_status": "needs_review"})
        self.assertEqual(self.read_payment(link.payment_id, TOKEN_A)["status"], "verified_paid")

    def test_webhook_needs_no_capability_header(self):
        quote = self.create_quote(TOKEN_A)
        link = self.create_link(TOKEN_A, quote.quote_id)
        event = {
            "eventId": str(uuid4()), "resourceId": self.resource_id, "type": "PAYMENT_REQUEST",
            "event": "PAID_SUCCESS", "mode": "TEST", "externalId": link.booking_reference,
            "timestamp": int(dt.datetime.now(dt.timezone.utc).timestamp() * 1000),
        }
        body = json.dumps(event).encode()
        signature = hmac.new(self.secret.encode(), body, hashlib.sha256).hexdigest()

        async def receive():
            return {"type": "http.request", "body": body, "more_body": False}

        request = Request({"type": "http", "headers": [(b"x-signature", signature.encode())]}, receive)
        details = self.paid_details(link.payment_id)
        with patch("rosapenna_backend.api.routes.packages.get_booking_operation_engine", return_value=self.engine), patch.object(
            self.client, "get_test_payment_details", return_value=details,
        ):
            result = asyncio.run(handle_prommt_webhook(request, self.service))
        self.assertEqual(result, {"status": "processed", "booking_status": "needs_review"})

    def test_stored_records_hold_hash_not_token(self):
        quote = self.create_quote(TOKEN_A)
        link = self.create_link(TOKEN_A, quote.quote_id)
        self.assertNotIn("owner_hash", quote.model_dump())
        self.assertNotIn("owner_hash", json.dumps(self.read_payment(link.payment_id, TOKEN_A)))

        owner_doc = self.db.package_quote_owners.find_one({"quote_id": quote.quote_id})
        self.assertEqual(owner_doc["owner_hash"], HASH_A)
        self.assertEqual(self.db.package_payments.find_one({"payment_id": link.payment_id})["owner_hash"], HASH_A)
        for name in self.db.list_collection_names():
            for doc in self.db[name].find({}):
                with self.subTest(collection=name):
                    self.assertNotIn(TOKEN_A, json.dumps(doc, default=str))
        self.assertNotIn(TOKEN_A, json.dumps(self.service._payments_cache, default=str))
        self.assertNotIn(TOKEN_A, json.dumps(self.service._quotes_cache, default=str))

    def test_owner_check_uses_constant_time_compare(self):
        quote = self.create_quote(TOKEN_A)
        with patch("rosapenna_backend.services.package_pricing.hmac.compare_digest",
                   wraps=hmac.compare_digest) as compare:
            self.read_quote(quote.quote_id, TOKEN_A)
        compare.assert_called()


class SimulateOwnershipTests(unittest.TestCase):
    def setUp(self):
        self.mongo = MongoClient(tz_aware=True)
        self.db = self.mongo["n06_ownership_sim_db"]
        self.run_id = "n06-ownership-run"
        secret = "n06-ownership-sim-secret-0123456789"
        self.settings = SimpleNamespace(
            environment="test", payment_mode="simulation", simulation_enabled=True, security_bypass_in_dev=False,
            host="127.0.0.1", simulation_run_id=self.run_id, simulation_signing_secret=secret,
        )
        self.simulator = PaymentSimulator(database=self.db, default_secret=secret)
        self.service = PackagePricingService(
            opera_client=MagicMock(), teesheet_cache=MagicMock(),
            booking_store=BookingIntentStore(self.db.booking_intents), database=self.db,
            prommt_client=MagicMock(), settings=self.settings,
        )
        req = self.simulator.create_or_get_payment_request(
            booking_intent_id="bki_n06_owner", booking_reference="RSP-N06-OWNER", quote_id="QTE-N06-OWNER",
            quote_version=1, quote_hash="hash_n06_owner", amount="100.00", environment="test",
            simulation_run_id=self.run_id,
        )
        self.payment_id = req.payment_id
        self.db["package_payments"].insert_one({
            "payment_id": self.payment_id, "status": "unpaid", "evidence_source": "simulation",
            "is_test": True, "simulation_run_id": self.run_id, "purpose": "deposit", "owner_hash": HASH_A,
            "expires_at": (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1)).isoformat(),
            "booking_reference": "RSP-N06-OWNER", "quote_id": "QTE-N06-OWNER", "amount": "100.00", "currency": "EUR",
        })
        self.engine = MagicMock()
        self.engine.execute_operation.return_value.status = "needs_review"
        self.engine.execute_operation.return_value.model_dump.return_value = {"status": "needs_review"}

    def tearDown(self):
        self.mongo.close()

    def simulate(self, token, payment_id=None):
        return asyncio.run(simulate_payment(
            payment_id or self.payment_id, SimulatePaymentRequestBody(scenario="success"),
            self.settings, self.simulator, self.service, self.engine, owner_hash=owner(token),
        ))

    def test_simulate_and_read_are_owner_only(self):
        for token in (TOKEN_B, None, *MALFORMED_TOKENS):
            with self.subTest(token=token), self.assertRaises(HTTPException) as raised:
                self.simulate(token)
            self.assertEqual(error_of(raised.exception), NOT_FOUND)
            with self.assertRaises(HTTPException) as read:
                get_simulation_payment(
                    self.payment_id, self.settings, self.simulator, self.service, owner(token),
                )
            self.assertEqual(error_of(read.exception), NOT_FOUND)
        with self.assertRaises(HTTPException) as missing:
            self.simulate(TOKEN_A, payment_id="pay_sim_missing")
        self.assertEqual(error_of(missing.exception), NOT_FOUND)
        self.assertEqual(self.simulator.get_payment_request(self.payment_id).status, "unpaid")
        self.engine.execute_operation.assert_not_called()

        self.assertEqual(self.simulate(TOKEN_A)["payment_status"], "verified_paid")
        read = get_simulation_payment(self.payment_id, self.settings, self.simulator, self.service, owner(TOKEN_A))
        self.assertEqual(read["payment_id"], self.payment_id)


if __name__ == "__main__":
    unittest.main()
