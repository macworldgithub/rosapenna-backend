"""Unit tests for Ticket N08 — Storage, atomic claims, reconciliation, and repair scans."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
import tempfile
import unittest
from pathlib import Path
from mongomock import MongoClient

from rosapenna_backend.api.schemas.pre_arrival import (
    PreArrivalPolicyDoc,
)
from rosapenna_backend.services.email_delivery import (
    RecordingConfirmationTransport,
    TransportSubmitOutcome,
)
from rosapenna_backend.services.pre_arrival import (
    PreArrivalConflictError,
    PreArrivalPolicyError,
    PreArrivalService,
)


class TestPreArrivalStorageAndWorker(unittest.TestCase):
    """Test suite covering MongoDB persistence, atomic claims, and failure recovery."""

    def setUp(self):
        self.client = MongoClient()
        self.db = self.client["rosapenna_test"]
        self.temp_dir = tempfile.TemporaryDirectory()
        self.recording_dir = Path(self.temp_dir.name)

        self.transport = RecordingConfirmationTransport(
            recording_dir=self.recording_dir,
            owned_root=self.recording_dir,
        )

        self.service = PreArrivalService(
            database=self.db,
            transport=self.transport,
            environment="test",
            test_run_id="test-run-storage-01",
        )

        self.fixed_now = dt.datetime(2027, 5, 1, 8, 0, 0, tzinfo=dt.timezone.utc)
        self.policy = PreArrivalPolicyDoc(
            policy_id="pol-storage-test",
            version=1,
            environment="test",
            approval_status="approved",
            approved_by="staff_admin",
            approved_at=self.fixed_now,
            source="storage_unit_test",
            timezone="Europe/Dublin",
            local_send_time="09:00",
            balance_request_lead_days=42,
            balance_due_lead_days=42,
            rooming_caddy_lead_days=42,
            late_booking_action="immediate",
        )
        self.service.save_policy(self.policy)

        self.booking_ref = "RSP-20270810-STRG01"
        self._seed_linkage(
            ref=self.booking_ref,
            arrival="2027-08-10",
            departure="2027-08-14",
            balance_due="1200.00",
            revision=1,
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def _seed_linkage(
        self,
        ref: str,
        arrival: str,
        departure: str,
        balance_due: str,
        revision: int = 1,
        status: str = "linked",
    ):
        self.db["mmb_booking_linkages"].update_one(
            {"booking_reference": ref},
            {
                "$set": {
                    "booking_reference": ref,
                    "source_channel": "online",
                    "status": status,
                    "revision": revision,
                    "revision_number": revision,
                    "discrepancies": [],
                    "lead_guest": {
                        "first_name": "Rory",
                        "last_name": "McIlroy",
                        "email": "rory@guest.invalid",
                        "telephone": "+353871234567",
                    },
                    "opera_stay": {
                        "reservation_id": f"OP-{ref}",
                        "hotel_id": "ROSA",
                        "arrival_date": arrival,
                        "departure_date": departure,
                        "room_category": "Luxury Suite",
                        "room_quantity": 1,
                    },
                    "brs_rounds": [
                        {
                            "round_id": f"RND-{ref}-1",
                            "course_name": "Sandy Hills",
                            "date": arrival,
                            "time": "09:30",
                            "players": 2,
                            "caddy_request_status": "Requested",
                        }
                    ],
                    "financials": {
                        "total_amount": "1600.00",
                        "deposit_paid": "400.00",
                        "balance_due": balance_due,
                        "currency": "EUR",
                    },
                    "created_at": self.fixed_now.isoformat(),
                }
            },
            upsert=True,
        )

    def test_reconciliation_idempotency_and_no_duplicates(self):
        """AC 2 & AC 4: Calling reconcile twice produces no duplicate jobs."""
        jobs_first = self.service.reconcile_booking(self.booking_ref, now=self.fixed_now)
        self.assertEqual(len(jobs_first), 2)

        # Call reconcile again
        jobs_second = self.service.reconcile_booking(self.booking_ref, now=self.fixed_now)
        self.assertEqual(len(jobs_second), 2)

        # Total count in database must be strictly 2
        total_in_db = self.db["scheduled_booking_reminders"].count_documents({
            "booking_reference": self.booking_ref
        })
        self.assertEqual(total_in_db, 2)

    def test_amendment_cancels_obsolete_milestones(self):
        """AC 4: Revision bump reschedules new milestones and cancels older revision pending jobs."""
        self.service.reconcile_booking(self.booking_ref, now=self.fixed_now)

        # Update linkage to revision 2 with new arrival date
        new_arrival = "2027-09-15"
        self._seed_linkage(
            ref=self.booking_ref,
            arrival=new_arrival,
            departure="2027-09-19",
            balance_due="1200.00",
            revision=2,
        )

        jobs_rev2 = self.service.reconcile_booking(self.booking_ref, expected_revision=2, now=self.fixed_now)
        self.assertEqual(len(jobs_rev2), 4)  # 2 active rev 2 + 2 canceled rev 1

        active_rev2 = [j for j in jobs_rev2 if j.revision_number == 2]
        canceled_rev1 = [j for j in jobs_rev2 if j.revision_number == 1]

        self.assertEqual(len(active_rev2), 2)
        self.assertEqual(len(canceled_rev1), 2)

        for old_job in canceled_rev1:
            self.assertEqual(old_job.status, "canceled_by_amendment")
            self.assertIn("SUPERSEDED_BY_REVISION_2", old_job.reason_code or "")

        for new_job in active_rev2:
            self.assertEqual(new_job.status, "scheduled")

    def test_same_revision_schedule_conflict_detection(self):
        """Safety: Modifying stay dates without revision increment raises conflict."""
        self.service.reconcile_booking(self.booking_ref, now=self.fixed_now)

        # Change arrival date in DB without incrementing revision
        self.db["mmb_booking_linkages"].update_one(
            {"booking_reference": self.booking_ref},
            {
                "$set": {
                    "opera_stay.arrival_date": "2027-08-25",
                    "opera_stay.departure_date": "2027-08-29",
                }
            },
        )

        with self.assertRaises(PreArrivalConflictError):
            self.service.reconcile_booking(self.booking_ref, expected_revision=1, now=self.fixed_now)

    def test_atomic_claim_and_dispatch_with_recording_transport(self):
        """AC 2 & AC 7: Due job is atomically claimed, dispatched via transport, and marked recorded_test."""
        self.service.reconcile_booking(self.booking_ref, now=self.fixed_now)

        # Move clock past due_at (42 days prior to 2027-08-10 is 2027-06-29)
        dispatch_clock = dt.datetime(2027, 6, 29, 9, 30, tzinfo=dt.timezone.utc)

        # 1. Claim due jobs
        claimed = self.service.claim_due_jobs(now=dispatch_clock, batch_size=10)
        self.assertEqual(len(claimed), 2)
        for job in claimed:
            self.assertEqual(job.status, "sending")
            self.assertEqual(job.attempt_count, 1)
            self.assertIsNotNone(job.lease_expires_at)

        # 2. Dispatch
        result_job = self.service.dispatch_claimed_job(claimed[0], now=dispatch_clock)
        self.assertEqual(result_job.status, "recorded_test")
        self.assertIsNotNone(result_job.message_id)
        self.assertIsNotNone(result_job.message_hash)

        # Verify .eml file was recorded
        eml_files = list(self.recording_dir.glob("*.eml"))
        self.assertEqual(len(eml_files), 1)

    def test_parallel_worker_claim_mutex(self):
        """Safety: Two concurrent workers claiming the same due job only allow one winner."""
        self.service.reconcile_booking(self.booking_ref, now=self.fixed_now)
        dispatch_clock = dt.datetime(2027, 6, 29, 9, 30, tzinfo=dt.timezone.utc)

        # Worker 1 claims
        worker1_claimed = self.service.claim_due_jobs(now=dispatch_clock, batch_size=10)
        self.assertEqual(len(worker1_claimed), 2)

        # Worker 2 attempts to claim at the same moment
        worker2_claimed = self.service.claim_due_jobs(now=dispatch_clock, batch_size=10)
        self.assertEqual(len(worker2_claimed), 0)

    def test_stale_worker_completion_write_is_ignored(self):
        """Safety: A worker with an expired lease cannot overwrite a re-claimed job."""
        self.service.reconcile_booking(self.booking_ref, now=self.fixed_now)
        dispatch_clock = dt.datetime(2027, 6, 29, 9, 30, tzinfo=dt.timezone.utc)

        claimed = self.service.claim_due_jobs(now=dispatch_clock, batch_size=1)
        job = claimed[0]

        # Simulate another worker taking over with a new attempt_id
        new_attempt = "ATT-NEW-WINNER"
        self.db["scheduled_booking_reminders"].update_one(
            {"reminder_id": job.reminder_id},
            {"$set": {"attempt_id": new_attempt, "status": "sending"}},
        )

        # Stale worker tries to finalize with old attempt_id
        self.service._finalize_job_state(
            reminder_id=job.reminder_id,
            attempt_id=job.attempt_id,  # stale attempt ID
            status="recorded_test",
            reason_code=None,
            now=dispatch_clock,
        )

        # Verify status remained unchanged by stale worker
        updated = self.service.get_job_by_id(job.reminder_id)
        self.assertEqual(updated.attempt_id, new_attempt)
        self.assertEqual(updated.status, "sending")

    def test_missed_hook_repair_scan(self):
        """Operations: Bounded scan automatically discovers and reconciles unscheduled linkages."""
        # Linkage exists but reconcile_booking was never called
        repaired_count = self.service.scan_and_repair_missed_hooks(limit=10, now=self.fixed_now)
        self.assertEqual(repaired_count, 1)

        # Verify jobs were created
        jobs = self.service.get_jobs_for_booking(self.booking_ref)
        self.assertEqual(len(jobs), 2)


if __name__ == "__main__":
    unittest.main()
