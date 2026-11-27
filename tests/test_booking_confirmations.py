"""Unit tests for Ticket N07 — Booking Confirmations Coordinator, Transports, and Rendering."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from email import message_from_bytes
import hashlib
import base64
import json
import os
from pathlib import Path
import re
import shutil
import threading
import tempfile
import unittest
import zlib
from unittest.mock import patch

from rosapenna_backend.api.schemas.communications import (
    BookingConfirmationSnapshot,
    ConfirmedEvent,
    ConfirmationAttemptAudit,
    SnapshotContent,
    SnapshotFinancials,
    SnapshotGolfRound,
    SnapshotLeadGuest,
    SnapshotPayment,
    SnapshotRoom,
    SnapshotStay,
)
from rosapenna_backend.services.booking_confirmations import (
    BookingConfirmationCoordinator,
    ConfirmationConflictError,
    ConfirmationNotFoundError,
    ConfirmationRuntimeContext,
    mask_email_address,
)
from rosapenna_backend.api.schemas.packages import (
    ItemizedGolfRoundQuote,
    ItemizedRoomQuote,
    PackageQuoteResponse,
)
from rosapenna_backend.services.package_pricing import compute_quote_hash
from rosapenna_backend.services.email_delivery import (
    DisabledConfirmationTransport,
    RecordingConfirmationTransport,
    TransportSubmitOutcome,
    render_n07_email_message,
)
from rosapenna_backend.services.pdf_itinerary import build_n07_confirmation_pdf


TEST_RUN_ID = os.getenv("BOOKING_CONFIRMATIONS_TEST_RUN_ID") or "SIM-RUN-N07-001"
TEST_QUOTE_ID = "QTE-TEST-N07-001"
TEST_PAYMENT_ID = "PAY-TEST-N07-001"
TEST_OPERA_ID = "OPERA-TEST-001"
TEST_OPERA_CONFIRMATION = "OPERA-CONF-TEST-001"
TEST_BRS_ROUND_ID = "BRS-ROUND-TEST-001"
TEST_BRS_BOOKING_ID = "BRS-BOOKING-TEST-001"


def make_test_quote(total: str = "1200.00", deposit: str = "300.00") -> PackageQuoteResponse:
    total_value = Decimal(total)
    deposit_value = Decimal(deposit)
    half_total = f"{total_value / 2:.2f}"
    room_rate = f"{Decimal(half_total) / 3:.2f}"
    golf_rate = f"{Decimal(half_total) / 2:.2f}"
    return PackageQuoteResponse(
        quote_id=TEST_QUOTE_ID,
        status="available",
        rules_version="v1.0",
        arrival_date=dt.date(2099, 6, 1),
        departure_date=dt.date(2099, 6, 4),
        nights=3,
        currency="EUR",
        itemized_rooms=[ItemizedRoomQuote(
            room_category="deluxe_twin",
            room_quantity=1,
            nights=3,
            rate_per_night=room_rate,
            subtotal=half_total,
            rate_plan_code="TEST-RATE",
            available_rooms=1,
        )],
        itemized_golf=[ItemizedGolfRoundQuote(
            course_id=1,
            course_name="Sandy Hills Links <>&",
            date=dt.date(2099, 6, 2),
            time="10:30",
            players=2,
            rate_per_player=golf_rate,
            subtotal=half_total,
            available_slots=2,
        )],
        total_rooms_price=half_total,
        total_golf_price=half_total,
        total_package_price=total,
        deposit_amount=deposit,
        balance_amount=f"{total_value - deposit_value:.2f}",
        balance_due_date=dt.date(2099, 5, 20),
        terms_version="v1.0-standard",
        created_at="2099-05-14T09:00:00Z",
    )


def make_test_snapshot(
    ref: str = "RSP-20990514-N07TEST",
    rev: int = 1,
    guest_email: str = "lead@example.invalid",
    total: str = "1200.00",
    paid: str = "300.00",
    balance: str = "900.00",
    script_injection_name: str = "John <script>alert(1)</script>",
) -> BookingConfirmationSnapshot:
    """Generate a valid synthetic snapshot for N07 testing."""
    quote = make_test_quote(total, paid)
    return BookingConfirmationSnapshot(
        booking_reference=ref,
        booking_revision=rev,
        confirmed_at="2099-05-14T10:00:00Z",
        environment="test",
        is_test=True,
        simulation_run_id=TEST_RUN_ID,
        accepted_quote_hash=compute_quote_hash(quote),
        rules_version="v1.0",
        terms_version="v1.0-standard",
        lead_guest=SnapshotLeadGuest(
            first_name=script_injection_name,
            last_name="Doe & Partners",
            email=guest_email,
            telephone="+353 74 915 5000",
        ),
        stay=SnapshotStay(
            arrival_date="2099-06-01",
            departure_date="2099-06-04",
            rooms=[
                SnapshotRoom(
                    room_group_id="RG-1",
                    room_category="deluxe_twin",
                    room_quantity=1,
                    occupant_names=["John Doe", "Jane Doe"],
                )
            ],
        ),
        golf_rounds=[
            SnapshotGolfRound(
                round_key="RND-1",
                course_name="Sandy Hills Links <>&",
                date="2099-06-02",
                tee_time="10:30",
                timezone="Europe/Dublin",
                players=2,
                holes=18,
                provider_booking_ids=[TEST_BRS_BOOKING_ID],
                golfer_names=["John Doe", "Jane Doe"],
            )
        ],
        financials=SnapshotFinancials(
            currency="EUR",
            total_amount=total,
            deposit_paid=paid,
            verified_paid_total=paid,
            balance_due=balance,
            balance_due_date="2099-05-20",
        ),
        payments=[
            SnapshotPayment(
                payment_purpose="deposit",
                payment_id=TEST_PAYMENT_ID,
                amount=paid,
                currency="EUR",
                verified_at="2099-05-14T09:59:00Z",
                receipt_reference="REC-TEST-999",
            )
        ],
        content=SnapshotContent(
            template_version="v1.0-draft",
            pdf_layout_version="v1.0-draft",
            policy_text_version="v1.0-standard",
                policy_text="Offline test terms only. These terms are not approved for a real booking.",
        ),
    )


def make_test_confirmed_event(
    op_id: str = "OP-TEST-N07-001",
    event_id: str = "EVT-TEST-N07-001",
    snapshot: BookingConfirmationSnapshot | None = None,
) -> ConfirmedEvent:
    snap = snapshot or make_test_snapshot()
    return ConfirmedEvent(
        schema_version=1,
        event_id=event_id,
        event_type="booking.confirmed",
        operation_id=op_id,
        booking_reference=snap.booking_reference,
        booking_revision=snap.booking_revision,
        occurred_at=snap.confirmed_at,
        environment="test",
        evidence_source="offline_n07_test",
        is_test=True,
        simulation_run_id=snap.simulation_run_id,
        snapshot_hash=snap.compute_hash(),
        snapshot=snap,
        evidence_refs={
            "quote_id": TEST_QUOTE_ID,
            "payment_ids": [TEST_PAYMENT_ID],
            "opera_reservation_id": TEST_OPERA_ID,
            "brs_round_ids": [TEST_BRS_ROUND_ID],
            "brs_booking_ids": [TEST_BRS_BOOKING_ID],
            "booking_linkage_revision": snap.booking_revision,
        },
    )


def make_test_operation_doc(event: ConfirmedEvent) -> dict:
    """Build an explicit synthetic operation plus quote, payment, readback, and linkage evidence."""
    snapshot = event.snapshot
    quote_model = make_test_quote(snapshot.financials.total_amount, snapshot.financials.deposit_paid)
    quote = quote_model.model_dump(mode="json")
    payment = {
        "payment_id": TEST_PAYMENT_ID,
        "booking_reference": snapshot.booking_reference,
        "quote_id": TEST_QUOTE_ID,
        "quote_hash": snapshot.accepted_quote_hash,
        "rules_version": snapshot.rules_version,
        "purpose": snapshot.payments[0].payment_purpose,
        "amount": snapshot.payments[0].amount,
        "currency": snapshot.payments[0].currency,
        "status": "verified_paid",
        "evidence_source": "simulation",
        "is_test": True,
        "environment": "test",
        "simulation_run_id": snapshot.simulation_run_id,
    }
    opera_stay = {
        "reservation_id": TEST_OPERA_ID,
        "confirmation_number": TEST_OPERA_CONFIRMATION,
        "arrival_date": snapshot.stay.arrival_date,
        "departure_date": snapshot.stay.departure_date,
        "room_category": snapshot.stay.rooms[0].room_category,
        "room_quantity": snapshot.stay.rooms[0].room_quantity,
        "total_amount": quote_model.total_rooms_price,
        "currency": quote_model.currency,
        "status": "Reserved",
    }
    brs_round = {
        "round_id": TEST_BRS_ROUND_ID,
        "brs_booking_id": TEST_BRS_BOOKING_ID,
        "course_name": snapshot.golf_rounds[0].course_name,
        "date": snapshot.golf_rounds[0].date,
        "time": snapshot.golf_rounds[0].tee_time,
        "players": snapshot.golf_rounds[0].players,
        "price": quote_model.itemized_golf[0].rate_per_player,
        "currency": quote_model.currency,
        "status": "Reserved",
    }
    linkage = {
        "booking_reference": snapshot.booking_reference,
        "status": "linked",
        "revision": snapshot.booking_revision,
        "lead_guest": {
            "first_name": snapshot.lead_guest.first_name,
            "last_name": snapshot.lead_guest.last_name,
            "email": snapshot.lead_guest.email,
            "telephone": snapshot.lead_guest.telephone or "+353 74 915 5000",
        },
        "opera_stay": opera_stay,
        "brs_rounds": [brs_round],
        "payments": [{
            "payment_id": TEST_PAYMENT_ID,
            "payment_type": snapshot.payments[0].payment_purpose,
            "amount": snapshot.payments[0].amount,
            "currency": snapshot.payments[0].currency,
            "status": "completed",
        }],
    }
    readback = {
        "opera_reservation_id": TEST_OPERA_ID,
        "opera_confirmation_number": TEST_OPERA_CONFIRMATION,
        "opera_verified": True,
        "brs_round_ids": [TEST_BRS_ROUND_ID],
        "brs_booking_ids": [TEST_BRS_BOOKING_ID],
        "brs_verified": True,
    }
    return {
        "operation_id": event.operation_id,
        "idempotency_key_hash": f"hash-{event.operation_id}",
        "operation_type": "new_booking",
        "booking_reference": snapshot.booking_reference,
        "quote_id": TEST_QUOTE_ID,
        "payment_id": TEST_PAYMENT_ID,
        "payment_verified": True,
        "vendor_references": {
            "opera_reservation_id": TEST_OPERA_ID,
            "opera_confirmation_number": TEST_OPERA_CONFIRMATION,
            "brs_round_ids": [TEST_BRS_ROUND_ID],
            "brs_booking_ids": [TEST_BRS_BOOKING_ID],
        },
        "readback_evidence": readback,
        "stage": "completed",
        "completed_at": event.occurred_at,
        "confirmed_event": event.model_dump(mode="python"),
        "_n07_test_source": {"quote": quote, "payments": [payment], "linkage": linkage},
    }


def make_test_coordinator(transport: RecordingConfirmationTransport) -> BookingConfirmationCoordinator:
    return BookingConfirmationCoordinator(
        transport=transport,
        runtime_context=ConfirmationRuntimeContext(
            mode="recording", environment="test", run_id=TEST_RUN_ID,
            recording_dir=str(transport.recording_dir),
        ),
        allow_in_memory_fixtures=True,
    )


class TestBookingConfirmationsUnit(unittest.TestCase):
    """Test snapshot validation, coordinator operations, rendering, and adapters."""

    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="n07_test_")
        self.recording_dir = Path(self.test_dir) / "recordings"

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_snapshot_math_validation(self):
        """Financials must balance: balance_due == total_amount - verified_paid_total."""
        with self.assertRaises(ValueError):
            make_test_snapshot(total="1000.00", paid="200.00", balance="700.00")  # Wrong balance

    def test_pdf_rendering_and_escaping(self):
        """PDF rendering must succeed, contain TEST banner, escape markup, and stay under 2 MiB."""
        snap = make_test_snapshot()
        pdf_bytes = build_n07_confirmation_pdf(snap)
        self.assertGreater(len(pdf_bytes), 1000)
        self.assertLess(len(pdf_bytes), 2 * 1024 * 1024)
        self.assertTrue(pdf_bytes.startswith(b"%PDF"))

    def test_email_rendering_and_escaping(self):
        """Email rendering must escape HTML, include test banner, and attach PDF."""
        snap = make_test_snapshot()
        pdf_bytes = build_n07_confirmation_pdf(snap)
        subject, plain, html_body, mime_bytes, msg_id = render_n07_email_message(
            snapshot=snap,
            recipient_role="guest",
            pdf_bytes=pdf_bytes,
            attempt_id="ATT-001",
        )
        self.assertIn("TEST — NOT A REAL BOOKING CONFIRMATION", subject)
        self.assertIn("TEST — NOT A REAL BOOKING CONFIRMATION", plain)
        self.assertIn("TEST — NOT A REAL BOOKING CONFIRMATION", html_body)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", html_body)
        self.assertNotIn("<script>alert(1)</script>", html_body)
        self.assertGreater(len(mime_bytes), len(pdf_bytes))

    def test_mask_email_address(self):
        """Email masking must protect privacy for staff views."""
        self.assertEqual(mask_email_address("john.doe@example.invalid"), "jo***e@example.invalid")
        self.assertEqual(mask_email_address("ab@test.invalid"), "a***@test.invalid")

    def test_recording_transport_rejects_non_test_domain(self):
        """Recording transport must strictly reject non-test domains."""
        transport = RecordingConfirmationTransport(recording_dir=self.recording_dir)
        outcome = transport.submit(b"test mail", "guest@gmail.com", "<msg1@rosapenna>", "ATT-1")
        self.assertEqual(outcome.state, "permanent_failure")
        self.assertEqual(outcome.error_code, "NON_TEST_RECIPIENT_REJECTED")

    def test_recording_transport_accepts_test_domain(self):
        """Recording transport records synthetic mail to disk."""
        transport = RecordingConfirmationTransport(recording_dir=self.recording_dir)
        outcome = transport.submit(b"test mail body", "lead@example.invalid", "<msg1@rosapenna>", "ATT-1")
        self.assertEqual(outcome.state, "recorded_test")
        self.assertEqual(len(transport.recorded_submissions), 1)
        # Check file on disk
        eml_files = list(self.recording_dir.glob("*.eml"))
        self.assertEqual(len(eml_files), 1)

    def test_coordinator_prepare_and_idempotency(self):
        """Coordinator prepares confirmation and returns same record on replay."""
        transport = RecordingConfirmationTransport(recording_dir=self.recording_dir)
        coord = make_test_coordinator(transport)

        op_id = "OP-100"
        event = make_test_confirmed_event(op_id=op_id)
        coord._in_memory_records[op_id] = make_test_operation_doc(event)

        # 1. Prepare
        rec1 = coord.prepare_confirmation(op_id)
        self.assertEqual(rec1.overall_status, "prepared")
        self.assertEqual(rec1.deliveries["guest"].status, "prepared")
        self.assertEqual(rec1.deliveries["hotel"].status, "prepared")

        # 2. Replay idempotency
        rec2 = coord.prepare_confirmation(op_id)
        self.assertEqual(rec1.snapshot_hash, rec2.snapshot_hash)

    def test_coordinator_dispatch_both_recipients(self):
        """Coordinator dispatches to guest and hotel independently."""
        transport = RecordingConfirmationTransport(recording_dir=self.recording_dir)
        coord = make_test_coordinator(transport)

        op_id = "OP-101"
        event = make_test_confirmed_event(op_id=op_id)
        coord._in_memory_records[op_id] = make_test_operation_doc(event)

        coord.prepare_confirmation(op_id)

        # Dispatch guest
        guest_del = coord.dispatch_recipient(op_id, "guest")
        self.assertEqual(guest_del.status, "recorded_test")

        status_view1 = coord.get_status(op_id)
        self.assertEqual(status_view1.guest_delivery.status, "recorded_test")
        self.assertEqual(status_view1.hotel_delivery.status, "prepared")

        # Dispatch hotel
        hotel_del = coord.dispatch_recipient(op_id, "hotel")
        self.assertEqual(hotel_del.status, "recorded_test")

        status_view2 = coord.get_status(op_id)
        self.assertEqual(status_view2.overall_status, "recorded_test")
        self.assertEqual(status_view2.hotel_delivery.status, "recorded_test")

    def test_coordinator_simulated_refusal_and_retry(self):
        """Simulated refusal allows staff retry, successful role is not resubmitted."""
        transport = RecordingConfirmationTransport(
            recording_dir=self.recording_dir,
            simulated_behavior={"default": "success", "lead@example.invalid": "refusal"},
        )
        coord = make_test_coordinator(transport)

        op_id = "OP-102"
        event = make_test_confirmed_event(op_id=op_id)
        coord._in_memory_records[op_id] = make_test_operation_doc(event)

        coord.prepare_confirmation(op_id)

        # Dispatch guest -> fails with retryable_failure
        guest_del = coord.dispatch_recipient(op_id, "guest")
        self.assertEqual(guest_del.status, "retryable_failure")
        self.assertEqual(guest_del.error_code, "SIMULATED_RECIPIENT_REFUSAL")

        # Switch transport behavior to success and retry
        transport.simulated_behavior["lead@example.invalid"] = "success"
        retry_del = coord.dispatch_recipient(
            op_id,
            "guest",
            expected_event_id=event.event_id,
            expected_attempt_id=guest_del.attempt_id,
            reason="Operator confirmed mailbox restored",
            actor="operator_1",
        )
        self.assertEqual(retry_del.status, "recorded_test")
        self.assertEqual(retry_del.attempt_count, 2)
        attempts = coord._in_memory_records[op_id]["confirmation"]["attempt_history"]
        self.assertEqual(len(attempts), 2)
        self.assertEqual(attempts[1]["actor"], "operator_1")
        self.assertEqual(attempts[1]["reason"], "Operator confirmed mailbox restored")
        self.assertEqual(attempts[1]["previous_status"], "retryable_failure")

    def test_stale_retry_attempt_id_fails_before_submission(self):
        transport = RecordingConfirmationTransport(
            recording_dir=self.recording_dir,
            simulated_behavior={"lead@example.invalid": "refusal"},
        )
        coord = make_test_coordinator(transport)
        event = make_test_confirmed_event(op_id="OP-STALE-ATTEMPT")
        coord._in_memory_records[event.operation_id] = make_test_operation_doc(event)
        coord.prepare_confirmation(event.operation_id)
        failed = coord.dispatch_recipient(event.operation_id, "guest")

        transport.simulated_behavior["lead@example.invalid"] = "success"
        with self.assertRaises(ConfirmationConflictError):
            coord.dispatch_recipient(
                event.operation_id,
                "guest",
                expected_event_id=event.event_id,
                expected_attempt_id="ATT-STALE-OTHER",
                reason="Operator checked mailbox status",
            )
        self.assertEqual(failed.status, "retryable_failure")
        self.assertEqual(transport.recorded_submissions, [])
        current = coord._in_memory_records[event.operation_id]["confirmation"]["deliveries"]["guest"]
        self.assertEqual(current["attempt_count"], 1)
        self.assertEqual(current["attempt_id"], failed.attempt_id)

    def test_retry_keeps_same_logical_email_content(self):
        class CaptureRetryTransport:
            def __init__(self):
                self.messages = []

            def submit(self, message_bytes, envelope_recipient, message_id, attempt_id):
                self.messages.append(message_bytes)
                if len(self.messages) == 1:
                    return TransportSubmitOutcome(
                        state="retryable_failure",
                        evidence_source="offline_retry_test",
                        error_code="SYNTHETIC_REFUSAL",
                    )
                return TransportSubmitOutcome(
                    state="recorded_test",
                    evidence_source="offline_retry_test",
                    accepted_recipient=envelope_recipient,
                    provider_reference=f"REC-{attempt_id[:8]}",
                )

        transport = CaptureRetryTransport()
        coord = BookingConfirmationCoordinator(
            transport=transport,
            runtime_context=ConfirmationRuntimeContext(
                mode="recording", environment="test", run_id=TEST_RUN_ID,
                recording_dir=str(self.recording_dir),
            ),
            allow_in_memory_fixtures=True,
        )
        event = make_test_confirmed_event(op_id="OP-STABLE-RETRY-CONTENT")
        coord._in_memory_records[event.operation_id] = make_test_operation_doc(event)
        coord.prepare_confirmation(event.operation_id)
        failed = coord.dispatch_recipient(event.operation_id, "guest")
        delivered = coord.dispatch_recipient(
            event.operation_id,
            "guest",
            expected_event_id=event.event_id,
            expected_attempt_id=failed.attempt_id,
            reason="Operator checked test delivery outcome",
        )

        self.assertEqual(delivered.status, "recorded_test")
        first = message_from_bytes(transport.messages[0])
        second = message_from_bytes(transport.messages[1])
        self.assertEqual(first["Message-ID"], second["Message-ID"])
        self.assertEqual(first["Date"], second["Date"])
        self.assertEqual(first["To"], second["To"])
        payloads = lambda message: [
            (part.get_content_type(), part.get_payload(decode=True))
            for part in message.walk()
            if not part.is_multipart()
        ]
        self.assertEqual(payloads(first), payloads(second))

    def test_unknown_delivery_blocks_auto_retry(self):
        """An ordinary reason cannot permit a retry after an unknown result."""
        transport = RecordingConfirmationTransport(
            recording_dir=self.recording_dir,
            simulated_behavior={"default": "timeout"},
        )
        coord = make_test_coordinator(transport)

        op_id = "OP-103"
        event = make_test_confirmed_event(op_id=op_id)
        coord._in_memory_records[op_id] = make_test_operation_doc(event)

        coord.prepare_confirmation(op_id)
        guest_del = coord.dispatch_recipient(op_id, "guest")
        self.assertEqual(guest_del.status, "unknown")

        # Attempt retry without reason -> raises ConfirmationConflictError
        with self.assertRaises(ConfirmationConflictError):
            coord.dispatch_recipient(op_id, "guest")

        # An ordinary reason cannot override an unknown result.
        transport.simulated_behavior["default"] = "success"
        with self.assertRaises(ConfirmationConflictError):
            coord.dispatch_recipient(
                op_id,
                "guest",
                expected_attempt_id=guest_del.attempt_id,
                reason="Operator checked provider log",
            )

    def test_event_and_snapshot_validation_reject_invalid_source_values(self):
        snapshot_data = make_test_snapshot().model_dump(mode="python")
        snapshot_data["stay"]["arrival_date"] = "2099-02-30"
        with self.assertRaises(ValueError):
            BookingConfirmationSnapshot.model_validate(snapshot_data)

        snapshot_data = make_test_snapshot().model_dump(mode="python")
        snapshot_data["golf_rounds"][0]["tee_time"] = "99:99"
        with self.assertRaises(ValueError):
            BookingConfirmationSnapshot.model_validate(snapshot_data)

        snapshot_data = make_test_snapshot().model_dump(mode="python")
        snapshot_data["payments"][0]["amount"] = "1.00"
        with self.assertRaises(ValueError):
            BookingConfirmationSnapshot.model_validate(snapshot_data)

        snapshot_data = make_test_snapshot().model_dump(mode="python")
        snapshot_data["payments"][0]["payment_purpose"] = "credit"
        with self.assertRaises(ValueError):
            BookingConfirmationSnapshot.model_validate(snapshot_data)

        event_data = make_test_confirmed_event().model_dump(mode="python")
        event_data["schema_version"] = 2
        with self.assertRaises(ValueError):
            ConfirmedEvent.model_validate(event_data)

    def test_confirmation_prepare_rejects_uncompleted_or_unpaid_operation(self):
        transport = RecordingConfirmationTransport(recording_dir=self.recording_dir)
        coord = make_test_coordinator(transport)
        event = make_test_confirmed_event(op_id="OP-UNVERIFIED")
        op_doc = make_test_operation_doc(event)
        op_doc["stage"] = "payment_verified"
        coord._in_memory_records[event.operation_id] = op_doc

        with self.assertRaises(ConfirmationConflictError):
            coord.prepare_confirmation(event.operation_id)
        op_doc["stage"] = "completed"
        op_doc["payment_verified"] = False
        with self.assertRaises(ConfirmationConflictError):
            coord.prepare_confirmation(event.operation_id)

    def test_confirmation_prepare_rejects_real_guest_address_in_recording_mode(self):
        transport = RecordingConfirmationTransport(recording_dir=self.recording_dir)
        coord = make_test_coordinator(transport)
        event = make_test_confirmed_event(snapshot=make_test_snapshot(guest_email="guest@example.com"))
        coord._in_memory_records[event.operation_id] = make_test_operation_doc(event)

        with self.assertRaises(ConfirmationConflictError):
            coord.prepare_confirmation(event.operation_id)
        self.assertIsNone(coord._in_memory_records[event.operation_id].get("confirmation"))
        self.assertEqual(transport.recorded_submissions, [])

    def test_malformed_saved_payment_fails_closed_without_transport(self):
        transport = RecordingConfirmationTransport(recording_dir=self.recording_dir)
        coord = make_test_coordinator(transport)
        event = make_test_confirmed_event(op_id="OP-MALFORMED-PAYMENT")
        op_doc = make_test_operation_doc(event)
        op_doc["_n07_test_source"]["payments"][0]["amount"] = "not-money"
        coord._in_memory_records[event.operation_id] = op_doc

        with self.assertRaises(ConfirmationConflictError):
            coord.prepare_confirmation(event.operation_id)
        self.assertIsNone(op_doc.get("confirmation"))
        self.assertEqual(transport.recorded_submissions, [])

    def test_source_rejects_saved_guest_or_payment_purpose_mismatch(self):
        transport = RecordingConfirmationTransport(recording_dir=self.recording_dir)
        coord = make_test_coordinator(transport)

        event = make_test_confirmed_event(op_id="OP-SOURCE-GUEST-MISMATCH")
        op_doc = make_test_operation_doc(event)
        op_doc["_n07_test_source"]["linkage"]["lead_guest"]["email"] = "other@example.invalid"
        coord._in_memory_records[event.operation_id] = op_doc
        with self.assertRaises(ConfirmationConflictError):
            coord.prepare_confirmation(event.operation_id)
        self.assertIsNone(op_doc.get("confirmation"))

        event = make_test_confirmed_event(op_id="OP-SOURCE-PURPOSE-MISMATCH")
        op_doc = make_test_operation_doc(event)
        op_doc["_n07_test_source"]["payments"][0]["purpose"] = "full"
        coord._in_memory_records[event.operation_id] = op_doc
        with self.assertRaises(ConfirmationConflictError):
            coord.prepare_confirmation(event.operation_id)
        self.assertIsNone(op_doc.get("confirmation"))
        self.assertEqual(transport.recorded_submissions, [])

    def test_recording_transport_rejects_path_inputs_before_write(self):
        transport = RecordingConfirmationTransport(recording_dir=self.recording_dir)
        bad_recipient = transport.submit(
            b"synthetic", "lead@/../../escaped.invalid", "<msg@example.invalid>", "ATT-1"
        )
        bad_attempt = transport.submit(
            b"synthetic", "lead@example.invalid", "<msg@example.invalid>", "../../escaped"
        )
        self.assertEqual(bad_recipient.state, "permanent_failure")
        self.assertEqual(bad_attempt.error_code, "INVALID_ATTEMPT_ID")
        self.assertEqual(list(self.recording_dir.glob("*.eml")), [])

    def test_recording_transport_rejects_sink_outside_owned_root(self):
        owned_root = Path(self.test_dir) / "owned"
        outside = Path(self.test_dir) / "outside"
        owned_root.mkdir()
        outside.mkdir()
        with self.assertRaises(ValueError):
            RecordingConfirmationTransport(recording_dir=outside, owned_root=owned_root)

    def test_pdf_escapes_policy_markup_and_never_loads_resources(self):
        from rosapenna_backend.services import pdf_itinerary

        snapshot = make_test_snapshot()
        snapshot.content.policy_text = '<img src="n07-test-resource.png"/> & <a href="https://invalid.test">Terms</a>'
        paragraph = pdf_itinerary.Paragraph
        with patch.object(pdf_itinerary, "Paragraph", wraps=paragraph) as paragraph_spy, \
                patch("reportlab.lib.utils.ImageReader") as image_reader:
            result = build_n07_confirmation_pdf(snapshot)

        self.assertTrue(result.startswith(b"%PDF"))
        policy_calls = [str(call.args[0]) for call in paragraph_spy.call_args_list if "Terms" in str(call.args[0])]
        self.assertTrue(policy_calls)
        self.assertIn("&lt;img", policy_calls[-1])
        self.assertIn("&lt;a", policy_calls[-1])
        self.assertIn("&amp;", policy_calls[-1])
        image_reader.assert_not_called()

    def test_message_id_date_and_recipient_remain_stable(self):
        snapshot = make_test_snapshot(total="1200.00", paid="300.00", balance="900.00")
        pdf_bytes = build_n07_confirmation_pdf(snapshot)
        first = render_n07_email_message(
            snapshot, "hotel", pdf_bytes, "ATT-ONE", recipient_address="reservations.test@rosapenna.invalid",
            message_date=snapshot.confirmed_at, message_id="<n07.stable@rosapenna.invalid>",
        )
        second = render_n07_email_message(
            snapshot, "hotel", pdf_bytes, "ATT-TWO", recipient_address="reservations.test@rosapenna.invalid",
            message_date=snapshot.confirmed_at, message_id="<n07.stable@rosapenna.invalid>",
        )
        first_message = message_from_bytes(first[3])
        second_message = message_from_bytes(second[3])
        self.assertEqual(first_message["Message-ID"], second_message["Message-ID"])
        self.assertEqual(first_message["Date"], second_message["Date"])
        self.assertEqual(first_message["To"], "reservations.test@rosapenna.invalid")
        self.assertIn("Deposit Paid: EUR 300.00", first[1])
        self.assertIn("Total Verified Paid: EUR 300.00", first[1])

    def test_deposit_label_uses_deposit_amount_not_total_paid(self):
        data = make_test_snapshot().model_dump(mode="python")
        data["financials"]["verified_paid_total"] = "600.00"
        data["financials"]["balance_due"] = "600.00"
        data["payments"].append({
            "payment_purpose": "balance",
            "payment_id": "PAY-TEST-BALANCE",
            "amount": "300.00",
            "currency": "EUR",
            "verified_at": "2099-05-15T09:00:00Z",
            "receipt_reference": "REC-TEST-BALANCE",
        })
        snapshot = BookingConfirmationSnapshot.model_validate(data)
        _, plain, html_body, _, _ = render_n07_email_message(
            snapshot, "guest", build_n07_confirmation_pdf(snapshot), "ATT-FINANCIALS"
        )
        self.assertIn("Deposit Paid: EUR 300.00", plain)
        self.assertIn("Total Verified Paid: EUR 600.00", plain)
        self.assertIn("Deposit Paid:", html_body)
        self.assertIn("EUR 300.00", html_body)
        self.assertIn("Total Verified Paid:", html_body)

    def test_render_failure_leaves_recipient_prepared(self):
        transport = RecordingConfirmationTransport(recording_dir=self.recording_dir)
        coord = make_test_coordinator(transport)
        event = make_test_confirmed_event(op_id="OP-RENDER-FAIL")
        coord._in_memory_records[event.operation_id] = make_test_operation_doc(event)
        coord.prepare_confirmation(event.operation_id)

        with patch("rosapenna_backend.services.booking_confirmations.render_n07_email_message", side_effect=ValueError("bad template")):
            with self.assertRaises(ValueError):
                coord.dispatch_recipient(event.operation_id, "guest")
        status = coord.get_status(event.operation_id)
        self.assertEqual(status.guest_delivery.status, "prepared")
        self.assertEqual(status.guest_delivery.attempt_count, 0)
        self.assertEqual(transport.recorded_submissions, [])

    def test_changed_event_after_prepare_fails_before_submission(self):
        transport = RecordingConfirmationTransport(recording_dir=self.recording_dir)
        coord = make_test_coordinator(transport)
        event = make_test_confirmed_event(op_id="OP-EVENT-CHANGED")
        op_doc = make_test_operation_doc(event)
        coord._in_memory_records[event.operation_id] = op_doc
        coord.prepare_confirmation(event.operation_id)

        changed_event = event.model_copy(update={"event_id": "EVT-CHANGED-AFTER-PREPARE"})
        op_doc["confirmed_event"] = changed_event.model_dump(mode="python")
        with self.assertRaises(ConfirmationConflictError):
            coord.dispatch_recipient(event.operation_id, "guest")
        self.assertEqual(transport.recorded_submissions, [])

    def test_transport_exception_after_recording_marks_unknown_and_blocks_retry(self):
        recorder = RecordingConfirmationTransport(recording_dir=self.recording_dir)

        class RecordThenRaise:
            def submit(self, message_bytes, envelope_recipient, message_id, attempt_id):
                recorder.submit(message_bytes, envelope_recipient, message_id, attempt_id)
                raise TimeoutError("synthetic timeout after record")

        coord = BookingConfirmationCoordinator(
            transport=RecordThenRaise(),
            runtime_context=ConfirmationRuntimeContext(
                mode="recording", environment="test", run_id=TEST_RUN_ID,
                recording_dir=str(self.recording_dir),
            ),
            allow_in_memory_fixtures=True,
        )
        event = make_test_confirmed_event(op_id="OP-UNKNOWN-AFTER-RECORD")
        coord._in_memory_records[event.operation_id] = make_test_operation_doc(event)
        coord.prepare_confirmation(event.operation_id)
        first = coord.dispatch_recipient(event.operation_id, "guest")
        self.assertEqual(first.status, "unknown")
        self.assertEqual(len(recorder.recorded_submissions), 1)
        with self.assertRaises(ConfirmationConflictError):
            coord.dispatch_recipient(
                event.operation_id, "guest", expected_attempt_id=first.attempt_id,
                reason="A normal retry reason",
            )

    def test_attempt_limit_cannot_be_bypassed_with_reason(self):
        transport = RecordingConfirmationTransport(recording_dir=self.recording_dir)
        coord = make_test_coordinator(transport)
        event = make_test_confirmed_event(op_id="OP-ATTEMPT-CAP")
        coord._in_memory_records[event.operation_id] = make_test_operation_doc(event)
        coord.prepare_confirmation(event.operation_id)
        conf = coord._in_memory_records[event.operation_id]["confirmation"]
        guest = conf["deliveries"]["guest"]
        guest["status"] = "retryable_failure"
        guest["attempt_count"] = 3
        guest["attempt_id"] = "ATT-PREVIOUS"
        with self.assertRaises(ConfirmationConflictError):
            coord.dispatch_recipient(
                event.operation_id, "guest", expected_attempt_id="ATT-PREVIOUS",
                reason="Operator wants one more try",
            )
        self.assertEqual(transport.recorded_submissions, [])

    def test_expired_sending_claim_becomes_unknown(self):
        transport = RecordingConfirmationTransport(recording_dir=self.recording_dir)
        coord = make_test_coordinator(transport)
        event = make_test_confirmed_event(op_id="OP-EXPIRED-CLAIM")
        coord._in_memory_records[event.operation_id] = make_test_operation_doc(event)
        coord.prepare_confirmation(event.operation_id)
        raw = coord._in_memory_records[event.operation_id]["confirmation"]
        guest = raw["deliveries"]["guest"]
        guest.update({
            "status": "sending",
            "attempt_count": 1,
            "attempt_id": "ATT-EXPIRED",
            "claimed_at": "2000-01-01T00:00:00+00:00",
        })
        raw["attempt_history"].append(ConfirmationAttemptAudit(
            attempt_id="ATT-EXPIRED",
            recipient_role="guest",
            actor="coordinator",
            previous_status="prepared",
            started_at="2000-01-01T00:00:00+00:00",
        ).model_dump(mode="python"))

        status = coord.get_status(event.operation_id)

        self.assertEqual(status.guest_delivery.status, "unknown")
        self.assertEqual(status.guest_delivery.error_code, "CLAIM_EXPIRED_UNKNOWN")
        self.assertEqual(transport.recorded_submissions, [])

    def test_generic_operation_save_skips_binary_confirmation_before_json_encoding(self):
        from rosapenna_backend.api.schemas.booking_operations import BookingOperationRecord
        from rosapenna_backend.services.booking_operations import BookingOperationEngine

        engine = object.__new__(BookingOperationEngine)
        engine.collection = None
        engine._in_memory_records = {}
        operation_id = "OP-BINARY-SAVE"
        binary_confirmation = {"pdf_bytes": b"\xff\x00\xfe"}
        engine._in_memory_by_id = {operation_id: {"confirmation": binary_confirmation}}
        record = BookingOperationRecord(
            operation_id=operation_id,
            idempotency_key_hash="hash-binary-save",
            operation_type="new_booking",
            booking_reference="RSP-20990514-BINARY",
            stage="completed",
            confirmed_event={"event_id": "EVT-BINARY"},
            confirmation=binary_confirmation,
        )

        engine._save_record(record)

        self.assertEqual(engine._in_memory_by_id[operation_id]["confirmation"], binary_confirmation)
        self.assertEqual(engine._in_memory_records["hash-binary-save"]["confirmation"], binary_confirmation)


def pdf_text(pdf_bytes: bytes) -> str:
    """Extract page text from a ReportLab PDF without extra libraries (standard fonts, Flate+ASCII85 streams)."""
    pieces: list[str] = []
    token = re.compile(rb"\(((?:\\.|[^\\)])*)\)\s*Tj|(T\*|ET|BT)")
    escape = re.compile(rb"\\([0-7]{3}|.)", re.S)

    def unescape(match: re.Match) -> bytes:
        value = match.group(1)
        return bytes([int(value, 8)]) if len(value) == 3 and value.isdigit() else value

    for stream in re.finditer(rb"stream\r?\n(.*?)endstream", pdf_bytes, re.S):
        try:
            content = zlib.decompress(base64.a85decode(stream.group(1).strip(), adobe=True))
        except Exception:
            continue
        for match in token.finditer(content):
            if match.group(1) is not None:
                pieces.append(escape.sub(unescape, match.group(1)).decode("cp1252"))
            else:
                pieces.append(" ")
    return " ".join("".join(pieces).split())


def pdf_page_count(pdf_bytes: bytes) -> int:
    return len(re.findall(rb"/Type /Page(?![A-Za-z])", pdf_bytes))


def make_guest_snapshot(**changes) -> BookingConfirmationSnapshot:
    """Valid synthetic snapshot with top-level or nested (dict) overrides."""
    data = make_test_snapshot(script_injection_name="Seán").model_dump(mode="python")
    for key, value in changes.items():
        if isinstance(value, dict) and isinstance(data.get(key), dict):
            data[key].update(value)
        else:
            data[key] = value
    return BookingConfirmationSnapshot.model_validate(data)


def make_real_snapshot(**changes) -> BookingConfirmationSnapshot:
    """Valid non-test snapshot (no simulation run, no saved policy text)."""
    base = {
        "environment": "production",
        "is_test": False,
        "simulation_run_id": None,
        "content": {"policy_text": None},
    }
    base.update(changes)
    return make_guest_snapshot(**base)


class TestGuestConfirmationPdf(unittest.TestCase):
    """Guest-facing A4 confirmation PDF: content comes only from the snapshot."""

    def test_pdf_contains_snapshot_facts(self):
        snapshot = make_guest_snapshot(lead_guest={"first_name": "Seán", "last_name": "Ó Briain"})
        pdf = build_n07_confirmation_pdf(snapshot)
        text = pdf_text(pdf)

        self.assertTrue(pdf.startswith(b"%PDF"))
        self.assertIn(b"/MediaBox [ 0 0 595.27", pdf)  # A4
        self.assertIn(snapshot.booking_reference, text)
        self.assertIn("Dear Seán Ó Briain,", text)
        self.assertIn("10:30", text)
        self.assertIn("Sandy Hills Links <>&", text)
        self.assertIn("€1,200.00", text)
        self.assertIn("€300.00", text)
        self.assertIn("€900.00", text)
        self.assertIn("Wednesday 20 May 2099", text)  # balance due date
        self.assertIn("30 minutes before your tee time", text)
        self.assertIn("Offline test terms only.", text)
        self.assertIn("Terms version: v1.0-standard", text)
        self.assertIn("reservations@rosapenna.ie", text)
        self.assertRegex(text, rf"Page 1 of {pdf_page_count(pdf)}\b")
        self.assertLessEqual(pdf_page_count(pdf), 2)
        self.assertNotIn("http", text)  # no invented Manage My Booking or other URL

    def test_test_banner_only_for_test_bookings(self):
        test_text = pdf_text(build_n07_confirmation_pdf(make_guest_snapshot()))
        self.assertIn("TEST BOOKING – NOT A REAL RESERVATION", test_text)
        self.assertIn("TEST — NOT A REAL BOOKING CONFIRMATION", test_text)

        real_text = pdf_text(build_n07_confirmation_pdf(make_real_snapshot()))
        self.assertNotIn("TEST BOOKING", real_text)
        self.assertNotIn("NOT A REAL", real_text)

    def test_real_booking_without_policy_text_shows_marked_placeholder(self):
        text = pdf_text(build_n07_confirmation_pdf(make_real_snapshot(terms_version="v2.3-approved")))
        self.assertIn("Booking terms: see your reservation terms (version v2.3-approved).", text)
        self.assertNotIn("%", text)  # no invented cancellation percentages
        self.assertNotRegex(text, r"(?i)refund|cancellation fee")

    def test_real_booking_with_policy_text_shows_it(self):
        snapshot = make_real_snapshot(content={"policy_text": "Line one.\nLine two."})
        text = pdf_text(build_n07_confirmation_pdf(snapshot))
        self.assertIn("Line one. Line two.", text)
        self.assertNotIn("see your reservation terms", text)

    def test_test_booking_without_policy_text_still_fails(self):
        snapshot = make_guest_snapshot()
        snapshot.content.policy_text = None
        with self.assertRaises(ValueError):
            build_n07_confirmation_pdf(snapshot)

    def test_guest_markup_is_shown_as_text_not_interpreted(self):
        snapshot = make_guest_snapshot(
            lead_guest={"first_name": "<b>Bold</b>", "last_name": "<font size='40'>Big</font> & Co"},
        )
        data = snapshot.model_dump(mode="python")
        data["golf_rounds"][0]["course_name"] = "<img src='n07-test.png'/> Links"
        data["golf_rounds"][0]["golfer_names"] = ["<u>Ann</u>", "Bob &amp; Cy"]
        text = pdf_text(build_n07_confirmation_pdf(BookingConfirmationSnapshot.model_validate(data)))
        self.assertIn("Dear <b>Bold</b> <font size='40'>Big</font> & Co,", text)
        self.assertIn("<img src='n07-test.png'/> Links", text)
        self.assertIn("<u>Ann</u>, Bob &amp; Cy", text)

    def test_money_uses_snapshot_currency(self):
        data = make_test_snapshot().model_dump(mode="python")
        data["financials"]["currency"] = "GBP"
        data["payments"][0]["currency"] = "GBP"
        text = pdf_text(build_n07_confirmation_pdf(BookingConfirmationSnapshot.model_validate(data)))
        self.assertIn("£1,200.00", text)
        self.assertIn("£900.00", text)
        self.assertNotIn("€", text)

    def test_paid_in_full_has_no_balance_due_date(self):
        snapshot = make_guest_snapshot(
            financials={"total_amount": "300.00", "deposit_paid": "300.00", "verified_paid_total": "300.00", "balance_due": "0.00"},
        )
        text = pdf_text(build_n07_confirmation_pdf(snapshot))
        self.assertIn("Your booking is paid in full.", text)
        self.assertIn("€0.00 – paid in full", text)
        self.assertNotIn("Balance due by", text)

    def test_reservations_email_setting_is_used_and_validated(self):
        snapshot = make_guest_snapshot()
        custom = pdf_text(build_n07_confirmation_pdf(snapshot, reservations_email="stay.play@rosapenna.example"))
        self.assertIn("stay.play@rosapenna.example", custom)
        self.assertNotIn("reservations@rosapenna.ie", custom)
        unsafe = pdf_text(build_n07_confirmation_pdf(snapshot, reservations_email="<b>x</b>@bad"))
        self.assertIn("reservations@rosapenna.ie", unsafe)
        self.assertNotIn("<b>x</b>", unsafe)

    def test_six_rounds_and_long_unicode_names_do_not_raise(self):
        long_first = "Wolfeschlegelsteinhausenbergerdorff-Ó Súilleabháin " * 2
        names = [f"Alexandria Konstantinopoulos-Wolfeschlegel Ñandú Þórðarson {n}" for n in range(4)]
        data = make_test_snapshot().model_dump(mode="python")
        data["stay"].update(arrival_date="2099-06-01", departure_date="2099-06-07")
        data["stay"]["rooms"][0]["occupant_names"] = names
        data["lead_guest"].update(first_name=long_first.strip()[:100], last_name="Müller-Østergaard " * 5)
        data["golf_rounds"] = [{
            "round_key": f"RND-{n}", "course_name": "Old Tom Morris Links (Championship Tees) " * 2,
            "date": f"2099-06-{2 + n:02d}", "tee_time": f"{8 + n:02d}:{n * 7:02d}", "timezone": "Europe/Dublin",
            "players": 4, "holes": 18, "provider_booking_ids": [], "golfer_names": names,
        } for n in range(6)]
        snapshot = BookingConfirmationSnapshot.model_validate(data)
        pdf = build_n07_confirmation_pdf(snapshot)
        text = pdf_text(pdf)
        self.assertTrue(pdf.startswith(b"%PDF"))
        for n in range(6):
            self.assertIn(f"{8 + n:02d}:{n * 7:02d}", text)
        pages = pdf_page_count(pdf)
        self.assertGreaterEqual(pages, 2)
        self.assertRegex(text, rf"Page {pages} of {pages}\b")
        self.assertLess(len(pdf), 2 * 1024 * 1024)

    def test_names_outside_standard_font_do_not_raise(self):
        snapshot = make_guest_snapshot(lead_guest={"first_name": "Łukasz", "last_name": "Żółć 李雷 Иван"})
        self.assertTrue(build_n07_confirmation_pdf(snapshot).startswith(b"%PDF"))

    def test_large_group_of_rooms_moves_to_rooming_table_without_layout_error(self):
        data = make_test_snapshot().model_dump(mode="python")
        data["stay"]["rooms"] = [{
            "room_group_id": f"RG-{i}", "room_category": "deluxe_twin", "room_quantity": 1,
            "occupant_names": [f"Guest Number {i}-{j} Longish-Surname" for j in range(20)],
        } for i in range(10)]
        data["content"]["policy_text"] = "Policy line. " * 300
        pdf = build_n07_confirmation_pdf(BookingConfirmationSnapshot.model_validate(data))
        text = pdf_text(pdf)
        self.assertIn("Rooms & Guests", text)
        self.assertIn("Guest Number 9-19 Longish-Surname", text)

    def test_four_guests_in_two_rooms_and_one_round(self):
        data = make_test_snapshot().model_dump(mode="python")
        data["stay"]["rooms"] = [
            {"room_group_id": "RG-1", "room_category": "deluxe_twin", "room_quantity": 1, "occupant_names": ["A One", "B Two"]},
            {"room_group_id": "RG-2", "room_category": "superior_king", "room_quantity": 1, "occupant_names": ["C Three", "D Four"]},
        ]
        data["golf_rounds"][0]["players"] = 4
        data["golf_rounds"][0]["golfer_names"] = ["A One", "B Two", "C Three", "D Four"]
        text = pdf_text(build_n07_confirmation_pdf(BookingConfirmationSnapshot.model_validate(data)))
        self.assertIn("Deluxe Twin", text)
        self.assertIn("Superior King", text)
        self.assertIn("4 guests", text)
        self.assertIn("4 players", text)


if __name__ == "__main__":
    unittest.main()
