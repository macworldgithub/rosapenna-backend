import datetime as dt
import json
import unittest

import mongomock

from rosapenna_backend.services.pre_arrival_report import report
from rosapenna_backend.services.pre_arrival_rooming import SandboxRooming
from scripts.run_n10_sandbox import create_fixture


class StaffReportTests(unittest.TestCase):
    def setUp(self):
        self.client = mongomock.MongoClient(tz_aware=True)
        self.db = self.client.rosapenna_n10_test_sandbox
        self.rooming = SandboxRooming(self.db)
        create_fixture(self.rooming)
        self.row = self.db.rooming_bookings.find_one()
        self.now = dt.datetime.now(dt.UTC)

    def update(self, **fields):
        self.db.rooming_bookings.update_one({"_id": self.row["_id"]}, {"$set": fields})

    def test_filters_and_private_fields(self):
        rows = report(self.db, self.now)["cases"]
        self.assertEqual({row["status"] for row in rows}, {"unpaid", "missing"})
        self.assertNotIn("example.invalid", json.dumps(rows))
        self.assertNotIn("lead_version", json.dumps(rows))
        due = dt.date.fromisoformat(self.row["n12_test_balance"]["due"])
        filtered = report(self.db, self.now, status="unpaid", due_from=due, due_to=due)["cases"]
        self.assertEqual(len(filtered), 1)
        self.assertEqual(filtered[0]["remaining_minor"], 37500)
        self.assertTrue(filtered[0]["source_updated_at"])
        self.assertFalse(report(self.db, self.now, arrival_to=self.now.date())["cases"])

    def test_late_unverified_and_failed_pending_accepted_are_distinct(self):
        self.update(**{
            "n12_test_balance.due": (self.now.date() - dt.timedelta(days=1)).isoformat(),
            "caddy_history": [
                {"round_id": state, "golfers": ["guest-a"], "recorded_at": self.now.isoformat(),
                 "handoff_status": state, "notes": "private note"}
                for state in ("not_sent", "accepted", "failed", "unknown")
            ],
        })
        rows = report(self.db, self.now)["cases"]
        self.assertEqual({row["status"] for row in rows},
                         {"overdue", "late_booking", "missing", "pending", "accepted", "failed", "unknown"})
        self.assertNotIn("private note", json.dumps(rows))
        self.update(n12_test_balance=None)
        rows = report(self.db, self.now, status="unknown")["cases"]
        self.assertEqual({row["kind"] for row in rows}, {"balance", "caddy"})

    def test_database_and_booking_scope(self):
        with self.assertRaises(ValueError):
            report(self.client.production, self.now)
        self.update(synthetic=False)
        self.assertFalse(report(self.db, self.now)["cases"])
        self.update(synthetic=True, cleanup_at=self.now - dt.timedelta(hours=1))
        self.assertFalse(report(self.db, self.now)["cases"])

    def test_invalid_history_is_visible_without_losing_balance_results(self):
        self.update(history=[None], caddy_history=None)

        cases = report(self.db, self.now)["cases"]

        self.assertEqual(
            {(case["kind"], case["status"]) for case in cases},
            {("balance", "unpaid"), ("rooming", "unknown"), ("caddy", "unknown")},
        )

    def test_paid_and_recorded_rooming_need_no_follow_up(self):
        fixture = self.row["fixture"]
        snapshot = {"occupants": [{
            "id": slot["id"], "group_id": group,
            "first_name": "Sample", "last_name": "Guest", "start": slot["start"], "end": slot["end"],
        } for slot, group in zip(fixture["slots"], ("group-a", "group-a", "group-b"))], "sharing_requests": []}
        self.update(rooming=snapshot, history=[{"recorded": snapshot}],
                    **{"n12_test_balance.verified_paid_minor": 50000})
        self.assertFalse(report(self.db, self.now)["cases"])
        self.update(history=[])
        self.assertEqual(report(self.db, self.now)["cases"][0]["status"], "unknown")
