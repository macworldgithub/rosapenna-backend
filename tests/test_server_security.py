"""Automated security and access control tests for Ticket F06."""

import asyncio
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import HTTPException, Request
from mongomock import MongoClient

from rosapenna_backend.api.routes.staff_operations import list_sessions, refresh_teesheets
from rosapenna_backend.api.security import _rate_buckets, _rate_lock, rate_limit, verify_api_key
from rosapenna_backend.api.staff_security import StaffPrincipal, require_staff_roles
from rosapenna_backend.config import Settings, validate_security_settings
from rosapenna_backend.core.middleware import RequestBodySizeLimitMiddleware
from rosapenna_backend.services.booking_sessions import BookingSessionManager
from rosapenna_backend.services.staff_audit import StaffAuditLog
from rosapenna_backend.services.tee_sheets import TeesheetCache


def make_mock_request(client_ip: str = "127.0.0.1", headers: dict | None = None) -> Request:
    """Create a minimal mock FastAPI Request."""
    headers_list = [(k.lower().encode("latin-1"), v.encode("latin-1")) for k, v in (headers or {}).items()]
    scope = {
        "type": "http",
        "client": (client_ip, 12345),
        "headers": headers_list,
        "path": "/test",
        "method": "GET",
    }
    return Request(scope)


class ServerSecurityTests(unittest.TestCase):
    """Test suite for server security, rate limits, size limits, and staff authorization."""

    def setUp(self):
        self.mongo = MongoClient(tz_aware=True)
        self.db = self.mongo["security_test_db"]
        self.audit_log = StaffAuditLog(self.db["staff_audit_events"])

        # Reset rate limiting state between tests
        with _rate_lock:
            _rate_buckets.clear()

    def tearDown(self):
        self.mongo.close()

    def test_root_endpoint_is_publicly_accessible(self):
        """Root endpoint / returns service status without authentication."""
        from rosapenna_backend.api.routes.health import health

        mock_app = MagicMock()
        mock_manager = MagicMock(spec=BookingSessionManager)
        mock_manager.active_count.return_value = 0
        mock_app.state.session_manager = mock_manager
        mock_app.state.settings = Settings()

        req = MagicMock(spec=Request)
        req.app = mock_app

        res = health(req)
        self.assertEqual(res["status"], "ok")
        self.assertIn("service", res)
        self.assertEqual(res["service"], "Rosapenna Booking API")
        self.assertEqual(res["active_sessions"], 0)

    def test_dev_bypass_allows_unauthenticated_api_calls(self):
        """When SECURITY_BYPASS_IN_DEV=true in development, requests pass without API key."""
        dev_settings = Settings(
            api_key="M@cWorld123!!",
            staff_api_key="M@cWorld123!!",
            security_bypass_in_dev=True,
            environment="development",
        )
        with patch("rosapenna_backend.api.security.settings", dev_settings), \
             patch("rosapenna_backend.api.staff_security.settings", dev_settings):
            # 1. verify_api_key without header succeeds
            verify_api_key(x_api_key=None)

            # 2. staff dependency without header returns developer principal
            staff_dep = require_staff_roles("site_admin")
            principal = staff_dep(x_staff_token=None)
            self.assertEqual(principal.subject, "developer")
            self.assertIn("site_admin", principal.roles)

            # 3. rate_limit is bypassed
            req = make_mock_request()
            for _ in range(50):
                rate_limit(req)

    def test_enforced_security_blocks_missing_or_invalid_api_key(self):
        """When security is enforced, requests must supply valid X-API-Key."""
        prod_settings = Settings(
            api_key="M@cWorld123!!",
            staff_api_key="M@cWorld123!!",
            security_bypass_in_dev=False,
            environment="production",
            cors_origins="https://rosapenna.ie",
            docs_enabled=False,
        )
        with patch("rosapenna_backend.api.security.settings", prod_settings):
            # Missing key -> 401
            with self.assertRaises(HTTPException) as ctx:
                verify_api_key(x_api_key=None)
            self.assertEqual(ctx.exception.status_code, 401)
            self.assertIn("Missing or invalid", ctx.exception.detail)

            # Wrong key -> 401
            with self.assertRaises(HTTPException) as ctx:
                verify_api_key(x_api_key="wrong-key")
            self.assertEqual(ctx.exception.status_code, 401)

            # Valid key (M@cWorld123!!) -> succeeds
            verify_api_key(x_api_key="M@cWorld123!!")

    def test_enforced_security_blocks_missing_or_invalid_staff_token(self):
        """When security is enforced, staff operations require valid X-Staff-Token."""
        prod_settings = Settings(
            api_key="M@cWorld123!!",
            staff_api_key="M@cWorld123!!",
            security_bypass_in_dev=False,
            environment="production",
            cors_origins="https://rosapenna.ie",
            docs_enabled=False,
        )
        with patch("rosapenna_backend.api.staff_security.settings", prod_settings):
            staff_dep = require_staff_roles("site_admin")

            # Missing staff token -> 401
            with self.assertRaises(HTTPException) as ctx:
                staff_dep(x_staff_token=None)
            self.assertEqual(ctx.exception.status_code, 401)
            self.assertIn("X-Staff-Token", ctx.exception.detail)

            # Wrong staff token -> 401
            with self.assertRaises(HTTPException) as ctx:
                staff_dep(x_staff_token="wrong-token")
            self.assertEqual(ctx.exception.status_code, 401)

            # Valid staff token (M@cWorld123!!) -> succeeds and assigns principal
            principal = staff_dep(x_staff_token="M@cWorld123!!")
            self.assertEqual(principal.subject, "staff_operator")
            self.assertIn("site_admin", principal.roles)

    def test_staff_operations_write_immutable_audit_logs(self):
        """Staff endpoints execute and append immutable events in MongoDB staff_audit_events."""
        principal = StaffPrincipal(
            issuer="rosapenna_staff_auth",
            subject="staff_operator",
            roles=frozenset(["site_admin"]),
        )

        mock_manager = MagicMock(spec=BookingSessionManager)
        mock_manager.list_sessions.return_value = [{"session_id": "sess-123"}]

        # 1. list_sessions writes audit entry
        sessions = list_sessions(principal=principal, audit=self.audit_log, manager=mock_manager)
        self.assertEqual(len(sessions), 1)

        events = list(self.db["staff_audit_events"].find({"action": "sessions.list"}))
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["actor_subject"], "staff_operator")
        self.assertEqual(events[0]["outcome"], "success")

        # 2. refresh_teesheets writes audit entry
        mock_cache = MagicMock(spec=TeesheetCache)
        mock_cache.refresh.return_value = {"updated": 10}

        res = refresh_teesheets(
            retry_failed=False,
            principal=principal,
            audit=self.audit_log,
            cache=mock_cache,
        )
        self.assertEqual(res["updated"], 10)

        refresh_events = list(self.db["staff_audit_events"].find({"action": "teesheets.refresh"}))
        self.assertGreaterEqual(len(refresh_events), 1)
        self.assertEqual(refresh_events[0]["actor_subject"], "staff_operator")

    def test_rate_limiting_enforcement(self):
        """When security is enforced, exceeding rate_limit_per_minute returns 429."""
        strict_settings = Settings(
            api_key="M@cWorld123!!",
            staff_api_key="M@cWorld123!!",
            security_bypass_in_dev=False,
            rate_limit_per_minute=5,
            environment="production",
            cors_origins="https://rosapenna.ie",
            docs_enabled=False,
        )
        with patch("rosapenna_backend.api.security.settings", strict_settings):
            req = make_mock_request(client_ip="192.168.1.100")

            # First 5 calls pass
            for _ in range(5):
                rate_limit(req)

            # 6th call exceeds limit -> 429
            with self.assertRaises(HTTPException) as ctx:
                rate_limit(req)
            self.assertEqual(ctx.exception.status_code, 429)
            self.assertIn("Rate limit exceeded", ctx.exception.detail)

    def test_request_body_size_limit_rejection(self):
        """Middleware rejects request bodies that exceed max_bytes with HTTP 413."""
        async def dummy_app(scope, receive, send):
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"OK"})

        middleware = RequestBodySizeLimitMiddleware(dummy_app, max_bytes=100)

        sent_messages = []
        async def mock_send(msg):
            sent_messages.append(msg)

        # 1. Content-Length header exceeding limit
        oversized_scope = {
            "type": "http",
            "headers": [(b"content-length", b"200")],
        }
        async def mock_receive():
            return {"type": "http.request", "body": b"A" * 200, "more_body": False}

        asyncio.run(middleware(oversized_scope, mock_receive, mock_send))
        self.assertEqual(sent_messages[0]["status"], 413)

        # 2. Payload within limit (50 bytes <= 100)
        sent_messages.clear()
        valid_scope = {
            "type": "http",
            "headers": [(b"content-length", b"50")],
        }
        async def mock_receive_valid():
            return {"type": "http.request", "body": b"A" * 50, "more_body": False}

        asyncio.run(middleware(valid_scope, mock_receive_valid, mock_send))
        self.assertEqual(sent_messages[0]["status"], 200)

    def test_production_security_validation(self):
        """validate_security_settings enforces production rules and accepts M@cWorld123!!."""
        # 1. Rejects dev bypass in production
        bad_bypass = Settings(
            environment="production",
            security_bypass_in_dev=True,
            api_key="M@cWorld123!!",
            staff_api_key="M@cWorld123!!",
            cors_origins="https://rosapenna.ie",
            docs_enabled=False,
        )
        with patch("rosapenna_backend.config.settings", bad_bypass):
            with self.assertRaises(RuntimeError) as ctx:
                validate_security_settings()
            self.assertIn("SECURITY_BYPASS_IN_DEV", str(ctx.exception))

        # 2. Rejects key shorter than min_key_length
        short_key = Settings(
            environment="production",
            security_bypass_in_dev=False,
            api_key="short",
            staff_api_key="M@cWorld123!!",
            cors_origins="https://rosapenna.ie",
            docs_enabled=False,
            min_key_length=12,
        )
        with patch("rosapenna_backend.config.settings", short_key):
            with self.assertRaises(RuntimeError) as ctx:
                validate_security_settings()
            self.assertIn("BOOKING_API_KEY", str(ctx.exception))

        # 3. User chosen password M@cWorld123!! (13 chars) passes production validation
        valid_prod = Settings(
            environment="production",
            security_bypass_in_dev=False,
            api_key="M@cWorld123!!",
            staff_api_key="M@cWorld123!!",
            cors_origins="https://rosapenna.ie",
            docs_enabled=False,
            min_key_length=12,
        )
        with patch("rosapenna_backend.config.settings", valid_prod):
            validate_security_settings()


if __name__ == "__main__":
    unittest.main()
