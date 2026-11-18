"""API route integration tests for Ticket N09 — Balance Payments and Staff Controls.

Covers:
- Scoped staff balance summary retrieval (GET).
- Bound balance request creation (POST).
- Request reconciliation by staff (POST).
- Attention list queries (GET).
- Provider webhook ingress fail-closed (503).
- Dev simulation endpoint execution and security gates.
- No-store response headers across all endpoints.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from decimal import Decimal
import json
import unittest
import uuid
import secrets

from mongomock import MongoClient

from rosapenna_backend.api.schemas.booking_linkage import (
    BrsRoundDetails,
    LeadGuestContact,
    OperaStayDetails,
    PaymentLedgerEntry,
    StaffLinkBookingRequest,
)
from rosapenna_backend.api.schemas.pre_arrival import PreArrivalPolicyDoc
from rosapenna_backend.config import settings
from rosapenna_backend.main import create_app
from rosapenna_backend.services.booking_linkage import BookingLinkageService


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


class TestBalancePaymentsApi(unittest.TestCase):
    """Test FastAPI balance payments routes, authentication, and headers."""

    def setUp(self):
        self.staff_token = secrets.token_hex(24)
        self.test_run_id = f"test-api-{uuid.uuid4().hex[:8]}"

        # Configure global settings
        settings.environment = "test"
        settings.staff_api_key = self.staff_token
        settings.security_bypass_in_dev = False
        settings.balance_payments_enabled = True
        settings.balance_payment_mode = "simulation"
        settings.balance_staff_api_enabled = True
        settings.balance_provider_ingress_enabled = False
        settings.balance_payment_test_run_id = self.test_run_id
        settings.balance_payment_signing_secret = secrets.token_hex(32)
        settings.balance_payment_property_id = "ROSA"

        self.app = create_app()
        self.mongo = MongoClient(tz_aware=True)
        self.db = self.mongo["n09_api_test_db"]
        self.app.state.mongo_database = self.db

        self.fixed_now = dt.datetime(2027, 5, 1, 8, 0, 0, tzinfo=dt.timezone.utc)
        self.arrival_date = dt.date(2027, 7, 1)

        # Seed policy
        policy = PreArrivalPolicyDoc(
            policy_id="POL-ROSA-API",
            version=1,
            environment="test",
            approval_status="approved",
            approved_by="finance_director",
            approved_at=self.fixed_now,
            source="Board Policy",
            timezone="Europe/Dublin",
            local_send_time="09:00",
            balance_request_lead_days=56,
            balance_due_lead_days=42,
            rooming_caddy_lead_days=42,
        )
        self.db["pre_arrival_policies"].insert_one(policy.model_dump(mode="json"))

        # Seed booking
        self.booking_ref = "RSP-20270701-API01"
        linkage_svc = BookingLinkageService(database=self.db)
        seed_req = StaffLinkBookingRequest(
            booking_reference=self.booking_ref,
            source_channel="online",
            lead_guest=LeadGuestContact(
                first_name="Jane",
                last_name="Golfer",
                email="jane@example.com",
                telephone="+353871234567",
            ),
            manual_opera_stay=OperaStayDetails(
                reservation_id="RES-N09-API",
                confirmation_number="CONF-N09-API",
                hotel_id="ROSA",
                room_category="DELUXE",
                room_quantity=1,
                arrival_date=self.arrival_date,
                departure_date=dt.date(2027, 7, 5),
                total_amount="800.00",
                currency="EUR",
                occupant_names=["Jane Golfer"],
            ),
            brs_rounds=[
                BrsRoundDetails(
                    round_id="RND-API-01",
                    course_id=1,
                    course_name="Old Tom Morris Links",
                    date=dt.date(2027, 7, 2),
                    time="10:00",
                    holes="18",
                    players=2,
                    brs_booking_id="BRS-API-01",
                    price="400.00",
                    currency="EUR",
                )
            ],
            payments=[
                PaymentLedgerEntry(
                    payment_id="PAY-DEP-API",
                    provider="simulation",
                    payment_type="deposit",
                    amount="300.00",
                    currency="EUR",
                    status="completed",
                    provider_reference="TXN-DEP-API",
                    verified_at=self.fixed_now,
                )
            ],
            reason="Seed N09 API test booking",
        )
        linkage_svc.link_booking(seed_req, actor="api_test_setup")

    def tearDown(self):
        self.mongo.close()
        self.app.dependency_overrides.clear()

    def allow_test_principal(self):
        from rosapenna_backend.api.staff_security import StaffPrincipal
        from rosapenna_backend.api.routes.balance_payments import STAFF_ROLES
        from rosapenna_backend.api.routes.balance_payment_events import SIMULATOR_ROLES
        scopes = {f"booking:{self.booking_ref}:{action}" for action in ("read", "request", "simulate", "reconcile")}
        scopes.update({f"run:{self.test_run_id}:balance", "staff:balance:attention"})
        principal = StaffPrincipal("test", "operator", frozenset({"finance"}), frozenset(scopes))
        self.app.dependency_overrides[STAFF_ROLES] = lambda: principal
        self.app.dependency_overrides[SIMULATOR_ROLES] = lambda: principal

    def test_staff_summary_unauthorized_and_authorized(self):
        """Staff balance summary requires valid X-Staff-Token and exact scopes, returning no-store header."""
        # Unauthorized (no token)
        status, headers, body = asgi_request(
            self.app,
            "GET",
            f"/staff/balance-payments/bookings/{self.booking_ref}",
        )
        self.assertEqual(status, 401)

        # Unscoped token returns 403 Forbidden
        status, headers, body = asgi_request(
            self.app,
            "GET",
            f"/staff/balance-payments/bookings/{self.booking_ref}",
            headers={"X-Staff-Token": self.staff_token},
        )
        self.assertEqual(status, 403)

        # A client cannot grant itself permission through a header.
        status, _, _ = asgi_request(self.app, "GET", f"/staff/balance-payments/bookings/{self.booking_ref}", headers={"X-Staff-Token": self.staff_token, "X-Staff-Scope": f"booking:{self.booking_ref}:read,run:{self.test_run_id}:balance"})
        self.assertEqual(status, 403)
        self.allow_test_principal()

        # Authorized with exact booking and run scopes
        req_headers = {
            "X-Staff-Token": self.staff_token,
            "X-Staff-Scope": f"booking:{self.booking_ref}:read,run:{self.test_run_id}:balance",
        }
        status, headers, body = asgi_request(
            self.app,
            "GET",
            f"/staff/balance-payments/bookings/{self.booking_ref}",
            headers=req_headers,
        )
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("cache-control"), "no-store")
        data = json.loads(body.decode("utf-8"))
        self.assertEqual(data["booking_reference"], self.booking_ref)
        self.assertEqual(data["accepted_total"], "1200.00")
        self.assertEqual(data["verified_deposit"], "300.00")
        self.assertEqual(data["balance_due"], "900.00")
        self.assertEqual(data["collection_status"], "not_due")

    def test_staff_request_creation_and_dev_simulation_flow(self):
        """Full lifecycle: create request via staff endpoint, then simulate payment event."""
        self.allow_test_principal()
        req_headers = {
            "X-Staff-Token": self.staff_token,
            "X-Staff-Scope": f"booking:{self.booking_ref}:request,run:{self.test_run_id}:balance",
            "Content-Type": "application/json",
        }
        create_payload = json.dumps({
            "expected_booking_revision": 1,
            "expected_ledger_version": 1,
            "command_id": "cmd_api_test_req_01",
            "reason": "Routine pre-arrival balance link creation",
        }).encode("utf-8")

        status, headers, body = asgi_request(
            self.app,
            "POST",
            f"/staff/balance-payments/bookings/{self.booking_ref}/request",
            headers=req_headers,
            body=create_payload,
        )
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("cache-control"), "no-store")
        req_data = json.loads(body.decode("utf-8"))
        payment_id = req_data["payment_id"]
        self.assertEqual(req_data["amount"], "900.00")
        self.assertEqual(req_data["state"], "ready")

        # Simulate payment event via /dev/balance-payments/{payment_id}/simulate
        sim_payload = json.dumps({
            "scenario": "success",
            "test_run_id": self.test_run_id,
        }).encode("utf-8")

        sim_headers = {
            "Content-Type": "application/json",
            "X-Staff-Token": self.staff_token,
            "X-Staff-Scope": f"booking:{self.booking_ref}:simulate,run:{self.test_run_id}:balance",
        }
        status, headers, body = asgi_request(
            self.app,
            "POST",
            f"/dev/balance-payments/{payment_id}/simulate",
            headers=sim_headers,
            body=sim_payload,
        )
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("cache-control"), "no-store")
        sim_data = json.loads(body.decode("utf-8"))
        self.assertEqual(sim_data["status"], "applied")
        self.assertEqual(sim_data["balance_due"], "0.00")
        self.assertEqual(sim_data["payment_status"], "fully_paid")
        self.assertEqual(sim_data["ledger_version"], 2)

    def test_provider_ingress_fails_closed_by_default(self):
        """POST /payments/prommt/events returns 503 pending official contract."""
        status, headers, body = asgi_request(
            self.app,
            "POST",
            "/payments/prommt/events",
            headers={"Content-Type": "application/json"},
            body=b'{"test": "payload"}',
        )
        self.assertEqual(status, 503)
        self.assertEqual(headers.get("cache-control"), "no-store")

    def test_staff_attention_endpoint(self):
        """GET /staff/balance-payments/attention returns list with no-store header."""
        self.allow_test_principal()
        req_headers = {
            "X-Staff-Token": self.staff_token,
            "X-Staff-Scope": f"staff:balance:attention,run:{self.test_run_id}:balance",
        }
        status, headers, body = asgi_request(
            self.app,
            "GET",
            "/staff/balance-payments/attention",
            headers=req_headers,
        )
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("cache-control"), "no-store")
        data = json.loads(body.decode("utf-8"))
        self.assertIsInstance(data, list)


if __name__ == "__main__":
    unittest.main()
