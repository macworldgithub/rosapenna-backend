"""Standalone bounded worker CLI for Ticket N08 — Pre-arrival scheduling and dispatch.

Usage:
  # Dry-run inspection (default):
  python scripts/run_n08_worker.py --once --dry-run

  # Bounded execution run:
  python scripts/run_n08_worker.py --once --execute
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path
from typing import Dict

# Ensure backend root is on sys.path
backend_root = Path(__file__).resolve().parents[1]
if str(backend_root) not in sys.path:
    sys.path.insert(0, str(backend_root))

from rosapenna_backend.config import settings, validate_security_settings
from rosapenna_backend.database import connect_database
from rosapenna_backend.services.email_delivery import (
    DisabledConfirmationTransport,
    RecordingConfirmationTransport,
)
from rosapenna_backend.services.pre_arrival import PreArrivalService

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("n08_worker")


def main() -> int:
    parser = argparse.ArgumentParser(description="Rosapenna N08 Pre-Arrival Bounded Worker")
    parser.add_argument("--once", action="store_true", default=True, help="Run a single bounded batch and exit.")
    parser.add_argument("--dry-run", action="store_true", default=False, help="Inspect due jobs and candidates without mutating state.")
    parser.add_argument("--execute", action="store_true", default=False, help="Perform atomic claims and dispatches.")
    parser.add_argument("--batch-bookings", type=int, default=100, help="Maximum bookings to evaluate in repair scan (1..100, default 100).")
    parser.add_argument("--batch-jobs", type=int, default=50, help="Maximum due jobs to claim and dispatch (1..50, default 50).")
    args = parser.parse_args()

    if not (1 <= args.batch_bookings <= 100):
        logger.error("--batch-bookings must be between 1 and 100 (got %d)", args.batch_bookings)
        return 1
    if not (1 <= args.batch_jobs <= 50):
        logger.error("--batch-jobs must be between 1 and 50 (got %d)", args.batch_jobs)
        return 1

    is_dry_run = not args.execute or args.dry_run

    print("=" * 70)
    print("ROSAPENNA PRE-ARRIVAL SCHEDULER WORKER (Ticket N08)")
    print(f"  Mode        : {'DRY-RUN (read-only)' if is_dry_run else 'EXECUTE'}")
    print(f"  Environment : {settings.environment}")
    print(f"  Config Mode : {settings.pre_arrival_mode}")
    print(f"  Enabled     : {settings.pre_arrival_enabled}")
    print("=" * 70)

    try:
        validate_security_settings()
    except Exception as exc:
        logger.error("Configuration validation failed: %s", exc)
        return 1

    client = None
    try:
        client, db = connect_database()
    except Exception as exc:
        logger.error("Failed to connect to MongoDB: %s", exc)
        return 1

    try:
        if not is_dry_run and not settings.pre_arrival_enabled:
            logger.error("BOOKING_PRE_ARRIVAL_ENABLED=false. Worker execution is refused by server policy.")
            return 1

        mode = settings.pre_arrival_mode.strip().lower()
        if not is_dry_run and mode == "disabled":
            logger.error("BOOKING_PRE_ARRIVAL_MODE is 'disabled'. Execution refused.")
            return 1

        transport = DisabledConfirmationTransport()
        if mode == "recording":
            if not settings.pre_arrival_recording_dir:
                logger.error("BOOKING_PRE_ARRIVAL_RECORDING_DIR is required for recording mode.")
                return 1
            transport = RecordingConfirmationTransport(
                recording_dir=settings.pre_arrival_recording_dir,
            )

        mmb_enabled = False
        try:
            from rosapenna_backend.services.mmb_gate import MmbGateService
            mmb_enabled = MmbGateService(database=db).is_enabled()
        except Exception:
            pass

        service = PreArrivalService(
            database=db,
            transport=transport,
            environment=settings.environment,
            property_id="ROSA",
            test_run_id=settings.pre_arrival_test_run_id,
            mmb_enabled=mmb_enabled,
            init_indexes=not is_dry_run,
        )

        if is_dry_run:
            from rosapenna_backend.services.pre_arrival import _utc_now
            clock = _utc_now()
            # Dry-run inspection: distinguish due now from future pending
            due_query = {
                "environment": settings.environment,
                "property_id": "ROSA",
                "status": {"$in": ["scheduled", "failed"]},
                "$or": [
                    {"next_attempt_at": {"$lte": clock}},
                    {"next_attempt_at": None, "due_at": {"$lte": clock}},
                ],
            }
            future_query = {
                "environment": settings.environment,
                "property_id": "ROSA",
                "status": "scheduled",
                "due_at": {"$gt": clock},
            }
            due_count = db["scheduled_booking_reminders"].count_documents(due_query)
            future_count = db["scheduled_booking_reminders"].count_documents(future_query)
            blocked_count = db["scheduled_booking_reminders"].count_documents({
                "environment": settings.environment,
                "property_id": "ROSA",
                "status": "blocked_dependency",
            })
            candidates_count = db["mmb_booking_linkages"].count_documents({})

            print()
            print("DRY-RUN SUMMARY COUNTS:")
            print(f"  Candidate Linkages in DB  : {candidates_count}")
            print(f"  Due Reminder Jobs (<=now) : {due_count}")
            print(f"  Future Pending Jobs (>now): {future_count}")
            print(f"  Blocked Dependency Jobs   : {blocked_count}")
            print("  Zero mutations executed. Exiting cleanly.")
            return 0

        # Execute run
        logger.info("Step 1: Running bounded repair scan (limit: %d)...", args.batch_bookings)
        repaired = service.scan_and_repair_missed_hooks(limit=args.batch_bookings)
        logger.info("Repaired / checked %d booking schedules.", repaired)

        logger.info("Step 2: Claiming due jobs (limit: %d)...", args.batch_jobs)
        claimed_jobs = service.claim_due_jobs(batch_size=args.batch_jobs)
        logger.info("Claimed %d jobs for dispatch.", len(claimed_jobs))

        outcomes: Dict[str, int] = {}
        for job in claimed_jobs:
            result_job = service.dispatch_claimed_job(job)
            st = result_job.status
            outcomes[st] = outcomes.get(st, 0) + 1

        print()
        print("EXECUTION BATCH SUMMARY:")
        print(f"  Repaired Bookings : {repaired}")
        print(f"  Claimed Jobs      : {len(claimed_jobs)}")
        for status_name, count in sorted(outcomes.items()):
            print(f"  - Status '{status_name}': {count}")
        print()
        return 0
    finally:
        if client:
            client.close()


if __name__ == "__main__":
    raise SystemExit(main())
