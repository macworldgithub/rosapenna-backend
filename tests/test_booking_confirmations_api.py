"""Automated API and security tests for Ticket N07 — Booking Confirmations Routes & Denials."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import shutil
import tempfile
import threading
import unittest

from mongomock import MongoClient

from rosapenna_backend.api.schemas.communications import (
    StaffConfirmationRetryRequest,
)
from rosapenna_backend.api.staff_security import StaffPrincipal
from rosapenna_backend.config import settings
from rosapenna_backend.main import create_app
from rosapenna_backend.api.routes.booking_confirmations import STAFF_ROLES
from rosapenna_backend.services.booking_confirmations import (
    BookingConfirmationCoordinator,
    ConfirmationConflictError,
)
from rosapenna_backend.services.booking_intents import BookingIntentStore
from rosapenna_backend.services.email_delivery import RecordingConfirmationTransport
from tests.test_booking_confirmations import (
    TEST_RUN_ID,
    make_test_confirmed_event,
    make_test_operation_doc,
)


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


def asgi_request(app, method: str, path: str, headers: dict | None = None, body: bytes = b""):
    return asyncio.run(async_asgi_request(app, method, path, headers, body))


class TestBookingConfirmationsApi(unittest.TestCase):
    """Test staff confirmations route access, gating, retries, and legacy path denials."""

    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="n07_api_test_")
        self.recording_dir = Path(self.test_dir) / "recordings"

        self.mongo = MongoClient(tz_aware=True)
        self.db = self.mongo["n07_api_test_db"]

        # Configure test settings
        settings.api_key = "test_api_key_secret_123"
        settings.staff_api_key = "test_staff_key_secret_456"
        settings.security_bypass_in_dev = False
        settings.environment = "test"
        settings.confirmations_mode = "recording"
        settings.confirmations_test_run_id = TEST_RUN_ID
        settings.confirmations_recording_dir = str(self.recording_dir)
        settings.confirmations_staff_api_enabled = True

        self.app = create_app()
        self.app.state.mongo_database = self.db
        self.app.state.booking_intent_store = BookingIntentStore(self.db["booking_intents"])

        self.transport = RecordingConfirmationTransport(recording_dir=self.recording_dir)
        self.coordinator = BookingConfirmationCoordinator(database=self.db, transport=self.transport)
        self.app.state.confirmation_coordinator = self.coordinator

    def seed_operation(self, event):
        operation = make_test_operation_doc(event)
        source = operation.pop("_n07_test_source")
        self.db["booking_operations"].insert_one(operation)
        self.db["package_quotes"].insert_one(source["quote"])
        for payment in source["payments"]:
            self.db["package_payments"].insert_one(payment)
        self.db["booking_linkages"].insert_one(source["linkage"])

    def authorize_scopes(self, event, actions=("read", "retry")):
        scopes = {f"booking:{event.booking_reference}:{action}" for action in actions}
        scopes.add(f"run:{event.simulation_run_id}:confirmations")
        self.app.dependency_overrides[STAFF_ROLES] = lambda: StaffPrincipal(
            issuer="offline_test", subject="test_staff", roles=frozenset({"staff_operator"}),
            scopes=frozenset(scopes),
        )

    def tearDown(self):
        self.mongo.close()
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_staff_confirmations_gated_when_disabled(self):
        """Staff confirmations endpoints return 503 when gate is disabled."""
        settings.confirmations_staff_api_enabled = False
        status_code, headers, body = asgi_request(
            self.app,
            "GET",
            "/staff/confirmations/OP-TEST",
            headers={"X-Staff-Token": settings.staff_api_key},
        )
        self.assertEqual(status_code, 503)
        self.assertEqual(headers.get("cache-control"), "no-store")

    def test_staff_confirmations_unauthorized_without_staff_token(self):
        """Staff confirmations endpoints return 401 when token is missing or invalid."""
        status_code, headers, body = asgi_request(
            self.app,
            "GET",
            "/staff/confirmations/OP-TEST",
            headers={"X-Staff-Token": "wrong-token"},
        )
        self.assertEqual(status_code, 401)
        self.assertEqual(headers.get("cache-control"), "no-store")

    def test_staff_confirmations_unauthorized_when_token_is_missing(self):
        status_code, headers, _ = asgi_request(
            self.app,
            "GET",
            "/staff/confirmations/OP-TEST",
        )
        self.assertEqual(status_code, 401)
        self.assertEqual(headers.get("cache-control"), "no-store")

    def test_staff_get_status_success(self):
        """Authorized staff can inspect confirmation status with masked emails."""
        op_id = "OP-API-101"
        event = make_test_confirmed_event(op_id=op_id)
        self.seed_operation(event)
        self.authorize_scopes(event)

        # Prepare confirmation
        self.coordinator.prepare_confirmation(op_id)

        status_code, headers, body = asgi_request(
            self.app,
            "GET",
            f"/staff/confirmations/{op_id}",
            headers={"X-Staff-Token": settings.staff_api_key},
        )
        self.assertEqual(status_code, 200)
        self.assertEqual(headers.get("cache-control"), "no-store")

        data = json.loads(body.decode("utf-8"))
        self.assertEqual(data["operation_id"], op_id)
        self.assertEqual(data["overall_status"], "prepared")
        self.assertIn("***", data["guest_delivery"]["recipient_address_masked"])
        self.assertIn("***", data["hotel_delivery"]["recipient_address_masked"])

    def test_staff_retry_endpoint(self):
        """Authorized staff can trigger retry for a failed recipient role."""
        op_id = "OP-API-102"
        event = make_test_confirmed_event(op_id=op_id)
        self.seed_operation(event)
        self.authorize_scopes(event)

        # Set transport to simulate temporary refusal for guest
        self.transport.simulated_behavior = {"default": "success", "lead@example.invalid": "refusal"}
        self.coordinator.prepare_confirmation(op_id)
        failed = self.coordinator.dispatch_recipient(op_id, "guest")

        # Now staff retries via POST /staff/confirmations/{op_id}/retry
        self.transport.simulated_behavior["lead@example.invalid"] = "success"
        retry_req = {
            "recipient_role": "guest",
            "expected_event_id": event.event_id,
            "expected_attempt_id": failed.attempt_id,
            "reason": "Operator verified guest mailbox is active",
        }

        status_code, headers, body = asgi_request(
            self.app,
            "POST",
            f"/staff/confirmations/{op_id}/retry",
            headers={
                "X-Staff-Token": settings.staff_api_key,
                "Content-Type": "application/json",
            },
            body=json.dumps(retry_req).encode("utf-8"),
        )
        self.assertEqual(status_code, 200)
        self.assertEqual(headers.get("cache-control"), "no-store")

        res = json.loads(body.decode("utf-8"))
        self.assertEqual(res["operation_id"], op_id)
        self.assertEqual(res["status"], "recorded_test")

    def test_staff_scope_cannot_read_or_retry_another_booking(self):
        event = make_test_confirmed_event(op_id="OP-API-103")
        self.seed_operation(event)
        self.coordinator.prepare_confirmation(event.operation_id)
        self.authorize_scopes(event, actions=("read",))

        status_code, headers, _ = asgi_request(
            self.app,
            "GET",
            f"/staff/confirmations/{event.operation_id}",
            headers={"X-Staff-Token": settings.staff_api_key},
        )
        self.assertEqual(status_code, 200)

        self.app.dependency_overrides[STAFF_ROLES] = lambda: StaffPrincipal(
            issuer="offline_test", subject="wrong_scope", roles=frozenset({"staff_operator"}),
            scopes=frozenset({"booking:RSP-20990514-OTHER:retry", f"run:{event.simulation_run_id}:confirmations"}),
        )
        body = json.dumps({
            "recipient_role": "guest",
            "expected_event_id": event.event_id,
            "reason": "Try cross booking access",
        }).encode("utf-8")
        status_code, headers, _ = asgi_request(
            self.app,
            "POST",
            f"/staff/confirmations/{event.operation_id}/retry",
            headers={"X-Staff-Token": settings.staff_api_key, "Content-Type": "application/json"},
            body=body,
        )
        self.assertEqual(status_code, 404)
        self.assertEqual(headers.get("cache-control"), "no-store")

    def test_staff_role_is_checked_at_confirmation_route(self):
        event = make_test_confirmed_event(op_id="OP-API-WRONG-ROLE")
        self.seed_operation(event)
        self.coordinator.prepare_confirmation(event.operation_id)
        self.app.dependency_overrides[STAFF_ROLES] = lambda: StaffPrincipal(
            issuer="offline_test", subject="non_staff", roles=frozenset({"guest"}),
            scopes=frozenset({
                f"booking:{event.booking_reference}:read",
                f"run:{event.simulation_run_id}:confirmations",
            }),
        )

        status_code, headers, _ = asgi_request(
            self.app,
            "GET",
            f"/staff/confirmations/{event.operation_id}",
            headers={"X-Staff-Token": settings.staff_api_key},
        )
        self.assertEqual(status_code, 403)
        self.assertEqual(headers.get("cache-control"), "no-store")

    def test_staff_retry_body_is_limited_to_four_kib(self):
        body = b'{"reason":"' + (b"x" * 4100) + b'"}'
        status_code, headers, _ = asgi_request(
            self.app,
            "POST",
            "/staff/confirmations/OP-API-LARGE/retry",
            headers={"X-Staff-Token": settings.staff_api_key, "Content-Type": "application/json"},
            body=body,
        )
        self.assertEqual(status_code, 413)
        self.assertEqual(headers.get("cache-control"), "no-store")

    def _barrier_before_claim(self, coordinators, barrier):
        """Pause concurrent dispatches after read and before the conditional database claim."""
        for coordinator in coordinators:
            original_claim = coordinator._claim_attempt

            def synchronized_claim(*args, _original=original_claim, **kwargs):
                barrier.wait(timeout=5)
                return _original(*args, **kwargs)

            coordinator._claim_attempt = synchronized_claim

    def _concurrent_coordinators(self):
        return [
            self.coordinator,
            BookingConfirmationCoordinator(database=self.db, transport=self.transport),
        ]

    def test_concurrent_dispatches_for_one_role_create_only_one_attempt(self):
        event = make_test_confirmed_event(op_id="OP-API-CONCURRENT-SAME")
        self.seed_operation(event)
        self.coordinator.prepare_confirmation(event.operation_id)
        coordinators = self._concurrent_coordinators()
        self._barrier_before_claim(coordinators, threading.Barrier(2))

        def dispatch(coordinator):
            try:
                return ("ok", coordinator.dispatch_recipient(event.operation_id, "guest").status)
            except ConfirmationConflictError:
                return ("conflict", None)

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(dispatch, coordinators))

        self.assertEqual(sum(result == "ok" for result, _ in outcomes), 1)
        self.assertEqual(sum(result == "conflict" for result, _ in outcomes), 1)
        self.assertEqual(len(self.transport.recorded_submissions), 1)
        saved = self.db["booking_operations"].find_one({"operation_id": event.operation_id})
        self.assertEqual(saved["confirmation"]["deliveries"]["guest"]["attempt_count"], 1)
        self.assertEqual(len(saved["confirmation"]["attempt_history"]), 1)

    def test_concurrent_dispatches_for_different_roles_do_not_overwrite(self):
        event = make_test_confirmed_event(op_id="OP-API-CONCURRENT-DIFFERENT")
        self.seed_operation(event)
        self.coordinator.prepare_confirmation(event.operation_id)
        coordinators = self._concurrent_coordinators()
        self._barrier_before_claim(coordinators, threading.Barrier(2))

        with ThreadPoolExecutor(max_workers=2) as pool:
            guest_future = pool.submit(coordinators[0].dispatch_recipient, event.operation_id, "guest")
            hotel_future = pool.submit(coordinators[1].dispatch_recipient, event.operation_id, "hotel")
            guest = guest_future.result(timeout=10)
            hotel = hotel_future.result(timeout=10)

        self.assertEqual(guest.status, "recorded_test")
        self.assertEqual(hotel.status, "recorded_test")
        self.assertEqual(len(self.transport.recorded_submissions), 2)
        saved = self.db["booking_operations"].find_one({"operation_id": event.operation_id})
        deliveries = saved["confirmation"]["deliveries"]
        self.assertEqual(deliveries["guest"]["status"], "recorded_test")
        self.assertEqual(deliveries["hotel"]["status"], "recorded_test")
        self.assertEqual(len(saved["confirmation"]["attempt_history"]), 2)
        self.assertEqual(self.coordinator.get_status(event.operation_id).overall_status, "recorded_test")

    def test_legacy_booking_confirmation_template_is_denied(self):
        """POST /communications/send with booking_confirmation must return 403."""
        payload = {
            "booking_intent_id": "INTENT-TEST-001",
            "template": "booking_confirmation",
        }
        status_code, headers, body = asgi_request(
            self.app,
            "POST",
            "/communications/send",
            headers={
                "X-API-Key": settings.api_key,
                "Content-Type": "application/json",
            },
            body=json.dumps(payload).encode("utf-8"),
        )
        self.assertEqual(status_code, 403)
        self.assertEqual(headers.get("cache-control"), "no-store")

    def test_legacy_itinerary_download_is_denied(self):
        """GET /communications/itinerary/{id}/pdf must return 403."""
        status_code, headers, body = asgi_request(
            self.app,
            "GET",
            "/communications/itinerary/INTENT-TEST-001/pdf",
            headers={"X-API-Key": settings.api_key},
        )
        self.assertEqual(status_code, 403)
        self.assertEqual(headers.get("cache-control"), "no-store")


if __name__ == "__main__":
    unittest.main()
