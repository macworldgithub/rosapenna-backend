"""Tests for Ticket M16: Action history and recovery view."""

import datetime as dt
from decimal import Decimal
import unittest
from unittest.mock import MagicMock

from mongomock import MongoClient

from rosapenna_backend.api.schemas.booking_linkage import (
    BrsRoundDetails,
    LeadGuestContact,
    OperaStayDetails,
    PaymentLedgerEntry,
    StaffCorrectLinkageRequest,
    StaffLinkBookingRequest,
)
from rosapenna_backend.api.schemas.booking_operations import (
    BookingOperationRecord,
    ReadbackEvidence,
    StaffReconciliationAlert,
    StageHistoryEntry,
)
from rosapenna_backend.api.schemas.mmb_amendments import (
    AmendmentOfferSnapshot,
    AmendmentPricingComparison,
    GuestSafeBookingSummary,
    ProposedGolfRoundOption,
    ProposedStayOption,
)
from rosapenna_backend.api.schemas.mmb_history import StaffRecoveryRetryRequest
from rosapenna_backend.api.staff_security import StaffPrincipal
from rosapenna_backend.services.booking_linkage import BookingLinkageService
from rosapenna_backend.services.booking_operations import BookingOperationEngine
from rosapenna_backend.services.mmb_amendments import MmbAmendmentService
from rosapenna_backend.services.mmb_history import MmbHistoryService
from rosapenna_backend.services.staff_audit import StaffAuditLog


class TestMmbM16ActionHistoryAndRecovery(unittest.TestCase):
    def setUp(self):
        self.client = MongoClient()
        self.db = self.client["rosapenna_test"]
        self.audit_log = StaffAuditLog(self.db["staff_audit_events"])

        self.mock_opera = MagicMock()
        self.mock_brs = MagicMock()
        self.mock_email = MagicMock()
        self.mock_pricing = MagicMock()

        self.linkage_service = BookingLinkageService(
            database=self.db,
            opera_client=self.mock_opera,
        )

        self.amendment_service = MmbAmendmentService(
            database=self.db,
            linkage_service=self.linkage_service,
        )

        self.operation_engine = BookingOperationEngine(
            database=self.db,
            pricing_service=self.mock_pricing,
            linkage_service=self.linkage_service,
            opera_client=self.mock_opera,
            brs_client=self.mock_brs,
            email_service=self.mock_email,
            amendment_service=self.amendment_service,
        )

        self.history_service = MmbHistoryService(
            database=self.db,
            linkage_service=self.linkage_service,
            operation_engine=self.operation_engine,
            staff_audit_log=self.audit_log,
        )

        self.staff_principal = StaffPrincipal(
            issuer="test_staff",
            subject="agent_sarah",
            roles=frozenset(["reservations_agent"]),
        )

        # Setup standard test booking
        self.ref = "RSP-20270810-M1601"
        self._setup_test_booking()

    def _setup_test_booking(self):
        lead = LeadGuestContact(
            first_name="Arthur",
            last_name="Balfour",
            email="arthur.balfour@example.com",
            telephone="+44 7700 900123",
        )
        stay = OperaStayDetails(
            reservation_id="RES-OP-M1601",
            confirmation_number="CONF-OP-M1601",
            hotel_id="rosapenna",
            room_category="Deluxe Room",
            room_quantity=1,
            arrival_date=dt.date(2027, 8, 10),
            departure_date=dt.date(2027, 8, 13),
            total_amount="1200.00",
            currency="EUR",
            status="Confirmed",
            occupant_names=["Arthur Balfour"],
        )
        rounds = [
            BrsRoundDetails(
                round_id="RND-SANDY-101",
                course_id=3,
                course_name="Sandy Hills Links",
                date=dt.date(2027, 8, 11),
                time="09:30",
                players=2,
                holes="18",
                price="300.00",
                status="Confirmed",
            )
        ]
        payments = [
            PaymentLedgerEntry(
                payment_id="PMT-DEP-001",
                provider="prommt",
                payment_type="deposit",
                amount="300.00",
                currency="EUR",
                status="completed",
                provider_reference="ch_prommt_dep_001",
                verified_at=dt.datetime(2027, 1, 10, 10, 0, tzinfo=dt.timezone.utc),
            )
        ]
        link_req = StaffLinkBookingRequest(
            booking_reference=self.ref,
            reason="Initial booking test setup for M16",
            lead_guest=lead,
            manual_opera_stay=stay,
            brs_rounds=rounds,
            payments=payments,
            source_channel="online",
        )
        self.linkage_service.link_booking(link_req, actor="test_setup")

    def test_guest_history_privacy_filtered(self):
        """AC 1, AC 2: Guest view contains time-ordered material events with sensitive staff data excluded."""
        # 1. Add guest request
        self.linkage_service.add_pending_request(
            booking_reference=self.ref,
            request_item={
                "request_id": "REQ-TEST-001",
                "request_type": "special_request",
                "category": "dietary",
                "description": "Gluten free breakfast required for room 101.",
                "notes": "internal_notes: VIP guest partner",
            },
            actor="guest",
        )

        # 2. Add pre-arrival reminder in database
        self.db["booking_reminders"].insert_one({
            "reminder_id": "REM-001",
            "booking_reference": self.ref,
            "revision_number": 1,
            "reminder_type": "balance_due_first",
            "title": "Balance Due Notification (42 Days)",
            "scheduled_date": "2027-06-29",
            "status": "scheduled",
            "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        })

        # 3. Simulate an unconfirmed exception alert in booking_operations
        op_rec = BookingOperationRecord(
            operation_id="OP-HALTED-001",
            idempotency_key_hash="hash_halted_001",
            operation_type="amendment",
            booking_reference=self.ref,
            stage="unconfirmed_reconciliation",
            payment_id="PMT-AMEND-001",
            payment_verified=True,
            staff_alert=StaffReconciliationAlert(
                operation_id="OP-HALTED-001",
                booking_reference=self.ref,
                failed_stage="hotel_write",
                reason="Opera PMS timed out after 3 retries. Stack trace: ConnectionResetError",
            ),
        )
        self.operation_engine._save_record(op_rec)

        res = self.history_service.get_guest_history(self.ref)
        self.assertEqual(res.booking_reference, self.ref)
        self.assertEqual(res.lead_guest_name, "Arthur Balfour")
        self.assertGreaterEqual(len(res.events), 3)

        # Verify privacy filtering
        for ev in res.events:
            # Sensitive internal staff notes excluded
            if ev.details:
                self.assertNotIn("internal_notes:", ev.details)
                self.assertNotIn("ConnectionResetError", ev.details)
            # Staff actor identifiers masked to friendly label
            self.assertNotEqual(ev.actor_label, "agent_sarah")

        # Verify exception event is framed as reassuring holding state
        exc_events = [e for e in res.events if e.is_exception]
        self.assertEqual(len(exc_events), 1)
        self.assertIn("held for staff review", exc_events[0].summary.lower())
        self.assertIn("remain confirmed and protected", exc_events[0].details.lower())

    def test_staff_history_full_fidelity_and_audit(self):
        """AC 1, AC 3, AC 7: Staff view contains full technical details and records StaffAuditLog entry."""
        # Add a halted operation with full technical details
        op_rec = BookingOperationRecord(
            operation_id="OP-TECH-002",
            idempotency_key_hash="hash_tech_002",
            operation_type="amendment",
            booking_reference=self.ref,
            stage="unconfirmed_reconciliation",
            payment_id="PMT-AMEND-002",
            payment_verified=True,
            vendor_references={"amended_opera_reservation_id": "RES-OP-999"},
            staff_alert=StaffReconciliationAlert(
                operation_id="OP-TECH-002",
                booking_reference=self.ref,
                failed_stage="hotel_write",
                reason="Opera PMS returned 504 Gateway Timeout",
            ),
            stage_history=[
                StageHistoryEntry(stage="initialized", actor="guest", details="Init commit"),
                StageHistoryEntry(stage="unconfirmed_reconciliation", actor="operation_engine", details="Halted at hotel_write"),
            ],
        )
        self.operation_engine._save_record(op_rec)

        res = self.history_service.get_staff_history(self.ref, self.staff_principal)
        self.assertEqual(res.booking_reference, self.ref)
        self.assertEqual(res.open_exceptions_count, 1)

        # Confirm stage history is visible in staff view
        stage_history_evs = [e for e in res.events if "Init commit" in (e.details or "")]
        self.assertEqual(len(stage_history_evs), 1)

        # Confirm audit event was persisted in StaffAuditLog
        audit_events = list(self.db["staff_audit_events"].find({"resource": f"booking:{self.ref}"}))
        self.assertGreaterEqual(len(audit_events), 1)
        self.assertEqual(audit_events[0]["actor_subject"], "agent_sarah")
        self.assertEqual(audit_events[0]["action"], "history.view")

    def test_list_recovery_exceptions(self):
        """AC 3: Staff can list unconfirmed reconciliations, partial writes, and notification failures."""
        # 1. Halted amendment operation in booking_operations
        op_rec = BookingOperationRecord(
            operation_id="OP-EXC-003",
            idempotency_key_hash="hash_exc_003",
            operation_type="amendment",
            booking_reference=self.ref,
            stage="unconfirmed_reconciliation",
            payment_id="PMT-BAL-003",
            payment_verified=True,
            staff_alert=StaffReconciliationAlert(
                operation_id="OP-EXC-003",
                booking_reference=self.ref,
                failed_stage="golf_write",
                reason="BRS tee slot conflict on St Patrick's Links",
            ),
        )
        self.operation_engine._save_record(op_rec)

        # 2. Failed F12 notification request in staff_request_operations
        self.db["staff_request_operations"].insert_one({
            "request_id": "REQ-NOTIF-FAIL-01",
            "booking_reference": self.ref,
            "f12_delivery_status": "failed",
            "f14_operation_id": "OP-EXC-003",
            "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        })

        res = self.history_service.list_recovery_exceptions(principal=self.staff_principal)
        self.assertGreaterEqual(res.total_exceptions, 2)
        self.assertGreaterEqual(res.unconfirmed_reconciliations_count, 1)
        self.assertGreaterEqual(res.notification_failures_count, 1)

        unconfirmed_item = next(e for e in res.exceptions if e.operation_id == "OP-EXC-003")
        self.assertTrue(unconfirmed_item.payment_verified)
        self.assertTrue(unconfirmed_item.can_retry_automatically)
        self.assertTrue(unconfirmed_item.original_booking_intact)
        self.assertIn("Inspect Opera and BRS availability", unconfirmed_item.safe_next_action)

    def test_recovery_retry_maintains_operation_identity_and_succeeds(self):
        """AC 4, AC 5: Safe recovery retry resumes the SAME operation ID and synchronizes without duplicate writes."""
        # 1. Set up an accepted amendment offer
        offer_id = "OFFER-M16-REC-01"
        safe_summary = self.linkage_service.guest_lookup(self.ref, "Balfour")
        offer = AmendmentOfferSnapshot(
            offer_id=offer_id,
            booking_reference=self.ref,
            booking_revision=1,
            option_id="OPT-AMEND-1",
            current_itinerary=safe_summary,
            proposed_stay=ProposedStayOption(
                room_category="Junior Suite",
                room_quantity=1,
                nights=3,
                nightly_rate="466.67",
                arrival_date=dt.date(2027, 8, 10),
                departure_date=dt.date(2027, 8, 13),
                stay_total="1400.00",
                is_available=True,
            ),
            proposed_golf_rounds=[
                ProposedGolfRoundOption(
                    course_name="Sandy Hills Links",
                    date=dt.date(2027, 8, 11),
                    time="09:30",
                    players=2,
                    holes="18",
                    round_price_total="300.00",
                    is_available=True,
                )
            ],
            pricing_comparison=AmendmentPricingComparison(
                original_package_total="1200.00",
                new_package_total="1400.00",
                price_delta="+200.00",
                deposit_already_paid="300.00",
                revised_balance_due="1100.00",
            ),
            credit_amount="0.00",
            amount_due_now="0.00",
            balance_due_date=dt.date(2027, 6, 29),
            status="accepted",
            expires_at=dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=2),
            created_at=dt.datetime.now(dt.timezone.utc),
            accepted_at=dt.datetime.now(dt.timezone.utc),
        )
        self.amendment_service._offers_cache[offer_id] = offer.model_dump(mode="json")
        if self.amendment_service.collection is not None:
            self.amendment_service.collection.insert_one(offer.model_dump(mode="json"))

        # 2. Set up halted operation record in unconfirmed_reconciliation
        op_id = "OP-RECOVER-999"
        op_rec = BookingOperationRecord(
            operation_id=op_id,
            idempotency_key_hash="hash_rec_999",
            operation_type="amendment",
            booking_reference=self.ref,
            amendment_offer_id=offer_id,
            stage="unconfirmed_reconciliation",
            payment_id="PMT-REC-01",
            payment_verified=True,
            staff_alert=StaffReconciliationAlert(
                operation_id=op_id,
                booking_reference=self.ref,
                failed_stage="hotel_write",
                reason="Opera gateway temporary failure",
            ),
            stage_history=[
                StageHistoryEntry(stage="unconfirmed_reconciliation", actor="engine", details="Halted"),
            ],
        )
        self.operation_engine._save_record(op_rec)

        # 3. Configure mock vendor writes to succeed on retry
        mock_cat = MagicMock()
        mock_cat.room_category = "Junior Suite"
        mock_cat.category_name = "Junior Suite"
        mock_cat.available_rooms = 5
        mock_room_avail = MagicMock()
        mock_room_avail.categories = [mock_cat]
        self.mock_opera.get_room_availability.return_value = mock_room_avail

        mock_opera_write = MagicMock()
        mock_opera_write.verified_via_readback = True
        mock_opera_write.reservation_id = "RES-OP-M1601"
        mock_opera_write.confirmation_number = "CONF-OP-M1601"
        self.mock_opera.amend_reservation.return_value = mock_opera_write

        mock_brs_write = MagicMock()
        mock_brs_write.verified_via_readback = True
        mock_brs_write.round_id = "RND-SANDY-REPLACE-01"
        mock_brs_write.brs_booking_id = "BRS-BK-REPLACE-01"
        self.mock_brs.create_round.return_value = mock_brs_write

        mock_deliv = MagicMock()
        mock_deliv.delivery_id = "DELIV-REC-001"
        self.mock_email.send_revised_booking_confirmation.return_value = mock_deliv

        # 4. Execute recovery retry
        req = StaffRecoveryRetryRequest(
            staff_notes="Verified Opera gateway restored; executing automated recovery.",
        )
        res = self.history_service.retry_recovery_operation(
            operation_id=op_id,
            request=req,
            principal=self.staff_principal,
        )

        # AC 4: Maintains the SAME operation identity
        self.assertEqual(res.operation_id, op_id)
        self.assertEqual(res.booking_reference, self.ref)
        self.assertEqual(res.outcome, "recovered_completed")
        self.assertEqual(res.stage, "completed")
        self.assertTrue(res.readback_verified)
        self.assertEqual(res.new_booking_revision, 2)

        # Linkage is updated to Revision 2
        linkage_after = self.linkage_service.get_linkage(self.ref)
        self.assertEqual(linkage_after.revision, 2)
        self.assertEqual(linkage_after.opera_stay.room_category, "Junior Suite")

        # Re-calling recover_operation idempotently returns already completed
        replay_res = self.history_service.retry_recovery_operation(
            operation_id=op_id,
            request=req,
            principal=self.staff_principal,
        )
        self.assertEqual(replay_res.outcome, "recovered_completed")
        self.assertTrue(replay_res.replayed)

    def test_recovery_retry_conflict_requires_manual_reconciliation(self):
        """AC 4, AC 6: Revision conflict or missing offer leaves stage in unconfirmed_reconciliation."""
        # 1. Advance booking linkage revision to 3
        linkage = self.linkage_service.get_linkage(self.ref)
        linkage.revision = 3
        if self.linkage_service.collection is not None:
            self.linkage_service.collection.update_one(
                {"booking_reference": self.ref},
                {"$set": {"revision": 3}},
            )

        # 2. Operation offer was created against old revision 1
        offer_id = "OFFER-OLD-REV-1"
        safe_summary = self.linkage_service.guest_lookup(self.ref, "Balfour")
        offer = AmendmentOfferSnapshot(
            offer_id=offer_id,
            booking_reference=self.ref,
            booking_revision=1,  # Stale revision!
            option_id="OPT-1",
            current_itinerary=safe_summary,
            proposed_stay=ProposedStayOption(
                room_category="Executive Suite",
                room_quantity=1,
                nights=3,
                nightly_rate="500.00",
                arrival_date=dt.date(2027, 8, 10),
                departure_date=dt.date(2027, 8, 13),
                stay_total="1500.00",
                is_available=True,
            ),
            proposed_golf_rounds=[],
            pricing_comparison=AmendmentPricingComparison(
                original_package_total="1200.00",
                new_package_total="1500.00",
                price_delta="+300.00",
                deposit_already_paid="300.00",
                revised_balance_due="1200.00",
            ),
            balance_due_date=dt.date(2027, 6, 29),
            status="accepted",
            expires_at=dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=2),
            created_at=dt.datetime.now(dt.timezone.utc),
        )
        self.amendment_service._offers_cache[offer_id] = offer.model_dump(mode="json")

        op_id = "OP-CONFLICT-004"
        op_rec = BookingOperationRecord(
            operation_id=op_id,
            idempotency_key_hash="hash_conflict_004",
            operation_type="amendment",
            booking_reference=self.ref,
            amendment_offer_id=offer_id,
            stage="unconfirmed_reconciliation",
            payment_id="PMT-CONF-004",
            payment_verified=True,
        )
        self.operation_engine._save_record(op_rec)

        req = StaffRecoveryRetryRequest(staff_notes="Attempting recovery")
        res = self.history_service.retry_recovery_operation(
            operation_id=op_id,
            request=req,
            principal=self.staff_principal,
        )

        # AC 6: Unmatched or conflict cannot complete safely; remains explicit exception
        self.assertEqual(res.outcome, "requires_manual_reconciliation")
        self.assertIn("Revision conflict", res.message)
        self.assertFalse(res.readback_verified)

        # Operation record remains safely in unconfirmed_reconciliation
        saved = self.operation_engine.get_operation(op_id)
        self.assertEqual(saved.stage, "unconfirmed_reconciliation")


if __name__ == "__main__":
    unittest.main()
