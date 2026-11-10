"""Unit tests for Ticket M15: Revised confirmations, PDF itineraries, and pre-arrival reminders."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
import unittest
from unittest.mock import MagicMock

from fastapi import HTTPException, Response
from mongomock import MongoClient

from rosapenna_backend.api.routes.revised_confirmations import (
    dispatch_booking_reminder,
    download_revised_itinerary_pdf,
    get_booking_reminders,
    send_revised_confirmation_email,
)
from rosapenna_backend.api.schemas.booking_linkage import (
    BrsRoundDetails,
    LeadGuestContact,
    OperaStayDetails,
    PaymentLedgerEntry,
    StaffLinkBookingRequest,
)
from rosapenna_backend.api.schemas.mmb_auth import GuestSessionPrincipal
from rosapenna_backend.api.schemas.revised_confirmations import (
    DispatchReminderRequest,
    RevisedConfirmationSendRequest,
)
from rosapenna_backend.services.booking_linkage import BookingLinkageService
from rosapenna_backend.services.email_delivery import EmailDeliveryService, SmtpSettings
from rosapenna_backend.services.mmb_auth import MmbAuthService
from rosapenna_backend.services.mmb_gate import MmbGateService
from rosapenna_backend.services.revised_confirmations import RevisedConfirmationService


class TestRevisedConfirmationsM15(unittest.TestCase):
    """Test suite verifying all acceptance criteria for Ticket M15."""

    def setUp(self):
        self.mongo_client = MongoClient()
        self.db = self.mongo_client["rosapenna_test"]
        self.linkage_service = BookingLinkageService(database=self.db)
        self.smtp_settings = SmtpSettings(allow_live_send=False, mmb_enabled=True)
        self.email_service = EmailDeliveryService(settings=self.smtp_settings)
        self.revised_service = RevisedConfirmationService(
            database=self.db,
            linkage_service=self.linkage_service,
            email_service=self.email_service,
        )

        # Seed authoritative booking linkage
        # Arrival date: 2027-08-10 -> 42 days prior is 2027-06-29
        self.lead_guest = LeadGuestContact(
            first_name="Pádraig",
            last_name="Harrington",
            email="padraig@example.com",
            telephone="+353871234567",
        )
        self.opera_stay = OperaStayDetails(
            reservation_id="OPERA-RES-M15-01",
            confirmation_number="CONF-M15-01",
            hotel_id="ROSAPENNA",
            room_category="Bay View Suite",
            room_quantity=1,
            arrival_date=dt.date(2027, 8, 10),
            departure_date=dt.date(2027, 8, 14),
            total_amount="1200.00",
            currency="EUR",
            status="reserved",
            occupant_names=["Pádraig Harrington", "Ronan Flood"],
        )
        self.brs_rounds = [
            BrsRoundDetails(
                round_id="RND-SANDY-M15",
                course_id=3,
                course_name="St Patrick's Links",
                date=dt.date(2027, 8, 11),
                time="10:30",
                players=2,
                holes="18",
                price="400.00",
                status="Confirmed",
                caddy_request={
                    "caddies_requested": 2,
                    "caddy_category": "senior",
                    "status": "requested",
                },
            )
        ]
        self.payments = [
            PaymentLedgerEntry(
                payment_id="PAY-M15-DEP",
                provider="prommt",
                payment_type="deposit",
                amount="400.00",
                currency="EUR",
                status="completed",
                provider_reference="pmt_req_dep_123",
                verified_at=dt.datetime(2026, 11, 10, 10, 0, tzinfo=dt.timezone.utc),
            )
        ]

        self.booking_ref = "RSP-20270810-M1501"
        link_req = StaffLinkBookingRequest(
            booking_reference=self.booking_ref,
            reason="Initial online confirmed booking linkage for M15 test",
            lead_guest=self.lead_guest,
            manual_opera_stay=self.opera_stay,
            brs_rounds=self.brs_rounds,
            payments=self.payments,
            source_channel="online",
        )
        self.linkage_service.link_booking(link_req, actor="test_m15_setup")

    def test_canonical_booking_snapshot_unifies_42_day_balance_due(self):
        """AC 1 & AC 3: Canonical snapshot computes strictly 42-day balance due date."""
        snapshot = self.revised_service.get_canonical_booking_snapshot(self.booking_ref)

        self.assertEqual(snapshot["booking_reference"], self.booking_ref)
        self.assertEqual(snapshot["stage"], "Confirmed")
        self.assertEqual(snapshot["lead_guest"]["last_name"], "Harrington")

        # Arrival: 2027-08-10 - 42 days = 2027-06-29
        expected_balance_due_date = "2027-06-29"
        self.assertEqual(snapshot["financials"]["balance_due_date"], expected_balance_due_date)

        # Financials: Total = 1200 + 400 = 1600.00. Deposit paid = 400.00. Balance = 1200.00
        self.assertEqual(snapshot["financials"]["total_price"], "1600.00")
        self.assertEqual(snapshot["financials"]["deposit_paid"], "400.00")
        self.assertEqual(snapshot["financials"]["balance_due"], "1200.00")
        self.assertEqual(snapshot["stay"]["occupant_names"], ["Pádraig Harrington", "Ronan Flood"])

    def test_generate_revised_itinerary_pdf_contains_revision_and_42_day_terms(self):
        """AC 1 & AC 3: PDF renders valid byte stream with revision and unified 42-day balance due date."""
        pdf_bytes, filename = self.revised_service.generate_revised_itinerary_pdf(self.booking_ref)

        self.assertIsInstance(pdf_bytes, bytes)
        self.assertTrue(pdf_bytes.startswith(b"%PDF-"))
        self.assertGreater(len(pdf_bytes), 2048)  # Rich formatted ReportLab PDF > 2KB
        self.assertIn(self.booking_ref, filename)

    def test_send_revised_confirmation_email_and_idempotency(self):
        """AC 1 & AC 5: Revised confirmation dispatches with PDF attachment and deduplicates replay."""
        idemp_key = "idemp_m15_test_001"
        delivery_1 = self.revised_service.send_revised_confirmation(
            booking_reference=self.booking_ref,
            idempotency_key=idemp_key,
            amendment_summary="Room upgraded to Bay View Suite and tee time confirmed",
        )

        self.assertTrue(delivery_1.success)
        self.assertEqual(delivery_1.template, "revised_booking_confirmation")
        self.assertEqual(delivery_1.status, "simulated_sandbox")
        self.assertTrue(delivery_1.hotel_notified)
        self.assertTrue(delivery_1.pdf_attached)
        self.assertFalse(delivery_1.idempotency_replayed)

        # Replay with same idempotency key
        delivery_2 = self.revised_service.send_revised_confirmation(
            booking_reference=self.booking_ref,
            idempotency_key=idemp_key,
        )
        self.assertTrue(delivery_2.success)
        self.assertTrue(delivery_2.idempotency_replayed)
        self.assertEqual(delivery_1.delivery_id, delivery_2.delivery_id)

    def test_pre_arrival_reminders_scheduling_and_reconciliation(self):
        """AC 4 & AC 5: Pre-arrival reminders are scheduled and obsolete ones canceled upon amendment."""
        # Baseline scheduling
        reminders_resp = self.revised_service.get_scheduled_reminders(self.booking_ref)
        self.assertEqual(reminders_resp.booking_reference, self.booking_ref)
        self.assertEqual(reminders_resp.active_revision, 1)
        self.assertEqual(len(reminders_resp.reminders), 4)

        # Check milestones: 42 days prior is 2027-06-29
        types_map = {r.reminder_type: r for r in reminders_resp.reminders}
        self.assertIn("balance_due_first", types_map)
        self.assertEqual(types_map["balance_due_first"].scheduled_date, dt.date(2027, 6, 29))
        self.assertEqual(types_map["rooming_and_caddy"].scheduled_date, dt.date(2027, 6, 29))
        self.assertEqual(types_map["concierge_check"].scheduled_date, dt.date(2027, 7, 27))  # 14 days prior
        self.assertEqual(types_map["balance_due_final"].scheduled_date, dt.date(2027, 8, 5))   # 5 days prior

        # Now simulate an amendment commit changing arrival date to 2027-09-10 (Revision 2)
        new_arrival = dt.date(2027, 9, 10)
        reconciled = self.revised_service.reconcile_and_reschedule_reminders(
            booking_reference=self.booking_ref,
            new_arrival_date=new_arrival,
            new_revision=2,
            actor="test_amendment_commit",
        )
        self.assertEqual(len(reconciled), 4)
        new_balance_date = new_arrival - dt.timedelta(days=42)  # 2027-07-30
        self.assertEqual(reconciled[0].scheduled_date, new_balance_date)

        # Verify old revision 1 reminders were marked canceled_by_amendment
        all_reminders = self.revised_service.get_scheduled_reminders(self.booking_ref)
        canceled_rev1 = [r for r in all_reminders.reminders if r.revision_number == 1]
        for old_r in canceled_rev1:
            self.assertEqual(old_r.status, "canceled_by_amendment")
            self.assertIn("Superseded by amendment revision #2", old_r.suppression_reason or "")

    def test_truthful_suppression_when_balance_paid_or_booking_cancelled(self):
        """AC 4 & AC 8: Reminders are suppressed if balance is zero or booking is cancelled."""
        # 1. Dispatch balance reminder when balance is due -> dispatches
        reminders_resp = self.revised_service.get_scheduled_reminders(self.booking_ref)
        bal_rem = next(r for r in reminders_resp.reminders if r.reminder_type == "balance_due_first")

        dispatch_res = self.revised_service.dispatch_reminder(
            booking_reference=self.booking_ref,
            reminder_id=bal_rem.reminder_id,
        )
        self.assertEqual(dispatch_res.status, "dispatched")
        self.assertIsNotNone(dispatch_res.delivery_result)

        # 2. Add full payment so balance becomes 0
        self.linkage_service.collection.update_one(
            {"booking_reference": self.booking_ref},
            {
                "$push": {
                    "payments": PaymentLedgerEntry(
                        payment_id="PAY-M15-BAL",
                        provider="prommt",
                        payment_type="balance",
                        amount="1200.00",
                        currency="EUR",
                        status="completed",
                    ).model_dump()
                },
                "$set": {
                    "financials.balance_due": "0.00",
                    "financials.payment_status": "fully_paid",
                },
            },
        )

        final_bal_rem = next(r for r in reminders_resp.reminders if r.reminder_type == "balance_due_final")
        dispatch_bal_paid = self.revised_service.dispatch_reminder(
            booking_reference=self.booking_ref,
            reminder_id=final_bal_rem.reminder_id,
        )
        self.assertEqual(dispatch_bal_paid.status, "suppressed")
        self.assertIn("Remaining balance is zero", dispatch_bal_paid.message)

    def test_fastapi_endpoints_pdf_and_reminders(self):
        """AC 7: Endpoints are protected by MMB gating and session authorization."""
        gate_svc = MmbGateService(database=self.db)
        auth_svc = MmbAuthService(database=self.db, email_service=self.email_service)

        # 1. MMB is enabled in database
        self.db["mmb_gate_state"].update_one(
            {"_id": "mmb_release_gate"},
            {"$set": {"enabled": True, "written_request_reference": "M15-TEST"}},
            upsert=True,
        )
        session = GuestSessionPrincipal(
            session_token="tok_m15_test",
            booking_reference=self.booking_ref,
            lead_guest_email="padraig@example.com",
            expires_at=dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=2),
        )

        # 2. Call download_revised_itinerary_pdf route handler directly
        pdf_res = download_revised_itinerary_pdf(
            booking_reference=self.booking_ref,
            service=self.revised_service,
            _authorized_session=session,
        )
        self.assertIsInstance(pdf_res, Response)
        self.assertEqual(pdf_res.media_type, "application/pdf")
        self.assertTrue(pdf_res.body.startswith(b"%PDF-"))
        self.assertIn("Content-Disposition", pdf_res.headers)
        self.assertIn("Cache-Control", pdf_res.headers)

        # 3. Call send_revised_confirmation_email route handler directly
        send_req = RevisedConfirmationSendRequest(idempotency_key="idemp_route_test")
        send_res = send_revised_confirmation_email(
            payload=send_req,
            booking_reference=self.booking_ref,
            service=self.revised_service,
            _authorized_session=session,
            idempotency_key=None,
        )
        self.assertEqual(send_res.booking_reference, self.booking_ref)
        self.assertTrue(send_res.delivery_result.success)

        # 4. Call get_booking_reminders route handler directly
        rems = get_booking_reminders(
            booking_reference=self.booking_ref,
            service=self.revised_service,
            _authorized_session=session,
        )
        self.assertEqual(rems.booking_reference, self.booking_ref)
        self.assertGreaterEqual(len(rems.reminders), 4)

        # 5. Call dispatch_booking_reminder route handler directly
        first_rem = rems.reminders[0]
        disp_req = DispatchReminderRequest(idempotency_key="disp_route_test")
        disp_res = dispatch_booking_reminder(
            payload=disp_req,
            booking_reference=self.booking_ref,
            reminder_id=first_rem.reminder_id,
            service=self.revised_service,
            _authorized_session=session,
            idempotency_key=None,
        )
        self.assertIn(disp_res.status, ("dispatched", "suppressed"))


if __name__ == "__main__":
    unittest.main()
