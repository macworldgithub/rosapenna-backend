"""Unit and integration tests for Ticket M14 — Staff Request Outcomes."""

import datetime as dt
import unittest
from unittest.mock import MagicMock
from mongomock import MongoClient

from rosapenna_backend.api.schemas.booking_operations import (
    BookingOperationRecord,
    ReadbackEvidence,
)
from rosapenna_backend.api.schemas.staff_requests import (
    StaffRequestTransitionCreate,
)
from rosapenna_backend.api.staff_security import StaffPrincipal
from rosapenna_backend.services.booking_linkage import BookingLinkageService
from rosapenna_backend.services.booking_operations import BookingOperationEngine
from rosapenna_backend.services.email_delivery import EmailDeliveryService
from rosapenna_backend.services.staff_audit import StaffAuditLog
from rosapenna_backend.services.staff_requests import (
    StaffRequestError,
    StaffRequestService,
)


class TestStaffRequestOutcomesM14(unittest.TestCase):
    """Test suite covering authorized staff review and outcomes for special and cancellation requests."""

    def setUp(self):
        self.mongo = MongoClient(tz_aware=True)
        self.db = self.mongo["test_staff_m14_db"]
        self.linkage_service = BookingLinkageService(database=self.db)
        self.email_service = EmailDeliveryService()
        self.audit_log = StaffAuditLog(collection=self.db["staff_audit_events"])
        self.operation_engine = MagicMock(spec=BookingOperationEngine)
        self.operation_engine.get_operation.return_value = None
        self.service = StaffRequestService(
            database=self.db,
            linkage_service=self.linkage_service,
            email_service=self.email_service,
            operation_engine=self.operation_engine,
            staff_audit_log=self.audit_log,
        )

        self.agent_principal = StaffPrincipal(
            issuer="test_auth",
            subject="staff_agent_sarah",
            roles=frozenset(["reservations_agent"]),
        )
        self.manager_principal = StaffPrincipal(
            issuer="test_auth",
            subject="staff_mgr_fiona",
            roles=frozenset(["reservations_manager", "site_admin"]),
        )

        # Seed confirmed booking with a pending special request and a cancellation request
        self.booking_ref = "RSP-20270810-M1401"
        self.db.mmb_booking_linkages.insert_one({
            "booking_reference": self.booking_ref,
            "source_channel": "online",
            "status": "linked",
            "revision": 1,
            "lead_guest": {
                "first_name": "Ciaran",
                "last_name": "Gallagher",
                "email": "ciaran@example.com",
                "telephone": "+353 74 915 5000",
            },
            "opera_stay": {
                "reservation_id": "OPERA-M1401",
                "confirmation_number": "CONF-M1401",
                "room_category": "DELUXE",
                "room_quantity": 1,
                "arrival_date": "2027-08-10",
                "departure_date": "2027-08-13",
                "total_amount": "1200.00",
                "status": "Confirmed",
                "occupant_names": ["Ciaran Gallagher"],
            },
            "brs_rounds": [
                {
                    "round_id": "RND-SANDY-M1401",
                    "course_id": 2,
                    "course_name": "Sandy Hills",
                    "date": "2027-08-11",
                    "time": "09:30",
                    "holes": "18",
                    "players": 2,
                    "price": "200.00",
                    "currency": "EUR",
                    "status": "Confirmed",
                    "brs_booking_id": "BRS-M1401",
                }
            ],
            "payments": [
                {
                    "payment_id": "PAY-M1401",
                    "provider": "prommt",
                    "amount": "300.00",
                    "currency": "EUR",
                    "status": "completed",
                    "payment_type": "deposit",
                    "verified_at": dt.datetime(2026, 11, 1, 10, 5, tzinfo=dt.timezone.utc),
                }
            ],
            "financials": {
                "total_amount": "1200.00",
                "deposit_paid": "300.00",
                "balance_due": "900.00",
                "currency": "EUR",
            },
            "created_at": dt.datetime(2026, 11, 1, 10, 0, tzinfo=dt.timezone.utc),
            "updated_at": dt.datetime(2026, 11, 1, 10, 0, tzinfo=dt.timezone.utc),
            "pending_requests": [
                {
                    "request_id": "REQ-DIET-001",
                    "request_type": "special_request",
                    "category": "dietary",
                    "description": "Strict coeliac meal prep needed for 1 guest",
                    "status": "pending_review",
                    "submitted_at": dt.datetime.now(dt.timezone.utc),
                    "history": [],
                },
                {
                    "request_id": "REQ-SHIFT-002",
                    "request_type": "special_request",
                    "category": "golf_preference",
                    "description": "Proposing shift of Sandy Hills tee-time to 11:00 AM",
                    "desired_golf_notes": "11:00 AM Sandy Hills",
                    "status": "pending_review",
                    "submitted_at": dt.datetime.now(dt.timezone.utc),
                    "history": [],
                },
                {
                    "request_id": "REQ-CANCEL-003",
                    "request_type": "cancellation_request",
                    "category": "cancellation_inquiry",
                    "description": "Guest family wedding conflict; requesting cancellation policy inquiry",
                    "status": "pending_review",
                    "submitted_at": dt.datetime.now(dt.timezone.utc),
                    "history": [],
                },
            ],
            "audit_trail": [],
        })

    def test_ac1_staff_list_and_details_authorized(self):
        """AC 1: Authorized staff can view requests across bookings and inspect detail context."""
        resp = self.service.list_requests(self.agent_principal)
        self.assertEqual(resp.total, 3)

        detail = self.service.get_request_details(self.agent_principal, self.booking_ref, "REQ-DIET-001")
        self.assertEqual(detail.request_id, "REQ-DIET-001")
        self.assertEqual(detail.lead_guest_name, "Ciaran Gallagher")
        self.assertEqual(detail.status, "pending_review")

    def test_ac2_distinct_persisted_outcomes_approval_not_completed(self):
        """AC 2: needs-information, rejected, and approved-awaiting-implementation are distinct; approval is not completed."""
        # 1. Transition to needs_information
        resp1 = self.service.transition_request(
            principal=self.agent_principal,
            booking_reference=self.booking_ref,
            request_id="REQ-DIET-001",
            payload=StaffRequestTransitionCreate(
                target_status="needs_information",
                reason="Please clarify if coeliac certification is required for restaurant catering",
                guest_message="Could you confirm if cross-contamination precautions are sufficient?",
            ),
            idempotency_key="idemp_needs_info_111",
        )
        self.assertEqual(resp1.prior_status, "pending_review")
        self.assertEqual(resp1.new_status, "needs_information")
        self.assertEqual(resp1.f12_delivery_status, "simulated_sandbox")

        # Authoritative readback
        detail1 = self.service.get_request_details(self.agent_principal, self.booking_ref, "REQ-DIET-001")
        self.assertEqual(detail1.status, "needs_information")
        self.assertEqual(len(detail1.history), 1)
        self.assertEqual(detail1.history[0].actor, "staff_agent_sarah")

        # 2. Transition to approved_awaiting_implementation
        resp2 = self.service.transition_request(
            principal=self.agent_principal,
            booking_reference=self.booking_ref,
            request_id="REQ-SHIFT-002",
            payload=StaffRequestTransitionCreate(
                target_status="approved_awaiting_implementation",
                reason="Pro Shop agreed to 11:00 AM Sandy Hills tee time; queued for BRS booking write",
            ),
            idempotency_key="idemp_approve_222",
        )
        self.assertEqual(resp2.new_status, "approved_awaiting_implementation")

        detail2 = self.service.get_request_details(self.agent_principal, self.booking_ref, "REQ-SHIFT-002")
        # Explicit check: status is NOT completed
        self.assertEqual(detail2.status, "approved_awaiting_implementation")
        self.assertNotEqual(detail2.status, "completed")

    def test_ac3_ac4_completed_requires_verified_f14_or_vendor_readback(self):
        """AC 3 & 4: Staff toggle cannot bypass F14 / vendor readback for date/room/golf changes."""
        # Attempting to mark date/golf change completed without execution details fails
        with self.assertRaises(StaffRequestError) as ctx:
            self.service.transition_request(
                principal=self.agent_principal,
                booking_reference=self.booking_ref,
                request_id="REQ-SHIFT-002",
                payload=StaffRequestTransitionCreate(
                    target_status="completed",
                    reason="Trying to toggle completed without F14 or readback",
                ),
                idempotency_key="idemp_bypass_attempt_1",
            )
        self.assertEqual(ctx.exception.status_code, 422)
        self.assertIn("A staff status toggle alone cannot bypass these checks", ctx.exception.message)

        # Attempting with invalid or non-completed F14 operation fails
        with self.assertRaises(StaffRequestError) as ctx2:
            self.service.transition_request(
                principal=self.agent_principal,
                booking_reference=self.booking_ref,
                request_id="REQ-SHIFT-002",
                payload=StaffRequestTransitionCreate(
                    target_status="completed",
                    execution_type="f14_operation",
                    f14_operation_id="OP-DOES-NOT-EXIST",
                    reason="Referencing fake operation",
                ),
                idempotency_key="idemp_fake_op_attempt",
            )
        self.assertEqual(ctx2.exception.status_code, 422)
        self.assertIn("Referenced F14 operation 'OP-DOES-NOT-EXIST' was not found", ctx2.exception.message)

        # Successfully completing with verified vendor readback reference
        resp = self.service.transition_request(
            principal=self.manager_principal,
            booking_reference=self.booking_ref,
            request_id="REQ-SHIFT-002",
            payload=StaffRequestTransitionCreate(
                target_status="completed",
                execution_type="vendor_readback_verified",
                vendor_readback_ref="BRS-CONF-SHIFT-9876",
                reason="Pro Shop moved tee-time directly in BRS; verified on tee sheet 11:00 AM",
            ),
            idempotency_key="idemp_valid_readback_333",
        )
        self.assertEqual(resp.new_status, "completed")
        detail = self.service.get_request_details(self.agent_principal, self.booking_ref, "REQ-SHIFT-002")
        self.assertEqual(detail.status, "completed")

    def test_ac5_cancellation_execution_blocked_without_documented_policy(self):
        """AC 5: Cancellation execution unavailable without documented policy and manager signoff."""
        # Attempting to complete cancellation inquiry without authorization ref fails
        with self.assertRaises(StaffRequestError) as ctx:
            self.service.transition_request(
                principal=self.agent_principal,
                booking_reference=self.booking_ref,
                request_id="REQ-CANCEL-003",
                payload=StaffRequestTransitionCreate(
                    target_status="completed",
                    reason="Staff trying to close cancellation without authorization ref",
                ),
                idempotency_key="idemp_cancel_fail_1",
            )
        self.assertEqual(ctx.exception.status_code, 422)
        self.assertIn("cancellation_authorization_ref", ctx.exception.message)

        # Agent role cannot record completed cancellation
        with self.assertRaises(StaffRequestError) as ctx2:
            self.service.transition_request(
                principal=self.agent_principal,
                booking_reference=self.booking_ref,
                request_id="REQ-CANCEL-003",
                payload=StaffRequestTransitionCreate(
                    target_status="completed",
                    cancellation_authorization_ref="AUTH-POLICY-6WK-RETENTION",
                    execution_type="vendor_readback_verified",
                    vendor_readback_ref="MANUAL-FILE-SIGN",
                    reason="Manager approved terms",
                ),
                idempotency_key="idemp_cancel_role_fail",
            )
        self.assertEqual(ctx2.exception.status_code, 403)

        # Manager with authorization ref can record outcome
        resp = self.service.transition_request(
            principal=self.manager_principal,
            booking_reference=self.booking_ref,
            request_id="REQ-CANCEL-003",
            payload=StaffRequestTransitionCreate(
                target_status="completed",
                cancellation_authorization_ref="AUTH-POLICY-6WK-RETENTION",
                execution_type="vendor_readback_verified",
                vendor_readback_ref="MANUAL-FILE-SIGN",
                reason="Manager signed off on retention terms per booking contract",
            ),
            idempotency_key="idemp_cancel_mgr_success",
        )
        self.assertEqual(resp.new_status, "completed")

        # Crucially: Confirmed Opera stay and payments remain untouched
        linkage = self.linkage_service.get_linkage(self.booking_ref)
        self.assertEqual(linkage.opera_stay.status, "Confirmed")
        self.assertEqual(len(linkage.payments), 1)

    def test_ac6_f12_outcome_notification_and_retry(self):
        """AC 6: F12 notification sent after durable storage and can be retried safely."""
        resp = self.service.transition_request(
            principal=self.agent_principal,
            booking_reference=self.booking_ref,
            request_id="REQ-DIET-001",
            payload=StaffRequestTransitionCreate(
                target_status="rejected",
                reason="Requested private dining room is fully booked for requested dates",
                guest_message="We can accommodate your party in the main dining room with the same menu.",
            ),
            idempotency_key="idemp_reject_444",
        )
        self.assertEqual(resp.new_status, "rejected")
        self.assertIsNotNone(resp.f12_delivery_id)

        # Retry notification without mutating revision or request status
        retry_resp = self.service.retry_notification(
            principal=self.agent_principal,
            booking_reference=self.booking_ref,
            request_id="REQ-DIET-001",
        )
        self.assertEqual(retry_resp.new_status, "rejected")
        self.assertEqual(retry_resp.booking_revision, resp.booking_revision)

    def test_ac8_idempotency_replay_and_conflict(self):
        """AC 8: Idempotency deduplication replays identical response; conflicting payload raises HTTP 409."""
        payload = StaffRequestTransitionCreate(
            target_status="needs_information",
            reason="Initial question regarding golf handicap requirement",
        )
        key = "idemp_staff_dedup_555"

        resp1 = self.service.transition_request(
            principal=self.agent_principal,
            booking_reference=self.booking_ref,
            request_id="REQ-SHIFT-002",
            payload=payload,
            idempotency_key=key,
        )
        self.assertFalse(resp1.replayed)

        # Replay with same payload
        resp2 = self.service.transition_request(
            principal=self.agent_principal,
            booking_reference=self.booking_ref,
            request_id="REQ-SHIFT-002",
            payload=payload,
            idempotency_key=key,
        )
        self.assertTrue(resp2.replayed)
        self.assertEqual(resp1.transition_recorded_at, resp2.transition_recorded_at)

        # Replay with conflicting parameters raises 409
        conflicting = StaffRequestTransitionCreate(
            target_status="rejected",
            reason="Completely different decision under same idempotency key",
        )
        with self.assertRaises(StaffRequestError) as ctx:
            self.service.transition_request(
                principal=self.agent_principal,
                booking_reference=self.booking_ref,
                request_id="REQ-SHIFT-002",
                payload=conflicting,
                idempotency_key=key,
            )
        self.assertEqual(ctx.exception.status_code, 409)
