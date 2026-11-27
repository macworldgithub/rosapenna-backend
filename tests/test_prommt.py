"""Automated unit and integration tests for Prommt Payment Gateway Integration (Ticket F11).

Strictly adheres to the Live Prommt Payment Gateway Protection Rule:
Zero live HTTP calls to external Prommt endpoints; all tests run offline in sandbox mode.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import hmac
import io
import json
import logging
import re
import secrets
import unittest
from decimal import Decimal
from types import SimpleNamespace
from urllib.error import HTTPError
from unittest.mock import MagicMock, patch

from fastapi import HTTPException, Request
from mongomock import MongoClient
from pydantic import SecretStr, ValidationError

from rosapenna_backend.api.routes import packages as package_routes
from rosapenna_backend.api.schemas.booking_linkage import (
    BrsRoundDetails,
    LeadGuestContact,
    OperaStayDetails,
    PaymentLedgerEntry,
    StaffLinkBookingRequest,
)
from rosapenna_backend.api.schemas.packages import (
    PackagePaymentLinkRequest,
    PackageQuoteRequest,
    PackageQuoteResponse,
)
from rosapenna_backend.integrations.prommt.client import (
    PrommtClient,
    PrommtError,
    PrommtSettings,
)
from rosapenna_backend.integrations.prommt.schemas import (
    PrommtCreatePaymentRequest,
    PrommtPaymentDetailsResponse,
    PrommtPaymentRequestResponse,
    PrommtWebhookPayload,
)
from rosapenna_backend.services.booking_linkage import BookingLinkageService, LinkageConflictError
from rosapenna_backend.services.package_pricing import PackagePricingService


class PrommtIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.webhook_secret = "test-webhook-secret-key-123"
        self.settings = PrommtSettings(
            PROMMT_API_KEY="test_prommt_api_key_secret",
            PROMMT_WEBHOOK_SECRET=self.webhook_secret,
            PROMMT_BASE_URL="https://gw.prommt.com/api/v1",
            PROMMT_MODE="TEST",
            PROMMT_ALLOW_LIVE_CALLS=False,
        )
        self.client = PrommtClient(settings=self.settings)

    def test_real_checkout_only_returns_a_prommt_test_link(self):
        request = PrommtCreatePaymentRequest(
            amount=5, currency="EUR", payerEmail="guest@example.com",
            payerFirstName="Test", payerLastName="Guest", externalId="RSP-TEST-001",
        )
        created = {"resourceId": "test-resource", "url": "https://prommt.com/opaque-test-link"}
        app_settings = SimpleNamespace(environment="test", payment_mode="prommt_test")
        with patch("rosapenna_backend.config.settings", app_settings), patch("urllib.request.build_opener") as build:
            opener = build.return_value
            test_record = {
                "id": "test-resource", "externalId": "RSP-TEST-001",
                "currency": "EUR", "requestedAmount": "5.00",
            }
            for content, expected_error in (
                ([], "did not verify"),
                ([test_record | {"externalId": "another-booking"}], "did not verify"),
                ([test_record | {"requestedAmount": "4.99"}], "amount or currency"),
                ([test_record | {"currency": "USD"}], "amount or currency"),
                ([test_record | {"requestedAmount": None}], "amount or currency"),
                ([test_record], None),
            ):
                with self.subTest(content=content):
                    opener.open.reset_mock()
                    opener.open.side_effect = [
                        io.BytesIO(json.dumps(created).encode()),
                        io.BytesIO(json.dumps({"content": content}).encode()),
                    ]
                    if expected_error is None:
                        self.assertEqual(self.client.create_test_checkout_link(request).payment_url, created["url"])
                    else:
                        with self.assertRaisesRegex(PrommtError, expected_error):
                            self.client.create_test_checkout_link(request)
                    post, lookup = [call.args[0] for call in opener.open.call_args_list]
                    sent = json.loads(post.data)
                    self.assertEqual(sent["channel"], "LINK")
                    self.assertEqual(sent["customParams"], {"Confirmation Number:": "RSP-TEST-001"})
                    self.assertFalse(sent["confirmation"])
                    self.assertTrue(lookup.full_url.endswith("/paymentrequests?mode=TEST"))

            opener.open.reset_mock()
            opener.open.side_effect = HTTPError(
                "https://gw.prommt.com/api/v1/paymentrequests", 412,
                "Provider response", None, None,
            )
            with self.assertRaisesRegex(PrommtError, "returned HTTP 412"):
                self.client.create_test_checkout_link(request)
            opener.open.assert_called_once()

            app_settings.environment = "production"
            opener.open.reset_mock()
            with self.assertRaisesRegex(PrommtError, "unavailable"):
                self.client.create_test_checkout_link(request)
            opener.open.assert_not_called()

    def test_client_sandbox_guard_prevents_live_network_calls(self):
        """AC 1: allow_live_calls=False returns deterministic sandbox response without HTTP requests."""
        req = PrommtCreatePaymentRequest(
            amount=310.00,
            currency="EUR",
            channel="LINK",
            payerEmail="lead.guest@example.com",
            payerFirstName="Sarah",
            payerLastName="O'Connor",
            payerMobile="+353871234567",
            description="Stay & Play Deposit for RSP-20270514-TEST01",
            externalId="RSP-20270514-TEST01",
        )

        # Mock urllib to assert no network call is ever made
        with patch("urllib.request.urlopen") as mock_url:
            resp = self.client.create_payment_request(req)
            mock_url.assert_not_called()

        self.assertTrue(resp.resource_id.startswith("pmt_req_"))
        self.assertEqual(resp.status, "CREATED")
        self.assertIn(resp.resource_id, resp.payment_url)

    def test_webhook_signature_verification_valid_and_invalid(self):
        """AC 3: Signature verification uses HMAC-SHA256 with constant-time comparison."""
        payload_data = {
            "eventId": "evt_123456",
            "type": "PAYMENT_REQUEST",
            "timestamp": int(dt.datetime.now(dt.timezone.utc).timestamp()),
            "event": "PAID_SUCCESS",
            "resourceId": "pmt_req_abcdef",
            "mode": "TEST",
            "externalId": "RSP-20270514-TEST01",
        }
        raw_body = json.dumps(payload_data).encode("utf-8")

        # Generate valid signature
        valid_sig = hmac.new(
            self.webhook_secret.encode("utf-8"),
            raw_body,
            hashlib.sha256,
        ).hexdigest()

        # Valid signature succeeds
        self.assertTrue(self.client.verify_webhook_signature(raw_body, valid_sig))

        # Tampered body fails
        tampered_body = json.dumps({**payload_data, "amount": 999.00}).encode("utf-8")
        self.assertFalse(self.client.verify_webhook_signature(tampered_body, valid_sig))

        # Forged signature fails
        self.assertFalse(self.client.verify_webhook_signature(raw_body, "bad_signature_hex"))

    def test_webhook_timestamp_replay_protection(self):
        """AC 3: Rejects timestamps older than 300 seconds."""
        now_ts = int(dt.datetime.now(dt.timezone.utc).timestamp())
        self.assertTrue(self.client.verify_timestamp(now_ts))
        self.assertTrue(self.client.verify_timestamp(now_ts - 200))

        # Skew > 300 seconds rejected
        self.assertFalse(self.client.verify_timestamp(now_ts - 301))
        self.assertFalse(self.client.verify_timestamp(now_ts + 305))

    def test_payment_link_generation_tied_to_booking_and_lead_payer(self):
        """AC 2: Links are tied to booking, amount, EUR currency, and lead guest."""
        opera_mock = MagicMock()
        teesheet_mock = MagicMock()
        booking_mock = MagicMock()

        pricing_service = PackagePricingService(
            opera_client=opera_mock,
            teesheet_cache=teesheet_mock,
            booking_store=booking_mock,
            prommt_client=self.client,
        )

        # Seed valid quote
        quote_id = "quote_test_01"
        now_str = dt.datetime.now(dt.timezone.utc).isoformat()
        pricing_service._quotes_cache[quote_id] = {
            "quote_id": quote_id,
            "status": "available",
            "rules_version": "rules-2026-v1",
            "arrival_date": dt.date(2027, 5, 14),
            "departure_date": dt.date(2027, 5, 17),
            "nights": 3,
            "deposit_amount": "310.00",
            "balance_amount": "930.00",
            "balance_due_date": dt.date(2027, 4, 2),
            "currency": "EUR",
            "expires_at": (dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=10)).isoformat(),
            "created_at": now_str,
            "errors": [],
        }

        req = PackagePaymentLinkRequest(
            quote_id=quote_id,
            lead_first_name="Liam",
            lead_last_name="Dunne",
            lead_email="liam.dunne@example.com",
            lead_phone="+353871112233",
            residence_country="IE",
            accepted_terms=True,
        )

        # This test covers link binding only; the pre-payment stock check has its own tests.
        with patch.object(pricing_service, "_check_booking_can_complete"):
            resp = pricing_service.create_payment_link(req)
        self.assertEqual(resp.amount, "310.00")
        self.assertEqual(resp.currency, "EUR")
        self.assertEqual(resp.status, "unpaid")
        # N06 safety boundary: no simulated or sandbox link may point at a public provider host.
        self.assertTrue(resp.payment_url.startswith("/dev/payments/"))
        self.assertNotIn("prommt.com", resp.payment_url)

        # Verify payment doc cached with Prommt request ID
        stored = pricing_service.get_payment(resp.payment_id)
        self.assertIsNotNone(stored)
        self.assertEqual(stored["provider"], "prommt")
        self.assertTrue(stored["prommt_request_id"].startswith("pmt_req_"))
        self.assertEqual(stored["lead_guest"]["email"], "liam.dunne@example.com")

        # Zero card details stored (AC 6)
        self.assertNotIn("card_number", stored)
        self.assertNotIn("cvv", stored)


class _LogCapture(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.lines = []

    def emit(self, record):
        self.lines.append(record.getMessage())


class PrommtRoutedPaymentTests(unittest.TestCase):
    """F11 sandbox matrix on the routed TEST path: link, signed webhook, status lookup and recovery.

    The provider lookup, the booking engine and storage are mocks. No network call is possible.
    """

    def setUp(self):
        self.secret = secrets.token_urlsafe(32)
        self.api_key = "key-" + secrets.token_urlsafe(24)
        self.client = PrommtClient(PrommtSettings(
            PROMMT_API_KEY=self.api_key, PROMMT_WEBHOOK_SECRET=self.secret,
            PROMMT_MODE="TEST", PROMMT_ALLOW_LIVE_CALLS=False,
        ))
        self.mongo = MongoClient(tz_aware=True)
        self.db = self.mongo["f11_routed_test_db"]
        self.app_settings = SimpleNamespace(
            environment="test", payment_mode="prommt_test", simulation_enabled=False,
            security_bypass_in_dev=False, host="127.0.0.1", simulation_run_id=None,
        )
        self.service = PackagePricingService(
            opera_client=MagicMock(), teesheet_cache=MagicMock(), booking_store=MagicMock(),
            database=self.db, prommt_client=self.client, settings=self.app_settings,
        )
        now = dt.datetime.now(dt.timezone.utc)
        self.service._quotes_cache["quote_f11"] = {
            "quote_id": "quote_f11", "status": "available", "rules_version": "rules-2026-v1",
            "arrival_date": dt.date(2027, 5, 14), "departure_date": dt.date(2027, 5, 17), "nights": 3,
            "deposit_amount": "310.00", "balance_amount": "930.00", "balance_due_date": dt.date(2027, 4, 2),
            "currency": "EUR", "expires_at": (now + dt.timedelta(minutes=10)).isoformat(),
            "created_at": now.isoformat(), "errors": [],
        }
        self.resource_id = "prommt-resource-f11"
        with patch.object(self.service, "_check_booking_can_complete"), patch.object(
            self.client, "create_test_checkout_link",
            return_value=PrommtPaymentRequestResponse(resourceId=self.resource_id, url="https://pay.prommt.com/link/f11"),
        ) as create:
            self.link = self.service.create_payment_link(PackagePaymentLinkRequest(
                quote_id="quote_f11", lead_first_name="Liam", lead_last_name="Dunne",
                lead_email="liam.dunne@example.invalid", lead_phone="+353871112233", accepted_terms=True,
            ))
        self.create_call = create.call_args
        self.engine = MagicMock()
        self.engine.execute_operation.return_value.status = "needs_review"
        self.details = PrommtPaymentDetailsResponse(
            status="PAID_SUCCESS", amount=Decimal("310.00"), currency="EUR",
            externalId=self.link.booking_reference, transactionId="txn-f11",
        )
        self.event = {
            "eventId": "evt-f11-1", "resourceId": self.resource_id, "type": "PAYMENT_REQUEST",
            "event": "PAID_SUCCESS", "mode": "TEST", "externalId": self.link.booking_reference,
            "timestamp": int(dt.datetime.now(dt.timezone.utc).timestamp() * 1000),
        }
        self.logs = _LogCapture()
        logging.getLogger("booking_api").addHandler(self.logs)

    def tearDown(self):
        logging.getLogger("booking_api").removeHandler(self.logs)
        self.mongo.close()

    def stored(self):
        return self.db.package_payments.find_one({"payment_id": self.link.payment_id}, {"_id": 0})

    def deliver(self, payload, signature="valid", raw=None):
        body = raw if raw is not None else json.dumps(payload).encode()
        if signature == "valid":
            signature = hmac.new(self.secret.encode(), body, hashlib.sha256).hexdigest()

        async def receive():
            return {"type": "http.request", "body": body, "more_body": False}

        request = Request({"type": "http", "headers": [(b"x-signature", signature.encode())]}, receive)
        engine = "rosapenna_backend.api.routes.packages.get_booking_operation_engine"
        with patch(engine, return_value=self.engine):
            return asyncio.run(package_routes.handle_prommt_webhook(request, self.service))

    def test_link_is_bound_to_booking_purpose_amount_currency_expiry_and_lead_payer(self):
        """AC2: the provider request and the saved link carry the exact deposit facts for one lead payer."""
        sent = self.create_call.args[0]
        self.assertEqual(sent.amount, 310.0)
        self.assertEqual(sent.currency, "EUR")
        self.assertEqual(sent.channel, "LINK")
        self.assertEqual(sent.external_id, self.link.booking_reference)
        self.assertEqual(sent.payer_email, "liam.dunne@example.invalid")

        saved = self.stored()
        self.assertEqual(
            (saved["purpose"], saved["amount"], saved["currency"], saved["status"], saved["quote_id"]),
            ("deposit", "310.00", "EUR", "unpaid", "quote_f11"),
        )
        self.assertEqual(saved["booking_reference"], self.link.booking_reference)
        self.assertEqual(saved["prommt_request_id"], self.resource_id)
        self.assertEqual(saved["lead_guest"]["email"], "liam.dunne@example.invalid")
        self.assertTrue(saved["is_test"] and saved["quote_hash"])
        created = dt.datetime.fromisoformat(saved["created_at"])
        expires = dt.datetime.fromisoformat(saved["expires_at"])
        self.assertEqual(expires - created, dt.timedelta(days=7))
        self.assertFalse(saved["reserves_inventory"])

    def test_paid_event_records_ledger_fields_once(self):
        """AC4: a verified payment saves reference, amount, currency, purpose, status and times."""
        with patch.object(self.client, "get_test_payment_details", return_value=self.details):
            self.assertEqual(self.deliver(self.event), {"status": "processed", "booking_status": "needs_review"})
        saved = self.stored()
        self.assertEqual(
            (saved["status"], saved["provider_reference"], saved["transaction_id"], saved["amount"],
             saved["currency"], saved["purpose"], saved["prommt_event_id"]),
            ("verified_paid", self.resource_id, "txn-f11", "310.00", "EUR", "deposit", "evt-f11-1"),
        )
        self.assertTrue(saved["created_at"] and saved["paid_at"])

    def test_unverified_outcomes_never_mark_the_deposit_paid(self):
        """AC3 and AC7: bad signature, stale time, wrong amount or currency, pending and failed stay unpaid."""
        wrong_signature_body = json.dumps(self.event | {"eventId": "other"}).encode()
        cases = [
            ("tampered body", dict(raw=wrong_signature_body, signature=hmac.new(
                self.secret.encode(), json.dumps(self.event).encode(), hashlib.sha256).hexdigest()), 401, None),
            ("empty signature", dict(signature=""), 401, None),
            ("stale timestamp", dict(payload=self.event | {"timestamp": 1000}), 400, None),
            ("test event with live mode", dict(payload=self.event | {"mode": "LIVE"}), 400, None),
            ("amount mismatch", dict(payload=self.event), 409, {"amount": Decimal("309.99")}),
            ("amount missing", dict(payload=self.event), 409, {"amount": None}),
            ("currency mismatch", dict(payload=self.event), 409, {"currency": "GBP"}),
            ("currency missing", dict(payload=self.event), 409, {"currency": None}),
            ("booking mismatch", dict(payload=self.event), 409, {"external_id": "RSP-OTHER"}),
            ("no transaction id", dict(payload=self.event), 409, {"transaction_id": None}),
            ("provider pending", dict(payload=self.event), 409, {"status": "PENDING"}),
            ("provider processing", dict(payload=self.event), 409, {"status": "PROCESSING"}),
            ("provider created", dict(payload=self.event), 409, {"status": "CREATED"}),
            ("provider failed", dict(payload=self.event), 409, {"status": "PAID_FAILED"}),
            ("provider paid externally", dict(payload=self.event), 409, {"status": "PAID_EXTERNALLY"}),
            ("provider unknown status", dict(payload=self.event), 409, {"status": "SOMETHING_NEW"}),
        ]
        with patch.object(self.client, "get_test_payment_details") as lookup:
            for name, delivery, expected, override in cases:
                with self.subTest(name), self.assertRaises(HTTPException) as raised:
                    lookup.return_value = self.details.model_copy(update=override or {})
                    self.deliver(delivery.get("payload"), delivery.get("signature", "valid"), delivery.get("raw"))
                self.assertEqual(raised.exception.status_code, expected)
                self.assertEqual(self.stored()["status"], "unpaid")
                self.assertNotIn("transaction_id", self.stored())
            # The first four cases stop before the provider is asked.
            self.assertEqual(lookup.call_count, len(cases) - 4)
        self.engine.execute_operation.assert_not_called()
        self.assertEqual(self.db.package_payments.count_documents({"status": "verified_paid"}), 0)

    def test_pending_and_failed_events_are_ignored_without_lookup(self):
        for name in ("SENT", "PENDING", "PAID_FAILED", "EXPIRED", "CANCELED", "ABANDONED"):
            with self.subTest(name), patch.object(self.client, "get_test_payment_details") as lookup:
                self.assertEqual(self.deliver(self.event | {"event": name}), {"status": "ignored"})
                lookup.assert_not_called()
            self.assertEqual(self.stored()["status"], "unpaid")
        self.engine.execute_operation.assert_not_called()

    def test_settled_webhook_event_does_not_mark_the_deposit_paid(self):
        """SETTLED is undocumented, so a signed SETTLED event is ignored without a provider lookup."""
        with patch.object(self.client, "get_test_payment_details") as lookup:
            self.assertEqual(self.deliver(self.event | {"event": "SETTLED", "eventId": "evt-settled"}), {"status": "ignored"})
            lookup.assert_not_called()
        self.assertEqual(self.stored()["status"], "unpaid")
        self.engine.execute_operation.assert_not_called()
        self.assertEqual(self.db.package_payments.count_documents({"status": "verified_paid"}), 0)

    def test_settled_provider_status_on_reconcile_route_does_not_mark_the_deposit_paid(self):
        """SETTLED from the provider lookup fails closed on the reconcile route: no booking, no ledger credit."""
        with patch.object(self.client, "get_test_payment_details", return_value=self.details.model_copy(
            update={"status": "SETTLED"})), patch.object(
            package_routes, "get_booking_operation_engine", return_value=self.engine,
        ), self.assertRaises(HTTPException) as raised:
            package_routes.reconcile_prommt_test_payment(self.link.payment_id, MagicMock(), self.service)
        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(self.stored()["status"], "unpaid")
        self.assertNotIn("transaction_id", self.stored())
        self.engine.execute_operation.assert_not_called()
        self.assertEqual(self.db.package_payments.count_documents({"status": "verified_paid"}), 0)

    def test_failed_then_paid_then_duplicate_and_late_failure_credit_once(self):
        """AC4 and AC7: out-of-order and repeated events cannot credit twice or undo a verified payment."""
        with patch.object(self.client, "get_test_payment_details", return_value=self.details) as lookup:
            self.assertEqual(self.deliver(self.event | {"event": "PAID_FAILED", "eventId": "evt-early-fail"}), {"status": "ignored"})
            self.assertEqual(self.stored()["status"], "unpaid")
            self.assertEqual(self.deliver(self.event)["status"], "processed")
            self.assertEqual(self.deliver(self.event)["status"], "processed")
            self.assertEqual(self.deliver(self.event | {"eventId": "evt-new-id"})["status"], "processed")
            self.assertEqual(self.deliver(self.event | {"event": "PAID_FAILED", "eventId": "evt-late-fail"}), {"status": "ignored"})
            self.assertEqual(lookup.call_count, 1)
        saved = self.stored()
        self.assertEqual((saved["status"], saved["transaction_id"], saved["prommt_event_id"]), ("verified_paid", "txn-f11", "evt-f11-1"))
        self.assertEqual(self.db.package_payments.count_documents({"payment_id": self.link.payment_id}), 1)
        keys = {call.args[0].idempotency_key for call in self.engine.execute_operation.call_args_list}
        self.assertEqual(keys, {f"prommt_test_{self.link.payment_id}"})

    def test_status_reconciliation_needs_exact_provider_facts(self):
        """AC3: the recovery route uses the provider status lookup with the same exact checks."""
        recover = package_routes.reconcile_prommt_test_payment
        request = MagicMock()
        with patch.object(self.client, "get_test_payment_details") as lookup, patch.object(
            package_routes, "get_booking_operation_engine", return_value=self.engine,
        ):
            for override in ({"status": "PENDING"}, {"currency": "GBP"}, {"amount": Decimal("1.00")}):
                lookup.return_value = self.details.model_copy(update=override)
                with self.subTest(override), self.assertRaises(HTTPException) as raised:
                    recover(self.link.payment_id, request, self.service)
                self.assertEqual(raised.exception.status_code, 409)
                self.assertEqual(self.stored()["status"], "unpaid")
            lookup.return_value = self.details
            self.assertEqual(recover(self.link.payment_id, request, self.service)["status"], "processed")
        self.assertEqual(self.stored()["status"], "verified_paid")

    def test_malformed_and_unknown_webhooks_are_rejected_or_ignored_without_writes(self):
        """Malformed events fail closed. An unknown payment request is ignored and never looked up."""
        with patch.object(self.client, "get_test_payment_details") as lookup:
            for missing in ("type", "mode"):
                with self.subTest(missing=missing), self.assertRaises(HTTPException) as raised:
                    self.deliver({key: value for key, value in self.event.items() if key != missing})
                self.assertEqual(raised.exception.status_code, 400)
            self.assertEqual(self.deliver(self.event | {"resourceId": "prommt-unknown"}), {"status": "ignored"})
            lookup.assert_not_called()
        self.assertEqual(self.stored()["status"], "unpaid")
        self.engine.execute_operation.assert_not_called()

    def test_booking_failure_after_verification_is_retried_with_the_same_key(self):
        """A failed booking step after verification is retried once with the same idempotency key."""
        self.engine.execute_operation.side_effect = [RuntimeError("test write failure"), MagicMock(status="needs_review")]
        with patch.object(self.client, "get_test_payment_details", return_value=self.details):
            with self.assertRaisesRegex(RuntimeError, "test write failure"):
                self.deliver(self.event)
            self.assertEqual(self.deliver(self.event), {"status": "processed", "booking_status": "needs_review"})
        keys = {call.args[0].idempotency_key for call in self.engine.execute_operation.call_args_list}
        self.assertEqual(keys, {f"prommt_test_{self.link.payment_id}"})
        self.assertEqual(self.engine.execute_operation.call_count, 2)
        self.assertEqual(self.stored()["status"], "verified_paid")

    def test_legacy_callback_must_match_link_amount_and_currency(self):
        from rosapenna_backend.api.schemas.packages import PaymentCallbackRequest

        now_ts = int(dt.datetime.now(dt.timezone.utc).timestamp())
        for amount, currency in (("1.00", "EUR"), ("310.00", "GBP")):
            signed = f"{self.link.payment_id}:txn-x:completed:{Decimal(amount):.2f}:{now_ts}"
            callback = PaymentCallbackRequest(
                payment_id=self.link.payment_id, transaction_id="txn-x", status="completed", amount=amount,
                currency=currency, timestamp=now_ts,
                provider_signature=hmac.new(self.secret.encode(), signed.encode(), hashlib.sha256).hexdigest(),
            )
            with self.subTest(amount=amount, currency=currency), self.assertRaisesRegex(ValueError, "amount or currency"):
                self.service.process_payment_callback(callback, self.secret)
            self.assertEqual(self.stored()["status"], "unpaid")

    def test_no_card_data_split_pay_refund_or_cancel_surface(self):
        """AC6: no card fields, split payment, refund or cancel operations exist on the payment surface."""
        for model, extra in (
            (PrommtCreatePaymentRequest, {"amount": 5, "payerEmail": "a@b.invalid"}),
            (PaymentLedgerEntry, {"payment_id": "p", "provider": "prommt", "payment_type": "deposit",
                                  "amount": "5.00", "status": "completed"}),
        ):
            for field in ("card_number", "cvv", "security_code", "cardNumber"):
                with self.subTest(model=model.__name__, field=field), self.assertRaises(ValidationError):
                    model(**extra, **{field: "4111111111111111"})
        saved = json.dumps(self.stored(), default=str).lower()
        self.assertFalse(re.search(r"card_?number|cvv|cvc|security_?code|4111", saved))
        forbidden = re.compile(r"refund|cancel|split|chargeback|capture", re.I)
        names = [n for n in dir(PrommtClient) + dir(PackagePricingService) if not n.startswith("__")]
        names += [route.path for router in (package_routes.router, package_routes.webhook_router) for route in router.routes]
        self.assertEqual([n for n in names if forbidden.search(n)], [])

    def test_secrets_are_not_logged_returned_or_printed(self):
        """Secret hygiene: key and webhook secret stay out of repr, dumps, errors, logs and HTTP detail."""
        secret_values = (self.api_key, self.secret)

        def assert_clean(text):
            for value in secret_values:
                self.assertNotIn(value, text)

        for text in (repr(self.client.settings), str(self.client.settings), self.client.settings.model_dump_json()):
            assert_clean(text)

        with patch.object(self.client, "get_test_payment_details", return_value=self.details):
            self.deliver(self.event)
            self.deliver(self.event | {"eventId": "dup"})
        for bad in (self.event | {"mode": "LIVE"}, self.event | {"timestamp": 1000}):
            with self.assertRaises(HTTPException) as raised:
                self.deliver(bad)
            assert_clean(str(raised.exception.detail))
        with self.assertRaises(HTTPException) as raised:
            self.deliver(self.event, signature="wrong")
        assert_clean(str(raised.exception.detail))

        # Live-path provider errors: the response body and the key are neither logged nor returned.
        live = PrommtClient(PrommtSettings(PROMMT_API_KEY=self.api_key, PROMMT_ALLOW_LIVE_CALLS=True))
        request = PrommtCreatePaymentRequest(amount=5, payerEmail="guest@example.invalid", externalId="RSP-X")
        body = io.BytesIO(b'{"debug":"payer guest@example.invalid echoed"}')
        failure = HTTPError("https://gw.prommt.com/api/v1/paymentrequests", 500, "err", None, body)
        with patch("rosapenna_backend.config.settings", SimpleNamespace(environment="production")), patch.dict(
            "os.environ", {"PROMMT_ALLOW_LIVE_CALLS": "true"},
        ), patch("urllib.request.urlopen", side_effect=failure):
            with self.assertRaises(PrommtError) as raised:
                live.create_payment_request(request)
        self.assertEqual(raised.exception.message, "Prommt API error (HTTP 500)")
        with patch("rosapenna_backend.config.settings", SimpleNamespace(environment="production")), patch.dict(
            "os.environ", {"PROMMT_ALLOW_LIVE_CALLS": "true"},
        ), patch("urllib.request.urlopen", side_effect=OSError(f"boom {self.api_key}")):
            with self.assertRaises(PrommtError) as raised:
                live.create_payment_request(request)
        assert_clean(raised.exception.message)

        assert_clean("\n".join(self.logs.lines))
        self.assertNotIn("echoed", "\n".join(self.logs.lines))


class LedgerBalanceTests(unittest.TestCase):
    """AC5: the booking ledger counts only completed, same-currency payments toward the balance."""

    def setUp(self):
        self.mongo = MongoClient(tz_aware=True)
        self.service = BookingLinkageService(database=self.mongo["f11_ledger_test_db"])
        self.ref = "RSP-20270701-LEDG01"
        self.service.link_booking(StaffLinkBookingRequest(
            booking_reference=self.ref, source_channel="online",
            lead_guest=LeadGuestContact(first_name="Jane", last_name="Golfer", email="jane@example.invalid", telephone="+353871234567"),
            manual_opera_stay=OperaStayDetails(
                reservation_id="RES-F11", confirmation_number="CONF-F11", hotel_id="ROSA", room_category="DELUXE",
                room_quantity=1, arrival_date=dt.date(2027, 7, 1), departure_date=dt.date(2027, 7, 5),
                total_amount="1000.00", currency="EUR", occupant_names=["Jane Golfer"],
            ),
            brs_rounds=[BrsRoundDetails(
                round_id="RND-F11", course_id=1, course_name="Old Tom Morris Links", date=dt.date(2027, 7, 2),
                time="10:00", holes="18", players=2, brs_booking_id="BRS-F11", price="600.00", currency="EUR",
            )],
            reason="F11 ledger test",
        ), actor="test_setup")

    def tearDown(self):
        self.mongo.close()

    def entry(self, status, payment_id="PAY-1", currency="EUR"):
        return PaymentLedgerEntry(
            payment_id=payment_id, provider="prommt", payment_type="deposit", amount="400.00", currency=currency,
            status=status, provider_reference="resource-1", verified_at=dt.datetime.now(dt.timezone.utc),
        )

    def financials(self):
        return self.service.get_linkage(self.ref).financials

    def test_only_completed_payments_count(self):
        for status in ("pending", "failed", "refunded", "paid_unconfirmed"):
            with self.subTest(status):
                self.service.record_payment(self.ref, self.entry(status, f"PAY-{status}"), actor="test")
                financials = self.financials()
                self.assertEqual(financials["verified_paid_total"], "0.00")
                self.assertEqual(financials["balance_due"], "1600.00")
                self.assertEqual(financials["payment_status"], "unpaid")
        for status in ("disputed", "unknown", "chargeback"):
            with self.subTest(rejected=status), self.assertRaises(ValidationError):
                self.entry(status)

    def test_completed_counts_once_and_cannot_be_downgraded_or_repeated(self):
        self.service.record_payment(self.ref, self.entry("pending"), actor="test")
        self.service.record_payment(self.ref, self.entry("completed"), actor="test")
        self.assertEqual((self.financials()["verified_paid_total"], self.financials()["balance_due"]), ("400.00", "1200.00"))
        version = self.service.get_linkage(self.ref).ledger_version
        for late in ("completed", "pending", "failed"):
            self.service.record_payment(self.ref, self.entry(late), actor="test")
        linkage = self.service.get_linkage(self.ref)
        self.assertEqual(linkage.ledger_version, version)
        self.assertEqual([(p.payment_id, p.status) for p in linkage.payments], [("PAY-1", "completed")])
        self.assertEqual(linkage.financials["verified_paid_total"], "400.00")

    def test_payment_in_another_currency_is_not_credited(self):
        with self.assertRaisesRegex(LinkageConflictError, "currency"):
            self.service.record_payment(self.ref, self.entry("completed", currency="GBP"), actor="test")
        self.assertEqual(self.service.get_linkage(self.ref).payments, [])
        self.assertEqual(self.financials()["verified_paid_total"], "0.00")
        self.service.record_payment(self.ref, self.entry("pending", "PAY-GBP", currency="GBP"), actor="test")
        self.assertEqual(self.financials()["verified_paid_total"], "0.00")


if __name__ == "__main__":
    unittest.main()
