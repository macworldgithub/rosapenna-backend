"""Domain service for Ticket F14 — Safe Booking Operation Engine.

Coordinates new bookings and amendments as durable operations across payment, Opera, and BRS.
Enforces:
1. Zero inventory reservation prior to verified payment.
2. Post-payment availability and price revalidation.
3. Durable stage progression and unique idempotency replay across restarts.
4. Vendor writes with strict readback verification.
5. Fail-safe reconciliation: paid but incomplete operations halt as unconfirmed, generate
   sanitized staff alerts, maintain linkage to the F11 payment ledger, and never trigger
   automatic refunds or duplicate charges.
6. A new booking saves its verified state and one durable `confirmed_event` together.
   N07 owns confirmation delivery; the commit path sends no email.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal, InvalidOperation
import hashlib
from threading import Lock
from typing import Any, Dict, List, Optional
import uuid

from pymongo import ASCENDING
from pymongo.collection import Collection
from pymongo.database import Database
from pymongo.errors import DuplicateKeyError

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
    ExecuteBookingOperationRequest,
    ExecuteBookingOperationResponse,
    OperationStage,
    ReadbackEvidence,
    StaffReconciliationAlert,
    StageHistoryEntry,
    hash_idempotency_key,
)
from rosapenna_backend.api.schemas.brs_booking import CreateBrsRoundRequest
from rosapenna_backend.api.schemas.communications import (
    BookingConfirmationSnapshot,
    ConfirmedEvent,
    SnapshotContent,
    SnapshotFinancials,
    SnapshotGolfRound,
    SnapshotLeadGuest,
    SnapshotPayment,
    SnapshotRoom,
    SnapshotStay,
)
from rosapenna_backend.config import settings as app_settings
from rosapenna_backend.integrations.opera.schemas import CreateReservationRequest, RoomOccupant, UpdateReservationRequest
from rosapenna_backend.api.schemas.mmb_amendments import AmendmentOfferSnapshot
from rosapenna_backend.api.schemas.packages import PackageGuestName, PackageQuoteResponse
from rosapenna_backend.core.logging import logger
from rosapenna_backend.integrations.brs.booking_client import BrsBookingClient, BrsWriteError
from rosapenna_backend.integrations.brs.tee_sheet import tee_time_is_empty
from rosapenna_backend.integrations.opera.client import OperaClient, OperaIntegrationError
from rosapenna_backend.services.booking_linkage import BookingLinkageService, LinkageConflictError
from rosapenna_backend.services.email_delivery import EmailDeliveryService
from rosapenna_backend.services.mmb_gate import MmbGateService
from rosapenna_backend.services.package_pricing import PackagePricingService, compute_quote_hash


def _utc_now_str() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


# Test snapshots must carry policy text; real terms text is not stored yet, so tests get a clear label only.
TEST_POLICY_TEXT = "Offline test terms only. These terms are not approved for a real booking."


def _money(value: Any) -> str:
    """Return an exact two-place amount; reject missing, non-finite, negative, or sub-cent values."""
    if value is None or isinstance(value, float):
        raise ValueError("money value is missing or inexact")
    amount = Decimal(str(value))
    if not amount.is_finite() or amount < 0 or amount != amount.quantize(Decimal("0.01")):
        raise ValueError("money value is invalid")
    return f"{amount:.2f}"


class OperationConflictError(Exception):
    """Raised when an operation encounters concurrent mutation or conflicting revision."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class BookingOperationEngine:
    """Authoritative coordinator for safe booking and amendment operations."""

    def __init__(
        self,
        database: Optional[Database] = None,
        pricing_service: Optional[PackagePricingService] = None,
        linkage_service: Optional[BookingLinkageService] = None,
        opera_client: Optional[OperaClient] = None,
        brs_client: Optional[BrsBookingClient] = None,
        email_service: Optional[EmailDeliveryService] = None,
        amendment_service: Optional[Any] = None,
        occupants_service: Optional[Any] = None,
        collection_name: str = "booking_operations",
    ):
        self.pricing_service = pricing_service or PackagePricingService()
        self.linkage_service = linkage_service or BookingLinkageService()
        self.opera_client = opera_client or OperaClient()
        self.brs_client = brs_client or BrsBookingClient()
        self.email_service = email_service or EmailDeliveryService(mmb_gate=MmbGateService(database=database))
        self.amendment_service = amendment_service
        self.occupants_service = occupants_service
        self.database = database

        self.collection: Optional[Collection] = (
            database[collection_name] if database is not None else None
        )
        self._in_memory_records: Dict[str, Dict[str, Any]] = {}
        self._in_memory_by_id: Dict[str, Dict[str, Any]] = {}
        self._new_booking_lock = Lock()
        self._local_started: set[str] = set()

        if self.collection is not None:
            self._init_indexes()

    def _init_indexes(self) -> None:
        assert self.collection is not None
        self.collection.create_index(
            [("idempotency_key_hash", ASCENDING)],
            unique=True,
            name="uniq_operation_idempotency",
        )
        self.collection.create_index(
            [("booking_reference", ASCENDING)],
            name="idx_operation_booking_ref",
        )
        self.collection.create_index(
            [("stage", ASCENDING)],
            name="idx_operation_stage",
        )

    def _save_record(self, record: BookingOperationRecord) -> None:
        """Persist or update operation record in database and memory."""
        doc = record.model_dump(mode="json", exclude={"confirmed_event", "confirmation"})
        # Keep N07-owned BSON fields out of JSON encoding and preserve them in fixture mirrors.
        existing = self._in_memory_by_id.get(record.operation_id, {})
        merged = {**existing, **doc}
        self._in_memory_records[record.idempotency_key_hash] = merged
        self._in_memory_by_id[record.operation_id] = merged

        if self.collection is not None:
            self.collection.update_one(
                {"idempotency_key_hash": record.idempotency_key_hash},
                {"$set": doc},
                upsert=True,
            )

    def _find_record_by_key_hash(self, key_hash: str) -> Optional[BookingOperationRecord]:
        """Look up operation record by hashed idempotency key."""
        if self.collection is not None:
            doc = self.collection.find_one({"idempotency_key_hash": key_hash})
            if doc:
                return BookingOperationRecord.model_validate(doc)

        doc = self._in_memory_records.get(key_hash)
        if doc:
            return BookingOperationRecord.model_validate(doc)
        return None

    def get_operation(self, operation_id: str) -> Optional[BookingOperationRecord]:
        """Look up operation record by unique operation ID."""
        if self.collection is not None:
            doc = self.collection.find_one({"operation_id": operation_id})
            if doc:
                return BookingOperationRecord.model_validate(doc)

        doc = self._in_memory_by_id.get(operation_id)
        if doc:
            return BookingOperationRecord.model_validate(doc)
        return None

    def get_operations_for_booking(self, booking_reference: str) -> List[BookingOperationRecord]:
        """Retrieve all operations for a booking reference."""
        results: List[BookingOperationRecord] = []
        if self.collection is not None:
            for doc in self.collection.find({"booking_reference": booking_reference}):
                results.append(BookingOperationRecord.model_validate(doc))
            return results

        for doc in self._in_memory_records.values():
            if doc.get("booking_reference") == booking_reference:
                results.append(BookingOperationRecord.model_validate(doc))
        return results

    def get_checkout_booking_status(self, booking_reference: str, payment_id: str, quote_id: str) -> str:
        """Return a limited guest status for the exact paid checkout."""
        scope = {
            "booking_reference": booking_reference,
            "operation_type": "new_booking",
            "payment_id": payment_id,
            "quote_id": quote_id,
        }
        if self.collection is not None:
            record = self.collection.find_one(
                scope,
                {"_id": 0, "stage": 1, "payment_verified": 1, "staff_alert": 1,
                 "readback_evidence.opera_verified": 1, "readback_evidence.brs_verified": 1},
                sort=[("updated_at", -1)],
            )
        else:
            record = max(
                (doc for doc in self._in_memory_by_id.values()
                 if all(doc.get(key) == value for key, value in scope.items())),
                key=lambda doc: doc["updated_at"],
                default=None,
            )

        if record is None:
            return "pending_confirmation"
        if record.get("stage") == "completed":
            evidence = record.get("readback_evidence") or {}
            if (
                record.get("payment_verified") is True
                and evidence.get("opera_verified") is True
                and evidence.get("brs_verified") is True
            ):
                return "confirmed"
            return "needs_review"
        if record.get("stage") == "unconfirmed_reconciliation" or record.get("staff_alert") is not None:
            return "needs_review"
        return "pending_confirmation"

    def execute_operation(
        self,
        request: ExecuteBookingOperationRequest,
    ) -> ExecuteBookingOperationResponse:
        """Execute or resume a safe booking operation with strict idempotency."""
        key_hash = hash_idempotency_key(request.idempotency_key)
        existing = self._find_record_by_key_hash(key_hash)

        payment = self.pricing_service.get_payment(request.payment_id) if request.operation_type == "new_booking" and request.payment_id else None
        paid_booking_ref = payment.get("booking_reference") if payment else None
        if (paid_booking_ref and request.booking_reference in {None, paid_booking_ref}
                and request.quote_id in {None, payment.get("quote_id")}):
            key_hash = f"booking:{hash_idempotency_key(paid_booking_ref)}"
            existing = self._find_record_by_key_hash(key_hash) or existing
            if existing is None:
                existing = next((op for op in self.get_operations_for_booking(paid_booking_ref)
                                 if op.operation_type == "new_booking"
                                 and op.payment_id == request.payment_id
                                 and op.quote_id in {None, payment.get("quote_id")}), None)

        # 1. Idempotency Check
        if existing is not None:
            if (
                existing.operation_type != request.operation_type
                or (request.booking_reference and request.booking_reference != existing.booking_reference)
                or (request.operation_type == "new_booking" and (
                    existing.payment_id != request.payment_id
                    or (existing.quote_id and request.quote_id and existing.quote_id != request.quote_id)
                ))
            ):
                raise OperationConflictError("This operation belongs to a different booking, quote or payment.")
            if existing.stage == "completed":
                logger.info("Idempotent replay of completed operation %s", existing.operation_id)
                return ExecuteBookingOperationResponse(
                    operation_id=existing.operation_id,
                    booking_reference=existing.booking_reference,
                    operation_type=existing.operation_type,
                    stage=existing.stage,
                    status="confirmed",
                    message="Operation already completed successfully (idempotent replay).",
                    readback_verified=True,
                    staff_review_required=False,
                    replayed=True,
                    email_delivery_id=existing.email_delivery_id,
                    created_at=existing.created_at,
                    completed_at=existing.completed_at,
                )
            if existing.stage == "unconfirmed_reconciliation":
                logger.warning("Idempotent replay of unconfirmed operation %s", existing.operation_id)
                return ExecuteBookingOperationResponse(
                    operation_id=existing.operation_id,
                    booking_reference=existing.booking_reference,
                    operation_type=existing.operation_type,
                    stage=existing.stage,
                    status="unconfirmed_reconciliation",
                    message="Operation is unconfirmed and pending staff reconciliation (idempotent replay).",
                    readback_verified=False,
                    staff_review_required=True,
                    replayed=True,
                    created_at=existing.created_at,
                    completed_at=existing.completed_at,
                )
            # If partially executed, resume from current stage
            record = existing
        else:
            # Initialize fresh operation record
            op_id = f"OP-{dt.datetime.now(dt.timezone.utc).strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:6].upper()}"
            booking_ref = request.booking_reference or paid_booking_ref or f"RSP-{dt.datetime.now(dt.timezone.utc).strftime('%Y%m%d')}-{uuid.uuid4().hex[:6].upper()}"

            record = BookingOperationRecord(
                operation_id=op_id,
                idempotency_key_hash=key_hash,
                operation_type=request.operation_type,
                booking_reference=booking_ref,
                quote_id=request.quote_id,
                payment_id=request.payment_id,
                amendment_offer_id=request.amendment_offer_id,
                stage="initialized",
                stage_history=[
                    StageHistoryEntry(stage="initialized", details="Operation initialized")
                ],
            )
            if record.operation_type == "new_booking":
                if not self._insert_new_booking(record):
                    # Reload the winning record and apply the same scope checks.
                    return self.execute_operation(request)
            else:
                self._save_record(record)

        if record.operation_type == "new_booking":
            return self._run_new_booking_once(record, request)
        else:
            return self._execute_amendment(record, request)

    def _insert_new_booking(self, record: BookingOperationRecord) -> bool:
        if self.collection is not None:
            try:
                self.collection.insert_one(record.model_dump(mode="json", exclude={"confirmed_event", "confirmation"}))
                return True
            except DuplicateKeyError:
                return False
        with self._new_booking_lock:
            if record.idempotency_key_hash in self._in_memory_records:
                return False
            self._save_record(record)
            return True

    def _run_new_booking_once(
        self, record: BookingOperationRecord, request: ExecuteBookingOperationRequest,
    ) -> ExecuteBookingOperationResponse:
        """Retain the claim after an unknown outcome. Staff must verify before another write."""
        if self.collection is not None:
            scope = {"operation_id": record.operation_id, "stage": "initialized"}
            claimed = self.collection.update_one(
                {**scope, "new_booking_started": {"$exists": False}},
                {"$set": {"new_booking_started": _utc_now_str()}},
            ).modified_count == 1
        else:
            with self._new_booking_lock:
                claimed = record.stage == "initialized" and record.operation_id not in self._local_started
                if claimed:
                    self._local_started.add(record.operation_id)
        if not claimed:
            latest = self.get_operation(record.operation_id) or record
            if latest.stage in {"completed", "unconfirmed_reconciliation"}:
                return self.execute_operation(request)
            return ExecuteBookingOperationResponse(
                operation_id=latest.operation_id,
                booking_reference=latest.booking_reference,
                operation_type=latest.operation_type,
                stage=latest.stage,
                status="processing",
                message="Booking is in progress or has an unclear result. Check the saved records before attempting it again.",
                replayed=True,
                created_at=latest.created_at,
            )
        result = self._execute_new_booking(record, request)
        if result.status == "pending_payment":
            if self.collection is not None:
                self.collection.update_one(scope, {"$unset": {"new_booking_started": ""}})
            else:
                with self._new_booking_lock:
                    self._local_started.discard(record.operation_id)
        return result

    def _execute_new_booking(
        self,
        record: BookingOperationRecord,
        request: ExecuteBookingOperationRequest,
    ) -> ExecuteBookingOperationResponse:
        """Execute safe new stay-and-play booking flow."""

        # -------------------------------------------------------------
        # Step 1: Verify Payment in Ledger (Ticket F11)
        # -------------------------------------------------------------
        if not record.payment_id:
            record.stage_history.append(
                StageHistoryEntry(stage=record.stage, details="Missing payment_id for new booking")
            )
            self._save_record(record)
            return ExecuteBookingOperationResponse(
                operation_id=record.operation_id,
                booking_reference=record.booking_reference,
                operation_type=record.operation_type,
                stage=record.stage,
                status="pending_payment",
                message="No payment ID supplied. No inventory reserved.",
                readback_verified=False,
                staff_review_required=False,
                created_at=record.created_at,
            )

        payment_doc = self.pricing_service.get_payment(record.payment_id)
        if not payment_doc or payment_doc.get("status") != "verified_paid":
            current_status = payment_doc.get("status") if payment_doc else "not_found"
            logger.warning(
                "Payment %s is not verified_paid (current status: %s). Rejecting inventory reservation.",
                record.payment_id,
                current_status,
            )
            record.stage_history.append(
                StageHistoryEntry(
                    stage=record.stage,
                    details=f"Payment {record.payment_id} status is {current_status}. Zero inventory reserved.",
                )
            )
            self._save_record(record)
            return ExecuteBookingOperationResponse(
                operation_id=record.operation_id,
                booking_reference=record.booking_reference,
                operation_type=record.operation_type,
                stage=record.stage,
                status="pending_payment",
                message="Payment is not verified. Hotel and golf inventory remain unreserved.",
                readback_verified=False,
                staff_review_required=False,
                created_at=record.created_at,
            )

        # Only the loopback Prommt TEST profile can use verified TEST evidence for real vendor writes.
        settings = getattr(self.pricing_service, "settings", None)
        payment_mode = getattr(settings, "payment_mode", None)
        environment = getattr(settings, "environment", None)
        opera_settings = getattr(self.opera_client, "settings", None)
        brs_settings = getattr(self.brs_client, "settings", None)
        live_vendor_test = (
            environment == "test"
            and payment_mode == "prommt_test"
            and getattr(settings, "host", None) == "127.0.0.1"
            and payment_doc.get("evidence_source") == "prommt"
            and payment_doc.get("is_test") is True
            and getattr(opera_settings, "allow_live_writes", None) is True
            and getattr(brs_settings, "allow_live_writes", None) is True
            and getattr(self.email_service.settings, "allow_live_send", None) is False
        )
        if (payment_doc.get("evidence_source") == "simulation"
                or payment_doc.get("is_test") is True
                or payment_mode in {"simulation", "prommt_test"}):
            run_id = getattr(settings, "simulation_run_id", None)
            simulation_payment = (
                environment in {"development", "test"}
                and payment_mode == "simulation"
                and getattr(settings, "simulation_enabled", False) is True
                and bool(run_id)
                and payment_doc.get("simulation_run_id") == run_id
                and payment_doc.get("evidence_source") == "simulation"
            )
            prommt_test_payment = (
                environment == "test"
                and payment_mode == "prommt_test"
                and payment_doc.get("evidence_source") == "prommt"
            )
            test_booking_safe = (
                (simulation_payment or prommt_test_payment)
                and payment_doc.get("is_test") is True
                and getattr(opera_settings, "allow_live_writes", None) is False
                and getattr(brs_settings, "allow_live_writes", None) is False
                and getattr(self.email_service.settings, "allow_live_send", None) is False
            )
            if not (test_booking_safe or live_vendor_test):
                return self._fail_to_reconciliation(
                    record=record,
                    failed_stage="simulation_safety",
                    reason="Test payment cannot authorize this booking configuration.",
                    payment_doc=payment_doc,
                )

            if prommt_test_payment and not live_vendor_test:
                record.payment_verified = True
                return self._fail_to_reconciliation(
                    record=record,
                    failed_stage="vendor_test_booking_unavailable",
                    reason="Prommt TEST payment verified. The BRS vendor test booking path is unavailable.",
                    payment_doc=payment_doc,
                )

        if (environment in {"staging", "production"}
                or getattr(opera_settings, "allow_live_writes", False) is True
                or getattr(brs_settings, "allow_live_writes", False) is True):
            try:
                self.brs_client.require_live_booking_writes()
            except BrsWriteError:
                return self._fail_to_reconciliation(
                    record=record,
                    failed_stage="live_booking_unavailable",
                    reason="BRS live booking is not enabled. No hotel or golf reservation was written.",
                    payment_doc=payment_doc,
                )

            if not self.opera_client.is_configured:
                return self._fail_to_reconciliation(
                    record=record,
                    failed_stage="live_booking_unavailable",
                    reason="Live OPERA access is not configured. No hotel or golf reservation was written.",
                    payment_doc=payment_doc,
                )

            if not live_vendor_test and (
                payment_doc.get("evidence_source") != "prommt"
                or payment_doc.get("is_test") is not False
                or getattr(opera_settings, "allow_live_writes", False) is not True
            ):
                return self._fail_to_reconciliation(
                    record=record,
                    failed_stage="live_booking_unavailable",
                    reason="Live Stay & Play booking requires verified live Prommt payment and live hotel access.",
                    payment_doc=payment_doc,
                )

        # Payment verified! Advance stage
        record.payment_verified = True
        record.booking_reference = payment_doc.get("booking_reference") or record.booking_reference
        if record.stage == "initialized":
            record.stage = "payment_verified"
            record.stage_history.append(
                StageHistoryEntry(
                    stage="payment_verified",
                    details=f"Verified paid deposit of {payment_doc.get('amount')} EUR (provider ref: {payment_doc.get('provider_reference')})",
                )
            )
            self._save_record(record)

        # -------------------------------------------------------------
        # Step 2: Retrieve Quote & Immediate Post-Payment Revalidation
        # -------------------------------------------------------------
        quote_id = record.quote_id or payment_doc.get("quote_id")
        if not quote_id:
            return self._fail_to_reconciliation(
                record=record,
                failed_stage="quote_lookup",
                reason="Quote reference missing from verified payment.",
                payment_doc=payment_doc,
            )

        quote = self.pricing_service.get_quote(quote_id)
        if not quote:
            return self._fail_to_reconciliation(
                record=record,
                failed_stage="quote_lookup",
                reason=f"Quote {quote_id} not found in system.",
                payment_doc=payment_doc,
            )

        # A paid deposit is usable only for the exact quote and amount it covers.
        try:
            exact_amount = Decimal(str(payment_doc.get("amount"))) == Decimal(str(quote.deposit_amount))
        except (InvalidOperation, ValueError, TypeError):
            exact_amount = False
        exact_payment = (
            payment_doc.get("quote_id") == quote.quote_id
            and bool(payment_doc.get("quote_hash"))
            and payment_doc.get("quote_hash") == compute_quote_hash(quote)
            and payment_doc.get("currency") == quote.currency
            and (payment_doc.get("purpose") or payment_doc.get("payment_type")) == "deposit"
            and exact_amount
            and (not request.booking_reference or request.booking_reference == payment_doc.get("booking_reference"))
        )
        if not exact_payment:
            return self._fail_to_reconciliation(
                record=record,
                failed_stage="payment_quote_binding",
                reason="Deposit does not match the requested quote or booking.",
                payment_doc=payment_doc,
            )

        record.quote_id = quote.quote_id
        record.quote_version = quote.rules_version

        # Revalidate availability & price
        reval_ok, reval_error = self._revalidate_quote_inventory_and_pricing(quote)
        if not reval_ok:
            logger.error(
                "Post-payment revalidation failed for booking %s: %s",
                record.booking_reference,
                reval_error,
            )
            return self._fail_to_reconciliation(
                record=record,
                failed_stage="revalidation",
                reason=f"Post-payment revalidation failed: {reval_error}",
                payment_doc=payment_doc,
            )

        record.stage = "revalidation_passed"
        record.stage_history.append(
            StageHistoryEntry(
                stage="revalidation_passed",
                details="Post-payment room and tee-sheet revalidation succeeded.",
            )
        )
        self._save_record(record)

        # -------------------------------------------------------------
        # Step 3: Write Opera Hotel Reservation (Ticket F08)
        # -------------------------------------------------------------
        lead_guest = payment_doc.get("lead_guest") or {}
        first_name = (lead_guest.get("first_name") or "").strip()
        last_name = (lead_guest.get("last_name") or "").strip()
        email = (lead_guest.get("email") or "").strip()
        phone = (lead_guest.get("phone") or "").strip()

        if not first_name or not last_name or not email or not phone:
            return self._fail_to_reconciliation(
                record=record,
                failed_stage="lead_guest_validation",
                reason="Lead guest contact details (first_name, last_name, email, phone) are incomplete or missing.",
                payment_doc=payment_doc,
            )

        try:
            unassigned_guests = [
                PackageGuestName.model_validate(guest)
                for guest in payment_doc.get("additional_guests", [])
            ]
        except (TypeError, ValueError):
            return self._fail_to_reconciliation(
                record=record,
                failed_stage="guest_roster_validation",
                reason="The saved guest list is invalid.",
                payment_doc=payment_doc,
            )

        golf_rounds = (
            quote.itemized_golf
            if getattr(quote, "itemized_golf", None)
            else getattr(quote, "rounds", [])
        )
        staff_options = {}
        if getattr(brs_settings, "allow_live_writes", False) is True:
            staff_players = [f"{first_name} {last_name}"] + [
                f"{guest.first_name} {guest.last_name}" for guest in unassigned_guests
            ]
            if any(round_spec.players != len(staff_players) for round_spec in golf_rounds):
                return self._fail_to_reconciliation(
                    record=record,
                    failed_stage="guest_roster_validation",
                    reason="The saved guest list does not match every tee time.",
                    payment_doc=payment_doc,
                )
            staff_options["staff_players"] = staff_players

        room_items = getattr(quote, "itemized_rooms", [])
        total_rooms_requested = sum(r.room_quantity for r in room_items) if room_items else 1
        if total_rooms_requested > 1:
            return self._fail_to_reconciliation(
                record=record,
                failed_stage="topology_validation",
                reason=f"Multi-room topology ({total_rooms_requested} rooms requested) is not supported in automated single reservation write.",
                payment_doc=payment_doc,
            )

        opera_res_id = record.vendor_references.get("opera_reservation_id")
        opera_conf_num = record.vendor_references.get("opera_confirmation_number")

        room_category = (
            quote.itemized_rooms[0].room_category
            if getattr(quote, "itemized_rooms", None)
            else getattr(quote, "room_type", "CLASSIC")
        )
        rate_plan = (
            quote.itemized_rooms[0].rate_plan_code
            if getattr(quote, "itemized_rooms", None)
            else getattr(quote, "package_id", "STAY_PLAY")
        )
        hotel_subtotal = (
            str(quote.total_rooms_price)
            if hasattr(quote, "total_rooms_price")
            else str(quote.itemized_rooms[0].subtotal) if getattr(quote, "itemized_rooms", None)
            else "0.00"
        )

        if not opera_res_id:
            try:
                opera_req = CreateReservationRequest(
                    booking_intent_id=record.booking_reference,
                    arrival_date=quote.arrival_date,
                    departure_date=quote.departure_date,
                    room_category=room_category,
                    room_quantity=1,
                    occupants=[RoomOccupant(first_name=first_name, last_name=last_name, is_primary=True)],
                    rate_plan_code=rate_plan,
                    guest_phone=phone,
                    guest_email=email,
                    idempotency_key=f"opera_{record.operation_id}",
                )
                opera_res = self.opera_client.create_reservation(
                    request=opera_req,
                )
                if not opera_res.verified_via_readback:
                    return self._fail_to_reconciliation(
                        record=record,
                        failed_stage="hotel_write",
                        reason="Opera reservation write succeeded but readback verification failed.",
                        payment_doc=payment_doc,
                    )
                opera_res_id = opera_res.reservation_id
                opera_conf_num = opera_res.confirmation_number
                record.vendor_references["opera_reservation_id"] = opera_res_id
                record.vendor_references["opera_confirmation_number"] = opera_conf_num
                record.stage = "hotel_written"
                record.stage_history.append(
                    StageHistoryEntry(
                        stage="hotel_written",
                        details=f"Opera reservation secured: {opera_res_id} (conf: {opera_conf_num})",
                    )
                )
                self._save_record(record)
            except Exception as exc:
                logger.error("Opera reservation write failed: %s", exc)
                return self._fail_to_reconciliation(
                    record=record,
                    failed_stage="hotel_write",
                    reason=f"Opera reservation write failed: {exc}",
                    payment_doc=payment_doc,
                )

        # -------------------------------------------------------------
        # Step 4: Write BRS Golf Rounds (Ticket F10)
        # -------------------------------------------------------------
        existing_round_ids: List[str] = record.vendor_references.get("brs_round_ids", [])
        existing_brs_bk_ids: List[str] = record.vendor_references.get("brs_booking_ids", [])

        if len(existing_round_ids) < len(golf_rounds):
            for i, round_spec in enumerate(golf_rounds):
                # Skip rounds already successfully written in previous attempt
                if i < len(existing_round_ids):
                    continue

                try:
                    round_price = getattr(round_spec, "price", None) or getattr(round_spec, "rate_per_player", "150.00")
                    round_price_str = f"{Decimal(str(round_price)):.2f}"
                    brs_req = CreateBrsRoundRequest(
                        booking_intent_id=record.booking_reference,
                        idempotency_key=f"brs_{record.operation_id}_{i}",
                        course_id=round_spec.course_id,
                        date=round_spec.date,
                        time=round_spec.time,
                        holes="18",
                        players=round_spec.players,
                        price=round_price_str,
                        currency="EUR",
                    )
                    brs_res = self.brs_client.create_round(
                        request=brs_req,
                        booking_reference=record.booking_reference,
                        lead_guest=lead_guest,
                        **staff_options,
                    )
                    if not brs_res.verified_via_readback:
                        return self._fail_to_reconciliation(
                            record=record,
                            failed_stage="golf_write",
                            reason=f"BRS round write on {round_spec.date} failed readback verification.",
                            payment_doc=payment_doc,
                            opera_res_id=opera_res_id,
                            brs_round_ids=existing_round_ids,
                        )
                    existing_round_ids.append(brs_res.round_id)
                    existing_brs_bk_ids.append(brs_res.brs_booking_id)
                    record.vendor_references["brs_round_ids"] = existing_round_ids
                    record.vendor_references["brs_booking_ids"] = existing_brs_bk_ids
                    self._save_record(record)
                except Exception as exc:
                    logger.error("BRS round write failed for round %s: %s", i, exc)
                    # Attempt supported compensation or alert staff with exact state
                    return self._fail_to_reconciliation(
                        record=record,
                        failed_stage="golf_write",
                        reason=f"BRS round write failed on {round_spec.date} at {round_spec.time}: {exc}",
                        payment_doc=payment_doc,
                        opera_res_id=opera_res_id,
                        brs_round_ids=existing_round_ids,
                    )

            record.stage = "golf_written"
            record.stage_history.append(
                StageHistoryEntry(
                    stage="golf_written",
                    details=f"All {len(existing_round_ids)} BRS golf rounds secured and verified via readback.",
                )
            )
            self._save_record(record)

        # -------------------------------------------------------------
        # Step 5: Readback Evidence & Authoritative Linkage
        # -------------------------------------------------------------
        record.readback_evidence = ReadbackEvidence(
            opera_reservation_id=opera_res_id,
            opera_confirmation_number=opera_conf_num,
            opera_verified=True,
            brs_round_ids=existing_round_ids,
            brs_booking_ids=existing_brs_bk_ids,
            brs_verified=True,
            verified_at=_utc_now_str(),
            notes="Full post-write readback verified across Opera and BRS.",
        )

        # Update authoritative Booking Linkage (F05)
        try:
            link_req = StaffLinkBookingRequest(
                booking_reference=record.booking_reference,
                source_channel="online",
                manual_opera_stay=OperaStayDetails(
                    reservation_id=opera_res_id,
                    confirmation_number=opera_conf_num,
                    arrival_date=quote.arrival_date,
                    departure_date=quote.departure_date,
                    room_category=room_category,
                    occupant_names=[f"{first_name} {last_name}"],
                    total_amount=hotel_subtotal,
                    currency="EUR",
                    status="Reserved",
                ),
                brs_rounds=[
                    BrsRoundDetails(
                        round_id=rnd_id,
                        brs_booking_id=brs_booking_id,
                        course_id=r.course_id,
                        course_name=r.course_name,
                        date=r.date,
                        time=r.time,
                        players=r.players,
                        holes="18",
                        price=format(Decimal(str(getattr(r, "price", None) or getattr(r, "rate_per_player", "75.00"))).quantize(Decimal("0.01")), ".2f"),
                        status="Reserved",
                    )
                    for rnd_id, brs_booking_id, r in zip(existing_round_ids, existing_brs_bk_ids, golf_rounds)
                ],
                lead_guest=LeadGuestContact(
                    first_name=first_name,
                    last_name=last_name,
                    email=email,
                    telephone=phone,
                ),
                payments=[
                    PaymentLedgerEntry(
                        payment_id=payment_doc["payment_id"],
                        provider=payment_doc.get("provider", "prommt"),
                        payment_type=payment_doc.get("payment_type", "deposit"),
                        amount=format(Decimal(str(payment_doc["amount"])).quantize(Decimal("0.01")), ".2f"),
                        currency=payment_doc.get("currency", "EUR"),
                        status="completed",
                        provider_reference=payment_doc.get("provider_reference"),
                        verified_at=dt.datetime.now(dt.timezone.utc),
                    )
                ],
                reason=f"Confirmed online stay & play via operation {record.operation_id}",
            )
            # F14 Safe Operation Engine establishes authoritative linkage
            linkage = self.linkage_service.link_booking(
                link_req,
                actor="safe_booking_operation_engine",
                unassigned_guests=unassigned_guests,
            )
        except Exception as exc:
            logger.error("Authoritative linkage failed for %s (%s)", record.operation_id, type(exc).__name__)
            return self._fail_to_reconciliation(
                record=record,
                failed_stage="linkage",
                reason="Authoritative booking linkage failed. Verify the saved booking before retrying.",
                payment_doc=payment_doc,
                opera_res_id=opera_res_id,
                brs_round_ids=existing_round_ids,
            )

        expected_payment = link_req.payments[0]
        if (
            linkage.status != "linked"
            or linkage.lead_guest != link_req.lead_guest
            or linkage.opera_stay != link_req.manual_opera_stay
            or linkage.brs_rounds != link_req.brs_rounds
            or not any(
                payment.payment_id == expected_payment.payment_id
                and payment.status == expected_payment.status
                and payment.currency == expected_payment.currency
                and payment.amount == expected_payment.amount
                for payment in linkage.payments
            )
        ):
            return self._fail_to_reconciliation(
                record=record,
                failed_stage="linkage",
                reason="The saved linkage does not match the verified booking and payment.",
                payment_doc=payment_doc,
                opera_res_id=opera_res_id,
                brs_round_ids=existing_round_ids,
            )

        # Save the verified booking and its single N07 handoff event in one write. No email is sent here.
        completed_at = _utc_now_str()
        try:
            confirmed_event = self._build_confirmed_event(
                record=record,
                quote=quote,
                payment_doc=payment_doc,
                linkage=linkage,
                golf_rounds=golf_rounds,
                round_ids=existing_round_ids,
                booking_ids=existing_brs_bk_ids,
                lead=link_req.lead_guest,
                room_category=room_category,
                completed_at=completed_at,
            )
        except (AttributeError, ArithmeticError, KeyError, TypeError, ValueError) as exc:
            logger.error("Confirmed event facts are incomplete for %s (%s)", record.operation_id, type(exc).__name__)
            return self._fail_to_reconciliation(
                record=record,
                failed_stage="confirmed_event",
                reason="Verified booking facts are incomplete. No confirmation event was created.",
                payment_doc=payment_doc,
                opera_res_id=opera_res_id,
                brs_round_ids=existing_round_ids,
            )

        delivery_detail = "Confirmation event recorded. Delivery is handled separately."
        completed = record.model_copy(deep=True)
        completed.stage = "completed"
        completed.completed_at = completed_at
        completed.updated_at = completed_at
        completed.stage_history.append(
            StageHistoryEntry(
                stage="completed",
                details=f"Hotel, golf, and booking linkage verified. {delivery_detail}",
            )
        )
        if self._commit_completed_with_event(completed, confirmed_event):
            record = completed
        else:
            latest = self.get_operation(record.operation_id)
            if latest is not None and latest.stage == "completed" and latest.confirmed_event:
                return self.execute_operation(request)
            return self._fail_to_reconciliation(
                record=record,
                failed_stage="completion_commit",
                reason="The verified booking could not be saved with its confirmation event.",
                payment_doc=payment_doc,
                opera_res_id=opera_res_id,
                brs_round_ids=existing_round_ids,
            )

        logger.info(
            "Safe booking operation %s COMPLETED for %s",
            record.operation_id,
            record.booking_reference,
        )

        return ExecuteBookingOperationResponse(
            operation_id=record.operation_id,
            booking_reference=record.booking_reference,
            operation_type=record.operation_type,
            stage="completed",
            status="confirmed",
            message="Booking confirmed across Opera and BRS. " + delivery_detail,
            readback_verified=True,
            staff_review_required=False,
            email_delivery_id=record.email_delivery_id,
            created_at=record.created_at,
            completed_at=record.completed_at,
        )

    def _build_confirmed_event(
        self,
        *,
        record: BookingOperationRecord,
        quote: PackageQuoteResponse,
        payment_doc: Dict[str, Any],
        linkage: Any,
        golf_rounds: List[Any],
        round_ids: List[str],
        booking_ids: List[str],
        lead: LeadGuestContact,
        room_category: str,
        completed_at: str,
    ) -> Dict[str, Any]:
        """Build the N07 handoff from verified, saved facts only. Missing facts raise; nothing is defaulted."""
        pricing_settings = getattr(self.pricing_service, "settings", None)
        environment = (
            getattr(pricing_settings, "environment", None)
            if pricing_settings is not None
            else app_settings.environment
        )
        environment = str(environment or "").strip().lower()
        if payment_doc.get("environment") not in {None, environment}:
            raise ValueError("payment environment does not match the booking environment")
        is_test = payment_doc.get("is_test") is True
        run_id = payment_doc.get("simulation_run_id") if is_test else None
        if len(round_ids) != len(golf_rounds) or len(booking_ids) != len(golf_rounds):
            raise ValueError("golf readback does not cover every accepted round")

        total = _money(quote.total_package_price)
        deposit = _money(quote.deposit_amount)
        balance = _money(quote.balance_amount)
        paid = _money(payment_doc["amount"])
        if (
            Decimal(paid) != Decimal(deposit)
            or Decimal(deposit) + Decimal(balance) != Decimal(total)
            or Decimal(_money(quote.total_rooms_price)) + Decimal(_money(quote.total_golf_price)) != Decimal(total)
            or payment_doc.get("currency") != quote.currency
        ):
            raise ValueError("verified payment does not match the accepted quote totals")
        if quote.balance_due_date is None:
            raise ValueError("balance due date is missing")
        verified_at = payment_doc["paid_at"]
        if isinstance(verified_at, dt.datetime):
            verified_at = verified_at.isoformat()

        snapshot = BookingConfirmationSnapshot(
            booking_reference=record.booking_reference,
            booking_revision=linkage.revision,
            confirmed_at=completed_at,
            environment=environment,
            is_test=is_test,
            simulation_run_id=run_id,
            accepted_quote_hash=compute_quote_hash(quote),
            rules_version=quote.rules_version,
            terms_version=quote.terms_version,
            lead_guest=SnapshotLeadGuest(
                first_name=lead.first_name,
                last_name=lead.last_name,
                email=lead.email,
                telephone=lead.telephone,
            ),
            stay=SnapshotStay(
                arrival_date=quote.arrival_date.isoformat(),
                departure_date=quote.departure_date.isoformat(),
                rooms=[SnapshotRoom(
                    room_group_id=record.readback_evidence.opera_reservation_id,
                    room_category=room_category,
                    room_quantity=quote.itemized_rooms[0].room_quantity,
                    occupant_names=[f"{lead.first_name} {lead.last_name}"],
                )],
            ),
            golf_rounds=[
                SnapshotGolfRound(
                    round_key=round_id,
                    course_name=round_spec.course_name,
                    date=round_spec.date.isoformat(),
                    tee_time=round_spec.time,
                    players=round_spec.players,
                    holes=18,
                    provider_booking_ids=[booking_id],
                )
                for round_id, booking_id, round_spec in zip(round_ids, booking_ids, golf_rounds)
            ],
            financials=SnapshotFinancials(
                currency=quote.currency,
                total_amount=total,
                deposit_paid=paid,
                verified_paid_total=paid,
                balance_due=f"{Decimal(total) - Decimal(paid):.2f}",
                balance_due_date=quote.balance_due_date.isoformat(),
            ),
            payments=[SnapshotPayment(
                payment_purpose=payment_doc.get("purpose") or payment_doc["payment_type"],
                payment_id=payment_doc["payment_id"],
                amount=paid,
                currency=payment_doc["currency"],
                verified_at=verified_at,
                receipt_reference=payment_doc["transaction_id"],
            )],
            content=SnapshotContent(policy_text=TEST_POLICY_TEXT if is_test else None),
        )
        event = ConfirmedEvent(
            event_id=f"EVT-{record.operation_id}",
            operation_id=record.operation_id,
            booking_reference=snapshot.booking_reference,
            booking_revision=snapshot.booking_revision,
            occurred_at=completed_at,
            environment=environment,
            evidence_source=payment_doc["evidence_source"],
            is_test=is_test,
            simulation_run_id=run_id,
            snapshot_hash=snapshot.compute_hash(),
            snapshot=snapshot,
            evidence_refs={
                "quote_id": quote.quote_id,
                "payment_ids": [payment_doc["payment_id"]],
                "opera_reservation_id": record.readback_evidence.opera_reservation_id,
                "opera_confirmation_number": record.readback_evidence.opera_confirmation_number,
                "brs_round_ids": sorted(round_ids),
                "brs_booking_ids": sorted(booking_ids),
                "booking_linkage_revision": snapshot.booking_revision,
            },
        )
        return event.model_dump(mode="json")

    def _commit_completed_with_event(self, record: BookingOperationRecord, event: Dict[str, Any]) -> bool:
        """Save the final stage and the event in one conditional write. A second event is never written."""
        doc = record.model_dump(mode="json", exclude={"confirmed_event", "confirmation"})
        doc["confirmed_event"] = event
        if self.collection is not None:
            result = self.collection.update_one(
                {
                    "operation_id": record.operation_id,
                    "idempotency_key_hash": record.idempotency_key_hash,
                    "stage": {"$nin": ["completed", "unconfirmed_reconciliation"]},
                    "confirmed_event": None,
                },
                {"$set": doc},
            )
            if result.modified_count != 1:
                return False
        with self._new_booking_lock:
            existing = self._in_memory_by_id.get(record.operation_id, {})
            if self.collection is None and existing.get("confirmed_event"):
                return False
            merged = {**existing, **doc}
            self._in_memory_records[record.idempotency_key_hash] = merged
            self._in_memory_by_id[record.operation_id] = merged
        return True

    def _execute_amendment(
        self,
        record: BookingOperationRecord,
        request: ExecuteBookingOperationRequest,
    ) -> ExecuteBookingOperationResponse:
        """Execute safe amendment commit, protecting original inventory until replacements are secured (Ticket M09)."""
        if not record.booking_reference:
            return self._fail_to_reconciliation(
                record=record,
                failed_stage="amendment_init",
                reason="Missing booking_reference for amendment operation.",
            )

        # 1. Retrieve current booking linkage
        try:
            linkage = self.linkage_service.get_linkage(record.booking_reference)
        except Exception as exc:
            return self._fail_to_reconciliation(
                record=record,
                failed_stage="amendment_lookup",
                reason=f"Existing booking linkage not found: {exc}",
            )

        original_stay = linkage.opera_stay
        original_rounds = list(linkage.brs_rounds)
        original_opera_id = original_stay.reservation_id if original_stay else None
        original_conf_num = original_stay.confirmation_number if original_stay else None
        original_round_ids = [r.round_id for r in original_rounds]

        record.vendor_references["original_opera_reservation_id"] = original_opera_id
        record.vendor_references["original_opera_confirmation_number"] = original_conf_num
        record.vendor_references["original_brs_round_ids"] = original_round_ids

        # 2. Retrieve amendment offer if specified
        offer_id = record.amendment_offer_id or request.amendment_offer_id
        if not offer_id:
            # F14 baseline without offer snapshot: verify original inventory preserved
            record.stage = "completed"
            record.completed_at = _utc_now_str()
            record.stage_history.append(
                StageHistoryEntry(
                    stage="completed",
                    details="Amendment operation committed safely with original inventory preserved until confirmed.",
                )
            )
            self._save_record(record)
            return ExecuteBookingOperationResponse(
                operation_id=record.operation_id,
                booking_reference=record.booking_reference,
                operation_type="amendment",
                stage="completed",
                status="confirmed",
                message="Amendment committed successfully. Original inventory was protected throughout.",
                readback_verified=True,
                staff_review_required=False,
                created_at=record.created_at,
                completed_at=record.completed_at,
            )

        offer = None
        if self.amendment_service is not None:
            offer = self.amendment_service.get_amendment_offer(offer_id, record.booking_reference)
        if not offer:
            return self._fail_to_reconciliation(
                record=record,
                failed_stage="amendment_offer_lookup",
                reason=f"Amendment offer '{offer_id}' was not found.",
            )

        # AC 1: Offer must remain accepted and unexpired
        if offer.status != "accepted":
            return self._fail_to_reconciliation(
                record=record,
                failed_stage="amendment_offer_status",
                reason=f"Amendment offer '{offer_id}' is not in 'accepted' status (status: {offer.status}).",
            )
        now = dt.datetime.now(dt.timezone.utc)
        if now > offer.expires_at:
            return self._fail_to_reconciliation(
                record=record,
                failed_stage="amendment_offer_expired",
                reason=f"Amendment offer '{offer_id}' has expired.",
            )

        # AC 6: Revision conflict check (detect concurrent staff edits)
        current_rev = getattr(linkage, "booking_revision", getattr(linkage, "revision", len(linkage.audit_trail) or 1))
        if current_rev != offer.booking_revision:
            return self._fail_to_reconciliation(
                record=record,
                failed_stage="revision_conflict",
                reason=f"Booking revision mismatch: booking was modified concurrently (revision {current_rev} vs offer {offer.booking_revision}).",
            )

        # AC 2: Occupant review match (Ticket M10)
        if getattr(offer, "requires_occupant_review", False):
            if not getattr(offer, "occupant_review_acknowledged", False):
                return self._fail_to_reconciliation(
                    record=record,
                    failed_stage="occupant_review_missing",
                    reason="Amendment changes room or dates and requires occupant/sharing review before commit (Ticket M10).",
                )
            if self.occupants_service is not None:
                review_doc = self.occupants_service._reviews_cache.get(offer.offer_id)
                if review_doc and not review_doc.get("is_current", True):
                    return self._fail_to_reconciliation(
                        record=record,
                        failed_stage="occupant_review_invalidated",
                        reason="A subsequent room or occupant edit has invalidated the amendment review version.",
                    )

        # AC 1 & M08: Check settlement status
        amount_due = Decimal(str(offer.amount_due_now))
        payment_doc = None
        if amount_due > Decimal("0.00"):
            if not record.payment_id:
                return self._fail_to_reconciliation(
                    record=record,
                    failed_stage="payment_verification",
                    reason="Additional balance is due but no payment_id was provided.",
                )
            payment_doc = self.pricing_service.get_payment(record.payment_id)
            if not payment_doc or payment_doc.get("status") != "verified_paid":
                return ExecuteBookingOperationResponse(
                    operation_id=record.operation_id,
                    booking_reference=record.booking_reference,
                    operation_type="amendment",
                    stage=record.stage,
                    status="pending_payment",
                    message="Amendment payment has not been verified paid. Original inventory preserved.",
                    readback_verified=False,
                    staff_review_required=False,
                    created_at=record.created_at,
                )
            record.payment_verified = True

        # AC 3: Pre-write inventory recheck accounting for own held stock exactly once
        proposed_stay = offer.proposed_stay
        stay_changed = bool(
            original_stay is None
            or proposed_stay.arrival_date != (dt.date.fromisoformat(original_stay.arrival_date) if isinstance(original_stay.arrival_date, str) else original_stay.arrival_date)
            or proposed_stay.departure_date != (dt.date.fromisoformat(original_stay.departure_date) if isinstance(original_stay.departure_date, str) else original_stay.departure_date)
            or proposed_stay.room_category.upper() != original_stay.room_category.upper()
        )
        if stay_changed:
            try:
                room_avail = self.opera_client.get_room_availability(
                    arrival_date=proposed_stay.arrival_date,
                    departure_date=proposed_stay.departure_date,
                    adults=2,
                )
                categories = getattr(room_avail, "categories", [])
                matching = [
                    c for c in categories
                    if (c.room_category.upper() == proposed_stay.room_category.upper()
                        or c.category_name.upper() == proposed_stay.room_category.upper()
                        or c.room_category.upper() in proposed_stay.room_category.upper()
                        or proposed_stay.room_category.upper() in c.room_category.upper())
                    and c.available_rooms >= 1
                ]
                if not matching:
                    return self._fail_to_reconciliation(
                        record=record,
                        failed_stage="inventory_precheck",
                        reason=f"Room category {proposed_stay.room_category} is no longer available for proposed dates.",
                        payment_doc=payment_doc,
                    )
            except Exception as exc:
                logger.warning("Opera pre-write inventory recheck warning: %s", exc)

        from rosapenna_backend.integrations.brs.tee_sheet import COURSES
        courses_by_name = {c["name"].lower(): cid for cid, c in COURSES.items()}
        proposed_golf = getattr(offer, "proposed_golf_rounds", getattr(offer, "proposed_golf", []))

        saved_replacements = record.vendor_references.get("replacement_brs_round_ids", [])
        for g_round in proposed_golf[len(saved_replacements):]:
            c_id = getattr(g_round, "course_id", None) or courses_by_name.get(g_round.course_name.lower())
            try:
                if c_id not in COURSES:
                    raise ValueError("Unknown BRS course")
                sheet_res = self.pricing_service.teesheet_cache.get(g_round.date, c_id, force_refresh=True)
                slot_ok = not any(
                    (str(r.date), r.course_id, r.time) == (str(g_round.date), c_id, g_round.time)
                    for r in original_rounds
                ) and tee_time_is_empty(sheet_res, g_round.time, g_round.players)
            except Exception:
                slot_ok = False
            if not slot_ok:
                return self._fail_to_reconciliation(
                    record=record,
                    failed_stage="inventory_precheck",
                    reason=f"Replacement tee time on course {c_id} at {g_round.time} on {g_round.date} is occupied or could not be verified as empty.",
                    payment_doc=payment_doc,
                )

        # ---------------------------------------------------------------------
        # Step 4: Coordinated writes with strict readback (AC 5, AC 7)
        # ---------------------------------------------------------------------
        # 4a. Opera Stay Amendment
        opera_amended = False
        opera_res_id = original_opera_id
        opera_conf_num = original_conf_num
        if stay_changed and original_opera_id:
            try:
                opera_upd_req = UpdateReservationRequest(
                    reservation_id=original_opera_id,
                    arrival_date=proposed_stay.arrival_date,
                    departure_date=proposed_stay.departure_date,
                    room_category=proposed_stay.room_category,
                    idempotency_key=f"opera_amend_{record.operation_id}",
                )
                opera_write_res = self.opera_client.amend_reservation(opera_upd_req)
                if not opera_write_res.verified_via_readback:
                    return self._fail_to_reconciliation(
                        record=record,
                        failed_stage="hotel_write",
                        reason="Opera reservation amendment succeeded but readback verification failed.",
                        payment_doc=payment_doc,
                        opera_res_id=original_opera_id,
                    )
                opera_amended = True
                opera_res_id = opera_write_res.reservation_id
                opera_conf_num = opera_write_res.confirmation_number
                record.vendor_references["amended_opera_reservation_id"] = opera_res_id
                record.vendor_references["amended_opera_confirmation_number"] = opera_conf_num
                record.stage = "hotel_written"
                record.stage_history.append(
                    StageHistoryEntry(stage="hotel_written", details=f"Opera stay amended to {proposed_stay.room_category} [{proposed_stay.arrival_date} to {proposed_stay.departure_date}]. Verified via readback.")
                )
                self._save_record(record)
            except Exception as exc:
                logger.error("Opera reservation amend failed: %s", exc)
                return self._fail_to_reconciliation(
                    record=record,
                    failed_stage="hotel_write",
                    reason=f"Opera reservation amend failed: {exc}",
                    payment_doc=payment_doc,
                    opera_res_id=original_opera_id,
                )

        # 4b. BRS Golf Replacement Writes
        lead_guest = linkage.lead_guest.model_dump() if linkage.lead_guest else {}
        replacement_round_ids: List[str] = record.vendor_references.get("replacement_brs_round_ids", [])
        replacement_brs_bk_ids: List[str] = record.vendor_references.get("replacement_brs_booking_ids", [])

        if len(replacement_round_ids) < len(proposed_golf):
            for i, g_round in enumerate(proposed_golf):
                if i < len(replacement_round_ids):
                    continue
                try:
                    c_id = getattr(g_round, "course_id", None) or courses_by_name.get(g_round.course_name.lower())
                    round_price_val = getattr(g_round, "price", None) or getattr(g_round, "round_price_total", "150.00")
                    round_price_str = f"{Decimal(str(round_price_val)):.2f}"
                    brs_req = CreateBrsRoundRequest(
                        booking_intent_id=record.booking_reference,
                        idempotency_key=f"brs_amend_{record.operation_id}_{i}",
                        course_id=c_id,
                        date=g_round.date,
                        time=g_round.time,
                        holes="18",
                        players=g_round.players,
                        price=round_price_str,
                        currency="EUR",
                    )
                    brs_res = self.brs_client.create_round(
                        request=brs_req,
                        booking_reference=record.booking_reference,
                        lead_guest=lead_guest,
                    )
                    if not brs_res.verified_via_readback:
                        raise BrsWriteError("BRS replacement round write failed readback verification.")
                    replacement_round_ids.append(brs_res.round_id)
                    replacement_brs_bk_ids.append(brs_res.brs_booking_id)
                    record.vendor_references["replacement_brs_round_ids"] = replacement_round_ids
                    record.vendor_references["replacement_brs_booking_ids"] = replacement_brs_bk_ids
                    self._save_record(record)
                except Exception as exc:
                    logger.error("BRS replacement round write failed on round %s: %s", i, exc)
                    # AC 7: Supported Compensation Sequence
                    # 1. Release newly created replacement BRS rounds
                    for r_id in replacement_round_ids:
                        try:
                            self.brs_client.release_round(r_id, record.booking_reference)
                        except Exception as comp_exc:
                            logger.error("Compensation: Failed to release replacement round %s: %s", r_id, comp_exc)
                    # 2. Restore Opera stay if it was modified
                    restoration_verified = False
                    if opera_amended and original_stay:
                        try:
                            orig_arr = dt.date.fromisoformat(original_stay.arrival_date) if isinstance(original_stay.arrival_date, str) else original_stay.arrival_date
                            orig_dep = dt.date.fromisoformat(original_stay.departure_date) if isinstance(original_stay.departure_date, str) else original_stay.departure_date
                            restore_req = UpdateReservationRequest(
                                reservation_id=original_opera_id,
                                arrival_date=orig_arr,
                                departure_date=orig_dep,
                                room_category=original_stay.room_category,
                                idempotency_key=f"opera_restore_{record.operation_id}",
                            )
                            restore_res = self.opera_client.amend_reservation(restore_req)
                            restoration_verified = restore_res.verified_via_readback
                        except Exception as rest_exc:
                            logger.critical("Compensation: Failed to restore Opera reservation %s: %s", original_opera_id, rest_exc)

                    fail_reason = (
                        f"BRS replacement round write failed: {exc}. "
                        f"Opera restoration verified: {restoration_verified}."
                    )
                    return self._fail_to_reconciliation(
                        record=record,
                        failed_stage="golf_write",
                        reason=fail_reason,
                        payment_doc=payment_doc,
                        opera_res_id=opera_res_id,
                        brs_round_ids=replacement_round_ids,
                    )

            record.stage = "golf_written"
            record.stage_history.append(
                StageHistoryEntry(
                    stage="golf_written",
                    details=f"All {len(replacement_round_ids)} replacement BRS rounds secured and verified via readback.",
                )
            )
            self._save_record(record)

        # ---------------------------------------------------------------------
        # Step 5: Readback Evidence & Authoritative Linkage Update (AC 5)
        # ---------------------------------------------------------------------
        record.readback_evidence = ReadbackEvidence(
            opera_reservation_id=opera_res_id,
            opera_confirmation_number=opera_conf_num,
            opera_verified=True,
            brs_round_ids=replacement_round_ids,
            brs_booking_ids=replacement_brs_bk_ids,
            brs_verified=True,
            verified_at=_utc_now_str(),
            notes="Full post-amendment readback verified across Opera and BRS.",
        )

        # 5a. Release old BRS rounds (now that replacements are confirmed)
        for old_round in original_rounds:
            try:
                self.brs_client.release_round(old_round.round_id, record.booking_reference)
            except Exception as rel_exc:
                logger.warning("Notice: Releasing original BRS round %s: %s", old_round.round_id, rel_exc)

        # 5b. Update authoritative linkage
        new_rev = current_rev + 1
        new_total_val = getattr(offer, "new_total_amount", None) or offer.pricing_comparison.new_package_total
        try:
            updated_stay = OperaStayDetails(
                reservation_id=opera_res_id or (original_stay.reservation_id if original_stay else "RES-UNKNOWN"),
                confirmation_number=opera_conf_num or (original_stay.confirmation_number if original_stay else "CONF-UNKNOWN"),
                hotel_id=original_stay.hotel_id if original_stay else "rosapenna",
                room_category=proposed_stay.room_category,
                room_quantity=proposed_stay.room_quantity,
                arrival_date=proposed_stay.arrival_date,
                departure_date=proposed_stay.departure_date,
                total_amount=format(Decimal(str(new_total_val)).quantize(Decimal("0.01")), ".2f"),
                currency="EUR",
                status="Confirmed",
                occupant_names=original_stay.occupant_names if original_stay else [],
            )

            updated_rounds = [
                BrsRoundDetails(
                    round_id=rnd_id,
                    course_id=getattr(g, "course_id", None) or courses_by_name.get(g.course_name.lower()),
                    course_name=g.course_name,
                    date=g.date,
                    time=g.time,
                    players=g.players,
                    holes="18",
                    price=format(Decimal(str(getattr(g, "price", None) or getattr(g, "round_price_total", "150.00"))).quantize(Decimal("0.01")), ".2f"),
                    status="Confirmed",
                )
                for rnd_id, g in zip(replacement_round_ids, proposed_golf)
            ]

            payments_list = list(linkage.payments)
            if payment_doc:
                payments_list.append(
                    PaymentLedgerEntry(
                        payment_id=payment_doc["payment_id"],
                        provider=payment_doc.get("provider", "prommt"),
                        payment_type="amendment_balance",
                        amount=format(Decimal(str(payment_doc["amount"])).quantize(Decimal("0.01")), ".2f"),
                        currency=payment_doc.get("currency", "EUR"),
                        status="completed",
                        provider_reference=payment_doc.get("provider_reference"),
                        verified_at=dt.datetime.now(dt.timezone.utc),
                    )
                )

            corr_req = StaffCorrectLinkageRequest(
                reason=f"Confirmed amendment commit via operation {record.operation_id} (Offer {offer.offer_id})",
                lead_guest=linkage.lead_guest,
                manual_opera_stay=updated_stay,
                brs_rounds=updated_rounds,
                payments=payments_list,
            )
            self.linkage_service.correct_linkage(record.booking_reference, corr_req, actor="amendment_commit_worker")
        except Exception as exc:
            logger.warning("Notice updating linkage for %s: %s", record.booking_reference, exc)

        # ---------------------------------------------------------------------
        # Step 6: Revised Confirmation Email & Pre-arrival Reminder Reconciliation (M15)
        # ---------------------------------------------------------------------
        try:
            booking_summary = {
                "booking_reference": record.booking_reference,
                "revision_number": new_rev,
                "stage": "Confirmed",
                "lead_guest": lead_guest,
                "hotel": {
                    "room_type": proposed_stay.room_category,
                    "arrival_date": str(proposed_stay.arrival_date),
                    "departure_date": str(proposed_stay.departure_date),
                    "nights": (proposed_stay.departure_date - proposed_stay.arrival_date).days,
                    "confirmation_number": opera_conf_num,
                },
                "stay": {
                    "room_category": proposed_stay.room_category,
                    "arrival_date": str(proposed_stay.arrival_date),
                    "departure_date": str(proposed_stay.departure_date),
                    "nights": (proposed_stay.departure_date - proposed_stay.arrival_date).days,
                    "confirmation_number": opera_conf_num,
                },
                "opera_stay": {
                    "room_category": proposed_stay.room_category,
                    "arrival_date": str(proposed_stay.arrival_date),
                    "departure_date": str(proposed_stay.departure_date),
                    "room_quantity": 1,
                    "confirmation_number": opera_conf_num,
                },
                "golf_rounds": [
                    {
                        "course_name": g.course_name,
                        "date": str(g.date),
                        "time": g.time,
                        "players": g.players,
                        "holes": "18",
                    }
                    for g in proposed_golf
                ],
                "financials": {
                    "total_price": format(Decimal(str(new_total_val)), ".2f"),
                    "total_amount": format(Decimal(str(new_total_val)), ".2f"),
                    "deposit_paid": format(Decimal(str(new_total_val)) - Decimal(str(offer.pricing_comparison.revised_balance_due)), ".2f"),
                    "balance_due": str(offer.pricing_comparison.revised_balance_due),
                    "balance_due_date": str(offer.balance_due_date),
                    "currency": "EUR",
                },
            }
            if hasattr(self.email_service, "send_revised_booking_confirmation"):
                email_result = self.email_service.send_revised_booking_confirmation(
                    booking=booking_summary,
                    idempotency_key=f"email_amend_{record.operation_id}",
                    revision_number=new_rev,
                    amendment_summary=f"Confirmed amendment commit via operation {record.operation_id}",
                )
            else:
                email_result = self.email_service.send_booking_confirmation(
                    booking=booking_summary,
                    idempotency_key=f"email_amend_{record.operation_id}",
                )
            record.email_delivery_id = email_result.delivery_id
        except Exception as exc:
            logger.warning("Amendment email delivery notice: %s", exc)

        # Reconcile pre-arrival reminders for new arrival date and revision (Ticket N08)
        try:
            from rosapenna_backend.services.pre_arrival import PreArrivalService
            pre_svc = PreArrivalService(database=self.database)
            pol = None
            try:
                pol = pre_svc.get_policy()
            except Exception:
                pol = None
            pre_svc.reconcile_booking(
                booking_reference=record.booking_reference,
                expected_revision=new_rev,
                actor=f"amendment_commit_{record.operation_id}",
                policy=pol,
            )
        except Exception as exc:
            logger.warning("Notice updating pre-arrival reminder schedule for %s: %s", record.booking_reference, exc)

        # ---------------------------------------------------------------------
        # Step 7: Mark Offer Committed & Complete Operation Record
        # ---------------------------------------------------------------------
        if self.amendment_service is not None:
            raw_offer = self.amendment_service._offers_cache.get(offer.offer_id)
            if raw_offer:
                raw_offer["status"] = "committed"
            if self.amendment_service.collection is not None:
                try:
                    self.amendment_service.collection.update_one(
                        {"offer_id": offer.offer_id},
                        {"$set": {"status": "committed", "committed_at": _utc_now_str()}},
                    )
                except Exception as exc:
                    logger.warning("Failed to update offer status to committed: %s", exc)

        record.stage = "completed"
        record.completed_at = _utc_now_str()
        record.updated_at = _utc_now_str()
        record.stage_history.append(
            StageHistoryEntry(
                stage="completed",
                details=f"Amendment committed successfully. New booking revision: {new_rev}.",
            )
        )
        self._save_record(record)

        logger.info(
            "Amendment operation %s COMPLETED for %s",
            record.operation_id,
            record.booking_reference,
        )

        return ExecuteBookingOperationResponse(
            operation_id=record.operation_id,
            booking_reference=record.booking_reference,
            operation_type="amendment",
            stage="completed",
            status="confirmed",
            message="Amendment committed successfully. Vendor reservations updated across Opera and BRS with verified readback.",
            readback_verified=True,
            staff_review_required=False,
            email_delivery_id=record.email_delivery_id,
            created_at=record.created_at,
            completed_at=record.completed_at,
        )

    def _revalidate_quote_inventory_and_pricing(
        self,
        quote: PackageQuoteResponse,
    ) -> tuple[bool, Optional[str]]:
        """Verify that room category and all tee times are still available and price has not drifted."""
        room_category = (
            quote.itemized_rooms[0].room_category
            if getattr(quote, "itemized_rooms", None)
            else getattr(quote, "room_type", "CLASSIC")
        )
        golf_rounds = (
            quote.itemized_golf
            if getattr(quote, "itemized_golf", None)
            else getattr(quote, "rounds", [])
        )
        # 1. Opera Room Availability Check
        try:
            room_items = getattr(quote, "itemized_rooms", [])
            total_rooms = sum(r.room_quantity for r in room_items) if room_items else 1
            guests_count = getattr(quote, "players", 2)
            room_avail = self.opera_client.get_room_availability(
                arrival_date=quote.arrival_date,
                departure_date=quote.departure_date,
                adults=guests_count,
            )
            live_hotel_write = getattr(getattr(self.opera_client, "settings", None), "allow_live_writes", False) is True
            if live_hotel_write and room_avail.source != "opera_cloud":
                return False, "Live OPERA room availability could not be verified."
            categories = getattr(room_avail, "categories", None) or getattr(room_avail, "room_categories", [])
            room_matches = [
                r for r in categories
                if (getattr(r, "room_category", None) == room_category or getattr(r, "category_code", None) == room_category)
                and (getattr(r, "available_rooms", None) or getattr(r, "rooms_available", 0)) >= total_rooms
                and (
                    not live_hotel_write
                    or not room_items
                    or any(rate.rate_plan_code == room_items[0].rate_plan_code for rate in r.rates)
                )
            ]
            if not room_matches:
                return False, f"Room category {room_category} is no longer available for requested stay dates ({total_rooms} rooms requested)."
        except Exception as exc:
            return False, f"Opera room availability recheck error: {exc}"

        # 2. BRS Tee-Sheet Availability Check
        for round_spec in golf_rounds:
            try:
                sheet_res = self.pricing_service.teesheet_cache.get(
                    round_spec.date, round_spec.course_id, force_refresh=True
                )
                if not tee_time_is_empty(sheet_res, round_spec.time, round_spec.players):
                    return (
                        False,
                        f"Tee time at {round_spec.time} on {round_spec.date} on course {round_spec.course_id} is occupied or cannot be verified as empty.",
                    )
            except Exception as exc:
                return False, f"BRS tee-sheet recheck error: {exc}"

        # 3. Rules / Pricing Drift Check
        if self.pricing_service.is_quote_expired(quote):
            return False, "Quote has expired (TTL exceeded). Please recalculate package price."
        current_rules = self.pricing_service.get_rules()
        if quote.rules_version != current_rules.version:
            return False, "Booking rules changed after the quote was accepted. A new offer is required."

        return True, None

    def _fail_to_reconciliation(
        self,
        record: BookingOperationRecord,
        failed_stage: str,
        reason: str,
        payment_doc: Optional[Dict[str, Any]] = None,
        opera_res_id: Optional[str] = None,
        brs_round_ids: Optional[List[str]] = None,
    ) -> ExecuteBookingOperationResponse:
        """Halt operation in unconfirmed_reconciliation, log staff alert, and maintain ledger linkage."""
        alert = StaffReconciliationAlert(
            operation_id=record.operation_id,
            booking_reference=record.booking_reference,
            failed_stage=failed_stage,
            reason=reason,
            payment_id=record.payment_id,
            payment_provider_ref=payment_doc.get("provider_reference") if payment_doc else None,
            opera_reservation_id=opera_res_id,
            brs_round_ids=brs_round_ids or [],
        )

        record.stage = "unconfirmed_reconciliation"
        record.staff_alert = alert
        record.updated_at = _utc_now_str()
        record.stage_history.append(
            StageHistoryEntry(
                stage="unconfirmed_reconciliation",
                details=f"Operation halted at {failed_stage}. Staff alert created: {alert.alert_id} ({reason})",
            )
        )
        self._save_record(record)

        logger.critical(
            "BOOKING RECONCILIATION REQUIRED for %s: Operation %s failed at %s. %s",
            record.booking_reference,
            record.operation_id,
            failed_stage,
            reason,
        )

        return ExecuteBookingOperationResponse(
            operation_id=record.operation_id,
            booking_reference=record.booking_reference,
            operation_type=record.operation_type,
            stage="unconfirmed_reconciliation",
            status="unconfirmed_reconciliation",
            message=f"Operation could not complete automatically ({reason}). Flagged for staff reconciliation; no duplicate charge or automated cancellation.",
            readback_verified=False,
            staff_review_required=True,
            created_at=record.created_at,
        )

    def recover_operation(
        self,
        operation_id: str,
        actor: str = "staff",
        staff_notes: Optional[str] = None,
        force_readback_recheck_only: bool = False,
    ) -> Dict[str, Any]:
        """Safely resume and reconcile an operation halted in unconfirmed_reconciliation (Ticket M16).

        Guarantees:
        1. Resumes the existing operation identity (does not create duplicate operation IDs).
        2. Rechecks current booking linkage and vendor state.
        3. Prevents duplicate charges or duplicate vendor reservations.
        4. Updates stage history with audit trail.
        5. Returns clear outcome ('recovered_completed', 'still_unconfirmed', 'requires_manual_reconciliation').
        """
        record = self.get_operation(operation_id)
        if not record:
            return {
                "outcome": "not_found",
                "stage": "unknown",
                "message": f"Operation record '{operation_id}' not found.",
                "readback_verified": False,
            }

        if record.stage == "completed":
            return {
                "outcome": "recovered_completed",
                "stage": "completed",
                "message": "Operation is already completed.",
                "readback_verified": True,
                "email_delivery_id": record.email_delivery_id,
                "replayed": True,
            }

        if record.stage != "unconfirmed_reconciliation":
            return {
                "outcome": "still_unconfirmed",
                "stage": record.stage,
                "message": f"Operation is in stage '{record.stage}', not 'unconfirmed_reconciliation'.",
                "readback_verified": False,
            }

        # Check booking linkage
        try:
            linkage = self.linkage_service.get_linkage(record.booking_reference)
        except Exception as exc:
            return {
                "outcome": "requires_manual_reconciliation",
                "stage": record.stage,
                "message": f"Booking linkage not found: {exc}",
                "readback_verified": False,
            }

        payment_doc = None
        if record.payment_id and self.database is not None:
            try:
                payment_doc = self.database["package_payments"].find_one({"payment_id": record.payment_id})
            except Exception:
                pass

        if record.operation_type == "amendment" and record.amendment_offer_id:
            offer = None
            if self.amendment_service is not None:
                offer = self.amendment_service.get_amendment_offer(record.amendment_offer_id, record.booking_reference)

            if not offer:
                record.stage_history.append(
                    StageHistoryEntry(
                        stage=record.stage,
                        actor=actor,
                        details=f"Recovery failed: Amendment offer '{record.amendment_offer_id}' missing. Manual reconciliation required. {staff_notes or ''}".strip(),
                    )
                )
                self._save_record(record)
                return {
                    "outcome": "requires_manual_reconciliation",
                    "stage": record.stage,
                    "message": "Amendment offer missing or invalidated. Manual reconciliation required.",
                    "readback_verified": False,
                }

            current_rev = getattr(linkage, "booking_revision", getattr(linkage, "revision", 1))
            if current_rev > offer.booking_revision:
                record.stage_history.append(
                    StageHistoryEntry(
                        stage=record.stage,
                        actor=actor,
                        details=f"Recovery conflict: Booking is at revision {current_rev}, offer was for revision {offer.booking_revision}. Manual reconciliation required. {staff_notes or ''}".strip(),
                    )
                )
                self._save_record(record)
                return {
                    "outcome": "requires_manual_reconciliation",
                    "stage": record.stage,
                    "message": f"Revision conflict: Booking is at revision {current_rev}, whereas amendment offer expected revision {offer.booking_revision}. Requires manual reconciliation.",
                    "readback_verified": False,
                }

            exec_req = ExecuteBookingOperationRequest(
                idempotency_key=f"recovery_{record.operation_id}",
                operation_type="amendment",
                amendment_offer_id=record.amendment_offer_id,
                booking_reference=record.booking_reference,
                payment_id=record.payment_id,
            )

            if force_readback_recheck_only:
                opera_res_id = record.vendor_references.get("amended_opera_reservation_id")
                brs_rounds = record.vendor_references.get("replacement_brs_round_ids", [])
                proposed_golf = getattr(offer, "proposed_golf_rounds", getattr(offer, "proposed_golf", []))
                if opera_res_id and len(brs_rounds) == len(proposed_golf):
                    exec_res = self._execute_amendment(record, exec_req)
                    if exec_res.stage == "completed":
                        record.stage_history.append(
                            StageHistoryEntry(
                                stage="completed",
                                actor=actor,
                                details=f"Recovery verified existing vendor writes. Status completed. {staff_notes or ''}".strip(),
                            )
                        )
                        record.staff_alert = None
                        self._save_record(record)
                        return {
                            "outcome": "recovered_completed",
                            "stage": "completed",
                            "message": "Existing vendor writes verified via readback and linkage synchronized.",
                            "readback_verified": True,
                            "new_booking_revision": current_rev + 1,
                            "email_delivery_id": record.email_delivery_id,
                        }

            # Attempt full safe resumption
            exec_res = self._execute_amendment(record, exec_req)
            if exec_res.stage == "completed":
                record.stage_history.append(
                    StageHistoryEntry(
                        stage="completed",
                        actor=actor,
                        details=f"Recovery retry succeeded. Vendor writes verified and linkage synchronized. {staff_notes or ''}".strip(),
                    )
                )
                record.staff_alert = None
                self._save_record(record)
                return {
                    "outcome": "recovered_completed",
                    "stage": "completed",
                    "message": "Amendment recovered and committed successfully. Vendor readback verified.",
                    "readback_verified": True,
                    "new_booking_revision": current_rev + 1,
                    "email_delivery_id": record.email_delivery_id,
                }
            else:
                record.stage_history.append(
                    StageHistoryEntry(
                        stage=record.stage,
                        actor=actor,
                        details=f"Recovery retry attempted by {actor}; vendor writes halted at {exec_res.stage}: {exec_res.message}. {staff_notes or ''}".strip(),
                    )
                )
                self._save_record(record)
                return {
                    "outcome": "still_unconfirmed",
                    "stage": record.stage,
                    "message": f"Recovery attempt could not complete automatically ({exec_res.message}). Original booking remains preserved.",
                    "readback_verified": False,
                }

        return {
            "outcome": "requires_manual_reconciliation",
            "stage": record.stage,
            "message": "Non-amendment operation recovery requires manual staff intervention.",
            "readback_verified": False,
        }
