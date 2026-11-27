"""Unit and integration tests for Ticket N06: Paid Booking Creation and Safe Commit."""

from __future__ import annotations

import datetime as dt
import asyncio
import hashlib
import hmac
import json
import secrets
from uuid import uuid4
import unittest
from decimal import Decimal
from unittest.mock import MagicMock, patch

from mongomock import MongoClient

from rosapenna_backend.api.schemas.booking_operations import (
    ExecuteBookingOperationRequest,
    ExecuteBookingOperationResponse,
)
from rosapenna_backend.api.schemas.packages import (
    ItemizedGolfRoundQuote,
    ItemizedRoomQuote,
    PackagePaymentLinkRequest,
    PackageQuoteResponse,
    PackageRulesConfig,
)
from rosapenna_backend.config import Settings
from rosapenna_backend.api.routes.packages import (
    get_package_payment, handle_payment_callback, handle_prommt_webhook, reconcile_prommt_test_payment,
)
from fastapi import HTTPException, Request
from rosapenna_backend.integrations.brs.booking_client import BrsBookingClient
from rosapenna_backend.integrations.opera.client import OperaClient
from rosapenna_backend.integrations.payments.simulation import PaymentSimulator
from rosapenna_backend.integrations.prommt.client import PrommtClient, PrommtSettings
from rosapenna_backend.integrations.prommt.schemas import (
    PrommtPaymentDetailsResponse,
    PrommtPaymentRequestResponse,
    PrommtWebhookPayload,
)
from rosapenna_backend.services.booking_intents import BookingIntentStore
from rosapenna_backend.services.booking_linkage import BookingLinkageService
from rosapenna_backend.services.booking_operations import BookingOperationEngine
from rosapenna_backend.services.email_delivery import EmailDeliveryService
from rosapenna_backend.services.mmb_occupants import MmbOccupantsService
from rosapenna_backend.services.package_pricing import PackagePricingService, compute_quote_hash
from rosapenna_backend.services.tee_sheets import TeesheetCache


def make_mock_available_quote() -> PackageQuoteResponse:
    arrival = dt.date(2027, 5, 14)
    departure = dt.date(2027, 5, 17)
    return PackageQuoteResponse(
        quote_id="QTE-20270514-001",
        status="available",
        rules_version=PackageRulesConfig().version,
        arrival_date=arrival,
        departure_date=departure,
        nights=3,
        currency="EUR",
        itemized_rooms=[
            ItemizedRoomQuote(
                room_category="CLASSIC",
                room_quantity=1,
                nights=3,
                rate_per_night="220.00",
                subtotal="660.00",
                rate_plan_code="BB_STANDARD",
                breakfast_included=True,
                available_rooms=5,
            )
        ],
        itemized_golf=[
            ItemizedGolfRoundQuote(
                course_id=3,
                course_name="Sandy Hills Links",
                date=dt.date(2027, 5, 14),
                time="14:00",
                players=2,
                rate_per_player="90.00",
                subtotal="180.00",
                rate_basis="Standard Visitor Rate",
                available_slots=4,
            ),
            ItemizedGolfRoundQuote(
                course_id=4,
                course_name="St Patrick's Links",
                date=dt.date(2027, 5, 15),
                time="10:00",
                players=2,
                rate_per_player="150.00",
                subtotal="300.00",
                rate_basis="Standard Visitor Rate",
                available_slots=4,
            ),
        ],
        total_rooms_price="660.00",
        total_golf_price="480.00",
        total_package_price="1140.00",
        deposit_amount="285.00",
        balance_amount="855.00",
        balance_due_date=dt.date(2027, 4, 2),
        terms_version="terms-v1-20261014",
        deposit_percent=25,
        balance_percent=75,
        rate_basis_summary="Standard Visitor Rate",
        is_golf_ireland_eligible=False,
        reserves_inventory=False,
        freshness_ttl_seconds=3600,
        expires_at=(dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=1)).isoformat(),
        created_at=dt.datetime.now(dt.timezone.utc).isoformat(),
        errors=[],
    )


class TestN06CommitJourney(unittest.TestCase):
    def setUp(self):
        self.mongo_client = MongoClient()
        self.db = self.mongo_client["rosapenna_test_n06_commit"]

        self.settings = Settings(
            environment="test",
            payment_mode="simulation",
            simulation_enabled=True,
            simulation_signing_secret="test-sim-secret-key-12345",
            simulation_run_id="run-n06-commit-test-01",
        )

        self.opera_client = OperaClient()
        self.brs_client = BrsBookingClient()
        self.teesheet_cache = MagicMock()
        # Mock teesheet cache to return available slots
        self.teesheet_cache.get.return_value = {
            "status": "success",
            "data": {
                "tee_times": [
                    {
                        "time": "14:00",
                        "slots": {str(i): {"status": "Available"} for i in range(4)},
                    },
                    {
                        "time": "10:00",
                        "slots": {str(i): {"status": "Available"} for i in range(4)},
                    },
                ]
            },
        }

        self.store = BookingIntentStore(self.db["booking_intents"])
        self.linkage = BookingLinkageService(database=self.db, opera_client=self.opera_client)
        self.email_svc = EmailDeliveryService()
        self.simulator = PaymentSimulator(database=self.db, default_secret=self.settings.simulation_signing_secret)

        self.pricing_service = PackagePricingService(
            opera_client=self.opera_client,
            teesheet_cache=self.teesheet_cache,
            booking_store=self.store,
            database=self.db,
            linkage_service=self.linkage,
            payment_simulator=self.simulator,
            settings=self.settings,
        )

        self.engine = BookingOperationEngine(
            database=self.db,
            pricing_service=self.pricing_service,
            linkage_service=self.linkage,
            opera_client=self.opera_client,
            brs_client=self.brs_client,
            email_service=self.email_svc,
        )

    def test_deposit_payment_link_generation_in_simulation(self):
        """Creates scoped 25% deposit simulation link bound to quote and guest."""
        quote = make_mock_available_quote()
        self.pricing_service._cache_and_return_quote(quote)

        req = PackagePaymentLinkRequest(
            quote_id=quote.quote_id,
            lead_first_name="Jane",
            lead_last_name="Golfer",
            lead_email="jane@example.com",
            lead_phone="+353871234567",
            residence_country="IE",
            accepted_terms=True,
            special_requests="Ground floor room if available",
        )

        resp = self.pricing_service.create_payment_link(req)
        self.assertEqual(resp.amount, "285.00")
        self.assertEqual(resp.status, "unpaid")
        self.assertEqual(resp.evidence_source, "simulation")
        self.assertTrue(resp.is_test)
        self.assertIn("/booking/payment?payment_id=", resp.payment_url)

    def test_payment_simulation_requires_consistent_safe_settings(self):
        from rosapenna_backend.config import validate_security_settings

        quote = make_mock_available_quote()
        self.pricing_service._cache_and_return_quote(quote)
        request = PackagePaymentLinkRequest(
            quote_id=quote.quote_id, lead_first_name="Jane", lead_last_name="Golfer",
            lead_email="jane@example.com", lead_phone="+353871234567",
            residence_country="IE", accepted_terms=True,
        )
        for changes in (
            {"payment_mode": "disabled", "simulation_enabled": True},
            {"payment_mode": "simulation", "simulation_enabled": False},
            {"payment_mode": "unknown", "simulation_enabled": False},
            {"environment": "production"},
            {"environment": "staging"},
        ):
            with self.subTest(changes=changes):
                unsafe = self.settings.model_copy(update=changes)
                self.pricing_service.settings = unsafe
                with patch("rosapenna_backend.config.settings", unsafe):
                    with self.assertRaises(RuntimeError):
                        validate_security_settings()
                with patch.object(self.simulator, "create_or_get_payment_request") as create:
                    with self.assertRaises(ValueError):
                        self.pricing_service.create_payment_link(request)
                    create.assert_not_called()

    def test_simulated_payment_cannot_authorize_live_booking_adapters(self):
        """A paid test record cannot reach availability or vendor writes in unsafe settings."""
        for index, (target, field, value) in enumerate((
            (self.opera_client.settings, "allow_live_writes", True),
            (self.brs_client.settings, "allow_live_writes", True),
            (self.settings, "environment", "production"),
            (self.settings, "simulation_run_id", "another-test-run"),
        )):
            with self.subTest(field=field, value=value):
                # One open deposit per quote is enforced, so each pass uses its own quote.
                quote = make_mock_available_quote()
                quote.quote_id = f"{quote.quote_id}-{index}"
                self.pricing_service._cache_and_return_quote(quote)
                payment = self.pricing_service.create_payment_link(
                    PackagePaymentLinkRequest(
                        quote_id=quote.quote_id,
                        lead_first_name="Jane",
                        lead_last_name="Golfer",
                        lead_email="jane@example.invalid",
                        lead_phone="+353871234567",
                        accepted_terms=True,
                    )
                )
                _, signature, raw = self.simulator.generate_simulated_event(
                    payment_id=payment.payment_id,
                    scenario="success",
                    secret=self.settings.simulation_signing_secret,
                    simulation_run_id=self.settings.simulation_run_id,
                )
                self.simulator.process_simulation_event(
                    raw, signature, self.settings.simulation_signing_secret,
                    allowed_run_id=self.settings.simulation_run_id,
                )
                with patch.object(target, field, value), patch.object(
                    self.engine, "_revalidate_quote_inventory_and_pricing"
                ) as revalidate:
                    result = self.engine.execute_operation(ExecuteBookingOperationRequest(
                        operation_type="new_booking",
                        booking_reference=payment.booking_reference,
                        quote_id=quote.quote_id,
                        payment_id=payment.payment_id,
                        idempotency_key=f"unsafe_{field}_{value}",
                    ))
                self.assertIn(result.status, {"pending_payment", "unconfirmed_reconciliation"})
                if result.status == "unconfirmed_reconciliation":
                    self.assertTrue(result.staff_review_required)
                revalidate.assert_not_called()
                self.assertEqual(len(self.opera_client._sandbox_reservations), 0)
                self.assertEqual(len(self.brs_client._sandbox_rounds), 0)

    def test_simulation_mode_rejects_unmarked_payment_evidence(self):
        """Simulation mode must not trust a paid record without test provenance."""
        payment = {
            "payment_id": "pay_unmarked",
            "status": "verified_paid",
            "booking_reference": "RSP-TEST-UNMARKED",
        }
        with patch.object(self.pricing_service, "get_payment", return_value=payment), patch.object(
            self.engine, "_revalidate_quote_inventory_and_pricing"
        ) as revalidate:
            result = self.engine.execute_operation(ExecuteBookingOperationRequest(
                operation_type="new_booking",
                booking_reference=payment["booking_reference"],
                payment_id=payment["payment_id"],
                idempotency_key="unmarked_payment_test",
            ))
        self.assertEqual(result.status, "unconfirmed_reconciliation")
        revalidate.assert_not_called()
        self.assertEqual(len(self.opera_client._sandbox_reservations), 0)
        self.assertEqual(len(self.brs_client._sandbox_rounds), 0)

    def test_test_event_cannot_promote_non_test_payment_with_same_id(self):
        quote = make_mock_available_quote()
        self.pricing_service._cache_and_return_quote(quote)
        payment = self.pricing_service.create_payment_link(PackagePaymentLinkRequest(
            quote_id=quote.quote_id, lead_first_name="Jane", lead_last_name="Golfer",
            lead_email="jane@example.invalid", lead_phone="+353871234567", accepted_terms=True,
        ))
        _, signature, raw = self.simulator.generate_simulated_event(
            payment.payment_id, "success", self.settings.simulation_signing_secret,
            simulation_run_id=self.settings.simulation_run_id,
        )
        self.simulator.process_simulation_event(raw, signature, self.settings.simulation_signing_secret,
                                                allowed_run_id=self.settings.simulation_run_id)
        unrelated = {"evidence_source": "provider", "is_test": False, "status": "unpaid"}
        self.pricing_service._payments_cache[payment.payment_id].update(unrelated)
        self.db["package_payments"].update_one({"payment_id": payment.payment_id}, {"$set": unrelated})

        result = self.pricing_service.get_payment(payment.payment_id)
        self.assertEqual(result["status"], "unpaid")
        self.assertEqual(result["evidence_source"], "provider")
        stored = self.db["package_payments"].find_one({"payment_id": payment.payment_id})
        self.assertEqual(stored["status"], "unpaid")
        self.assertEqual(stored["evidence_source"], "provider")

    def test_legacy_payment_callback_fails_closed(self):
        """The old callback cannot verify payments with a built-in signing key."""
        request = MagicMock()
        with self.assertRaisesRegex(ValueError, "disabled"):
            self.pricing_service.process_payment_callback(request)
        service = MagicMock()
        with self.assertRaises(HTTPException) as raised:
            handle_payment_callback(request, service)
        self.assertEqual(raised.exception.status_code, 503)
        service.process_payment_callback.assert_not_called()

    def test_unverified_provider_webhook_fails_closed(self):
        """The webhook stays off outside the isolated Prommt TEST profile."""
        service = MagicMock()
        with self.assertRaises(HTTPException) as raised:
            asyncio.run(handle_prommt_webhook(MagicMock(), service))
        self.assertEqual(raised.exception.status_code, 503)
        service.apply_prommt_test_payment.assert_not_called()

    def test_prommt_test_webhook_verifies_payment_without_simulating_booking(self):
        self.settings.payment_mode = "prommt_test"
        self.settings.simulation_enabled = False
        secret = secrets.token_urlsafe(32)
        client = PrommtClient(PrommtSettings(
            PROMMT_MODE="TEST", PROMMT_WEBHOOK_SECRET=secret, PROMMT_ALLOW_LIVE_CALLS=False,
        ))
        self.pricing_service.prommt_client = client
        quote = make_mock_available_quote()
        quote.guest_count = 2
        self.pricing_service._cache_and_return_quote(quote)
        resource_id = str(uuid4())

        with patch.object(client, "create_test_checkout_link", return_value=PrommtPaymentRequestResponse(
            resourceId=resource_id, url="https://pay.prommt.com/link/test-fixture",
        )):
            link = self.pricing_service.create_payment_link(PackagePaymentLinkRequest(
                quote_id=quote.quote_id, lead_first_name="Test", lead_last_name="Guest",
                lead_email="guest@example.invalid", lead_phone="+442079460000",
                additional_guests=[{"first_name": "Test", "last_name": "Companion"}],
                accepted_terms=True,
            ))

        event = {
            "eventId": str(uuid4()), "resourceId": resource_id, "type": "PAYMENT_REQUEST",
            "event": "PAID_SUCCESS", "mode": "TEST", "externalId": link.booking_reference,
            "timestamp": int(dt.datetime.now(dt.timezone.utc).timestamp() * 1000),
        }
        details = PrommtPaymentDetailsResponse(
            status="PAID_SUCCESS", amount=Decimal(quote.deposit_amount), currency="EUR",
            externalId=link.booking_reference, transactionId="test-transaction",
        )

        def deliver(payload, valid_signature=True):
            body = json.dumps(payload).encode()
            signature = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()

            async def receive():
                return {"type": "http.request", "body": body, "more_body": False}

            request = Request({"type": "http", "headers": [
                (b"x-signature", (signature if valid_signature else "invalid").encode())
            ]}, receive)
            return asyncio.run(handle_prommt_webhook(request, self.pricing_service))

        route = "rosapenna_backend.api.routes.packages.get_booking_operation_engine"
        with patch(route, return_value=self.engine), patch.object(
            client, "get_test_payment_details", return_value=details,
        ) as lookup:
            for payload, signature_ok, status in (
                (event, False, 401),
                (event | {"mode": "LIVE"}, True, 400),
                ({k: v for k, v in event.items() if k != "mode"}, True, 400),
                (event | {"timestamp": 1000}, True, 400),
                (event | {"externalId": "wrong-booking"}, True, 409),
            ):
                with self.subTest(status=status), self.assertRaises(HTTPException) as exc:
                    deliver(payload, signature_ok)
                self.assertEqual(exc.exception.status_code, status)
            lookup.assert_not_called()
            self.assertEqual(deliver(event | {"event": "PAID_FAILED"}), {"status": "ignored"})
            self.assertEqual(len(self.brs_client._sandbox_rounds), 0)
            lookup.return_value = details.model_copy(update={"amount": Decimal("0.01")})
            with self.assertRaises(HTTPException) as exc:
                deliver(event)
            self.assertEqual(exc.exception.status_code, 409)
            self.assertEqual(self.db.package_payments.find_one({"payment_id": link.payment_id})["status"], "unpaid")

            lookup.return_value = details
            # Simulate interruption after the payment is saved, before booking starts.
            with patch.object(self.engine, "execute_operation", side_effect=RuntimeError("booking interrupted")):
                self.assertEqual(deliver(event).status_code, 202)
            saved = self.db.package_payments.find_one({"payment_id": link.payment_id})
            self.assertEqual((saved["status"], saved["booking_work"]), ("verified_paid", "pending"))
            self.assertEqual(saved["prommt_event_id"], event["eventId"])
            self.assertEqual(self.db.booking_operations.count_documents({}), 0)

            self.assertEqual(deliver(event)["booking_status"], "unconfirmed_reconciliation")
            self.assertEqual(deliver(event)["booking_status"], "unconfirmed_reconciliation")
            self.assertEqual(lookup.call_count, 2)
            operation_query = {"booking_reference": link.booking_reference}
            self.assertEqual(self.db.booking_operations.count_documents(operation_query), 1)
            operation = self.db.booking_operations.find_one(operation_query)
            self.assertTrue(operation["payment_verified"])
            self.assertEqual(operation["staff_alert"]["failed_stage"], "vendor_test_booking_unavailable")
            self.assertFalse(self.opera_client._sandbox_reservations)
            self.assertFalse(self.brs_client._sandbox_rounds)

            def checkout_status():
                return get_package_payment(link.payment_id, self.pricing_service, self.engine)

            self.assertEqual(checkout_status(), {"status": "verified_paid", "booking_status": "needs_review"})
            self.db.booking_operations.update_one(
                {"booking_reference": link.booking_reference},
                {"$set": {"payment_id": "different-payment"}},
            )
            self.assertEqual(checkout_status()["booking_status"], "pending_confirmation")

    def test_prommt_test_recovery_verifies_exact_payment_and_reuses_booking_operation(self):
        self.settings.payment_mode = "prommt_test"
        self.settings.security_bypass_in_dev = False
        payment = {
            "payment_id": "pay_existing_test", "booking_reference": "RSP-TEST-RECOVERY",
            "quote_id": "quote-existing", "prommt_request_id": "resource-existing",
            "status": "unpaid", "evidence_source": "prommt", "is_test": True,
            "purpose": "deposit", "amount": "50.00", "currency": "EUR",
        }
        self.db.package_payments.insert_one(payment.copy())
        self.db.package_payments.insert_one({**payment, "payment_id": "pay_non_test", "is_test": False})
        client = self.pricing_service.prommt_client = PrommtClient(PrommtSettings(PROMMT_MODE="TEST"))
        details = PrommtPaymentDetailsResponse(
            status="PAID_SUCCESS", amount=Decimal("50.00"), currency="EUR",
            externalId=payment["booking_reference"], transactionId="test-transaction",
        )
        with patch("rosapenna_backend.api.routes.packages.get_booking_operation_engine") as engine, patch.object(
            client, "get_test_payment_details", return_value=details.model_copy(update={"status": "CREATED"}),
        ) as lookup:
            def recover(payment_id):
                return reconcile_prommt_test_payment(payment_id, MagicMock(), self.pricing_service)

            self.settings.security_bypass_in_dev = True
            with self.assertRaises(HTTPException) as disabled:
                recover(payment["payment_id"])
            self.assertEqual(disabled.exception.status_code, 503)
            self.settings.security_bypass_in_dev = False
            with self.assertRaises(HTTPException) as missing:
                recover("pay_non_test")
            self.assertEqual(missing.exception.status_code, 404)
            lookup.assert_not_called()
            with self.assertRaises(HTTPException) as unpaid:
                recover(payment["payment_id"])
            self.assertEqual(unpaid.exception.status_code, 409)
            engine.assert_not_called()
            self.assertEqual(self.db.package_payments.find_one({"payment_id": payment["payment_id"]})["status"], "unpaid")

            lookup.return_value = details
            engine.return_value.execute_operation.return_value.status = "unconfirmed_reconciliation"
            for _ in range(2):
                self.assertEqual(recover(payment["payment_id"])["booking_status"], "unconfirmed_reconciliation")
            self.assertEqual(lookup.call_count, 2)
            calls = engine.return_value.execute_operation.call_args_list
            self.assertEqual(calls[0], calls[1])
            self.assertEqual(calls[0].args[0].idempotency_key, "prommt_test_pay_existing_test")

        saved = self.db.package_payments.find_one({"payment_id": payment["payment_id"]})
        self.assertEqual(saved["transaction_id"], details.transaction_id)
        self.assertNotIn("prommt_event_id", saved)
        event = PrommtWebhookPayload(
            eventId="event-later", resourceId=payment["prommt_request_id"], type="PAYMENT_REQUEST",
            mode="TEST", event="PAID_SUCCESS", timestamp=0,
        )
        self.pricing_service.apply_prommt_test_payment(event)
        self.assertEqual(self.db.package_payments.find_one({"payment_id": payment["payment_id"]})["prommt_event_id"], "event-later")

    def test_prommt_test_rejects_ambiguous_or_changed_deposit_evidence(self):
        self.settings.payment_mode = "prommt_test"
        payments = self.db.package_payments
        deposit = {
            "payment_id": "pay_test_changed", "booking_reference": "RSP-TEST-CHANGED",
            "quote_id": "quote-original", "prommt_request_id": "resource-test",
            "status": "unpaid", "evidence_source": "prommt", "is_test": True,
            "purpose": "deposit", "amount": "50.00", "currency": "EUR",
        }
        original_id = payments.insert_one(deposit.copy()).inserted_id
        duplicate_id = payments.insert_one({**deposit, "payment_id": "pay_test_duplicate"}).inserted_id
        event = PrommtWebhookPayload(
            eventId="event-test", resourceId=deposit["prommt_request_id"], type="PAYMENT_REQUEST",
            mode="TEST", event="PAID_SUCCESS", timestamp=0,
        )
        details = PrommtPaymentDetailsResponse(
            status="PAID_SUCCESS", amount=Decimal("50.00"), currency="EUR",
            externalId=deposit["booking_reference"], transactionId="test-transaction",
        )

        def return_details_after_quote_changes(_resource_id):
            payments.update_one({"_id": original_id}, {"$set": {"quote_id": "quote-replaced"}})
            return details

        with patch.object(self.pricing_service.prommt_client, "get_test_payment_details") as lookup:
            with self.assertRaisesRegex(ValueError, "deposit identity is ambiguous"):
                self.pricing_service.apply_prommt_test_payment(event)
            lookup.assert_not_called()
            payments.delete_one({"_id": duplicate_id})
            lookup.side_effect = return_details_after_quote_changes
            with self.assertRaisesRegex(ValueError, "payment state changed"):
                self.pricing_service.apply_prommt_test_payment(event)
            self.assertEqual(payments.find_one({"_id": original_id})["status"], "unpaid")

            payments.update_one({"_id": original_id}, {"$set": {"status": "verified_paid"}})
            with self.assertRaisesRegex(ValueError, "missing verified provider evidence"):
                self.pricing_service.reconcile_prommt_test_payment(deposit["payment_id"])
            self.assertNotIn("prommt_event_id", payments.find_one({"_id": original_id}))
            self.assertEqual(lookup.call_count, 1)

    def test_paid_deposit_must_match_exact_quote_before_booking(self):
        quote = make_mock_available_quote()
        self.pricing_service._cache_and_return_quote(quote)
        payment = self.pricing_service.create_payment_link(PackagePaymentLinkRequest(
            quote_id=quote.quote_id, lead_first_name="Jane", lead_last_name="Golfer",
            lead_email="jane@example.invalid", lead_phone="+353871234567", accepted_terms=True,
        ))
        _, signature, raw = self.simulator.generate_simulated_event(
            payment.payment_id, "success", self.settings.simulation_signing_secret,
            simulation_run_id=self.settings.simulation_run_id,
        )
        self.simulator.process_simulation_event(raw, signature, self.settings.simulation_signing_secret,
                                                allowed_run_id=self.settings.simulation_run_id)
        correct = self.pricing_service.get_payment(payment.payment_id)
        for field, value in (
            ("quote_id", "another-quote"), ("quote_hash", "wrong-hash"),
            ("amount", "1.00"), ("currency", "GBP"),
            ("purpose", "balance"), ("booking_reference", "RSP-ANOTHER"),
        ):
            with self.subTest(field=field):
                changed = dict(correct, **{field: value})
                engine = BookingOperationEngine(
                    pricing_service=self.pricing_service, linkage_service=self.linkage,
                    opera_client=self.opera_client, brs_client=self.brs_client, email_service=self.email_svc,
                )
                with patch.object(self.pricing_service, "get_payment", return_value=changed), patch.object(
                    engine, "_revalidate_quote_inventory_and_pricing"
                ) as revalidate:
                    result = engine.execute_operation(ExecuteBookingOperationRequest(
                        operation_type="new_booking", booking_reference=payment.booking_reference,
                        quote_id=quote.quote_id, payment_id=payment.payment_id,
                        idempotency_key=f"mismatch_{field}",
                    ))
                self.assertEqual(result.status, "unconfirmed_reconciliation")
                self.assertIn("Deposit does not match", result.message)
                revalidate.assert_not_called()
                self.assertEqual(len(self.opera_client._sandbox_reservations), 0)
                self.assertEqual(len(self.brs_client._sandbox_rounds), 0)

    def test_changed_rules_block_booking_before_inventory_writes(self):
        quote = make_mock_available_quote()
        self.pricing_service._cache_and_return_quote(quote)
        payment = self.pricing_service.create_payment_link(PackagePaymentLinkRequest(
            quote_id=quote.quote_id, lead_first_name="Jane", lead_last_name="Golfer",
            lead_email="jane@example.invalid", lead_phone="+353871234567", accepted_terms=True,
        ))
        _, signature, raw = self.simulator.generate_simulated_event(
            payment.payment_id, "success", self.settings.simulation_signing_secret,
            simulation_run_id=self.settings.simulation_run_id,
        )
        self.simulator.process_simulation_event(raw, signature, self.settings.simulation_signing_secret,
                                                allowed_run_id=self.settings.simulation_run_id)
        current_rules = self.pricing_service.get_rules()
        self.pricing_service._cached_rules = current_rules.model_copy(update={"version": "changed-version"})
        result = self.engine.execute_operation(ExecuteBookingOperationRequest(
            operation_type="new_booking", booking_reference=payment.booking_reference,
            quote_id=quote.quote_id, payment_id=payment.payment_id,
            idempotency_key="rules_drift_test",
        ))
        self.assertEqual(result.status, "unconfirmed_reconciliation")
        self.assertIn("rules changed", result.message)
        self.assertEqual(len(self.opera_client._sandbox_reservations), 0)
        self.assertEqual(len(self.brs_client._sandbox_rounds), 0)

    def test_commit_blocked_prior_to_payment(self):
        """Engine rejects inventory reservation when payment is missing or unpaid."""
        quote = make_mock_available_quote()
        self.pricing_service._cache_and_return_quote(quote)

        link_resp = self.pricing_service.create_payment_link(
            PackagePaymentLinkRequest(
                quote_id=quote.quote_id,
                lead_first_name="Jane",
                lead_last_name="Golfer",
                lead_email="jane@example.com",
                lead_phone="+353871234567",
                accepted_terms=True,
            )
        )

        op_req = ExecuteBookingOperationRequest(
            operation_type="new_booking",
            booking_reference=link_resp.booking_reference,
            quote_id=quote.quote_id,
            payment_id=link_resp.payment_id,
            idempotency_key="commit_attempt_1",
        )
        res = self.engine.execute_operation(op_req)
        self.assertEqual(res.status, "pending_payment")
        self.assertFalse(res.readback_verified)
        self.assertEqual(len(self.opera_client._sandbox_reservations), 0)
        self.assertEqual(len(self.brs_client._sandbox_rounds), 0)

    def test_successful_commit_after_simulated_deposit(self):
        """End-to-end verified deposit, hotel write, golf write, linkage, and idempotency."""
        quote = make_mock_available_quote()
        quote.guest_count = 2
        self.pricing_service._cache_and_return_quote(quote)

        link_resp = self.pricing_service.create_payment_link(
            PackagePaymentLinkRequest(
                quote_id=quote.quote_id,
                lead_first_name="Jane",
                lead_last_name="Golfer",
                lead_email="jane@example.com",
                lead_phone="+353871234567",
                additional_guests=[{"first_name": "Test", "last_name": "Companion"}],
                accepted_terms=True,
            )
        )

        # Simulate verified payment
        envelope, sig, raw_bytes = self.simulator.generate_simulated_event(
            payment_id=link_resp.payment_id,
            scenario="success",
            secret=self.settings.simulation_signing_secret,
            simulation_run_id=self.settings.simulation_run_id,
        )
        self.simulator.process_simulation_event(
            raw_bytes=raw_bytes,
            signature=sig,
            secret=self.settings.simulation_signing_secret,
            allowed_run_id=self.settings.simulation_run_id,
        )

        # Trigger safe commit
        op_req = ExecuteBookingOperationRequest(
            operation_type="new_booking",
            booking_reference=link_resp.booking_reference,
            quote_id=quote.quote_id,
            payment_id=link_resp.payment_id,
            idempotency_key="commit_key_001",
        )
        res = self.engine.execute_operation(op_req)

        self.assertEqual(res.status, "confirmed")
        self.assertEqual(res.stage, "completed")
        self.assertTrue(res.readback_verified)
        self.assertFalse(res.staff_review_required)

        # Verify Opera reservation created and stored
        self.assertEqual(len(self.opera_client._sandbox_reservations), 1)
        res_id = list(self.opera_client._sandbox_reservations.keys())[0]
        opera_res = self.opera_client.get_reservation(res_id)
        self.assertEqual(opera_res.room_category, "CLASSIC")
        self.assertEqual(opera_res.arrival_date, quote.arrival_date)

        # Verify BRS golf rounds created and stored
        self.assertEqual(len(self.brs_client._sandbox_rounds), 2)

        # Verify authoritative linkage
        linkage_doc = self.linkage.get_linkage(link_resp.booking_reference)
        self.assertIsNotNone(linkage_doc.opera_stay)
        roster = MmbOccupantsService(linkage_service=self.linkage).get_roster(link_resp.booking_reference)
        self.assertEqual([name.last_name for name in roster.unassigned_guests], ["Companion"])
        self.assertEqual([guest.full_name for guest in roster.occupants], ["Jane Golfer"])
        self.assertEqual(len(linkage_doc.brs_rounds), 2)
        self.assertEqual(len(linkage_doc.payments), 1)
        self.assertEqual(linkage_doc.payments[0].amount, "285.00")

        # Verify idempotency replay returns same confirmed result without duplicate writes
        res_replay = self.engine.execute_operation(op_req)
        self.assertEqual(res_replay.status, "confirmed")
        self.assertTrue(res_replay.replayed)
        self.assertEqual(len(self.opera_client._sandbox_reservations), 1)
        self.assertEqual(len(self.brs_client._sandbox_rounds), 2)

    def test_unsupported_multi_room_topology_fails_before_payment(self):
        """A room arrangement the engine cannot write must not receive a payment link."""
        quote = make_mock_available_quote()
        quote.itemized_rooms[0].room_quantity = 2
        self.pricing_service._cache_and_return_quote(quote)

        with self.assertRaisesRegex(ValueError, "Contact Reservations"):
            self.pricing_service.create_payment_link(
                PackagePaymentLinkRequest(
                    quote_id=quote.quote_id,
                    lead_first_name="Alice",
                    lead_last_name="Golfer",
                    lead_email="alice@example.com",
                    lead_phone="+353871234567",
                    accepted_terms=True,
                )
            )
        self.assertFalse(self.pricing_service._payments_cache)
        self.assertEqual(len(self.opera_client._sandbox_reservations), 0)
        self.assertEqual(len(self.brs_client._sandbox_rounds), 0)

    def test_missing_lead_guest_fails_before_writes(self):
        """Missing or blank lead guest details safely halts before any vendor write."""
        quote = make_mock_available_quote()
        self.pricing_service._cache_and_return_quote(quote)

        link_resp = self.pricing_service.create_payment_link(
            PackagePaymentLinkRequest(
                quote_id=quote.quote_id,
                lead_first_name="Bob",
                lead_last_name="Golfer",
                lead_email="bob@example.com",
                lead_phone="+353871234567",
                accepted_terms=True,
            )
        )
        envelope, sig, raw_bytes = self.simulator.generate_simulated_event(
            payment_id=link_resp.payment_id,
            scenario="success",
            secret=self.settings.simulation_signing_secret,
            simulation_run_id=self.settings.simulation_run_id,
        )
        self.simulator.process_simulation_event(
            raw_bytes=raw_bytes,
            signature=sig,
            secret=self.settings.simulation_signing_secret,
            allowed_run_id=self.settings.simulation_run_id,
        )

        # Clear lead guest in payment doc
        pay_doc = self.pricing_service.get_payment(link_resp.payment_id)
        pay_doc["lead_guest"] = {"first_name": "", "last_name": "", "email": "", "phone": ""}

        res = self.engine.execute_operation(
            ExecuteBookingOperationRequest(
                operation_type="new_booking",
                booking_reference=link_resp.booking_reference,
                quote_id=quote.quote_id,
                payment_id=link_resp.payment_id,
                idempotency_key="guest_val_key_001",
            )
        )
        self.assertEqual(res.status, "unconfirmed_reconciliation")
        self.assertTrue(res.staff_review_required)
        self.assertIn("lead guest", res.message.lower())
        self.assertEqual(len(self.opera_client._sandbox_reservations), 0)

    def test_hotel_subtotal_isolation_in_linkage(self):
        """Hotel stay records hotel rooms price only, not full package price with golf."""
        quote = make_mock_available_quote()
        self.pricing_service._cache_and_return_quote(quote)

        link_resp = self.pricing_service.create_payment_link(
            PackagePaymentLinkRequest(
                quote_id=quote.quote_id,
                lead_first_name="Carol",
                lead_last_name="Danvers",
                lead_email="carol@example.com",
                lead_phone="+353871234567",
                accepted_terms=True,
            )
        )
        envelope, sig, raw_bytes = self.simulator.generate_simulated_event(
            payment_id=link_resp.payment_id,
            scenario="success",
            secret=self.settings.simulation_signing_secret,
            simulation_run_id=self.settings.simulation_run_id,
        )
        self.simulator.process_simulation_event(
            raw_bytes=raw_bytes,
            signature=sig,
            secret=self.settings.simulation_signing_secret,
            allowed_run_id=self.settings.simulation_run_id,
        )

        res = self.engine.execute_operation(
            ExecuteBookingOperationRequest(
                operation_type="new_booking",
                booking_reference=link_resp.booking_reference,
                quote_id=quote.quote_id,
                payment_id=link_resp.payment_id,
                idempotency_key="subtotal_key_001",
            )
        )
        self.assertEqual(res.status, "confirmed")
        linkage = self.linkage.get_linkage(link_resp.booking_reference)
        # Total rooms price is 660.00, package price is 1140.00
        self.assertEqual(linkage.opera_stay.total_amount, "660.00")

    def test_duplicate_protection_new_caller_key(self):
        """New caller idempotency key for same booking reference reuses canonical operation."""
        quote = make_mock_available_quote()
        self.pricing_service._cache_and_return_quote(quote)

        link_resp = self.pricing_service.create_payment_link(
            PackagePaymentLinkRequest(
                quote_id=quote.quote_id,
                lead_first_name="Dave",
                lead_last_name="Bowman",
                lead_email="dave@example.com",
                lead_phone="+353871234567",
                accepted_terms=True,
            )
        )
        envelope, sig, raw_bytes = self.simulator.generate_simulated_event(
            payment_id=link_resp.payment_id,
            scenario="success",
            secret=self.settings.simulation_signing_secret,
            simulation_run_id=self.settings.simulation_run_id,
        )
        self.simulator.process_simulation_event(
            raw_bytes=raw_bytes,
            signature=sig,
            secret=self.settings.simulation_signing_secret,
            allowed_run_id=self.settings.simulation_run_id,
        )

        res1 = self.engine.execute_operation(
            ExecuteBookingOperationRequest(
                operation_type="new_booking",
                booking_reference=link_resp.booking_reference,
                quote_id=quote.quote_id,
                payment_id=link_resp.payment_id,
                idempotency_key="caller_key_A",
            )
        )
        self.assertEqual(res1.status, "confirmed")
        # Second call with DIFFERENT idempotency key for the same booking reference
        res2 = self.engine.execute_operation(
            ExecuteBookingOperationRequest(
                operation_type="new_booking",
                booking_reference=link_resp.booking_reference,
                quote_id=quote.quote_id,
                payment_id=link_resp.payment_id,
                idempotency_key="caller_key_B",
            )
        )
        self.assertEqual(res2.status, "confirmed")
        self.assertEqual(res2.operation_id, res1.operation_id)
        # Total reservations in Opera is exactly 1, rounds is exactly 2
        self.assertEqual(len(self.opera_client._sandbox_reservations), 1)
        self.assertEqual(len(self.brs_client._sandbox_rounds), 2)


if __name__ == "__main__":
    unittest.main()
