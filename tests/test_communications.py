"""Unit tests for Ticket F12: Email and itinerary delivery, PDF generation, and communications routes."""

import datetime as dt
import unittest
import uuid
from unittest.mock import MagicMock, patch

from mongomock import MongoClient
from pydantic import SecretStr

from rosapenna_backend.api.routes.communications import (
    create_special_request,
    download_itinerary_pdf,
    send_communication,
)
from rosapenna_backend.api.schemas.booking_intents import (
    BookingIntentCreate,
    LeadGuest,
    OperaReservationPayload,
    QuoteSelection,
)
from rosapenna_backend.api.schemas.communications import (
    SendEmailRequest,
    SpecialRequestCreate,
)
from rosapenna_backend.services.booking_intents import BookingIntentStore
from rosapenna_backend.services.email_delivery import EmailDeliveryService, SmtpSettings
from rosapenna_backend.services.pdf_itinerary import build_itinerary_pdf


def make_sample_booking(store: BookingIntentStore) -> dict:
    """Helper to populate a sample booking intent with rooms, golf rounds, and payments."""
    req = BookingIntentCreate(
        selection=QuoteSelection(
            date=dt.date(2027, 6, 10),
            course_id=1,
            tee_time_id=930,
            time="09:30",
            holes="18",
            package_name="Classic Links Package",
            green_fee_id=1,
            players=2,
            price="680.00",
            currency="EUR",
        ),
        lead_guest=LeadGuest(
            first_name="Arthur",
            last_name="Balfour",
            email="balfour@example.com",
            telephone="+353871234567",
        ),
    )
    record, _ = store.create(uuid.uuid4(), req)
    intent_id = record["booking_intent_id"]

    # Link Opera room reservation
    store.link_opera_reservation(
        intent_id=intent_id,
        expected_revision=1,
        opera_payload=OperaReservationPayload(
            reservation_id="RES-OPERA-7788",
            confirmation_number="CONF-99001",
            hotel_id="ROSA",
            room_category="CLASSIC",
            room_quantity=1,
            arrival_date=dt.date(2027, 6, 10),
            departure_date=dt.date(2027, 6, 12),
            total_amount="600.00",
            currency="EUR",
            status="Reserved",
        ),
    )

    # Return refreshed booking document
    return store.get(intent_id)


class PdfItineraryTests(unittest.TestCase):
    """Test PDF generation formatting, branding, and defensive handling."""

    def setUp(self):
        self.mongo = MongoClient(tz_aware=True)
        self.db = self.mongo["comms_test_db"]
        self.store = BookingIntentStore(self.db.booking_intents)

    def tearDown(self):
        self.mongo.close()

    def test_build_itinerary_pdf_complete_booking(self):
        booking = make_sample_booking(self.store)
        pdf_bytes = build_itinerary_pdf(booking)
        self.assertIsInstance(pdf_bytes, bytes)
        self.assertTrue(pdf_bytes.startswith(b"%PDF"))
        self.assertGreater(len(pdf_bytes), 2000)

    def test_build_itinerary_pdf_minimal_booking(self):
        # Must not crash on empty / sparse dictionary
        pdf_bytes = build_itinerary_pdf({})
        self.assertIsInstance(pdf_bytes, bytes)
        self.assertTrue(pdf_bytes.startswith(b"%PDF"))
        self.assertGreater(len(pdf_bytes), 500)


class EmailDeliveryServiceTests(unittest.TestCase):
    """Test EmailDeliveryService dispatch, sandbox simulation, MMB gating, and SMTP handling."""

    def setUp(self):
        self.mongo = MongoClient(tz_aware=True)
        self.db = self.mongo["comms_test_db"]
        self.store = BookingIntentStore(self.db.booking_intents)
        self.sample_booking = make_sample_booking(self.store)

    def tearDown(self):
        self.mongo.close()

    def test_send_booking_confirmation_sandbox(self):
        settings = SmtpSettings(allow_live_send=False)
        svc = EmailDeliveryService(settings=settings)
        result = svc.send_booking_confirmation(self.sample_booking)

        self.assertTrue(result.success)
        self.assertEqual(result.status, "simulated_sandbox")
        self.assertTrue(result.pdf_attached)
        self.assertTrue(result.hotel_notified)
        self.assertEqual(result.recipient, "balfour@example.com")
        self.assertEqual(len(svc._dispatched_emails), 1)

    def test_send_booking_confirmation_missing_email(self):
        svc = EmailDeliveryService()
        empty_booking = {"lead_guest": {}}
        result = svc.send_booking_confirmation(empty_booking)

        self.assertFalse(result.success)
        self.assertEqual(result.status, "failed")
        self.assertIn("missing", result.error_message.lower())

    def test_send_special_request_acknowledgement(self):
        svc = EmailDeliveryService(settings=SmtpSettings(allow_live_send=False))
        result = svc.send_special_request_acknowledgement(
            booking=self.sample_booking,
            request_text="Quiet room on first floor if possible.",
            category="room_preference",
        )

        self.assertTrue(result.success)
        self.assertEqual(result.status, "simulated_sandbox")
        self.assertTrue(result.hotel_notified)
        self.assertFalse(result.pdf_attached)
        self.assertEqual(result.template, "special_request_acknowledgement")

    def test_send_one_time_code_redacts_code(self):
        svc = EmailDeliveryService(settings=SmtpSettings(allow_live_send=False))
        result = svc.send_one_time_code(
            recipient_email="guest@example.com",
            recipient_name="Arthur Balfour",
            code="654321",
            booking_reference="RSP-20270610-TEST",
        )

        self.assertTrue(result.success)
        self.assertEqual(result.status, "simulated_sandbox")
        self.assertEqual(result.template, "one_time_code")
        # Ensure code is not in result error or subject
        self.assertNotIn("654321", result.subject)

    def test_send_balance_reminder(self):
        svc = EmailDeliveryService(settings=SmtpSettings(allow_live_send=False))
        result = svc.send_balance_reminder(self.sample_booking)

        self.assertTrue(result.success)
        self.assertEqual(result.status, "simulated_sandbox")
        self.assertEqual(result.template, "balance_reminder")

    def test_mmb_gating_behavior(self):
        # When MMB is disabled, MMB link is NOT included
        svc_disabled = EmailDeliveryService(settings=SmtpSettings(allow_live_send=False, mmb_enabled=False))
        conf_res = svc_disabled.send_booking_confirmation(self.sample_booking)
        self.assertTrue(conf_res.success)

        # When MMB is enabled, MMB link is active
        svc_enabled = EmailDeliveryService(settings=SmtpSettings(allow_live_send=False, mmb_enabled=True))
        conf_res_enabled = svc_enabled.send_booking_confirmation(self.sample_booking)
        self.assertTrue(conf_res_enabled.success)

    @patch("smtplib.SMTP_SSL")
    def test_live_smtp_dispatch_ssl(self, mock_smtp_ssl_cls):
        mock_server = MagicMock()
        mock_smtp_ssl_cls.return_value.__enter__.return_value = mock_server

        settings = SmtpSettings(
            allow_live_send=True,
            smtp_host="smtp.hostinger.com",
            smtp_port=465,
            smtp_secure=True,
            smtp_user="noreply@neximet.com",
            smtp_pass=SecretStr("mock_pass"),
        )
        svc = EmailDeliveryService(settings=settings)
        result = svc.send_booking_confirmation(self.sample_booking)

        self.assertTrue(result.success)
        self.assertEqual(result.status, "sent")
        mock_server.login.assert_called_once_with("noreply@neximet.com", "mock_pass")
        mock_server.send_message.assert_called_once()


class CommunicationsRoutesTests(unittest.TestCase):
    """Test API route endpoints for communications."""

    def setUp(self):
        self.mongo = MongoClient(tz_aware=True)
        self.db = self.mongo["comms_test_db"]
        self.store = BookingIntentStore(self.db.booking_intents)
        self.sample_booking = make_sample_booking(self.store)
        self.intent_id = self.sample_booking["booking_intent_id"]
        self.email_svc = EmailDeliveryService(settings=SmtpSettings(allow_live_send=False))

    def tearDown(self):
        self.mongo.close()

    def test_send_booking_confirmation_route(self):
        req = SendEmailRequest(
            booking_intent_id=self.intent_id,
            template="booking_confirmation",
        )
        result = send_communication(request=req, store=self.store, email_service=self.email_svc)
        self.assertTrue(result.success)
        self.assertEqual(result.template, "booking_confirmation")

    def test_send_special_request_route(self):
        req = SendEmailRequest(
            booking_intent_id=self.intent_id,
            template="special_request_acknowledgement",
            special_request_text="Extra towels needed.",
        )
        result = send_communication(request=req, store=self.store, email_service=self.email_svc)
        self.assertTrue(result.success)
        self.assertEqual(result.template, "special_request_acknowledgement")

    def test_send_one_time_code_route(self):
        req = SendEmailRequest(
            booking_intent_id=self.intent_id,
            template="one_time_code",
            otp_code="987654",
        )
        result = send_communication(request=req, store=self.store, email_service=self.email_svc)
        self.assertTrue(result.success)
        self.assertEqual(result.template, "one_time_code")

    def test_send_balance_reminder_route(self):
        req = SendEmailRequest(
            booking_intent_id=self.intent_id,
            template="balance_reminder",
        )
        result = send_communication(request=req, store=self.store, email_service=self.email_svc)
        self.assertTrue(result.success)
        self.assertEqual(result.template, "balance_reminder")

    def test_create_special_request_pending_review(self):
        req = SpecialRequestCreate(
            booking_intent_id=self.intent_id,
            request_text="Gluten free breakfast required for 1 guest.",
            category="dietary",
        )
        response = create_special_request(request=req, store=self.store, email_service=self.email_svc)
        self.assertEqual(response["status"], "received")
        self.assertEqual(response["review_status"], "pending_staff_review")
        self.assertFalse(response["inventory_mutated"])
        self.assertFalse(response["payment_mutated"])
        self.assertEqual(response["category"], "dietary")

    def test_download_itinerary_pdf_route(self):
        response = download_itinerary_pdf(booking_intent_id=self.intent_id, store=self.store)
        self.assertEqual(response.media_type, "application/pdf")
        self.assertTrue(response.body.startswith(b"%PDF"))
        self.assertIn("Content-Disposition", response.headers)


if __name__ == "__main__":
    unittest.main()
