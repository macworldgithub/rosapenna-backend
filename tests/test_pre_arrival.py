"""Unit tests for Ticket N08 — Pure calculation, evidence validation, and message rendering."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
import unittest
from zoneinfo import ZoneInfo

from rosapenna_backend.api.schemas.pre_arrival import (
    EligibleBookingInput,
    PreArrivalPolicyDoc,
)
from rosapenna_backend.services.email_delivery import render_n08_pre_arrival_message
from rosapenna_backend.services.pre_arrival import (
    PreArrivalPolicyError,
    PreArrivalService,
    _resolve_send_time,
)


class TestPreArrivalPureLogic(unittest.TestCase):
    """Test suite covering scheduling rules, timezones, DST, and eligibility."""

    def setUp(self):
        self.service = PreArrivalService(environment="test")
        self.fixed_now = dt.datetime(2027, 5, 1, 8, 0, 0, tzinfo=dt.timezone.utc)
        self.arrival_date = dt.date(2027, 7, 1)  # 61 days ahead of fixed_now
        self.departure_date = dt.date(2027, 7, 5)

        self.approved_policy_42 = PreArrivalPolicyDoc(
            policy_id="pol-rosapenna-42",
            version=1,
            environment="test",
            approval_status="approved",
            approved_by="res_manager",
            approved_at=dt.datetime(2026, 11, 16, 12, 0, tzinfo=dt.timezone.utc),
            source="rosapenna_scope_v1.1",
            timezone="Europe/Dublin",
            local_send_time="09:00",
            balance_request_lead_days=42,
            balance_due_lead_days=42,
            rooming_caddy_lead_days=42,
            late_booking_action="immediate",
        )

        self.approved_policy_56 = PreArrivalPolicyDoc(
            policy_id="pol-rosapenna-56",
            version=1,
            environment="test",
            approval_status="approved",
            approved_by="res_manager",
            approved_at=dt.datetime(2026, 11, 16, 12, 0, tzinfo=dt.timezone.utc),
            source="rosapenna_scope_v1.1",
            timezone="Europe/Dublin",
            local_send_time="09:00",
            balance_request_lead_days=56,
            balance_due_lead_days=42,
            rooming_caddy_lead_days=42,
            late_booking_action="adjusted",
            late_booking_offset_hours=4,
        )

        self.unresolved_policy = PreArrivalPolicyDoc(
            policy_id="pol-unresolved",
            version=1,
            environment="test",
            approval_status="unresolved",
            source="unapproved_draft",
            timezone="Europe/Dublin",
            local_send_time="09:00",
            balance_request_lead_days=None,  # Unresolved 8 vs 6 weeks
            balance_due_lead_days=42,
            rooming_caddy_lead_days=42,
            late_booking_action="unresolved",
        )

    def _make_booking(
        self,
        balance_due: Decimal = Decimal("1200.00"),
        source_channel: str = "online",
        rooming_completed: bool = False,
        caddy_completed: bool = False,
    ) -> EligibleBookingInput:
        return EligibleBookingInput(
            booking_id="RSP-20270701-TEST01",
            booking_reference="RSP-20270701-TEST01",
            property_id="ROSA",
            source_channel=source_channel,  # type: ignore[arg-type]
            revision=1,
            arrival_date=self.arrival_date,
            departure_date=self.departure_date,
            confirmed_at=self.fixed_now,
            confirmation_status="confirmed",
            lead_name="Pádraig Harrington",
            lead_email="padraig@guest.invalid",
            total_amount=Decimal("1600.00"),
            verified_paid_total=Decimal("400.00"),
            currency="EUR",
            balance_due=balance_due,
            rooming_completed=rooming_completed,
            caddy_completed=caddy_completed,
            evidence_timestamp=self.fixed_now,
        )

    def test_milestone_lead_days_42_versus_56(self):
        """AC 5: Verify 42-day and 56-day balance requests differ while balance due remains 42 days."""
        booking = self._make_booking()

        # 42-day policy: 2027-07-01 minus 42 days = 2027-05-20
        milestones_42 = self.service.calculate_milestones(booking, self.approved_policy_42, now=self.fixed_now)
        bal_42 = next(m for m in milestones_42 if m["reminder_type"] == "balance_due_first")
        self.assertEqual(bal_42["scheduled_date"], "2027-05-20")
        self.assertEqual(bal_42["balance_due_date"], "2027-05-20")
        self.assertEqual(bal_42["status"], "scheduled")

        # 56-day policy: 2027-07-01 minus 56 days = 2027-05-06
        milestones_56 = self.service.calculate_milestones(booking, self.approved_policy_56, now=self.fixed_now)
        bal_56 = next(m for m in milestones_56 if m["reminder_type"] == "balance_due_first")
        self.assertEqual(bal_56["scheduled_date"], "2027-05-06")
        self.assertEqual(bal_56["balance_due_date"], "2027-05-20")  # Baseline due remains 42 days
        self.assertEqual(bal_56["status"], "scheduled")

    def test_unresolved_timing_blocks_activation(self):
        """AC 1: Missing timing configuration creates blocked_dependency jobs without send."""
        booking = self._make_booking()
        milestones = self.service.calculate_milestones(booking, self.unresolved_policy, now=self.fixed_now)
        bal_job = next(m for m in milestones if m["reminder_type"] == "balance_due_first")
        self.assertEqual(bal_job["status"], "blocked_dependency")
        self.assertEqual(bal_job["reason_code"], "UNRESOLVED_BALANCE_TIMING")

    def test_channel_agnostic_scheduling(self):
        """AC 3: Online, phone, and email bookings produce identical scheduling."""
        for channel in ("online", "phone", "email"):
            booking = self._make_booking(source_channel=channel)
            milestones = self.service.calculate_milestones(booking, self.approved_policy_42, now=self.fixed_now)
            self.assertEqual(len(milestones), 2)
            types = [m["reminder_type"] for m in milestones]
            self.assertEqual(types, ["balance_due_first", "rooming_and_caddy"])
            self.assertEqual(milestones[0]["scheduled_date"], "2027-05-20")
            self.assertEqual(milestones[1]["scheduled_date"], "2027-05-20")

    def test_paid_balance_suppression_preserves_rooming_caddy(self):
        """AC 6: Settling balance suppresses balance reminder but keeps rooming/caddy request."""
        booking = self._make_booking(balance_due=Decimal("0.00"))
        milestones = self.service.calculate_milestones(booking, self.approved_policy_42, now=self.fixed_now)

        bal_job = next(m for m in milestones if m["reminder_type"] == "balance_due_first")
        rc_job = next(m for m in milestones if m["reminder_type"] == "rooming_and_caddy")

        self.assertEqual(bal_job["status"], "suppressed")
        self.assertEqual(bal_job["reason_code"], "BALANCE_ALREADY_PAID")

        # Rooming & caddy must remain scheduled!
        self.assertEqual(rc_job["status"], "scheduled")
        self.assertIsNone(rc_job["reason_code"])

    def test_rooming_and_caddy_completion_suppression(self):
        """AC 6: Completed rooming list and caddy requirements suppresses combined request."""
        booking = self._make_booking(rooming_completed=True, caddy_completed=True)
        milestones = self.service.calculate_milestones(booking, self.approved_policy_42, now=self.fixed_now)
        rc_job = next(m for m in milestones if m["reminder_type"] == "rooming_and_caddy")
        self.assertEqual(rc_job["status"], "suppressed")
        self.assertEqual(rc_job["reason_code"], "ROOMING_CADDY_ALREADY_COMPLETED")

    def test_late_booking_immediate_and_adjusted_policies(self):
        """AC 5: Bookings inside the 42-day window follow approved late-booking actions."""
        # Booking confirmed only 10 days before arrival (2027-06-21)
        late_now = dt.datetime(2027, 6, 21, 10, 0, 0, tzinfo=dt.timezone.utc)
        booking = self._make_booking()
        booking.confirmed_at = late_now

        # 1. Immediate policy
        milestones_imm = self.service.calculate_milestones(booking, self.approved_policy_42, now=late_now)
        rc_imm = next(m for m in milestones_imm if m["reminder_type"] == "rooming_and_caddy")
        self.assertEqual(rc_imm["status"], "scheduled")
        self.assertEqual(rc_imm["reason_code"], "LATE_BOOKING_IMMEDIATE")
        self.assertEqual(rc_imm["due_at"], late_now)

        # 2. Adjusted policy (offset by 4 hours)
        milestones_adj = self.service.calculate_milestones(booking, self.approved_policy_56, now=late_now)
        rc_adj = next(m for m in milestones_adj if m["reminder_type"] == "rooming_and_caddy")
        self.assertEqual(rc_adj["status"], "scheduled")
        self.assertEqual(rc_adj["reason_code"], "LATE_BOOKING_ADJUSTED")
        expected_adj_due = late_now + dt.timedelta(hours=4)
        self.assertEqual(rc_adj["due_at"], expected_adj_due)

        # 3. Unresolved late policy -> blocked_dependency
        milestones_unres = self.service.calculate_milestones(booking, self.unresolved_policy, now=late_now)
        rc_unres = next(m for m in milestones_unres if m["reminder_type"] == "rooming_and_caddy")
        self.assertEqual(rc_unres["status"], "blocked_dependency")
        self.assertEqual(rc_unres["reason_code"], "UNRESOLVED_LATE_BOOKING_POLICY")

    def test_arrival_date_cutoff_suppresses_send(self):
        """AC 6: Jobs whose milestone is evaluated on or after arrival date are suppressed."""
        post_arrival = dt.datetime(2027, 7, 2, 10, 0, 0, tzinfo=dt.timezone.utc)
        booking = self._make_booking()
        milestones = self.service.calculate_milestones(booking, self.approved_policy_42, now=post_arrival)
        for m in milestones:
            self.assertEqual(m["status"], "suppressed")
            self.assertEqual(m["reason_code"], "ARRIVAL_DATE_ELAPSED")

    def test_timezone_and_dst_transition_resolution(self):
        """Verify Dublin local send time handles Irish Summer Time (IST) offset correctly."""
        dublin_tz = ZoneInfo("Europe/Dublin")

        # Summer date (IST = UTC+1): 2027-06-01 at 09:00 Dublin = 08:00 UTC
        summer_date = dt.date(2027, 6, 1)
        due_summer = _resolve_send_time(summer_date, "09:00", dublin_tz)
        self.assertEqual(due_summer.hour, 8)
        self.assertEqual(due_summer.minute, 0)
        self.assertEqual(due_summer.tzinfo, dt.timezone.utc)

        # Winter date (GMT = UTC+0): 2027-01-15 at 09:00 Dublin = 09:00 UTC
        winter_date = dt.date(2027, 1, 15)
        due_winter = _resolve_send_time(winter_date, "09:00", dublin_tz)
        self.assertEqual(due_winter.hour, 9)
        self.assertEqual(due_winter.minute, 0)
        self.assertEqual(due_winter.tzinfo, dt.timezone.utc)

    def test_email_rendering_and_escaping(self):
        """AC 8: Content escapes HTML, includes test banner, and calculates stable hash."""
        booking = self._make_booking()
        booking.lead_name = "<b>Pádraig & <script>alert(1)</script></b>"

        subject, plain, html_body, mime_bytes, msg_id, msg_hash = render_n08_pre_arrival_message(
            booking=booking,
            reminder_type="balance_due_first",
            policy=self.approved_policy_42,
            attempt_id="att-test-01",
            action_url="https://rosapenna.ie/pay/balance?id=123",
            mmb_enabled=False,
            is_test=True,
        )

        self.assertIn("TEST — NOT A REAL EMAIL", subject)
        self.assertIn("TEST — NOT A REAL EMAIL", html_body)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", html_body)
        self.assertNotIn("<script>alert(1)</script>", html_body)
        self.assertIn("https://rosapenna.ie/pay/balance?id=123", html_body)
        self.assertIn("EUR 1200.00", plain)
        self.assertGreater(len(msg_hash), 32)
        self.assertTrue(mime_bytes.startswith(b"Content-Type: multipart/alternative;"))

    def test_crlf_rejection_in_headers(self):
        """Security: Rejects carriage return or line feed injections in email headers."""
        booking = self._make_booking()
        booking.lead_email = "padraig@guest.invalid\r\nBcc: evil@attacker.invalid"

        with self.assertRaises(ValueError):
            render_n08_pre_arrival_message(
                booking=booking,
                reminder_type="balance_due_first",
                policy=self.approved_policy_42,
                attempt_id="att-test-02",
                is_test=True,
            )


if __name__ == "__main__":
    unittest.main()
