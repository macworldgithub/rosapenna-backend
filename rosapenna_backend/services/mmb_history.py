"""Service for Ticket M16 \u2014 Action History and Recovery View.

Aggregates durable time-ordered action histories across booking linkage, operations engine,
contact updates, caddy requests, occupant rosters, staff outcomes, and pre-arrival communications.
Provides strict privacy-filtered guest views and full-fidelity staff exception recovery.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any, Dict, List, Optional
import uuid

from pymongo.database import Database

from rosapenna_backend.api.schemas.mmb_history import (
    GuestActionHistoryResponse,
    RecoveryExceptionItem,
    StaffActionHistoryResponse,
    StaffRecoveryExceptionsListResponse,
    StaffRecoveryRetryRequest,
    StaffRecoveryRetryResponse,
    TimelineEvent,
)
from rosapenna_backend.api.staff_security import StaffPrincipal
from rosapenna_backend.services.booking_linkage import BookingLinkageService, LinkageNotFoundError
from rosapenna_backend.services.booking_operations import BookingOperationEngine, BookingOperationRecord
from rosapenna_backend.services.staff_audit import StaffAuditLog

logger = logging.getLogger("rosapenna.mmb_history")


def _utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _ensure_dt(val: Any) -> dt.datetime:
    if isinstance(val, dt.datetime):
        if val.tzinfo is None:
            return val.replace(tzinfo=dt.timezone.utc)
        return val
    if isinstance(val, dt.date):
        return dt.datetime.combine(val, dt.time.min, tzinfo=dt.timezone.utc)
    if isinstance(val, str):
        try:
            parsed = dt.datetime.fromisoformat(val)
            if parsed.tzinfo is None:
                return parsed.replace(tzinfo=dt.timezone.utc)
            return parsed
        except Exception:
            pass
    return _utc_now()


class MmbHistoryError(Exception):
    """Base exception for MMB history and recovery service."""

    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


class MmbHistoryNotFoundError(MmbHistoryError):
    def __init__(self, message: str = "Booking record not found."):
        super().__init__(message, status_code=404)


class MmbHistoryService:
    """Service orchestrating consolidated MMB action history and exception recovery."""

    def __init__(
        self,
        database: Optional[Database] = None,
        linkage_service: Optional[BookingLinkageService] = None,
        operation_engine: Optional[BookingOperationEngine] = None,
        staff_audit_log: Optional[StaffAuditLog] = None,
    ):
        self.database = database
        self.linkage_service = linkage_service or BookingLinkageService(database=database)
        self.operation_engine = operation_engine
        self.staff_audit_log = staff_audit_log

    # =========================================================================
    # Guest Timeline (Privacy-Filtered, IDOR Protected)
    # =========================================================================

    def get_guest_history(self, booking_reference: str) -> GuestActionHistoryResponse:
        """Compile a privacy-filtered time-ordered action timeline for an authenticated guest."""
        clean_ref = booking_reference.strip().upper()
        try:
            linkage = self.linkage_service.get_linkage(clean_ref)
        except LinkageNotFoundError:
            raise MmbHistoryNotFoundError(f"Booking '{clean_ref}' not found.") from None

        all_events = self._collect_raw_events(clean_ref, linkage)

        # Filter strictly for guest privacy
        guest_events: List[TimelineEvent] = []
        for ev in all_events:
            guest_ev = self._filter_for_guest(ev)
            if guest_ev is not None:
                guest_events.append(guest_ev)

        # Sort reverse-chronological (newest first for guest visibility)
        guest_events.sort(key=lambda e: e.timestamp, reverse=True)

        lead_name = "Guest"
        if linkage.lead_guest:
            lead_name = f"{linkage.lead_guest.first_name} {linkage.lead_guest.last_name}".strip()

        arr_str = None
        if linkage.opera_stay and linkage.opera_stay.arrival_date:
            arr_str = str(linkage.opera_stay.arrival_date)

        rev = getattr(linkage, "revision", getattr(linkage, "booking_revision", 1))

        return GuestActionHistoryResponse(
            booking_reference=clean_ref,
            current_revision=rev,
            lead_guest_name=lead_name,
            arrival_date=arr_str,
            events=guest_events,
            generated_at=_utc_now(),
        )

    # =========================================================================
    # Staff Timeline (Full Technical Fidelity, Audited)
    # =========================================================================

    def get_staff_history(
        self,
        booking_reference: str,
        principal: StaffPrincipal,
    ) -> StaffActionHistoryResponse:
        """Compile complete technical action history and audit log for authorized staff."""
        clean_ref = booking_reference.strip().upper()
        try:
            linkage = self.linkage_service.get_linkage(clean_ref)
        except LinkageNotFoundError:
            raise MmbHistoryNotFoundError(f"Booking '{clean_ref}' not found.") from None

        all_events = self._collect_raw_events(clean_ref, linkage, is_staff=True)
        all_events.sort(key=lambda e: e.timestamp, reverse=True)

        lead_name = "Guest"
        if linkage.lead_guest:
            lead_name = f"{linkage.lead_guest.first_name} {linkage.lead_guest.last_name}".strip()

        rev = getattr(linkage, "revision", getattr(linkage, "booking_revision", 1))
        open_exceptions = sum(1 for e in all_events if e.is_exception)

        # Record immutable staff audit entry
        if self.staff_audit_log is not None:
            try:
                self.staff_audit_log.record(
                    principal=principal,
                    action="history.view",
                    resource=f"booking:{clean_ref}",
                    outcome="success",
                )
            except Exception as exc:
                logger.warning("Staff audit event could not be recorded: %s", exc)

        return StaffActionHistoryResponse(
            booking_reference=clean_ref,
            current_revision=rev,
            lead_guest_name=lead_name,
            linkage_status=linkage.status,
            open_exceptions_count=open_exceptions,
            events=all_events,
            generated_at=_utc_now(),
        )

    # =========================================================================
    # Focused Recovery & Exceptions List (Staff View)
    # =========================================================================

    def list_recovery_exceptions(
        self,
        status_filter: Optional[str] = None,
        principal: Optional[StaffPrincipal] = None,
    ) -> StaffRecoveryExceptionsListResponse:
        """List all actionable MMB exceptions across bookings for staff review."""
        exceptions: List[RecoveryExceptionItem] = []

        # 1. Halted operations in unconfirmed_reconciliation from BookingOperationEngine
        if self.operation_engine is not None:
            unconfirmed_ops = self._get_unconfirmed_operations()
            for op in unconfirmed_ops:
                alert = op.staff_alert
                reason = alert.reason if alert else "Operation halted in unconfirmed reconciliation"
                failed_stage = alert.failed_stage if alert else op.stage

                payment_amount = None
                payment_doc = None
                if op.payment_id and self.database is not None:
                    try:
                        payment_doc = self.database["package_payments"].find_one({"payment_id": op.payment_id})
                        if payment_doc:
                            payment_amount = str(payment_doc.get("amount", ""))
                    except Exception:
                        pass

                opera_res_id = (
                    alert.opera_reservation_id if alert else op.vendor_references.get("amended_opera_reservation_id")
                )
                brs_rounds = (
                    alert.brs_round_ids if alert else op.vendor_references.get("replacement_brs_round_ids", [])
                )

                exc_item = RecoveryExceptionItem(
                    exception_id=f"EXC-{op.operation_id}",
                    booking_reference=op.booking_reference,
                    operation_id=op.operation_id,
                    exception_type="unconfirmed_reconciliation",
                    failed_stage=failed_stage,
                    reason=reason,
                    payment_verified=op.payment_verified,
                    payment_id=op.payment_id,
                    payment_amount=payment_amount,
                    payment_provider_ref=payment_doc.get("provider_reference") if payment_doc else None,
                    opera_reservation_id=opera_res_id,
                    opera_readback_verified=bool(op.readback_evidence and op.readback_evidence.opera_verified),
                    brs_round_ids=brs_rounds,
                    brs_readback_verified=bool(op.readback_evidence and op.readback_evidence.brs_verified),
                    can_retry_automatically=True,
                    safe_next_action=(
                        "Inspect Opera and BRS availability, then execute safe recovery retry with existing operation ID."
                    ),
                    original_booking_intact=True,
                    created_at=_ensure_dt(op.created_at),
                    updated_at=_ensure_dt(op.updated_at),
                )
                exceptions.append(exc_item)

        # 2. Staff Requests with F12 delivery failure
        if self.database is not None:
            try:
                failed_f12_requests = list(
                    self.database["staff_request_operations"].find({"f12_delivery_status": "failed"})
                )
                for req in failed_f12_requests:
                    created_t = _ensure_dt(req.get("timestamp") or req.get("created_at"))
                    exceptions.append(
                        RecoveryExceptionItem(
                            exception_id=f"EXC-F12-{req.get('request_id', uuid.uuid4().hex[:6])}",
                            booking_reference=req.get("booking_reference", "UNKNOWN"),
                            operation_id=req.get("f14_operation_id"),
                            exception_type="notification_failed",
                            failed_stage="f12_email_dispatch",
                            reason=f"Guest outcome email notification failed for request {req.get('request_id')}",
                            payment_verified=True,
                            can_retry_automatically=True,
                            safe_next_action="Retry F12 notification dispatch via staff request retry endpoint.",
                            original_booking_intact=True,
                            created_at=created_t,
                            updated_at=created_t,
                        )
                    )
            except Exception as exc:
                logger.warning("Error fetching failed F12 requests: %s", exc)

        # 3. Booking linkages with discrepancy status
        try:
            if hasattr(self.linkage_service, "collection") and self.linkage_service.collection is not None:
                discrepant_linkages = list(
                    self.linkage_service.collection.find({"status": "discrepancy"})
                )
                for link_doc in discrepant_linkages:
                    ref = link_doc.get("booking_reference", "")
                    disc_list = link_doc.get("discrepancies", [])
                    reason_str = "; ".join(disc_list) if disc_list else "Data mismatch between Opera and BRS records"
                    exceptions.append(
                        RecoveryExceptionItem(
                            exception_id=f"EXC-LINK-{ref}",
                            booking_reference=ref,
                            exception_type="partial_vendor_write",
                            failed_stage="linkage_verification",
                            reason=reason_str,
                            payment_verified=bool(link_doc.get("payments")),
                            can_retry_automatically=False,
                            safe_next_action="Review Opera PMS and BRS reservation numbers; execute staff manual correction.",
                            original_booking_intact=True,
                            created_at=_ensure_dt(link_doc.get("created_at")),
                            updated_at=_ensure_dt(link_doc.get("updated_at") or link_doc.get("created_at")),
                        )
                    )
        except Exception as exc:
            logger.warning("Error scanning discrepant linkages: %s", exc)

        # Apply optional filter
        if status_filter:
            exceptions = [e for e in exceptions if e.exception_type == status_filter]

        # Audit staff list access
        if principal and self.staff_audit_log is not None:
            try:
                self.staff_audit_log.record(
                    principal=principal,
                    action="recovery.list_exceptions",
                    resource="mmb_recovery_exceptions",
                    outcome="success",
                )
            except Exception:
                pass

        unconfirmed_count = sum(1 for e in exceptions if e.exception_type == "unconfirmed_reconciliation")
        notif_count = sum(1 for e in exceptions if e.exception_type == "notification_failed")

        return StaffRecoveryExceptionsListResponse(
            total_exceptions=len(exceptions),
            unconfirmed_reconciliations_count=unconfirmed_count,
            notification_failures_count=notif_count,
            exceptions=exceptions,
            read_at=_utc_now(),
        )

    # =========================================================================
    # Operation Recovery Retry (Maintains Same Operation ID)
    # =========================================================================

    def retry_recovery_operation(
        self,
        operation_id: str,
        request: StaffRecoveryRetryRequest,
        principal: StaffPrincipal,
    ) -> StaffRecoveryRetryResponse:
        """Safely resume and complete a halted operation using its existing operation identity."""
        if self.operation_engine is None:
            raise MmbHistoryError("Booking operation engine is not available for recovery retry.", status_code=503)

        record = self.operation_engine.get_operation(operation_id)
        if not record:
            raise MmbHistoryNotFoundError(f"Operation record '{operation_id}' was not found.")

        # Check booking linkage
        try:
            linkage = self.linkage_service.get_linkage(record.booking_reference)
        except LinkageNotFoundError:
            raise MmbHistoryNotFoundError(f"Booking linkage for '{record.booking_reference}' was not found.") from None

        # Check if already completed
        if record.stage == "completed":
            rev = getattr(linkage, "revision", getattr(linkage, "booking_revision", 1))
            return StaffRecoveryRetryResponse(
                operation_id=record.operation_id,
                booking_reference=record.booking_reference,
                outcome="recovered_completed",
                stage="completed",
                message="Operation was already completed and verified via readback.",
                readback_verified=True,
                new_booking_revision=rev,
                email_delivery_id=record.email_delivery_id,
                replayed=True,
                updated_at=_ensure_dt(record.updated_at),
            )

        # Must be in unconfirmed_reconciliation to be recovered
        if record.stage != "unconfirmed_reconciliation":
            raise MmbHistoryError(
                f"Operation is currently in stage '{record.stage}', not 'unconfirmed_reconciliation'.",
                status_code=409,
            )

        # Audit staff retry attempt
        if self.staff_audit_log is not None:
            try:
                self.staff_audit_log.record(
                    principal=principal,
                    action="recovery.retry_operation",
                    resource=f"operation:{operation_id}",
                    outcome="requested",
                )
            except Exception:
                pass

        actor_name = f"staff:{principal.subject}"
        notes = request.staff_notes or "Staff triggered safe recovery retry."

        # Delegate execution resumption to engine
        recovery_result = self.operation_engine.recover_operation(
            operation_id=operation_id,
            actor=actor_name,
            staff_notes=notes,
            force_readback_recheck_only=request.force_readback_recheck_only,
        )

        outcome_val = recovery_result.get("outcome", "still_unconfirmed")
        if self.staff_audit_log is not None:
            try:
                self.staff_audit_log.record(
                    principal=principal,
                    action="recovery.retry_operation",
                    resource=f"operation:{operation_id}",
                    outcome=outcome_val,
                )
            except Exception:
                pass

        return StaffRecoveryRetryResponse(
            operation_id=record.operation_id,
            booking_reference=record.booking_reference,
            outcome=outcome_val,
            stage=recovery_result.get("stage", record.stage),
            message=recovery_result.get("message", "Recovery executed."),
            readback_verified=recovery_result.get("readback_verified", False),
            new_booking_revision=recovery_result.get("new_booking_revision"),
            email_delivery_id=recovery_result.get("email_delivery_id"),
            replayed=recovery_result.get("replayed", False),
            updated_at=_utc_now(),
        )

    # =========================================================================
    # Internal Event Aggregation & Filtering
    # =========================================================================

    def _collect_raw_events(
        self,
        booking_reference: str,
        linkage: Any,
        is_staff: bool = False,
    ) -> List[TimelineEvent]:
        """Aggregate events from linkage, operations, phone, caddy, occupants, requests, and reminders."""
        events: List[TimelineEvent] = []

        # 1. Initial Linkage Event
        created_time = _utc_now()
        if linkage.audit_trail:
            first_entry = linkage.audit_trail[0]
            created_time = _ensure_dt(getattr(first_entry, "timestamp", None))
        events.append(
            TimelineEvent(
                event_id=f"EVT-INIT-{booking_reference}",
                timestamp=created_time,
                category="booking_created",
                summary="Reservation linked and confirmed in Rosapenna system",
                details=(
                    f"Room category: {linkage.opera_stay.room_category if linkage.opera_stay else 'Standard'}, "
                    f"Rounds: {len(linkage.brs_rounds)}"
                ),
                booking_revision=1,
                actor_type="system",
                actor_label="Rosapenna Central Reservation System",
                status="confirmed",
                vendor_readback_verified=True,
            )
        )

        # 2. Payments
        for p in linkage.payments:
            p_time = _ensure_dt(p.verified_at)
            events.append(
                TimelineEvent(
                    event_id=f"EVT-PAY-{p.payment_id}",
                    timestamp=p_time,
                    category="payment_recorded",
                    summary=f"Payment of €{p.amount} verified ({p.payment_type.replace('_', ' ').title()})",
                    details=f"Payment Reference: {p.provider_reference or p.payment_id}",
                    actor_type="guest",
                    actor_label="Lead Guest",
                    status=p.status,
                    metadata={"amount": p.amount, "currency": p.currency, "payment_type": p.payment_type},
                )
            )

        # 3. Operations from BookingOperationEngine
        if self.operation_engine is not None:
            ops = self.operation_engine.get_operations_for_booking(booking_reference)
            for op in ops:
                op_time = _ensure_dt(op.completed_at or op.updated_at or op.created_at)
                if op.stage == "completed":
                    events.append(
                        TimelineEvent(
                            event_id=f"EVT-OP-DONE-{op.operation_id}",
                            timestamp=op_time,
                            category="amendment_commit",
                            summary="Amendment committed & synchronized across Opera and BRS",
                            details=f"Operation {op.operation_id}. Readback verified with resort inventory.",
                            operation_id=op.operation_id,
                            actor_type="system",
                            actor_label="Operation Engine",
                            status="completed",
                            vendor_readback_verified=True,
                        )
                    )
                elif op.stage == "unconfirmed_reconciliation":
                    alert_reason = op.staff_alert.reason if op.staff_alert else "Vendor synchronization halted."
                    events.append(
                        TimelineEvent(
                            event_id=f"EVT-OP-ALERT-{op.operation_id}",
                            timestamp=op_time,
                            category="exception_alert",
                            summary="Amendment halted — flagged for staff reconciliation",
                            details=alert_reason,
                            operation_id=op.operation_id,
                            actor_type="system",
                            actor_label="Safe Operation Engine",
                            status="unconfirmed_reconciliation",
                            vendor_readback_verified=False,
                            is_exception=True,
                        )
                    )

                # Include Stage History in Staff View
                if is_staff:
                    for idx, sh in enumerate(op.stage_history):
                        events.append(
                            TimelineEvent(
                                event_id=f"EVT-SH-{op.operation_id}-{idx}",
                                timestamp=_ensure_dt(sh.timestamp),
                                category="recovery_action" if "recovery" in sh.details.lower() else "amendment_commit",
                                summary=f"Operation {op.operation_id} entered stage: {sh.stage}",
                                details=sh.details,
                                operation_id=op.operation_id,
                                actor_type="staff" if "staff" in sh.actor else "system",
                                actor_label=sh.actor,
                                status=sh.stage,
                            )
                        )

        # 4. Linkage Audit Trail
        for idx, entry in enumerate(linkage.audit_trail):
            e_time = _ensure_dt(getattr(entry, "timestamp", None))
            act = getattr(entry, "action", "")
            actor = getattr(entry, "actor", "system")
            details = getattr(entry, "details", "")

            cat = "booking_created"
            if "phone" in act:
                cat = "contact_update"
            elif "caddy" in act:
                cat = "caddy_update"
            elif "occupant" in act or "roster" in act:
                cat = "occupant_update"
            elif "amend" in act:
                cat = "amendment_commit"
            elif "request" in act:
                cat = "staff_request_outcome"

            events.append(
                TimelineEvent(
                    event_id=f"EVT-AUDIT-{idx}-{entry.timestamp if hasattr(entry, 'timestamp') else idx}",
                    timestamp=e_time,
                    category=cat,  # type: ignore
                    summary=act.replace("_", " ").title(),
                    details=details,
                    actor_type="staff" if "staff" in actor else "guest" if "guest" in actor else "system",
                    actor_label=actor,
                    status="confirmed",
                    vendor_readback_verified=True,
                )
            )

        # 5. Pending & Completed Guest Requests
        for req in linkage.pending_requests:
            req_id = req.get("request_id", "") if isinstance(req, dict) else getattr(req, "request_id", "")
            req_time = _ensure_dt(req.get("submitted_at") if isinstance(req, dict) else getattr(req, "submitted_at", None))
            desc = req.get("description", "Special request") if isinstance(req, dict) else getattr(req, "description", "Special request")
            notes = req.get("notes") if isinstance(req, dict) else getattr(req, "notes", None)
            cat = req.get("category", "general") if isinstance(req, dict) else getattr(req, "category", "general")
            status_val = req.get("status", "pending_review") if isinstance(req, dict) else getattr(req, "status", "pending_review")

            events.append(
                TimelineEvent(
                    event_id=f"EVT-REQ-SUB-{req_id}",
                    timestamp=req_time,
                    category="guest_request",
                    summary=f"Special request submitted: {desc}",
                    details=notes or f"Category: {cat}",
                    actor_type="guest",
                    actor_label="Lead Guest",
                    status=status_val,
                )
            )

        # 6. Database Pre-arrival Reminders
        if self.database is not None:
            try:
                col = self.database["booking_reminders"]
                rems = list(col.find({"booking_reference": booking_reference}))
                for r in rems:
                    r_time = _ensure_dt(r.get("created_at") or r.get("scheduled_date"))
                    cat = "reminder_dispatched" if r.get("status") == "dispatched" else "reminder_scheduled"
                    supp_note = f"Note: {r.get('suppression_reason')}" if r.get("suppression_reason") else ""
                    events.append(
                        TimelineEvent(
                            event_id=f"EVT-REM-{r.get('reminder_id')}",
                            timestamp=r_time,
                            category=cat,  # type: ignore
                            summary=r.get("title", f"Milestone Reminder: {r.get('reminder_type')}"),
                            details=f"Scheduled for: {r.get('scheduled_date')}. Status: {r.get('status')}. {supp_note}".strip(),
                            booking_revision=r.get("revision_number"),
                            actor_type="system",
                            actor_label="Automated Communications Engine",
                            status=r.get("status"),
                        )
                    )
            except Exception as exc:
                logger.warning("Error fetching reminders for history: %s", exc)

        return events

    def _filter_for_guest(self, event: TimelineEvent) -> Optional[TimelineEvent]:
        """Apply strict privacy rules to exclude internal staff notes, credentials, and raw errors."""
        # Clean copy
        ev_dict = event.model_dump()

        # If it's an internal exception, show friendly reassurance that booking is protected
        if ev_dict.get("is_exception"):
            ev_dict["summary"] = "Booking amendment held for staff review"
            ev_dict["details"] = (
                "Our reservations team has been alerted to confirm this change. "
                "Your original reservation dates and tee times remain confirmed and protected."
            )
            ev_dict["actor_label"] = "Rosapenna Reservations Staff"

        # Sanitize actor labels
        if "staff:" in ev_dict.get("actor_label", "").lower():
            ev_dict["actor_label"] = "Rosapenna Reservations Staff"
        elif "guest" in ev_dict.get("actor_label", "").lower():
            ev_dict["actor_label"] = "Lead Guest"
        elif "engine" in ev_dict.get("actor_label", "").lower() or "system" in ev_dict.get("actor_label", "").lower():
            ev_dict["actor_label"] = "Rosapenna Booking System"

        # Exclude internal staff notes or credentials in details
        details = ev_dict.get("details") or ""
        if "internal_notes:" in details.lower():
            parts = details.split("internal_notes:")
            ev_dict["details"] = parts[0].strip()

        # Strip internal metadata
        ev_dict["metadata"] = {}

        return TimelineEvent.model_validate(ev_dict)

    def _get_unconfirmed_operations(self) -> List[BookingOperationRecord]:
        """Fetch all operations halted in unconfirmed_reconciliation."""
        records: List[BookingOperationRecord] = []
        if self.operation_engine is None:
            return records

        if hasattr(self.operation_engine, "collection") and self.operation_engine.collection is not None:
            try:
                docs = list(
                    self.operation_engine.collection.find(
                        {"$or": [{"stage": "unconfirmed_reconciliation"}, {"staff_alert": {"$ne": None}}]}
                    )
                )
                for d in docs:
                    records.append(BookingOperationRecord.model_validate(d))
                return records
            except Exception as exc:
                logger.warning("MongoDB query failed for unconfirmed operations: %s", exc)

        # In-memory fallback
        for doc in self.operation_engine._in_memory_records.values():
            if doc.get("stage") == "unconfirmed_reconciliation" or doc.get("staff_alert"):
                records.append(BookingOperationRecord.model_validate(doc))
        return records
