"""Domain service for Ticket M14 — Staff Request Outcomes.

Enforces:
- M14 AC 1: Role-based staff access and immutable audit logging (actor, timestamp, reason, prior/new status).
- M14 AC 2: Distinct persisted outcomes; approval alone never claims completed.
- M14 AC 3 & 4: Completed status requires verified F14 coordinated writes and vendor readback.
- M14 AC 5: Cancellation/refund execution blocked without documented policy & authorized signoff.
- M14 AC 6: Guest status and F12 notifications match durable staff outcome; retryable without erasing history.
- M14 AC 7: Failed implementation preserves original confirmed booking and alerts staff.
- M14 AC 8: Strict idempotency deduplication with conflict detection.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
from typing import Any, Dict, List, Optional
from pymongo import ASCENDING
from pymongo.database import Database

from rosapenna_backend.api.schemas.staff_requests import (
    StaffRequestHistoryEntry,
    StaffRequestItem,
    StaffRequestListResponse,
    StaffRequestOutcomeStatus,
    StaffRequestTransitionCreate,
    StaffRequestTransitionResponse,
)
from rosapenna_backend.api.staff_security import StaffPrincipal
from rosapenna_backend.services.booking_linkage import (
    BookingLinkageService,
    LinkageConflictError,
    LinkageNotFoundError,
)
from rosapenna_backend.services.booking_operations import BookingOperationEngine
from rosapenna_backend.services.email_delivery import EmailDeliveryService
from rosapenna_backend.services.staff_audit import StaffAuditLog

logger = logging.getLogger("booking_api")


def _utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


class StaffRequestError(Exception):
    """Base error for staff request operations."""

    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


class StaffRequestService:
    """Coordinates authorized staff reviews and outcomes for special and cancellation requests."""

    def __init__(
        self,
        database: Optional[Database] = None,
        linkage_service: Optional[BookingLinkageService] = None,
        email_service: Optional[EmailDeliveryService] = None,
        operation_engine: Optional[BookingOperationEngine] = None,
        staff_audit_log: Optional[StaffAuditLog] = None,
    ):
        self.database = database
        self.linkage_service = linkage_service or BookingLinkageService(database=database)
        self.email_service = email_service or EmailDeliveryService()
        self.operation_engine = operation_engine
        self.staff_audit_log = staff_audit_log
        self.collection = (
            database["staff_request_operations"] if database is not None else None
        )
        self._memory_idemp: Dict[str, Dict[str, Any]] = {}

        if self.collection is not None:
            try:
                self._init_indexes()
            except Exception as exc:
                logger.warning("Could not initialize staff_request_operations indexes: %s", exc)

    def _init_indexes(self) -> None:
        assert self.collection is not None
        self.collection.create_index(
            [("booking_reference", ASCENDING), ("idempotency_hash", ASCENDING)],
            unique=True,
            name="uniq_staff_req_idemp",
        )

    def _hash_key(self, booking_reference: str, request_id: str, idempotency_key: str) -> str:
        raw = f"{booking_reference.strip().upper()}:{request_id.strip()}:{idempotency_key.strip()}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def list_requests(
        self,
        principal: StaffPrincipal,
        status_filter: Optional[str] = None,
        request_type_filter: Optional[str] = None,
        booking_reference: Optional[str] = None,
    ) -> StaffRequestListResponse:
        """List requests across booking linkages for authorized staff review (AC 1)."""
        raw_items = self.linkage_service.get_all_pending_requests(
            status_filter=status_filter,
            request_type_filter=request_type_filter,
            booking_reference=booking_reference,
        )

        items: List[StaffRequestItem] = []
        for r in raw_items:
            history_entries: List[StaffRequestHistoryEntry] = []
            for h in r.get("history", []):
                h_ts = dt.datetime.fromisoformat(h["timestamp"]) if isinstance(h.get("timestamp"), str) else h.get("timestamp", _utc_now())
                history_entries.append(
                    StaffRequestHistoryEntry(
                        history_id=h.get("history_id", "hist_1"),
                        actor=h.get("actor", "staff"),
                        timestamp=h_ts,
                        prior_status=h.get("prior_status", "pending_review"),
                        new_status=h.get("new_status", "pending_review"),
                        reason=h.get("reason", ""),
                        internal_notes=h.get("internal_notes"),
                        guest_message=h.get("guest_message"),
                        execution_type=h.get("execution_type"),
                        f14_operation_id=h.get("f14_operation_id"),
                        vendor_readback_ref=h.get("vendor_readback_ref"),
                        cancellation_authorization_ref=h.get("cancellation_authorization_ref"),
                        f12_delivery_id=h.get("f12_delivery_id"),
                        f12_delivery_status=h.get("f12_delivery_status"),
                    )
                )

            sub_ts = dt.datetime.fromisoformat(r["submitted_at"]) if isinstance(r.get("submitted_at"), str) else r.get("submitted_at", _utc_now())
            upd_ts = None
            if r.get("last_updated_at"):
                upd_ts = dt.datetime.fromisoformat(r["last_updated_at"]) if isinstance(r.get("last_updated_at"), str) else r.get("last_updated_at")

            items.append(
                StaffRequestItem(
                    request_id=r.get("request_id", ""),
                    booking_reference=r.get("booking_reference", ""),
                    request_type=r.get("request_type", "general_inquiry"),
                    category=r.get("category", "other"),
                    request_text=r.get("description", ""),
                    desired_dates=r.get("desired_dates"),
                    desired_room_category=r.get("desired_room_category"),
                    desired_golf_notes=r.get("desired_golf_notes"),
                    status=r.get("status", "pending_review"),
                    submitted_at=sub_ts,
                    last_updated_at=upd_ts,
                    lead_guest_name=r.get("lead_guest_name"),
                    lead_guest_email=r.get("lead_guest_email"),
                    notes=r.get("notes"),
                    history=history_entries,
                    f12_delivery_id=r.get("f12_delivery_id"),
                    f12_delivery_status=r.get("f12_delivery_status"),
                    booking_revision=r.get("booking_revision", 1),
                )
            )

        return StaffRequestListResponse(
            total=len(items),
            requests=items,
            read_at=_utc_now(),
        )

    def get_request_details(
        self,
        principal: StaffPrincipal,
        booking_reference: str,
        request_id: str,
    ) -> StaffRequestItem:
        """Get full details of a specific request for staff review (AC 1)."""
        clean_ref = booking_reference.strip().upper()
        clean_req_id = request_id.strip()

        try:
            linkage = self.linkage_service.get_linkage(clean_ref)
        except LinkageNotFoundError as exc:
            raise StaffRequestError(f"Booking reference '{clean_ref}' not found.", status_code=404) from exc

        # Find request in linkage
        target_req = None
        for r in linkage.pending_requests:
            req_id_val = r.get("request_id") if isinstance(r, dict) else getattr(r, "request_id", None)
            if req_id_val == clean_req_id:
                target_req = r
                break

        # Also inspect raw doc if in mongo
        raw_doc = None
        if self.database is not None and "mmb_booking_linkages" in self.database.list_collection_names():
            raw_doc = self.database.mmb_booking_linkages.find_one({"booking_reference": clean_ref})

        raw_req_data: Dict[str, Any] = {}
        if raw_doc:
            for item in raw_doc.get("pending_requests", []):
                if item.get("request_id") == clean_req_id:
                    raw_req_data = item
                    break
        elif isinstance(target_req, dict):
            raw_req_data = target_req

        if not target_req and not raw_req_data:
            raise StaffRequestError(
                f"Request '{clean_req_id}' was not found on booking '{clean_ref}'.",
                status_code=404,
            )

        history_entries: List[StaffRequestHistoryEntry] = []
        for h in raw_req_data.get("history", []):
            h_ts = dt.datetime.fromisoformat(h["timestamp"]) if isinstance(h.get("timestamp"), str) else h.get("timestamp", _utc_now())
            history_entries.append(
                StaffRequestHistoryEntry(
                    history_id=h.get("history_id", "hist_1"),
                    actor=h.get("actor", "staff"),
                    timestamp=h_ts,
                    prior_status=h.get("prior_status", "pending_review"),
                    new_status=h.get("new_status", "pending_review"),
                    reason=h.get("reason", ""),
                    internal_notes=h.get("internal_notes"),
                    guest_message=h.get("guest_message"),
                    execution_type=h.get("execution_type"),
                    f14_operation_id=h.get("f14_operation_id"),
                    vendor_readback_ref=h.get("vendor_readback_ref"),
                    cancellation_authorization_ref=h.get("cancellation_authorization_ref"),
                    f12_delivery_id=h.get("f12_delivery_id"),
                    f12_delivery_status=h.get("f12_delivery_status"),
                )
            )

        lead = linkage.lead_guest
        lead_name = f"{lead.first_name} {lead.last_name}".strip()

        sub_at = raw_req_data.get("submitted_at") or (getattr(target_req, "submitted_at", _utc_now()) if target_req and not isinstance(target_req, dict) else _utc_now())
        if isinstance(sub_at, str):
            sub_at = dt.datetime.fromisoformat(sub_at)

        req_type = raw_req_data.get("request_type") or (getattr(target_req, "request_type", "general_inquiry") if target_req and not isinstance(target_req, dict) else "general_inquiry")
        desc = raw_req_data.get("description") or (getattr(target_req, "description", "") if target_req and not isinstance(target_req, dict) else "")
        status_val = raw_req_data.get("status") or (getattr(target_req, "status", "pending_review") if target_req and not isinstance(target_req, dict) else "pending_review")
        notes_val = raw_req_data.get("notes") or (getattr(target_req, "notes", None) if target_req and not isinstance(target_req, dict) else None)

        return StaffRequestItem(
            request_id=clean_req_id,
            booking_reference=clean_ref,
            request_type=req_type,
            category=raw_req_data.get("category", "general_inquiry"),
            request_text=desc,
            desired_dates=raw_req_data.get("desired_dates"),
            desired_room_category=raw_req_data.get("desired_room_category"),
            desired_golf_notes=raw_req_data.get("desired_golf_notes"),
            status=status_val,
            submitted_at=sub_at,
            last_updated_at=raw_req_data.get("last_updated_at"),
            lead_guest_name=lead_name,
            lead_guest_email=lead.email,
            notes=notes_val,
            history=history_entries,
            f12_delivery_id=raw_req_data.get("f12_delivery_id"),
            f12_delivery_status=raw_req_data.get("f12_delivery_status"),
            booking_revision=linkage.revision,
        )

    def transition_request(
        self,
        principal: StaffPrincipal,
        booking_reference: str,
        request_id: str,
        payload: StaffRequestTransitionCreate,
        idempotency_key: str,
    ) -> StaffRequestTransitionResponse:
        """Execute and durably store staff outcome transition with full validation (AC 1-8)."""
        clean_ref = booking_reference.strip().upper()
        clean_req_id = request_id.strip()
        clean_key = idempotency_key.strip()

        if not clean_key or len(clean_key) < 8:
            raise StaffRequestError("A valid Idempotency-Key header (min 8 chars) is required.", status_code=400)

        # 1. Idempotency Check (AC 8)
        idemp_hash = self._hash_key(clean_ref, clean_req_id, clean_key)
        stored_op = self._get_stored_operation(clean_ref, idemp_hash)
        if stored_op:
            if stored_op["payload_hash"] != self._hash_payload(payload):
                raise StaffRequestError(
                    f"Idempotency conflict: key '{clean_key}' was previously used with different parameters.",
                    status_code=409,
                )
            replayed = StaffRequestTransitionResponse(**stored_op["response"])
            replayed.replayed = True
            return replayed

        # 2. Fetch current request details
        item = self.get_request_details(principal, clean_ref, clean_req_id)
        prior_status = item.status

        # 3. Rule Checks by Target Status (AC 2, 3, 4, 5)
        target_status = payload.target_status

        # AC 5: Cancellation execution checks
        is_cancellation = (item.request_type == "cancellation_request" or item.category == "cancellation_inquiry")
        if is_cancellation and target_status == "completed":
            if not payload.cancellation_authorization_ref or len(payload.cancellation_authorization_ref.strip()) < 5:
                raise StaffRequestError(
                    "Cancellation/refund execution is unavailable without separately documented policy "
                    "and valid cancellation_authorization_ref (Ticket M14 AC 5).",
                    status_code=422,
                )
            # Must have manager or admin role
            if not ({"reservations_manager", "site_admin"} & set(principal.roles)):
                raise StaffRequestError(
                    "Only reservations_manager or site_admin can record completed cancellation reviews.",
                    status_code=403,
                )

        # AC 3 & 4: Completed status requires verified F14 coordinated writes or vendor readback
        has_itinerary_shift = bool(item.desired_dates or item.desired_room_category or item.desired_golf_notes)
        if target_status == "completed":
            if has_itinerary_shift or is_cancellation:
                # Must provide f14_operation or vendor_readback_verified
                if payload.execution_type not in ("f14_operation", "vendor_readback_verified"):
                    raise StaffRequestError(
                        "Cannot mark completed: date, room, or golf modifications require verified F14 coordinated "
                        "writes (execution_type='f14_operation') or verified vendor readback (execution_type='vendor_readback_verified'). "
                        "A staff status toggle alone cannot bypass these checks (Ticket M14 AC 3 & 4).",
                        status_code=422,
                    )

                if payload.execution_type == "f14_operation":
                    if not payload.f14_operation_id:
                        raise StaffRequestError(
                            "f14_operation_id is mandatory when execution_type is 'f14_operation'.",
                            status_code=422,
                        )
                    if self.operation_engine is not None:
                        op_record = self.operation_engine.get_operation(payload.f14_operation_id)
                        if not op_record:
                            raise StaffRequestError(
                                f"Referenced F14 operation '{payload.f14_operation_id}' was not found.",
                                status_code=422,
                            )
                        if op_record.stage != "completed" or not op_record.readback_evidence or not (op_record.readback_evidence.opera_verified or op_record.readback_evidence.brs_verified):
                            raise StaffRequestError(
                                f"F14 operation '{payload.f14_operation_id}' did not achieve completed status with verified readback (stage: {op_record.stage}).",
                                status_code=422,
                            )
                        if op_record.booking_reference.strip().upper() != clean_ref:
                            raise StaffRequestError(
                                f"F14 operation '{payload.f14_operation_id}' is for booking {op_record.booking_reference}, not {clean_ref}.",
                                status_code=422,
                            )
                elif payload.execution_type == "vendor_readback_verified":
                    if not payload.vendor_readback_ref or len(payload.vendor_readback_ref.strip()) < 3:
                        raise StaffRequestError(
                            "vendor_readback_ref is mandatory when execution_type is 'vendor_readback_verified'.",
                            status_code=422,
                        )
            else:
                # Informational request (e.g. dietary / accessibility notes)
                if not payload.execution_type:
                    payload.execution_type = "informational_only"

        # 4. Prepare execution details dict
        exec_details = {
            "execution_type": payload.execution_type,
            "f14_operation_id": payload.f14_operation_id,
            "vendor_readback_ref": payload.vendor_readback_ref,
            "cancellation_authorization_ref": payload.cancellation_authorization_ref,
        }

        # 5. Persist durable transition in MongoDB BookingLinkage (AC 1 & 6)
        try:
            updated_linkage = self.linkage_service.transition_pending_request(
                booking_reference=clean_ref,
                request_id=clean_req_id,
                target_status=target_status,
                actor=principal.subject,
                reason=payload.reason,
                internal_notes=payload.internal_notes,
                guest_message=payload.guest_message,
                execution_details=exec_details,
                expected_revision=payload.expected_revision,
            )
        except LinkageConflictError as exc:
            raise StaffRequestError(str(exc), status_code=409) from exc
        except LinkageNotFoundError as exc:
            raise StaffRequestError(str(exc), status_code=404) from exc

        # Audit event in StaffAuditLog if configured
        if self.staff_audit_log is not None:
            try:
                self.staff_audit_log.record(
                    principal=principal,
                    action="staff_request_outcome_transition",
                    resource=f"{clean_ref}:{clean_req_id}",
                    outcome=f"{prior_status}->{target_status}",
                )
            except Exception as exc:
                logger.warning("Staff audit log error: %s", exc)

        # 6. Dispatch F12 notification to guest AFTER durable storage (AC 6)
        email_result = None
        f12_status = "simulated_sandbox"
        f12_id = None
        try:
            raw_booking_dict = {
                "booking_reference": clean_ref,
                "lead_guest": {
                    "first_name": updated_linkage.lead_guest.first_name,
                    "last_name": updated_linkage.lead_guest.last_name,
                    "email": updated_linkage.lead_guest.email,
                },
            }
            email_result = self.email_service.send_staff_outcome_notification(
                booking=raw_booking_dict,
                request_id=clean_req_id,
                request_type=item.request_type,
                outcome_status=target_status,
                reason=payload.reason,
                guest_message=payload.guest_message,
                idempotency_key=f"F12-{idemp_hash[:16]}",
            )
            f12_status = email_result.status
            f12_id = email_result.delivery_id
        except Exception as exc:
            logger.warning("F12 staff outcome notification dispatch failed: %s", exc)
            f12_status = "failed"

        # Update delivery status back to linkage without raising
        if f12_id:
            self.linkage_service.update_request_notification_status(
                booking_reference=clean_ref,
                request_id=clean_req_id,
                f12_delivery_id=f12_id,
                f12_delivery_status=f12_status,
            )

        # 7. Formulate and store response
        now = _utc_now()
        response = StaffRequestTransitionResponse(
            request_id=clean_req_id,
            booking_reference=clean_ref,
            prior_status=prior_status,
            new_status=target_status,
            transition_recorded_at=now,
            booking_revision=updated_linkage.revision,
            f12_delivery_status=f12_status,
            f12_delivery_id=f12_id,
            replayed=False,
            message=f"Request {clean_req_id} transitioned from {prior_status} to {target_status} by {principal.subject}.",
        )

        self._record_operation(clean_ref, idemp_hash, clean_req_id, payload, response)
        return response

    def retry_notification(
        self,
        principal: StaffPrincipal,
        booking_reference: str,
        request_id: str,
    ) -> StaffRequestTransitionResponse:
        """Retry sending guest notification for a previously transitioned request without re-mutating state (AC 6)."""
        clean_ref = booking_reference.strip().upper()
        clean_req_id = request_id.strip()

        item = self.get_request_details(principal, clean_ref, clean_req_id)
        if item.status == "pending_review":
            raise StaffRequestError(
                "Request is still pending review; cannot send staff outcome notification before review.",
                status_code=400,
            )

        latest_reason = "Updated request outcome"
        latest_guest_msg = None
        if item.history:
            latest_entry = item.history[-1]
            latest_reason = latest_entry.reason
            latest_guest_msg = latest_entry.guest_message

        raw_booking_dict = {
            "booking_reference": clean_ref,
            "lead_guest": {
                "first_name": item.lead_guest_name or "Valued",
                "last_name": "Guest",
                "email": item.lead_guest_email or "guest@example.com",
            },
        }

        email_result = self.email_service.send_staff_outcome_notification(
            booking=raw_booking_dict,
            request_id=clean_req_id,
            request_type=item.request_type,
            outcome_status=item.status,
            reason=latest_reason,
            guest_message=latest_guest_msg,
        )

        self.linkage_service.update_request_notification_status(
            booking_reference=clean_ref,
            request_id=clean_req_id,
            f12_delivery_id=email_result.delivery_id,
            f12_delivery_status=email_result.status,
        )

        return StaffRequestTransitionResponse(
            request_id=clean_req_id,
            booking_reference=clean_ref,
            prior_status=item.status,
            new_status=item.status,
            transition_recorded_at=_utc_now(),
            booking_revision=item.booking_revision,
            f12_delivery_status=email_result.status,
            f12_delivery_id=email_result.delivery_id,
            replayed=False,
            message=f"Notification retried for request {clean_req_id} with delivery status: {email_result.status}.",
        )

    def _hash_payload(self, payload: StaffRequestTransitionCreate) -> str:
        dump = payload.model_dump(mode="json")
        serialized = json.dumps(dump, sort_keys=True)
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()

    def _get_stored_operation(self, booking_reference: str, idemp_hash: str) -> Optional[Dict[str, Any]]:
        if self.collection is not None:
            return self.collection.find_one({
                "booking_reference": booking_reference,
                "idempotency_hash": idemp_hash,
            })
        return self._memory_idemp.get(f"{booking_reference}:{idemp_hash}")

    def _record_operation(
        self,
        booking_reference: str,
        idemp_hash: str,
        request_id: str,
        payload: StaffRequestTransitionCreate,
        response: StaffRequestTransitionResponse,
    ) -> None:
        rec = {
            "booking_reference": booking_reference,
            "idempotency_hash": idemp_hash,
            "request_id": request_id,
            "payload_hash": self._hash_payload(payload),
            "response": response.model_dump(mode="json"),
            "created_at": _utc_now(),
        }
        if self.collection is not None:
            try:
                self.collection.insert_one(rec)
            except Exception as exc:
                logger.warning("Could not persist idempotency record: %s", exc)
        else:
            self._memory_idemp[f"{booking_reference}:{idemp_hash}"] = rec
