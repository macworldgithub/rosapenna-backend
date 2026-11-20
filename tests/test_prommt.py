"""Automated unit and integration tests for Prommt Payment Gateway Integration (Ticket F11).

Strictly adheres to the Live Prommt Payment Gateway Protection Rule:
Zero live HTTP calls to external Prommt endpoints; all tests run offline in sandbox mode.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import io
import json
import unittest
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from pydantic import SecretStr

from rosapenna_backend.api.schemas.booking_linkage import (
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
from rosapenna_backend.services.booking_linkage import BookingLinkageService
from rosapenna_backend.services.package_pricing import PackagePricingService


class FakeCollection:
    def __init__(self):
        self.docs = {}

    def insert_one(self, doc):
        key = doc.get("booking_reference") or doc.get("payment_id") or doc.get("quote_id") or str(id(doc))
        self.docs[key] = dict(doc)

    def find_one(self, query, sort=None):
        for doc in self.docs.values():
            match = True
            for k, v in query.items():
                if k == "$or":
                    or_match = False
                    for subq in v:
                        if all(
                            (sk in doc) == sv["$exists"] if isinstance(sv, dict) and "$exists" in sv
                            else doc.get(sk) == sv
                            for sk, sv in subq.items()
                        ):
                            or_match = True
                            break
                    if not or_match:
                        match = False
                        break
                elif doc.get(k) != v:
                    match = False
                    break
            if match:
                return dict(doc)
        return None

    def update_one(self, query, update):
        target = self.find_one(query)
        if target:
            key = target.get("booking_reference") or target.get("payment_id") or target.get("quote_id")
            if "$set" in update:
                self.docs[key].update(update["$set"])
            if "$push" in update:
                for pk, pv in update["$push"].items():
                    if pk not in self.docs[key]:
                        self.docs[key][pk] = []
                    self.docs[key][pk].append(pv)
            return SimpleNamespace(modified_count=1)
        return SimpleNamespace(modified_count=0)


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
            for content, expected_return in (
                ([], False),
                ([{"id": "test-resource", "externalId": "another-booking"}], False),
                ([{"id": "test-resource", "externalId": "RSP-TEST-001"}], True),
            ):
                with self.subTest(content=content):
                    opener.open.reset_mock()
                    opener.open.side_effect = [
                        io.BytesIO(json.dumps(created).encode()),
                        io.BytesIO(json.dumps({"content": content}).encode()),
                    ]
                    if expected_return:
                        self.assertEqual(self.client.create_test_checkout_link(request).payment_url, created["url"])
                    else:
                        with self.assertRaisesRegex(PrommtError, "did not verify"):
                            self.client.create_test_checkout_link(request)
                    post, lookup = [call.args[0] for call in opener.open.call_args_list]
                    sent = json.loads(post.data)
                    self.assertEqual(sent["channel"], "LINK")
                    self.assertEqual(sent["customParams"], {"Confirmation Number:": "RSP-TEST-001"})
                    self.assertFalse(sent["confirmation"])
                    self.assertTrue(lookup.full_url.endswith("/paymentrequests?mode=TEST"))

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

    def test_prommt_webhook_paid_success_and_idempotency(self):
        """AC 4 & AC 5: Webhook transitions to verified_paid and duplicate calls are idempotent."""
        opera_mock = MagicMock()
        teesheet_mock = MagicMock()
        booking_mock = MagicMock()

        fake_coll = FakeCollection()
        linkage_service = BookingLinkageService(opera_client=opera_mock)
        linkage_service.collection = fake_coll

        pricing_service = PackagePricingService(
            opera_client=opera_mock,
            teesheet_cache=teesheet_mock,
            booking_store=booking_mock,
            prommt_client=self.client,
            linkage_service=linkage_service,
        )

        booking_ref = "RSP-20270514-TEST99"
        prommt_resource_id = "pmt_req_99887766"
        payment_id = "pay_test_001"

        # Seed payment doc
        pricing_service._payments_cache[payment_id] = {
            "payment_id": payment_id,
            "prommt_request_id": prommt_resource_id,
            "booking_reference": booking_ref,
            "amount": "310.00",
            "currency": "EUR",
            "status": "unpaid",
            "evidence_source": "prommt",
            "is_test": True,
            "expires_at": (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=7)).isoformat(),
        }
        pricing_service.payments_collection = FakeCollection()
        pricing_service.payments_collection.insert_one(pricing_service._payments_cache[payment_id])

        # Seed linkage record in FakeCollection
        fake_coll.docs[booking_ref] = {
            "booking_reference": booking_ref,
            "source_channel": "online",
            "lead_guest": {"first_name": "Liam", "last_name": "Dunne", "email": "liam@example.com", "telephone": "+353871112233"},
            "opera_stay": {
                "reservation_id": "RSV-999",
                "room_category": "Classic Room",
                "room_quantity": 1,
                "arrival_date": dt.date(2027, 5, 14),
                "departure_date": dt.date(2027, 5, 17),
                "total_amount": "860.00",
                "currency": "EUR",
                "status": "Reserved",
                "occupant_names": ["Liam Dunne"],
            },
            "brs_rounds": [],
            "payments": [],
            "status": "missing_components",
            "discrepancies": ["Missing golf rounds component for stay-and-play package"],
            "financials": {"total_amount": "860.00", "deposit_paid": "0.00", "balance_due": "860.00", "currency": "EUR", "payment_status": "unpaid"},
            "created_at": dt.datetime.now(dt.timezone.utc),
            "updated_at": dt.datetime.now(dt.timezone.utc),
            "audit_trail": [],
        }

        # Construct webhook event
        now_ts = int(dt.datetime.now(dt.timezone.utc).timestamp())
        payload = {
            "eventId": "evt_succ_001",
            "type": "PAYMENT_REQUEST",
            "timestamp": str(now_ts * 1000),
            "event": "PAID_SUCCESS",
            "resourceId": prommt_resource_id,
            "mode": "TEST",
            "externalId": booking_ref,
        }
        raw_body = json.dumps(payload).encode("utf-8")
        sig = hmac.new(self.webhook_secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()

        for invalid_event, error in (
            ({"resourceId": "other-request"}, LookupError),
            ({"mode": "LIVE"}, ValueError),
        ):
            invalid_body = json.dumps({**payload, **invalid_event}).encode()
            invalid_sig = hmac.new(self.webhook_secret.encode(), invalid_body, hashlib.sha256).hexdigest()
            with self.assertRaises(error):
                pricing_service.process_prommt_webhook(invalid_body, invalid_sig)

        provider_details = {
            "transactionId": prommt_resource_id,
            "externalId": booking_ref,
            "requestedAmount": 310,
            "amount": 310,
            "currency": "EUR",
            "status": "PAID_SUCCESS",
        }
        with patch.object(self.client, "get_payment_details") as lookup:
            for mismatched in (
                {"amount": 309.99}, {"currency": "GBP"}, {"currency": None}, {"status": "PROCESSING"},
            ):
                with self.subTest(mismatched=mismatched):
                    lookup.return_value = PrommtPaymentDetailsResponse.model_validate(
                        {**provider_details, **mismatched}
                    )
                    with self.assertRaises(ValueError):
                        pricing_service.process_prommt_webhook(raw_body, sig)
                    self.assertEqual(linkage_service.get_linkage(booking_ref).payments, [])
                    self.assertEqual(pricing_service.get_payment(payment_id)["status"], "unpaid")

            lookup.return_value = PrommtPaymentDetailsResponse.model_validate(provider_details)
            with patch.object(linkage_service, "record_payment", side_effect=RuntimeError("test write failure")):
                with self.assertRaisesRegex(RuntimeError, "test write failure"):
                    pricing_service.process_prommt_webhook(raw_body, sig)
            self.assertEqual(pricing_service.get_payment(payment_id)["status"], "unpaid")
            result = pricing_service.process_prommt_webhook(raw_body, sig)
            lookup.assert_called_with(prommt_resource_id)
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["payment_state"], "verified_paid")

        # Verify linkage ledger updated
        updated_linkage = linkage_service.get_linkage(booking_ref)
        self.assertEqual(updated_linkage.financials["deposit_paid"], "310.00")
        self.assertEqual(updated_linkage.financials["balance_due"], "550.00")
        self.assertEqual(len(updated_linkage.payments), 1)
        self.assertEqual(updated_linkage.payments[0].status, "completed")
        self.assertEqual(updated_linkage.payments[0].provider, "prommt")

        # Replay after losing the in-memory cache uses the persisted payment state.
        pricing_service._payments_cache.clear()
        retry_result = pricing_service.process_prommt_webhook(raw_body, sig)
        self.assertEqual(retry_result["status"], "already_processed")

        late_failure = json.dumps({**payload, "eventId": "evt_late", "event": "PAID_FAILED"}).encode()
        late_signature = hmac.new(self.webhook_secret.encode(), late_failure, hashlib.sha256).hexdigest()
        late_result = pricing_service.process_prommt_webhook(late_failure, late_signature)
        self.assertEqual(late_result["payment_state"], "verified_paid")

        # Ledger paid total must NOT be double-counted
        linkage_after_retry = linkage_service.get_linkage(booking_ref)
        self.assertEqual(linkage_after_retry.financials["deposit_paid"], "310.00")
        self.assertEqual(len(linkage_after_retry.payments), 1)

    def test_prommt_webhook_failed_event_does_not_credit_deposit(self):
        """AC 5: Failed status does not credit ledger or confirm booking."""
        opera_mock = MagicMock()
        teesheet_mock = MagicMock()
        booking_mock = MagicMock()

        pricing_service = PackagePricingService(
            opera_client=opera_mock,
            teesheet_cache=teesheet_mock,
            booking_store=booking_mock,
            prommt_client=self.client,
        )

        payment_id = "pay_fail_001"
        resource_id = "pmt_req_fail_99"
        pricing_service._payments_cache[payment_id] = {
            "payment_id": payment_id,
            "prommt_request_id": resource_id,
            "booking_reference": "RSP-20270514-FAIL99",
            "amount": "310.00",
            "currency": "EUR",
            "status": "unpaid",
            "evidence_source": "prommt",
            "is_test": True,
            "expires_at": (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=7)).isoformat(),
        }

        now_ts = int(dt.datetime.now(dt.timezone.utc).timestamp())
        payload = {
            "eventId": "evt_fail_001",
            "type": "PAYMENT_REQUEST",
            "timestamp": now_ts,
            "event": "PAID_FAILED",
            "resourceId": resource_id,
            "mode": "TEST",
        }
        raw_body = json.dumps(payload).encode("utf-8")
        sig = hmac.new(self.webhook_secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()

        result = pricing_service.process_prommt_webhook(raw_body, sig)
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["payment_state"], "failed")

        stored = pricing_service.get_payment(payment_id)
        self.assertEqual(stored["status"], "failed")
        self.assertIn("PAID_FAILED", stored["failure_reason"])


if __name__ == "__main__":
    unittest.main()
