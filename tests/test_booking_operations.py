"""Unit and fault-injection tests for Ticket F14 — Safe Booking Operation Engine.

Adheres strictly to the Live Prommt Payment Gateway and Test Bookings Protection Rules:
Zero live HTTP network calls to Prommt, Opera, or BRS; all tests run offline with verified sandboxes.
"""

from __future__ import annotations

import datetime as dt
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from threading import Event
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch
from mongomock import MongoClient
from pydantic import SecretStr

from rosapenna_backend.api.schemas.booking_linkage import (
    LeadGuestContact,
    OperaStayDetails,
    PaymentLedgerEntry,
    StaffLinkBookingRequest,
)
from rosapenna_backend.api.schemas.booking_operations import (
    ExecuteBookingOperationRequest,
    ExecuteBookingOperationResponse,
)
from rosapenna_backend.integrations.opera.schemas import (
    ReservationWriteResult,
    RoomAvailabilityResponse,
    RoomCategoryAvailability,
)
from rosapenna_backend.api.schemas.packages import (
    ItemizedGolfRoundQuote,
    ItemizedRoomQuote,
    PackageQuoteRequest,
    PackageQuoteResponse,
    PackageRulesConfig,
)
from rosapenna_backend.integrations.brs.booking_client import (
    BrsBookingClient,
    BrsReadbackMismatchError,
    BrsRoundWriteResult,
    BrsSettings,
    BrsWriteError,
)
from rosapenna_backend.integrations.opera.client import (
    OperaClient,
    OperaIntegrationError,
    OperaSettings,
)
from rosapenna_backend.services.booking_linkage import BookingLinkageService
from rosapenna_backend.services.booking_operations import BookingOperationEngine
from rosapenna_backend.services.email_delivery import EmailDeliveryService, SmtpSettings
from rosapenna_backend.services.package_pricing import PackagePricingService, compute_quote_hash


class FakeTeesheetCache:
    def __init__(self, times_data=None):
        self.times_data = times_data or {}

    def get(self, date_val, course_id, *, force_refresh=False):
        key = (str(date_val), int(course_id))
        return self.times_data.get(
            key,
            {
                "status": "cached",
                "data": {
                    "tee_times": [
                        {
                            "time": "10:00",
                            "slots": {"1": {"status": "Available"}, "2": {"status": "Available"}},
                        },
                        {
                            "time": "14:00",
                            "slots": {"1": {"status": "Available"}, "2": {"status": "Available"}},
                        },
                    ]
                },
            },
        )


class SafeBookingOperationEngineTests(unittest.TestCase):
    def setUp(self):
        # 0. Mock Database
        self.mongo = MongoClient(tz_aware=True)
        self.db = self.mongo["test_operations_db"]

        # 1. Mock Opera Client
        self.opera_settings = OperaSettings(
            hostname="",
            gateway_url="",
            client_id="",
            client_secret=SecretStr(""),
            app_key=SecretStr(""),
            token=SecretStr(""),
            allow_live_writes=False,
            enable_mock_fallback=True,
        )
        self.opera_client = OperaClient(settings=self.opera_settings)

        # 2. Mock BRS Client
        self.brs_settings = BrsSettings(allow_live_writes=False)
        self.brs_client = BrsBookingClient(settings=self.brs_settings)

        # 3. Email Delivery Service (Sandbox)
        self.email_settings = SmtpSettings(allow_live_send=False)
        self.email_service = EmailDeliveryService(settings=self.email_settings)

        # 4. Linkage Service (with mongomock)
        self.linkage_service = BookingLinkageService(
            database=self.db,
            opera_client=self.opera_client,
        )

        # 5. Pricing Service with mock quote and payment
        self.pricing_service = PackagePricingService(
            opera_client=self.opera_client,
            teesheet_cache=FakeTeesheetCache(),
            booking_store=MagicMock(),
            linkage_service=self.linkage_service,
        )

        # Build Engine
        self.engine = BookingOperationEngine(
            database=self.db,
            pricing_service=self.pricing_service,
            linkage_service=self.linkage_service,
            opera_client=self.opera_client,
            brs_client=self.brs_client,
            email_service=self.email_service,
        )

        # Seed valid quote
        self.sample_quote = PackageQuoteResponse(
            quote_id="quote_f14_test_001",
            status="available",
            rules_version=PackageRulesConfig().version,
            arrival_date=dt.date(2027, 7, 10),
            departure_date=dt.date(2027, 7, 12),
            nights=2,
            currency="EUR",
            itemized_rooms=[
                ItemizedRoomQuote(
                    room_category="CLASSIC",
                    room_quantity=1,
                    nights=2,
                    rate_per_night="250.00",
                    subtotal="500.00",
                    rate_plan_code="STAY_PLAY_2N",
                    breakfast_included=True,
                    available_rooms=5,
                )
            ],
            itemized_golf=[
                ItemizedGolfRoundQuote(
                    course_id=1,
                    course_name="Old Tom Morris Links",
                    date=dt.date(2027, 7, 10),
                    time="10:00",
                    players=2,
                    rate_per_player="75.00",
                    subtotal="150.00",
                    rate_basis="Standard Visitor",
                    available_slots=4,
                ),
                ItemizedGolfRoundQuote(
                    course_id=3,
                    course_name="Sandy Hills Links",
                    date=dt.date(2027, 7, 11),
                    time="14:00",
                    players=2,
                    rate_per_player="75.00",
                    subtotal="150.00",
                    rate_basis="Standard Visitor",
                    available_slots=4,
                ),
            ],
            total_rooms_price="500.00",
            total_golf_price="300.00",
            total_package_price="800.00",
            deposit_amount="200.00",
            balance_amount="600.00",
            balance_due_date=dt.date(2027, 5, 29),
            created_at=dt.datetime.now(dt.timezone.utc).isoformat(),
        )
        self.pricing_service._quotes_cache[self.sample_quote.quote_id] = self.sample_quote.model_dump(mode="json")

        # Seed valid payment doc
        now_str = dt.datetime.now(dt.timezone.utc).isoformat()
        future_str = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=7)).isoformat()
        self.sample_payment = {
            "payment_id": "pay_f14_test_001",
            "booking_reference": "RSP-20270710-F1401",
            "quote_id": self.sample_quote.quote_id,
            "quote_hash": compute_quote_hash(self.sample_quote),
            "purpose": "deposit",
            "amount": Decimal("200.00"),
            "currency": "EUR",
            "status": "verified_paid",
            "provider": "prommt",
            "provider_reference": "pmt_req_mock_999",
            "transaction_id": "txn_mock_999",
            "paid_at": now_str,
            "evidence_source": "prommt",
            "is_test": False,
            "expires_at": future_str,
            "created_at": now_str,
            "lead_guest": {
                "first_name": "Jack",
                "last_name": "Nicklaus",
                "email": "jack@example.com",
                "phone": "+353871234567",
            },
        }
        self.pricing_service._payments_cache[self.sample_payment["payment_id"]] = self.sample_payment

    def tearDown(self):
        self.mongo.close()

    def test_unpaid_payment_halts_without_reserving_inventory(self):
        """AC 2: Zero inventory is reserved prior to verified payment."""
        # Setup an unpaid payment doc
        future_str = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=7)).isoformat()
        self.pricing_service._payments_cache["pay_unpaid_001"] = {
            "payment_id": "pay_unpaid_001",
            "status": "unpaid",
            "booking_reference": "RSP-UNPAID-001",
            "amount": Decimal("200.00"),
            "expires_at": future_str,
            "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        }

        req = ExecuteBookingOperationRequest(
            idempotency_key="idemp_unpaid_test_123",
            operation_type="new_booking",
            payment_id="pay_unpaid_001",
            quote_id=self.sample_quote.quote_id,
        )

        resp = self.engine.execute_operation(req)

        self.assertEqual(resp.status, "pending_payment")
        self.assertFalse(resp.readback_verified)
        self.assertFalse(resp.staff_review_required)
        self.assertIn("remain unreserved", resp.message)

        # Confirm no Opera reservations created
        self.assertEqual(len(self.opera_client._sandbox_reservations), 0)
        # Confirm no BRS rounds created
        self.assertEqual(len(self.brs_client._sandbox_rounds), 0)

    def test_successful_new_booking_operation_flow(self):
        """AC 1, 2, 8: Complete flow verifies payment, revalidates, writes records, and records one confirmed event."""
        req = ExecuteBookingOperationRequest(
            idempotency_key="idemp_success_flow_001",
            operation_type="new_booking",
            payment_id=self.sample_payment["payment_id"],
            quote_id=self.sample_quote.quote_id,
        )

        resp = self.engine.execute_operation(req)

        self.assertEqual(resp.status, "confirmed")
        self.assertEqual(resp.stage, "completed")
        self.assertTrue(resp.readback_verified)
        self.assertFalse(resp.staff_review_required)
        self.assertIsNone(resp.email_delivery_id)

        # Verify operation record persistence
        op_record = self.engine.get_operation(resp.operation_id)
        self.assertIsNotNone(op_record)
        self.assertEqual(op_record.stage, "completed")
        self.assertTrue(op_record.payment_verified)
        self.assertIsNotNone(op_record.readback_evidence)
        self.assertTrue(op_record.readback_evidence.opera_verified)
        self.assertTrue(op_record.readback_evidence.brs_verified)
        self.assertEqual(len(op_record.readback_evidence.brs_round_ids), 2)

        # N06 records the handoff event only; N07 owns delivery.
        self.assertEqual(self.email_service._dispatched_emails, [])
        self.assertEqual(op_record.confirmed_event["event_id"], f"EVT-{resp.operation_id}")
        self.assertFalse(op_record.confirmed_event["is_test"])

    def test_commit_never_calls_mail_adapter(self):
        req = ExecuteBookingOperationRequest(
            idempotency_key="idemp_notification_exception_001",
            operation_type="new_booking",
            payment_id=self.sample_payment["payment_id"],
            quote_id=self.sample_quote.quote_id,
        )

        with patch.object(
            self.email_service, "send_booking_confirmation", side_effect=RuntimeError("mail unavailable"),
        ) as send:
            result = self.engine.execute_operation(req)
            replay = self.engine.execute_operation(req)

        send.assert_not_called()
        self.assertEqual(result.status, "confirmed")
        self.assertIsNone(result.email_delivery_id)
        self.assertEqual(replay.operation_id, result.operation_id)
        self.assertEqual(replay.status, "confirmed")
        saved = self.engine.get_operation(result.operation_id)
        self.assertEqual(saved.stage, "completed")
        self.assertIn("Delivery is handled separately", saved.stage_history[-1].details)

    def test_existing_linkage_for_different_round_does_not_confirm_booking(self):
        req = ExecuteBookingOperationRequest(
            idempotency_key="idemp_linkage_conflict_001",
            operation_type="new_booking",
            payment_id=self.sample_payment["payment_id"],
            quote_id=self.sample_quote.quote_id,
        )
        save_linkage = self.linkage_service.link_booking

        def return_conflicting_linkage(*args, **kwargs):
            saved = save_linkage(*args, **kwargs)
            saved.brs_rounds[0].brs_booking_id = "unrelated-test-booking"
            return saved

        with patch.object(self.linkage_service, "link_booking", side_effect=return_conflicting_linkage):
            result = self.engine.execute_operation(req)

        self.assertEqual(result.status, "unconfirmed_reconciliation")
        self.assertEqual(self.engine.get_operation(result.operation_id).staff_alert.failed_stage, "linkage")
        self.assertEqual(self.email_service._dispatched_emails, [])

    def test_verified_prommt_test_payment_waits_for_vendor_booking(self):
        self.pricing_service.settings = SimpleNamespace(environment="test", payment_mode="prommt_test")
        self.sample_payment.update(evidence_source="prommt", is_test=True)
        self.pricing_service._payments_cache[self.sample_payment["payment_id"]] = self.sample_payment
        reader = self.pricing_service.teesheet_cache
        with patch.object(reader, "get", wraps=reader.get) as availability:
            result = self.engine.execute_operation(ExecuteBookingOperationRequest(
                idempotency_key="prommt_test_vendor_booking",
                operation_type="new_booking",
                payment_id=self.sample_payment["payment_id"],
                quote_id=self.sample_quote.quote_id,
            ))

        self.assertEqual(result.status, "unconfirmed_reconciliation")
        availability.assert_not_called()
        operation = self.engine.get_operation(result.operation_id)
        self.assertTrue(operation.payment_verified)
        self.assertEqual(operation.staff_alert.failed_stage, "vendor_test_booking_unavailable")
        self.assertFalse(self.brs_client._sandbox_rounds)
        self.assertFalse(self.opera_client._sandbox_reservations)

    def test_loopback_prommt_test_supplies_saved_players_to_vendor_writes(self):
        self.pricing_service.settings = SimpleNamespace(
            environment="test", payment_mode="prommt_test", host="127.0.0.1",
        )
        self.sample_payment.update(
            evidence_source="prommt", is_test=True,
            additional_guests=[{"first_name": "Test", "last_name": "Player"}],
        )
        self.brs_settings.allow_live_writes = True
        self.opera_settings.allow_live_writes = True

        with (
            patch.object(OperaClient, "is_configured", True),
            patch.object(self.brs_client, "require_live_booking_writes"),
            patch.object(self.engine, "_revalidate_quote_inventory_and_pricing", return_value=(True, None)),
            patch.object(
                self.opera_client, "create_reservation",
                side_effect=self.opera_client._generate_fixture_write_result,
            ) as hotel,
            patch.object(
                self.brs_client, "create_round",
                side_effect=BrsWriteError("Test stopped at the real BRS boundary."),
            ) as golf,
        ):
            result = self.engine.execute_operation(ExecuteBookingOperationRequest(
                idempotency_key="prommt_loopback_vendor_test",
                operation_type="new_booking",
                payment_id=self.sample_payment["payment_id"],
                quote_id=self.sample_quote.quote_id,
            ))

        hotel.assert_called_once()
        golf.assert_called_once()
        self.assertEqual(golf.call_args.kwargs["staff_players"], ["Jack Nicklaus", "Test Player"])
        self.assertEqual(result.status, "unconfirmed_reconciliation")
        self.assertFalse(self.brs_client._sandbox_rounds)

    def test_prommt_test_payment_cannot_enter_live_brs_path(self):
        self.pricing_service.settings = SimpleNamespace(environment="test", payment_mode="prommt_test")
        self.sample_payment.update(evidence_source="prommt", is_test=True)
        self.pricing_service._payments_cache[self.sample_payment["payment_id"]] = self.sample_payment
        self.brs_settings.allow_live_writes = True
        with patch.object(self.brs_client, "create_round") as create:
            result = self.engine.execute_operation(ExecuteBookingOperationRequest(
                idempotency_key="prommt_test_live_brs_rejected",
                operation_type="new_booking",
                payment_id=self.sample_payment["payment_id"],
                quote_id=self.sample_quote.quote_id,
            ))
        self.assertEqual(result.status, "unconfirmed_reconciliation")
        create.assert_not_called()

    def test_concurrent_requests_with_different_keys_cannot_create_two_bookings(self):
        second_engine = BookingOperationEngine(
            database=self.db, pricing_service=self.pricing_service,
            linkage_service=self.linkage_service, opera_client=self.opera_client,
            brs_client=self.brs_client, email_service=self.email_service,
        )
        request = ExecuteBookingOperationRequest(
            idempotency_key="concurrent_request_A", payment_id=self.sample_payment["payment_id"],
            quote_id=self.sample_quote.quote_id,
        )
        entered = Event()
        release = Event()
        create = self.opera_client.create_reservation

        def delayed_create(*args, **kwargs):
            entered.set()
            if not release.wait(timeout=5):
                raise TimeoutError("Test worker was not released")
            return create(*args, **kwargs)

        with patch.object(self.opera_client, "create_reservation", side_effect=delayed_create) as write:
            with ThreadPoolExecutor(max_workers=1) as pool:
                first = pool.submit(self.engine.execute_operation, request)
                try:
                    self.assertTrue(entered.wait(timeout=5))
                    repeated = second_engine.execute_operation(request.model_copy(update={
                        "idempotency_key": "concurrent_request_B",
                        "booking_reference": self.sample_payment["booking_reference"],
                    }))
                    self.assertEqual(repeated.status, "processing")
                    self.assertEqual(write.call_count, 1)
                finally:
                    release.set()
                finished = first.result(timeout=5)
        self.assertEqual(finished.status, "confirmed")
        self.assertEqual(repeated.operation_id, finished.operation_id)
        self.assertEqual(self.engine.collection.count_documents({}), 1)

    def test_unknown_booking_outcome_cannot_automatically_retry_after_restart(self):
        request = ExecuteBookingOperationRequest(
            idempotency_key="crashed_booking_request", payment_id=self.sample_payment["payment_id"],
            quote_id=self.sample_quote.quote_id,
        )
        with patch.object(self.engine, "_execute_new_booking", side_effect=RuntimeError("interrupted")):
            with self.assertRaises(RuntimeError):
                self.engine.execute_operation(request)
        restarted = BookingOperationEngine(
            database=self.db, pricing_service=self.pricing_service,
            linkage_service=self.linkage_service, opera_client=self.opera_client,
            brs_client=self.brs_client, email_service=self.email_service,
        )
        with patch.object(restarted, "_execute_new_booking") as execute:
            result = restarted.execute_operation(request)
        self.assertEqual(result.status, "processing")
        execute.assert_not_called()

    def test_pending_live_brs_blocks_opera_before_any_vendor_write(self):
        self.pricing_service.settings = SimpleNamespace(environment="production", payment_mode="disabled")
        request = ExecuteBookingOperationRequest(
            idempotency_key="production_blocked_before_opera", payment_id=self.sample_payment["payment_id"],
            quote_id=self.sample_quote.quote_id,
        )
        with patch.object(self.opera_client, "create_reservation") as create:
            result = self.engine.execute_operation(request)
        self.assertEqual(result.status, "unconfirmed_reconciliation")
        self.assertIn("No hotel or golf", result.message)
        create.assert_not_called()

    def test_idempotent_duplicate_submission_replays_cleanly(self):
        """AC 1: Duplicate submissions return identical operation without duplicate writes or duplicate emails."""
        req = ExecuteBookingOperationRequest(
            idempotency_key="idemp_dup_check_001",
            operation_type="new_booking",
            payment_id=self.sample_payment["payment_id"],
            quote_id=self.sample_quote.quote_id,
        )

        # 1st run
        resp1 = self.engine.execute_operation(req)
        self.assertEqual(resp1.status, "confirmed")
        self.assertFalse(resp1.replayed)

        # 2nd run with exact same idempotency key
        resp2 = self.engine.execute_operation(req)
        self.assertEqual(resp2.status, "confirmed")
        self.assertTrue(resp2.replayed)
        self.assertEqual(resp1.operation_id, resp2.operation_id)
        self.assertEqual(resp1.email_delivery_id, resp2.email_delivery_id)

        # N06 commit sends no email; the single confirmed event is unchanged by replay.
        self.assertEqual(len(self.email_service._dispatched_emails), 0)
        self.assertEqual(self.db["booking_operations"].count_documents({"confirmed_event": {"$ne": None}}), 1)

    def test_revalidation_room_sold_out_enters_unconfirmed_reconciliation(self):
        """AC 2, 6, 7: Post-payment inventory sell-out halts operation as unconfirmed and alerts staff."""
        # Mock room availability to report zero available rooms
        with patch.object(
            self.opera_client,
            "get_room_availability",
            return_value=RoomAvailabilityResponse(
                hotel_id="ROSA",
                arrival_date=self.sample_quote.arrival_date,
                departure_date=self.sample_quote.departure_date,
                categories=[
                    RoomCategoryAvailability(
                        room_category="CLASSIC",
                        category_name="Classic Room",
                        available_rooms=0,  # Sold out!
                        rates=[],
                    )
                ],
            ),
        ):
            req = ExecuteBookingOperationRequest(
                idempotency_key="idemp_sold_out_room_001",
                operation_type="new_booking",
                payment_id=self.sample_payment["payment_id"],
                quote_id=self.sample_quote.quote_id,
            )

            resp = self.engine.execute_operation(req)

            self.assertEqual(resp.status, "unconfirmed_reconciliation")
            self.assertEqual(resp.stage, "unconfirmed_reconciliation")
            self.assertTrue(resp.staff_review_required)
            self.assertFalse(resp.readback_verified)

            # Verify staff alert is recorded without sensitive payment data
            op_record = self.engine.get_operation(resp.operation_id)
            self.assertIsNotNone(op_record.staff_alert)
            self.assertEqual(op_record.staff_alert.failed_stage, "revalidation")
            self.assertIn("CLASSIC", op_record.staff_alert.reason)
            self.assertEqual(op_record.staff_alert.payment_id, self.sample_payment["payment_id"])

            # Verify no vendor records were created and no confirmation email was sent
            self.assertEqual(len(self.opera_client._sandbox_reservations), 0)
            self.assertEqual(len(self.brs_client._sandbox_rounds), 0)
            self.assertEqual(len(self.email_service._dispatched_emails), 0)

    def test_revalidation_tee_time_unavailable_enters_unconfirmed_reconciliation(self):
        """AC 2, 6, 7: Post-payment tee time unavailability halts operation safely."""
        # Empty tee sheet cache simulation
        empty_cache = FakeTeesheetCache(times_data={
            (str(self.sample_quote.itemized_golf[0].date), 1): {
                "status": "cached",
                "data": {"tee_times": []},  # No tee times
            }
        })
        self.pricing_service.teesheet_cache = empty_cache

        req = ExecuteBookingOperationRequest(
            idempotency_key="idemp_sold_out_golf_001",
            operation_type="new_booking",
            payment_id=self.sample_payment["payment_id"],
            quote_id=self.sample_quote.quote_id,
        )

        resp = self.engine.execute_operation(req)

        self.assertEqual(resp.status, "unconfirmed_reconciliation")
        self.assertTrue(resp.staff_review_required)

        op_record = self.engine.get_operation(resp.operation_id)
        self.assertIsNotNone(op_record.staff_alert)
        self.assertEqual(op_record.staff_alert.failed_stage, "revalidation")

    def test_partial_vendor_write_failure_retains_ledger_and_alerts_staff(self):
        """AC 6, 7: When Opera succeeds but BRS write fails, booking remains unconfirmed with staff alert."""
        # Make BRS client raise error on second round
        original_create_round = self.brs_client.create_round
        calls = [0]

        def fail_second_round(*args, **kwargs):
            calls[0] += 1
            if calls[0] >= 2:
                raise BrsWriteError("BRS write timed out on second round", status_code=504)
            return original_create_round(*args, **kwargs)

        with patch.object(self.brs_client, "create_round", side_effect=fail_second_round):
            req = ExecuteBookingOperationRequest(
                idempotency_key="idemp_partial_failure_001",
                operation_type="new_booking",
                payment_id=self.sample_payment["payment_id"],
                quote_id=self.sample_quote.quote_id,
            )

            resp = self.engine.execute_operation(req)

            self.assertEqual(resp.status, "unconfirmed_reconciliation")
            self.assertTrue(resp.staff_review_required)

            op_record = self.engine.get_operation(resp.operation_id)
            self.assertIsNotNone(op_record.staff_alert)
            self.assertEqual(op_record.staff_alert.failed_stage, "golf_write")
            self.assertIsNotNone(op_record.staff_alert.opera_reservation_id)
            self.assertIn("BRS write timed out", op_record.staff_alert.reason)

            # Confirmation email must NOT be sent
            self.assertEqual(len(self.email_service._dispatched_emails), 0)

    def test_amendment_operation_protects_original_inventory(self):
        """AC 3, 4: Amendments keep original allocations protected."""
        # First link an existing booking
        booking_ref = "RSP-20270810-ORIG01"
        link_req = StaffLinkBookingRequest(
            booking_reference=booking_ref,
            source_channel="online",
            manual_opera_stay=OperaStayDetails(
                reservation_id="RES-ORIG-001",
                arrival_date=dt.date(2027, 8, 10),
                departure_date=dt.date(2027, 8, 12),
                room_category="SUPERIOR",
                total_amount="900.00",
                currency="EUR",
            ),
            lead_guest=LeadGuestContact(
                first_name="Tiger",
                last_name="Woods",
                email="tiger@example.com",
                telephone="+353871112233",
            ),
            reason="Test initial link setup for amendment protection",
        )
        self.linkage_service.link_booking(link_req, actor="test_setup")

        req = ExecuteBookingOperationRequest(
            idempotency_key="idemp_amend_protect_001",
            operation_type="amendment",
            booking_reference=booking_ref,
        )

        resp = self.engine.execute_operation(req)
        self.assertEqual(resp.status, "confirmed")
        self.assertEqual(resp.stage, "completed")

        op_record = self.engine.get_operation(resp.operation_id)
        self.assertEqual(
            op_record.vendor_references.get("original_opera_reservation_id"),
            "RES-ORIG-001",
        )


if __name__ == "__main__":
    unittest.main()
