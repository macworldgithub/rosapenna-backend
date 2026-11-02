"""Unit and integration tests for Manage My Booking (MMB) release control (Ticket M01)."""

import unittest
from unittest.mock import MagicMock
from fastapi import HTTPException
from mongomock import MongoClient

from rosapenna_backend.api.dependencies import require_mmb_enabled
from rosapenna_backend.api.routes.mmb_gate import (
    get_guest_mmb_booking,
    get_mmb_status,
    get_staff_mmb_gate,
    update_staff_mmb_gate,
)
from rosapenna_backend.api.schemas.mmb_gate import (
    StaffMmbGateUpdateRequest,
)
from rosapenna_backend.api.staff_security import StaffPrincipal, require_staff_roles
from rosapenna_backend.config import Settings
from rosapenna_backend.services.mmb_gate import MmbGateService
from rosapenna_backend.services.staff_audit import StaffAuditLog


class TestMmbGateService(unittest.TestCase):
    """Test the MmbGateService isolated logic, MongoDB persistence, and fail-closed defaults."""

    def setUp(self):
        self.mongo = MongoClient(tz_aware=True)
        self.db = self.mongo["mmb_test_db"]
        self.settings = Settings(
            environment="development",
            api_key="M@cWorld123!!",
            staff_api_key="M@cWorld123!!",
            security_bypass_in_dev=False,
        )
        self.service = MmbGateService(database=self.db, settings=self.settings)
        self.audit = StaffAuditLog(self.db["staff_audit_events"])

    def tearDown(self):
        self.mongo.close()

    def test_default_is_disabled_fail_closed(self):
        """MMB must default strictly to False in unconfigured / fresh environment."""
        self.assertFalse(self.service.is_enabled())

    def test_public_status_contains_no_sensitive_data(self):
        """Public status returns enabled=False and safe message without internal secrets."""
        status = self.service.get_public_status()
        self.assertFalse(status["enabled"])
        self.assertIn("disabled", status["message"].lower())
        self.assertNotIn("written_request_reference", status)
        self.assertNotIn("updated_by", status)

    def test_staff_update_records_audit_and_persists_state(self):
        """Staff update successfully flips gate state and records immutable audit event."""
        principal = StaffPrincipal(
            issuer="test_auth",
            subject="manager_1",
            roles=frozenset(["site_admin", "reservations_manager"]),
        )
        req = StaffMmbGateUpdateRequest(
            enabled=True,
            written_request_reference="RSP-MMB-202611-ACTIVATE",
            reason="Approved board release for seasonal beta",
        )

        res = self.service.update_gate(request=req, principal=principal, audit=self.audit)
        self.assertTrue(res["enabled"])
        self.assertEqual(res["written_request_reference"], "RSP-MMB-202611-ACTIVATE")
        self.assertTrue(self.service.is_enabled())

        # Verify MongoDB staff audit log
        audit_events = list(self.db["staff_audit_events"].find({"resource": "mmb_release_gate"}))
        self.assertEqual(len(audit_events), 1)
        self.assertEqual(audit_events[0]["action"], "mmb.gate.enable")
        self.assertEqual(audit_events[0]["actor_subject"], "manager_1")

        # Disable again
        req_off = StaffMmbGateUpdateRequest(
            enabled=False,
            written_request_reference="RSP-MMB-202611-DEACTIVATE",
            reason="Routine maintenance deactivation",
        )
        res_off = self.service.update_gate(request=req_off, principal=principal, audit=self.audit)
        self.assertFalse(res_off["enabled"])
        self.assertFalse(self.service.is_enabled())

        audit_events_after = list(self.db["staff_audit_events"].find({"resource": "mmb_release_gate"}))
        self.assertEqual(len(audit_events_after), 2)
        self.assertEqual(audit_events_after[1]["action"], "mmb.gate.disable")


class TestMmbGateRoutesAndDependencies(unittest.TestCase):
    """Test API route functions, staff authorization gating, and dependency enforcement."""

    def setUp(self):
        self.mongo = MongoClient(tz_aware=True)
        self.db = self.mongo["mmb_routes_db"]
        self.settings = Settings(
            environment="production",
            api_key="M@cWorld123!!",
            staff_api_key="StaffSecretKey123!!",
            security_bypass_in_dev=False,
        )
        self.service = MmbGateService(database=self.db, settings=self.settings)
        self.audit = StaffAuditLog(self.db["staff_audit_events"])

    def tearDown(self):
        self.mongo.close()

    def test_public_status_route(self):
        """get_mmb_status returns MmbStatusResponse with enabled=False."""
        resp = get_mmb_status(gate_service=self.service)
        self.assertFalse(resp.enabled)
        self.assertIn("disabled", resp.message)

    def test_guest_dependency_rejects_with_503_when_mmb_off(self):
        """require_mmb_enabled raises HTTP 503 with retry-after header when MMB is disabled."""
        mock_req = MagicMock()
        mock_req.app.state.mmb_gate_service = self.service

        with self.assertRaises(HTTPException) as ctx:
            require_mmb_enabled(mock_req)
        self.assertEqual(ctx.exception.status_code, 503)
        self.assertIn("unavailable", ctx.exception.detail.lower())
        self.assertEqual(ctx.exception.headers.get("Retry-After"), "300")

    def test_guest_endpoint_accessible_when_enabled(self):
        """get_guest_mmb_booking returns booking dict when called."""
        res = get_guest_mmb_booking(booking_ref="RSP-20261102-TEST")
        self.assertEqual(res["booking_reference"], "RSP-20261102-TEST")
        self.assertEqual(res["status"], "active")

    def test_staff_security_blocks_unauthorized_staff(self):
        """require_staff_roles raises 401 on missing or wrong staff token when security is enforced."""
        from unittest.mock import patch

        prod_settings = Settings(
            api_key="M@cWorld123!!",
            staff_api_key="StaffSecretKey123!!",
            security_bypass_in_dev=False,
            environment="production",
            cors_origins="https://rosapenna.ie",
            docs_enabled=False,
        )
        with patch("rosapenna_backend.api.staff_security.settings", prod_settings):
            staff_dep = require_staff_roles("site_admin", "reservations_manager")

            # Missing token
            with self.assertRaises(HTTPException) as ctx:
                staff_dep(x_staff_token=None)
            self.assertEqual(ctx.exception.status_code, 401)

            # Invalid token
            with self.assertRaises(HTTPException) as ctx:
                staff_dep(x_staff_token="invalid-token")
            self.assertEqual(ctx.exception.status_code, 401)

    def test_staff_update_route_lifecycle(self):
        """update_staff_mmb_gate updates state and returns updated staff status."""
        principal = StaffPrincipal(
            issuer="rosapenna_staff_auth",
            subject="staff_operator",
            roles=frozenset(["site_admin", "reservations_manager"]),
        )

        # 1. Inspect initial staff status
        initial_status = get_staff_mmb_gate(principal=principal, gate_service=self.service)
        self.assertFalse(initial_status.enabled)

        # 2. Update to enabled
        update_req = StaffMmbGateUpdateRequest(
            enabled=True,
            written_request_reference="RSP-MMB-WRITTEN-REQ-999",
            reason="Management written approval received",
        )
        updated_status = update_staff_mmb_gate(
            request=update_req,
            principal=principal,
            gate_service=self.service,
            audit=self.audit,
        )
        self.assertTrue(updated_status.enabled)
        self.assertEqual(updated_status.written_request_reference, "RSP-MMB-WRITTEN-REQ-999")
        self.assertTrue(self.service.is_enabled())

        # 3. Update to disabled
        disable_req = StaffMmbGateUpdateRequest(
            enabled=False,
            written_request_reference="RSP-MMB-WRITTEN-REQ-1000",
            reason="Deactivated after season closure",
        )
        disabled_status = update_staff_mmb_gate(
            request=disable_req,
            principal=principal,
            gate_service=self.service,
            audit=self.audit,
        )
        self.assertFalse(disabled_status.enabled)
        self.assertFalse(self.service.is_enabled())


if __name__ == "__main__":
    unittest.main()
