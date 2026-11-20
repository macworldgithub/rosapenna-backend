"""Run N10 on loopback with one synthetic booking and no vendor integrations."""

import argparse
import datetime as dt
import json
import os
import re
import secrets
import webbrowser

import uvicorn

from rosapenna_backend.api.routes.pre_arrival_rooming import create_rooming_sandbox
from rosapenna_backend.database import connect_database, MongoSettings
from rosapenna_backend.services.pre_arrival_report import report
from rosapenna_backend.services.pre_arrival_rooming import SandboxRooming


def create_fixture(service: SandboxRooming) -> str:
    reference = f"N10-TEST-{secrets.token_hex(5)}"
    # Keep test data for a short review window. These values are synthetic.
    created_at = dt.datetime.now(dt.UTC)
    arrival = created_at.date() + dt.timedelta(days=40)
    interval = {"start": arrival.isoformat(), "end": (arrival + dt.timedelta(days=3)).isoformat()}
    service.bookings.create_index("cleanup_at", expireAfterSeconds=0)
    service.bookings.insert_one({
        "_id": reference, "synthetic": True, "version": 1,
        "n12_test_balance": {
            "due": (created_at.date() + dt.timedelta(days=3)).isoformat(),
            "booked_on": created_at.date().isoformat(),
            "total_minor": 50000, "verified_paid_minor": 12500, "updated_at": created_at,
        },
        "caddy_rounds": [{
            "id": f"round-{secrets.token_hex(5)}", "course": "Sample Golf Course",
            "date": arrival.isoformat(), "golfers": ["guest-a", "guest-b"],
        }],
        "cleanup_at": created_at + dt.timedelta(days=1),
        "fixture": {
            "reference": reference, "confirmed": True, "lead_email": "lead@example.invalid",
            "lead_version": secrets.token_hex(16), **interval,
            "groups": [
                {"id": key, "label": label, "total_limit": 2, "adult_limit": 2,
                 "child_limit": 1, **interval}
                for key, label in (("group-a", "Room group A"), ("group-b", "Room group B"))
            ],
            "slots": [
                {"id": key, "category": category, **interval}
                for key, category in (("guest-a", "adult"), ("guest-b", "adult"), ("guest-c", "child"))
            ],
        },
    })
    return service.issue_invite(reference)[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--open-invite", action="store_true", help="Open a fresh test invitation in your browser")
    modes.add_argument("--caddy-history", metavar="BOOKING", help="Inspect caddy requests on a synthetic booking")
    modes.add_argument("--staff-report", action="store_true", help="Inspect the synthetic pre-arrival staff report")
    parser.add_argument("--status", choices=("unpaid", "overdue", "unknown", "missing", "late_booking", "pending", "accepted", "failed"))
    for name in ("arrival-from", "arrival-to", "due-from", "due-to"):
        parser.add_argument(f"--{name}", type=dt.date.fromisoformat, metavar="YYYY-MM-DD")
    args = parser.parse_args()
    filters = {name: getattr(args, name) for name in ("status", "arrival_from", "arrival_to", "due_from", "due_to")}
    if not args.staff_report and any(filters.values()):
        parser.error("Report filters require --staff-report")
    if args.caddy_history and (args.open_invite or not re.fullmatch(r"N10-TEST-[a-f0-9]{10}", args.caddy_history)):
        parser.error("Provide a synthetic booking reference on its own")
    settings = MongoSettings(
        _env_file=None, uri="mongodb://127.0.0.1:27017", database="rosapenna_n10_test_sandbox",
    )
    client, database = connect_database(settings)
    try:
        if args.staff_report:
            try:
                output = report(database, dt.datetime.now(dt.UTC), **filters)
            except ValueError:
                raise SystemExit("Could not generate the test report. Check the dates and source data.") from None
            print(json.dumps(output, indent=2))
            return
        service = SandboxRooming(database)
        if args.caddy_history:
            booking = service.bookings.find_one(
                {"_id": args.caddy_history, "synthetic": True}, {"caddy_history": 1},
            )
            if booking is None:
                raise SystemExit("Test booking unavailable")
            print(json.dumps(booking.get("caddy_history", []), indent=2, default=str))
            return
        if args.open_invite:
            invite = create_fixture(service)
            if not webbrowser.open(f"http://127.0.0.1:3032/pre-arrival/rooming#invite={invite}"):
                raise SystemExit("Could not open the browser for this invitation")
            return
        app = create_rooming_sandbox(service, os.environ.get("N10_SANDBOX_KEY", ""))
        uvicorn.run(app, host="127.0.0.1", port=4042, proxy_headers=False, access_log=False)
    finally:
        client.close()


if __name__ == "__main__":
    main()
