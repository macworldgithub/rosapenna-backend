"""Automated API and security tests for Ticket N08 — Pre-Arrival Routes & Denials."""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import unittest
from mongomock import MongoClient

from rosapenna_backend.api.schemas.pre_arrival import PreArrivalPolicyDoc
from rosapenna_backend.api.staff_security import StaffPrincipal
from rosapenna_backend.config import settings
from rosapenna_backend.main import create_app
from rosapenna_backend.api.routes.pre_arrival import STAFF_ROLES
from rosapenna_backend.services.pre_arrival import PreArrivalService


async def async_asgi_request(
    app,
    method: str,
    path: str,
    headers: dict | None = None,
    body: bytes = b"",
) -> tuple[int, dict, bytes]:
    """In-process ASGI HTTP request runner using standard library asyncio."""
    headers_list = [
        (k.lower().encode("latin-1"), v.encode("latin-1"))
        for k, v in (headers or {}).items()
    ]
    response_headers = {}
    response_status = 0
    response_body = bytearray()

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method.upper(),
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("ascii"),
        "query_string": b"",
        "headers": headers_list,
        "client": ("127.0.0.1", 54321),
        "server": ("127.0.0.1", 4040),
    }

    async def receive():
        return {
            "type": "http.request",
            "body": body,
            "more_body": False,
        }

    async def send(message):
        nonlocal response_status, response_headers, response_body
        if message["type"] == "http.response.start":
            response_status = message["status"]
            response_headers = {
                k.decode("latin-1"): v.decode("latin-1")
                for k, v in message.get("headers", [])
            }
        elif message["type"] == "http.response.body":
            response_body.extend(message.get("body", b""))

    await app(scope, receive, send)
    return response_status, response_headers, bytes(response_body)


class TestPreArrivalApi(unittest.TestCase):
    """Test suite covering staff pre-arrival API endpoints and guest dispatch denial."""

    def setUp(self):
        self.mongo = MongoClient()
        self.db = self.mongo["n08_api_test_db"]

        # Configure test settings
        settings.api_key = "test_api_key_secret_123"
        settings.staff_api_key = "test_staff_key_secret_456"
        settings.security_bypass_in_dev = False
        settings.environment = "test"
        settings.pre_arrival_enabled = True
        settings.pre_arrival_mode = "recording"
        settings.pre_arrival_test_run_id = "test-run-n08-api"
        settings.pre_arrival_recording_dir = "C:/tmp/rosapenna-recordings"
        settings.pre_arrival_staff_api_enabled = True

        self.fixed_now = dt.datetime(2027, 5, 1, 8, 0, 0, tzinfo=dt.timezone.utc)
        self.ref = "RSP-20270810-API001"

        # Seed policy
        self.policy = PreArrivalPolicyDoc(
            policy_id="pol-api-test",
            version=1,
            environment="test",
            approval_status="approved",
            approved_by="staff_admin",
            approved_at=self.fixed_now,
            source="api_unit_test",
            timezone="Europe/Dublin",
            local_send_time="09:00",
            balance_request_lead_days=42,
            balance_due_lead_days=42,
            rooming_caddy_lead_days=42,
            late_booking_action="immediate",
        )
        self.pre_service = PreArrivalService(
            database=self.db,
            environment="test",
        )
        self.pre_service.save_policy(self.policy)

        # Seed linkage
        self.db["mmb_booking_linkages"].insert_one({
            "booking_reference": self.ref,
            "source_channel": "online",
            "status": "linked",
            "revision": 1,
            "revision_number": 1,
            "discrepancies": [],
            "lead_guest": {
                "first_name": "Shane",
                "last_name": "Lowry",
                "email": "shane@guest.invalid",
                "telephone": "+353871234567",
            },
            "opera_stay": {
                "reservation_id": f"OP-{self.ref}",
                "hotel_id": "ROSA",
                "arrival_date": "2027-08-10",
                "departure_date": "2027-08-14",
                "room_category": "Deluxe",
                "room_quantity": 1,
            },
            "financials": {
                "total_amount": "1600.00",
                "deposit_paid": "400.00",
                "balance_due": "1200.00",
                "currency": "EUR",
            },
            "created_at": self.fixed_now.isoformat(),
        })

        # Pre-seed reminders
        self.pre_service.reconcile_booking(self.ref, now=self.fixed_now)

        self.app = create_app()
        self.app.state.mongo_database = self.db
        self.app.state.pre_arrival_service = self.pre_service

        self.authorized_principal = StaffPrincipal(
            issuer="rosapenna_staff_auth",
            subject="staff_operator",
            roles=frozenset(["staff_operator"]),
            scopes=frozenset([
                f"booking:{self.ref}:read",
                f"booking:{self.ref}:reconcile",
                f"booking:{self.ref}:retry",
                f"run:{settings.pre_arrival_test_run_id}:pre_arrival",
            ]),
        )

        self.staff_headers = {
            "X-Staff-Token": settings.staff_api_key,
            "Content-Type": "application/json",
        }

    def tearDown(self):
        self.mongo.close()
        self.app.dependency_overrides.clear()

    def test_get_booking_jobs_success(self):
        """Staff: Fetch pre-arrival jobs returns 200 with scoped job summaries."""
        settings.pre_arrival_staff_api_enabled = True
        self.app.dependency_overrides[STAFF_ROLES] = lambda: self.authorized_principal
        status, headers, body = asyncio.run(
            async_asgi_request(
                self.app,
                "GET",
                f"/staff/pre-arrival/bookings/{self.ref}/jobs",
                headers=self.staff_headers,
            )
        )
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("cache-control"), "no-store")
        data = json.loads(body.decode("utf-8"))
        self.assertEqual(data["booking_reference"], self.ref)
        self.assertEqual(data["total"], 2)
        for job in data["jobs"]:
            self.assertIn("reminder_id", job)
            self.assertIn("due_at", job)
            self.assertNotIn("rendered_content", job)  # Sensitive body not leaked

    def test_unscoped_staff_read_denied(self):
        """Security: Staff request without booking scope receives HTTP 403 Forbidden."""
        settings.pre_arrival_staff_api_enabled = True
        # Using default staff headers without scope overrides
        status, headers, body = asyncio.run(
            async_asgi_request(
                self.app,
                "GET",
                f"/staff/pre-arrival/bookings/{self.ref}/jobs",
                headers=self.staff_headers,
            )
        )
        self.assertEqual(status, 403)
        self.assertEqual(headers.get("cache-control"), "no-store")

    def test_reconcile_booking_success(self):
        """Staff: Post reconcile updates schedule with reason audit."""
        settings.pre_arrival_staff_api_enabled = True
        self.app.dependency_overrides[STAFF_ROLES] = lambda: self.authorized_principal
        payload = json.dumps({"expected_revision": 1, "reason": "Routine staff audit reconciliation"}).encode("utf-8")
        status, headers, body = asyncio.run(
            async_asgi_request(
                self.app,
                "POST",
                f"/staff/pre-arrival/bookings/{self.ref}/reconcile",
                headers=self.staff_headers,
                body=payload,
            )
        )
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("cache-control"), "no-store")
        data = json.loads(body.decode("utf-8"))
        self.assertEqual(data["booking_reference"], self.ref)
        self.assertEqual(data["reconciled_jobs_count"], 2)

    def test_body_over_4k_without_content_length_rejected(self):
        """Security: Body exceeding 4 KiB without Content-Length is rejected with 413."""
        settings.pre_arrival_staff_api_enabled = True
        self.app.dependency_overrides[STAFF_ROLES] = lambda: self.authorized_principal
        payload = json.dumps({"expected_revision": 1, "reason": "x" * 450}).encode() + b" " * 5000
        status, headers, body = asyncio.run(
            async_asgi_request(
                self.app,
                "POST",
                f"/staff/pre-arrival/bookings/{self.ref}/reconcile",
                headers=self.staff_headers,
                body=payload,
            )
        )
        self.assertEqual(status, 413)
        self.assertEqual(headers.get("cache-control"), "no-store")

    def test_retry_job_success_and_conflict_on_submitted(self):
        """Staff: Safe retry re-enqueues failed jobs; rejects completed submissions."""
        settings.pre_arrival_staff_api_enabled = True
        self.app.dependency_overrides[STAFF_ROLES] = lambda: self.authorized_principal
        jobs = self.pre_service.get_jobs_for_booking(self.ref)
        target_job = jobs[0]

        # Simulate job failed
        self.db["scheduled_booking_reminders"].update_one(
            {"reminder_id": target_job.reminder_id},
            {"$set": {"status": "failed", "reason_code": "SIMULATED_FAILURE"}},
        )

        payload = json.dumps({"expected_revision": 1, "reason": "Operator cleared SMTP issue"}).encode("utf-8")
        status, headers, body = asyncio.run(
            async_asgi_request(
                self.app,
                "POST",
                f"/staff/pre-arrival/jobs/{target_job.reminder_id}/retry",
                headers=self.staff_headers,
                body=payload,
            )
        )
        self.assertEqual(status, 200)
        data = json.loads(body.decode("utf-8"))
        self.assertEqual(data["status"], "scheduled")

        # Now simulate job was submitted
        self.db["scheduled_booking_reminders"].update_one(
            {"reminder_id": target_job.reminder_id},
            {"$set": {"status": "submitted"}},
        )

        conflict_payload = json.dumps({"expected_revision": 1, "reason": "Should fail because already submitted"}).encode("utf-8")
        c_status, _, _ = asyncio.run(
            async_asgi_request(
                self.app,
                "POST",
                f"/staff/pre-arrival/jobs/{target_job.reminder_id}/retry",
                headers=self.staff_headers,
                body=conflict_payload,
            )
        )
        self.assertEqual(c_status, 409)

    def test_staff_api_gate_disabled_returns_503(self):
        """Security: When pre_arrival_staff_api_enabled is False, endpoints return 503."""
        settings.pre_arrival_staff_api_enabled = False
        status, headers, _ = asyncio.run(
            async_asgi_request(
                self.app,
                "GET",
                f"/staff/pre-arrival/bookings/{self.ref}/jobs",
                headers=self.staff_headers,
            )
        )
        self.assertEqual(status, 503)
        self.assertEqual(headers.get("cache-control"), "no-store")

    def test_guest_reminder_dispatch_is_forbidden(self):
        """Security: Guest-facing reminder dispatch endpoint returns 403 Forbidden."""
        from rosapenna_backend.api.dependencies import require_guest_booking_scope
        from rosapenna_backend.api.schemas.mmb_auth import GuestSessionPrincipal

        # Enable MMB in DB
        self.db["mmb_release_gate"].update_one(
            {"_id": "mmb_release_gate"},
            {"$set": {"enabled": True, "written_request_reference": "N08-TEST"}},
            upsert=True,
        )
        self.app.dependency_overrides[require_guest_booking_scope] = lambda: GuestSessionPrincipal(
            session_token="tok_guest_session_valid",
            booking_reference=self.ref,
            lead_guest_email="shane@guest.invalid",
            expires_at=dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=2),
        )
        guest_headers = {
            "X-MMB-Session": "tok_guest_session_valid",
            "Content-Type": "application/json",
        }
        # Seed guest session in DB
        self.db["mmb_sessions"].insert_one({
            "session_token": "tok_guest_session_valid",
            "booking_reference": self.ref,
            "lead_guest_email": "shane@guest.invalid",
            "expires_at": dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=2),
            "created_at": dt.datetime.now(dt.timezone.utc),
        })

        status, _, body = asyncio.run(
            async_asgi_request(
                self.app,
                "POST",
                f"/mmb/confirmations/{self.ref}/reminders/REM-001/dispatch",
                headers=guest_headers,
                body=b"{}",
            )
        )
        self.assertEqual(status, 403)
        data = json.loads(body.decode("utf-8"))
        self.assertIn("Guest reminder dispatch is disabled", data.get("detail", ""))


if __name__ == "__main__":
    unittest.main()
