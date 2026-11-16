"""Coordinator service for Ticket N07 — Guest and Hotel Booking Confirmations.

Coordinates immutable snapshot validation, ReportLab PDF itinerary generation,
independent per-recipient delivery tracking, safe conditional claims, and staff retry/status inspection.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import logging
from pathlib import Path
import re
import threading
from decimal import Decimal
from typing import Any, Dict, List, Literal, Optional
import uuid
from dataclasses import dataclass

from pymongo.database import Database

from rosapenna_backend.api.schemas.communications import (
    BookingConfirmationRecord,
    BookingConfirmationSnapshot,
    ConfirmationAttemptAudit,
    ConfirmationDeliveryState,
    ConfirmedEvent,
    RecipientDeliveryState,
    RecipientDeliveryStatusSummary,
    StaffConfirmationStatusResponse,
)
from rosapenna_backend.config import settings
from rosapenna_backend.services.email_delivery import (
    ConfirmationTransportProtocol,
    DisabledConfirmationTransport,
    RecordingConfirmationTransport,
    TransportSubmitOutcome,
    render_n07_email_message,
)
from rosapenna_backend.services.pdf_itinerary import build_n07_confirmation_pdf

logger = logging.getLogger("booking_api")


@dataclass(frozen=True)
class ConfirmationRuntimeContext:
    """Explicit test-run scope for the offline confirmation adapter."""

    mode: str
    environment: str
    run_id: Optional[str]
    recording_dir: Optional[str]

    @classmethod
    def from_settings(cls) -> "ConfirmationRuntimeContext":
        return cls(
            mode=settings.confirmations_mode.strip().lower(),
            environment=settings.environment.strip().lower(),
            run_id=settings.confirmations_test_run_id,
            recording_dir=settings.confirmations_recording_dir,
        )


def mask_email_address(addr: str) -> str:
    """Mask email address for safe staff presentation without data leakage."""
    if not addr or "@" not in addr:
        return "masked@invalid"
    parts = addr.split("@", 1)
    name, domain = parts[0], parts[1]
    if len(name) <= 2:
        masked_name = name[0] + "***"
    else:
        masked_name = name[:2] + "***" + name[-1]
    return f"{masked_name}@{domain}"


class ConfirmationConflictError(Exception):
    """Raised when concurrency, version mismatch, or conflicting delivery state is detected."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class ConfirmationNotFoundError(Exception):
    """Raised when an operation or confirmation record does not exist."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class BookingConfirmationCoordinator:
    """Coordinates N07 confirmation preparation, rendering, and per-recipient dispatches."""

    def __init__(
        self,
        database: Optional[Database] = None,
        transport: Optional[ConfirmationTransportProtocol] = None,
        runtime_context: Optional[ConfirmationRuntimeContext] = None,
        allow_in_memory_fixtures: bool = False,
    ):
        self.database = database
        self.collection = database["booking_operations"] if database is not None else None
        self.transport = transport or DisabledConfirmationTransport()
        self.runtime_context = runtime_context or ConfirmationRuntimeContext.from_settings()
        self.allow_in_memory_fixtures = allow_in_memory_fixtures
        self._in_memory_records: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.RLock()

    def _validate_source(self, operation_id: str, op_doc: Dict[str, Any], event: ConfirmedEvent) -> None:
        """Reject malformed or incomplete saved evidence without leaking database errors."""
        try:
            self._validate_source_evidence(operation_id, op_doc, event)
        except ConfirmationConflictError:
            raise
        except (AttributeError, ArithmeticError, KeyError, TypeError, ValueError) as exc:
            raise ConfirmationConflictError("Saved booking evidence is invalid") from exc

    def _validate_source_evidence(
        self, operation_id: str, op_doc: Dict[str, Any], event: ConfirmedEvent
    ) -> None:
        """Bind the event to completed test operation, quote, payment, readback, and linkage records."""
        context = self.runtime_context
        if (
            context.mode != "recording"
            or context.environment != "test"
            or not context.run_id
            or not context.recording_dir
            or not Path(context.recording_dir).is_absolute()
        ):
            raise ConfirmationConflictError("An active offline test run is required for N07 recording")
        if isinstance(self.transport, RecordingConfirmationTransport):
            if Path(context.recording_dir).resolve(strict=False) != self.transport.recording_dir:
                raise ConfirmationConflictError("Recording transport does not match the active test run root")
        if self.collection is None and not self.allow_in_memory_fixtures:
            raise ConfirmationConflictError("Durable booking storage is required")
        snapshot = event.snapshot
        completed_at = op_doc.get("completed_at")
        if isinstance(completed_at, dt.datetime):
            completed_at = completed_at.isoformat()
        if (
            op_doc.get("operation_id") != operation_id
            or op_doc.get("stage") != "completed"
            or op_doc.get("operation_type") != "new_booking"
            or op_doc.get("payment_verified") is not True
            or not op_doc.get("payment_id")
            or event.operation_id != operation_id
            or not completed_at
            or event.occurred_at != completed_at
            or op_doc.get("booking_reference") != snapshot.booking_reference
            or event.booking_reference != snapshot.booking_reference
            or event.booking_revision != snapshot.booking_revision
            or event.environment != context.environment
            or snapshot.environment != context.environment
            or event.is_test is not True
            or snapshot.is_test is not True
            or event.simulation_run_id != context.run_id
            or snapshot.simulation_run_id != context.run_id
            or event.snapshot_hash != snapshot.compute_hash()
            or not snapshot.content.policy_text
        ):
            raise ConfirmationConflictError("Booking confirmation evidence is incomplete or outside the active test run")
        if not RecordingConfirmationTransport._valid_test_mailbox(snapshot.lead_guest.email):
            raise ConfirmationConflictError("N07 recording requires a synthetic .invalid guest address")

        quote_id = op_doc.get("quote_id")
        if not quote_id:
            raise ConfirmationConflictError("Completed operation has no accepted quote reference")
        if self.collection is not None:
            quote = self.database["package_quotes"].find_one({"quote_id": quote_id})
            payment_docs = [
                self.database["package_payments"].find_one(
                    {"payment_id": item.payment_id, "booking_reference": snapshot.booking_reference}
                )
                for item in snapshot.payments
            ]
            linkage = self.database["booking_linkages"].find_one(
                {"booking_reference": snapshot.booking_reference}
            )
        elif self.allow_in_memory_fixtures:
            source = op_doc.get("_n07_test_source") or {}
            quote = source.get("quote")
            payment_docs = source.get("payments") or []
            linkage = source.get("linkage")
        else:
            raise ConfirmationConflictError("Durable booking evidence is required")

        if not quote or not linkage or len(payment_docs) != len(snapshot.payments):
            raise ConfirmationConflictError("Accepted quote, payment, or linkage evidence is missing")
        quote = dict(quote)
        quote.pop("_id", None)
        linkage = dict(linkage)
        linkage.pop("_id", None)
        from rosapenna_backend.api.schemas.packages import PackageQuoteResponse
        from rosapenna_backend.services.package_pricing import compute_quote_hash

        quote_model = PackageQuoteResponse.model_validate(quote)
        if (
            quote_model.status != "available"
            or quote_model.quote_id != quote_id
            or quote_model.rules_version != snapshot.rules_version
            or quote_model.terms_version != snapshot.terms_version
            or quote_model.currency != snapshot.financials.currency
            or compute_quote_hash(quote_model) != snapshot.accepted_quote_hash
            or quote_model.arrival_date.isoformat() != snapshot.stay.arrival_date
            or quote_model.departure_date.isoformat() != snapshot.stay.departure_date
            or Decimal(str(quote_model.total_package_price)) != Decimal(snapshot.financials.total_amount)
            or Decimal(str(quote_model.deposit_amount)) != Decimal(snapshot.financials.deposit_paid)
            or Decimal(quote_model.total_rooms_price) + Decimal(quote_model.total_golf_price)
            != Decimal(quote_model.total_package_price)
            or Decimal(quote_model.deposit_amount) + Decimal(quote_model.balance_amount)
            != Decimal(quote_model.total_package_price)
            or len(quote_model.itemized_rooms) != len(snapshot.stay.rooms)
            or len(quote_model.itemized_golf) != len(snapshot.golf_rounds)
        ):
            raise ConfirmationConflictError("Accepted quote facts do not match the confirmation snapshot")
        for quoted, room in zip(quote_model.itemized_rooms, snapshot.stay.rooms):
            if quoted.room_category.casefold() != room.room_category.casefold() or quoted.room_quantity != room.room_quantity:
                raise ConfirmationConflictError("Room details do not match the accepted quote")
        for quoted, round_item in zip(quote_model.itemized_golf, snapshot.golf_rounds):
            if (
                quoted.course_name.casefold() != round_item.course_name.casefold()
                or quoted.date.isoformat() != round_item.date
                or quoted.time != round_item.tee_time
                or quoted.players != round_item.players
            ):
                raise ConfirmationConflictError("Golf details do not match the accepted quote")

        for payment, payment_doc in zip(snapshot.payments, payment_docs):
            if payment_doc is None:
                raise ConfirmationConflictError("Verified payment record is missing")
            payment_doc = dict(payment_doc)
            payment_doc.pop("_id", None)
            if (
                payment_doc.get("status") != "verified_paid"
                or payment_doc.get("payment_id") != payment.payment_id
                or payment_doc.get("booking_reference") != snapshot.booking_reference
                or Decimal(str(payment_doc.get("amount", "-1"))) != Decimal(payment.amount)
                or payment_doc.get("currency") != payment.currency
                or payment_doc.get("quote_id") != quote_id
                or payment_doc.get("quote_hash") != snapshot.accepted_quote_hash
                or payment_doc.get("rules_version") != snapshot.rules_version
                or (payment_doc.get("purpose") or payment_doc.get("payment_type")) != payment.payment_purpose
                or payment_doc.get("evidence_source") != "simulation"
                or payment_doc.get("is_test") is not True
                or payment_doc.get("environment") != context.environment
                or payment_doc.get("simulation_run_id") != context.run_id
            ):
                raise ConfirmationConflictError("Verified payment facts do not match the active test booking")
        if op_doc.get("payment_id") not in {item.payment_id for item in snapshot.payments}:
            raise ConfirmationConflictError("Operation payment is not in the confirmed payment ledger")

        readback = op_doc.get("readback_evidence") or {}
        if hasattr(readback, "model_dump"):
            readback = readback.model_dump(mode="python")
        vendor_refs = op_doc.get("vendor_references") or {}
        if (
            readback.get("opera_verified") is not True
            or not readback.get("opera_reservation_id")
            or not readback.get("opera_confirmation_number")
            or readback.get("brs_verified") is not True
            or len(readback.get("brs_round_ids") or []) != len(snapshot.golf_rounds)
            or len(readback.get("brs_booking_ids") or []) != len(snapshot.golf_rounds)
            or vendor_refs.get("opera_reservation_id") != readback.get("opera_reservation_id")
            or vendor_refs.get("opera_confirmation_number") != readback.get("opera_confirmation_number")
            or vendor_refs.get("brs_round_ids") != readback.get("brs_round_ids")
            or vendor_refs.get("brs_booking_ids") != readback.get("brs_booking_ids")
        ):
            raise ConfirmationConflictError("Opera or BRS readback evidence is incomplete")
        if (
            linkage.get("booking_reference") != snapshot.booking_reference
            or linkage.get("status") != "linked"
            or int(linkage.get("revision", 1)) != snapshot.booking_revision
        ):
            raise ConfirmationConflictError("Booking linkage does not match the confirmed revision")
        opera = linkage.get("opera_stay") or {}
        if (
            opera.get("reservation_id") != readback.get("opera_reservation_id")
            or opera.get("confirmation_number") != readback.get("opera_confirmation_number")
            or str(opera.get("status", "")).casefold() != "reserved"
            or str(opera.get("arrival_date"))[:10] != snapshot.stay.arrival_date
            or str(opera.get("departure_date"))[:10] != snapshot.stay.departure_date
            or opera.get("room_category", "").casefold() != snapshot.stay.rooms[0].room_category.casefold()
            or int(opera.get("room_quantity", 0)) != snapshot.stay.rooms[0].room_quantity
            or Decimal(str(opera.get("total_amount", "-1"))) != Decimal(quote_model.total_rooms_price)
            or opera.get("currency") != quote_model.currency
        ):
            raise ConfirmationConflictError("Opera linkage does not match the confirmed stay")
        lead_guest = linkage.get("lead_guest") or {}
        if (
            lead_guest.get("first_name") != snapshot.lead_guest.first_name
            or lead_guest.get("last_name") != snapshot.lead_guest.last_name
            or str(lead_guest.get("email", "")).strip().casefold() != snapshot.lead_guest.email.casefold()
            or (
                snapshot.lead_guest.telephone is not None
                and str(lead_guest.get("telephone", "")).strip() != snapshot.lead_guest.telephone
            )
        ):
            raise ConfirmationConflictError("Booking linkage does not match the confirmed guest")
        linked_round_ids = {item.get("round_id") for item in linkage.get("brs_rounds") or []}
        if linked_round_ids != set(readback.get("brs_round_ids") or []):
            raise ConfirmationConflictError("BRS linkage does not match the confirmed golf rounds")
        linked_rounds = {item.get("round_id"): item for item in linkage.get("brs_rounds") or []}
        for index, round_item in enumerate(snapshot.golf_rounds):
            round_id = readback["brs_round_ids"][index]
            booking_id = readback["brs_booking_ids"][index]
            linked_round = linked_rounds.get(round_id) or {}
            if (
                booking_id not in round_item.provider_booking_ids
                or linked_round.get("brs_booking_id") != booking_id
                or str(linked_round.get("status", "")).casefold() != "reserved"
                or str(linked_round.get("date"))[:10] != round_item.date
                or linked_round.get("time") != round_item.tee_time
                or linked_round.get("course_name", "").casefold() != round_item.course_name.casefold()
                or int(linked_round.get("players", 0)) != round_item.players
                or Decimal(str(linked_round.get("price", "-1")))
                != Decimal(quote_model.itemized_golf[index].rate_per_player)
                or linked_round.get("currency") != quote_model.currency
            ):
                raise ConfirmationConflictError("BRS round facts do not match confirmed golf details")
        for payment in snapshot.payments:
            linked_payment = next(
                (item for item in (linkage.get("payments") or []) if item.get("payment_id") == payment.payment_id),
                None,
            )
            if not linked_payment or (
                linked_payment.get("status") != "completed"
                or Decimal(str(linked_payment.get("amount", "-1"))) != Decimal(payment.amount)
                or linked_payment.get("currency") != payment.currency
                or linked_payment.get("payment_type") != payment.payment_purpose
            ):
                raise ConfirmationConflictError("Booking linkage payment facts do not match verified payment")

        expected_refs = {
            "quote_id": quote_id,
            "payment_ids": sorted(item.payment_id for item in snapshot.payments),
            "opera_reservation_id": readback["opera_reservation_id"],
            "brs_round_ids": sorted(readback["brs_round_ids"]),
            "brs_booking_ids": sorted(readback["brs_booking_ids"]),
            "booking_linkage_revision": snapshot.booking_revision,
        }
        if any(event.evidence_refs.get(key) != value for key, value in expected_refs.items()):
            raise ConfirmationConflictError("Confirmed event references do not match saved booking evidence")

    def authorize_operation_scope(self, operation_id: str, scopes: frozenset[str], action: str) -> None:
        """Require exact booking and active test-run scope before staff access."""
        op_doc = self._get_operation_doc(operation_id)
        try:
            event = ConfirmedEvent.model_validate(op_doc.get("confirmed_event") or {})
        except Exception as exc:
            raise ConfirmationNotFoundError("Confirmation record not found") from exc
        required = {
            f"booking:{event.booking_reference}:{action}",
            f"run:{event.simulation_run_id}:confirmations",
        }
        if not required.issubset(scopes):
            raise ConfirmationNotFoundError("Confirmation record not found")

    @staticmethod
    def _overall_status(conf: BookingConfirmationRecord) -> ConfirmationDeliveryState:
        states = [conf.deliveries[role].status for role in ("guest", "hotel")]
        if states == ["recorded_test", "recorded_test"]:
            return "recorded_test"
        if all(state in {"recorded_test", "accepted_by_transport"} for state in states):
            return "accepted_by_transport"
        for state in ("unknown", "permanent_failure", "retryable_failure", "sending"):
            if state in states:
                return state  # type: ignore[return-value]
        if "blocked_dependency" in states:
            return "blocked_dependency"
        return "prepared"

    def _get_operation_doc(self, operation_id: str) -> Dict[str, Any]:
        """Fetch raw operation document from MongoDB or memory."""
        if self.collection is not None:
            doc = self.collection.find_one({"operation_id": operation_id})
            if doc:
                return doc
            raise ConfirmationNotFoundError(f"Operation record {operation_id} was not found")
        if self.allow_in_memory_fixtures and operation_id in self._in_memory_records:
            return self._in_memory_records[operation_id]
        raise ConfirmationNotFoundError(f"Operation record {operation_id} was not found")

    def prepare_confirmation(
        self,
        operation_id: str,
        expected_event_id: Optional[str] = None,
    ) -> BookingConfirmationRecord:
        """Load confirmed booking event, validate snapshot, and render frozen artifacts."""
        op_doc = self._get_operation_doc(operation_id)

        # 1. Validate confirmed_event presence
        raw_event = op_doc.get("confirmed_event")
        if not raw_event:
            raise ConfirmationConflictError(f"Operation {operation_id} has no confirmed_event; booking is not verified confirmed")

        event = ConfirmedEvent.model_validate(raw_event)

        if expected_event_id and event.event_id != expected_event_id:
            raise ConfirmationConflictError(
                f"Event ID mismatch: expected {expected_event_id}, found {event.event_id}"
            )

        self._validate_source(operation_id, op_doc, event)

        # 3. Check for existing confirmation (Idempotency)
        existing_conf = op_doc.get("confirmation")
        if existing_conf:
            rec = BookingConfirmationRecord.model_validate(existing_conf)
            if rec.event_id == event.event_id:
                if rec.snapshot_hash == event.snapshot_hash:
                    return rec
                raise ConfirmationConflictError(
                    f"Conflict: existing confirmation has mismatched snapshot hash for event {event.event_id}"
                )

        # 4. Render frozen PDF artifact
        pdf_bytes = build_n07_confirmation_pdf(event.snapshot)
        pdf_hash = hashlib.sha256(pdf_bytes).hexdigest()
        pdf_filename = f"Rosapenna-Itinerary-{event.snapshot.booking_reference}.pdf"

        # 5. Initialize recipient deliveries
        now_str = dt.datetime.now(dt.timezone.utc).isoformat()
        ref = event.snapshot.booking_reference
        rev = event.snapshot.booking_revision

        # Use only the exact synthetic address validated against the saved snapshot.
        guest_email = event.snapshot.lead_guest.email.strip().lower()
        safe_ref_id = hashlib.sha256(ref.encode("utf-8")).hexdigest()[:16]
        hotel_email = f"reservations.{safe_ref_id}@rosapenna.invalid"

        prepared_deliveries: Dict[str, RecipientDeliveryState] = {}
        for role, recipient in (("guest", guest_email), ("hotel", hotel_email)):
            stable_id = hashlib.sha256(f"{event.event_id}:{role}".encode("utf-8")).hexdigest()[:24]
            stable_message_id = f"<n07.{stable_id}@rosapenna.invalid>"
            subject, plain, html_body, _, _ = render_n07_email_message(
                snapshot=event.snapshot,
                recipient_role=role,
                recipient_address=recipient,
                message_date=event.occurred_at,
                message_id=stable_message_id,
                pdf_bytes=pdf_bytes,
                attempt_id="prepared",
            )
            content_hash = hashlib.sha256(
                "\0".join((subject, plain, html_body, stable_message_id, recipient, pdf_hash)).encode("utf-8")
            ).hexdigest()
            prepared_deliveries[role] = RecipientDeliveryState(
                recipient_role=role,
                recipient_address=recipient,
                message_id=stable_message_id,
                content_hash=content_hash,
                status="prepared",
                attempt_count=0,
            )

        record = BookingConfirmationRecord(
            event_id=event.event_id,
            operation_id=operation_id,
            booking_reference=ref,
            snapshot_hash=event.snapshot_hash,
            overall_status="prepared",
            deliveries=prepared_deliveries,
            pdf_hash=pdf_hash,
            pdf_filename=pdf_filename,
            pdf_bytes=pdf_bytes,
            created_at=now_str,
            updated_at=now_str,
        )

        if self._initialize_confirmation(operation_id, record):
            return record
        current_doc = self._get_operation_doc(operation_id)
        current = current_doc.get("confirmation")
        if current:
            saved = BookingConfirmationRecord.model_validate(current)
            if saved.event_id == event.event_id and saved.snapshot_hash == event.snapshot_hash:
                return saved
        raise ConfirmationConflictError("A concurrent confirmation update changed the source event")

    def _initialize_confirmation(self, operation_id: str, record: BookingConfirmationRecord) -> bool:
        """Create the confirmation once and keep its initial state immutable."""
        now = dt.datetime.now(dt.timezone.utc).isoformat()
        if self.collection is not None:
            result = self.collection.update_one(
                {
                    "operation_id": operation_id,
                    "stage": "completed",
                    "operation_type": "new_booking",
                    "confirmed_event.event_id": record.event_id,
                    "confirmed_event.snapshot_hash": record.snapshot_hash,
                    "confirmation": None,
                },
                {"$set": {"confirmation": record.model_dump(mode="python"), "updated_at": now}},
            )
            return result.modified_count == 1
        with self._lock:
            op_doc = self._in_memory_records.get(operation_id)
            if not op_doc or op_doc.get("confirmation") is not None:
                return False
            op_doc["confirmation"] = record.model_dump(mode="python")
            op_doc["updated_at"] = now
            return True

    def _claim_attempt(
        self,
        operation_id: str,
        event_id: str,
        snapshot_hash: str,
        role: Literal["guest", "hotel"],
        expected_attempt_id: Optional[str],
        audit: ConfirmationAttemptAudit,
    ) -> Optional[BookingConfirmationRecord]:
        """Claim one role with a conditional update and persist audit intent first."""
        prefix = f"confirmation.deliveries.{role}"
        if self.collection is not None:
            result = self.collection.update_one(
                {
                    "operation_id": operation_id,
                    "stage": "completed",
                    "operation_type": "new_booking",
                    "confirmed_event.event_id": event_id,
                    "confirmed_event.snapshot_hash": snapshot_hash,
                    "confirmation.event_id": event_id,
                    "confirmation.snapshot_hash": snapshot_hash,
                    f"{prefix}.status": audit.previous_status,
                    f"{prefix}.attempt_count": {"$lt": 3},
                    f"{prefix}.attempt_id": expected_attempt_id,
                },
                {
                    "$set": {
                        f"{prefix}.status": "sending",
                        f"{prefix}.attempt_id": audit.attempt_id,
                        f"{prefix}.claimed_at": audit.started_at,
                        f"{prefix}.error_code": None,
                        f"{prefix}.error_details": None,
                        f"{prefix}.provider_reference": None,
                        "updated_at": audit.started_at,
                    },
                    "$inc": {f"{prefix}.attempt_count": 1},
                    "$push": {"confirmation.attempt_history": audit.model_dump(mode="python")},
                },
            )
            if result.modified_count != 1:
                return None
            return BookingConfirmationRecord.model_validate(self._get_operation_doc(operation_id)["confirmation"])

        with self._lock:
            op_doc = self._in_memory_records.get(operation_id)
            if not op_doc or op_doc.get("stage") != "completed" or op_doc.get("operation_type") != "new_booking":
                return None
            raw_conf = op_doc.get("confirmation")
            if not raw_conf:
                return None
            conf = BookingConfirmationRecord.model_validate(raw_conf)
            delivery = conf.deliveries[role]
            if (
                conf.event_id != event_id
                or conf.snapshot_hash != snapshot_hash
                or delivery.status != audit.previous_status
                or delivery.attempt_count >= 3
                or delivery.attempt_id != expected_attempt_id
            ):
                return None
            delivery.status = "sending"
            delivery.attempt_id = audit.attempt_id
            delivery.claimed_at = audit.started_at
            delivery.attempt_count += 1
            delivery.error_code = None
            delivery.error_details = None
            delivery.provider_reference = None
            conf.deliveries[role] = delivery
            conf.attempt_history.append(audit)
            conf.updated_at = audit.started_at
            op_doc["confirmation"] = conf.model_dump(mode="python")
            return conf

    def _finish_attempt(
        self,
        operation_id: str,
        event_id: str,
        snapshot_hash: str,
        role: Literal["guest", "hotel"],
        attempt_id: str,
        outcome: TransportSubmitOutcome,
    ) -> RecipientDeliveryState:
        """Save a result only when this worker still owns the active role attempt."""
        completed_at = dt.datetime.now(dt.timezone.utc).isoformat()
        prefix = f"confirmation.deliveries.{role}"
        updates = {
            f"{prefix}.status": outcome.state,
            f"{prefix}.error_code": outcome.error_code,
            f"{prefix}.error_details": outcome.error_details,
            f"{prefix}.provider_reference": outcome.provider_reference,
            "updated_at": completed_at,
        }
        if outcome.state in {"recorded_test", "accepted_by_transport"}:
            updates[f"{prefix}.accepted_at"] = completed_at
        if self.collection is not None:
            result = self.collection.update_one(
                {
                    "operation_id": operation_id,
                    "confirmed_event.event_id": event_id,
                    "confirmed_event.snapshot_hash": snapshot_hash,
                    "confirmation.event_id": event_id,
                    "confirmation.snapshot_hash": snapshot_hash,
                    f"{prefix}.attempt_id": attempt_id,
                    f"{prefix}.status": "sending",
                },
                {"$set": updates},
            )
            if result.modified_count != 1:
                raise ConfirmationConflictError("Delivery result could not be saved under its attempt claim")
            conf = BookingConfirmationRecord.model_validate(self._get_operation_doc(operation_id)["confirmation"])
            return conf.deliveries[role]
        with self._lock:
            op_doc = self._in_memory_records.get(operation_id)
            if not op_doc:
                raise ConfirmationNotFoundError("Operation record was not found")
            conf = BookingConfirmationRecord.model_validate(op_doc["confirmation"])
            delivery = conf.deliveries[role]
            if delivery.attempt_id != attempt_id or delivery.status != "sending":
                raise ConfirmationConflictError("Delivery result no longer owns the active attempt")
            delivery.status = outcome.state  # type: ignore[assignment]
            delivery.error_code = outcome.error_code
            delivery.error_details = outcome.error_details
            delivery.provider_reference = outcome.provider_reference
            if outcome.state in {"recorded_test", "accepted_by_transport"}:
                delivery.accepted_at = completed_at
            conf.deliveries[role] = delivery
            conf.updated_at = completed_at
            op_doc["confirmation"] = conf.model_dump(mode="python")
            return delivery

    def _mark_expired_attempt_unknown(
        self, operation_id: str, role: Literal["guest", "hotel"], attempt_id: str
    ) -> None:
        """Mark an old claim unknown after a process restart; never resend it automatically."""
        completed_at = dt.datetime.now(dt.timezone.utc).isoformat()
        prefix = f"confirmation.deliveries.{role}"
        updates = {
            f"{prefix}.status": "unknown",
            f"{prefix}.error_code": "CLAIM_EXPIRED_UNKNOWN",
            f"{prefix}.error_details": None,
            "updated_at": completed_at,
        }
        if self.collection is not None:
            self.collection.update_one(
                {
                    "operation_id": operation_id,
                    f"{prefix}.attempt_id": attempt_id,
                    f"{prefix}.status": "sending",
                },
                {"$set": updates},
            )
            return
        with self._lock:
            op_doc = self._in_memory_records.get(operation_id)
            if not op_doc:
                return
            conf = BookingConfirmationRecord.model_validate(op_doc["confirmation"])
            delivery = conf.deliveries[role]
            if delivery.attempt_id != attempt_id or delivery.status != "sending":
                return
            delivery.status = "unknown"
            delivery.error_code = "CLAIM_EXPIRED_UNKNOWN"
            delivery.error_details = None
            conf.deliveries[role] = delivery
            op_doc["confirmation"] = conf.model_dump(mode="python")

    def dispatch_recipient(
        self,
        operation_id: str,
        recipient_role: Literal["guest", "hotel"],
        expected_event_id: Optional[str] = None,
        expected_attempt_id: Optional[str] = None,
        reason: Optional[str] = None,
        actor: str = "coordinator",
    ) -> RecipientDeliveryState:
        """Claim and dispatch one prepared recipient with safe retry rules."""
        op_doc = self._get_operation_doc(operation_id)
        raw_conf = op_doc.get("confirmation")
        raw_event = op_doc.get("confirmed_event")
        if not raw_conf or not raw_event:
            raise ConfirmationConflictError("Prepared confirmation evidence is missing")
        conf = BookingConfirmationRecord.model_validate(raw_conf)
        event = ConfirmedEvent.model_validate(raw_event)
        self._validate_source(operation_id, op_doc, event)
        if (
            expected_event_id is not None and conf.event_id != expected_event_id
            or conf.event_id != event.event_id
            or conf.snapshot_hash != event.snapshot_hash
            or conf.booking_reference != event.snapshot.booking_reference
            or not conf.pdf_bytes
            or hashlib.sha256(conf.pdf_bytes).hexdigest() != conf.pdf_hash
        ):
            raise ConfirmationConflictError("Prepared confirmation no longer matches the confirmed event")

        delivery = conf.deliveries.get(recipient_role)
        if delivery is None:
            raise ConfirmationConflictError(f"No delivery record for role {recipient_role}")
        if delivery.status in {"recorded_test", "accepted_by_transport"}:
            return delivery
        if delivery.status == "sending":
            try:
                claimed = dt.datetime.fromisoformat((delivery.claimed_at or "").replace("Z", "+00:00"))
            except ValueError:
                claimed = dt.datetime.now(dt.timezone.utc)
            if claimed.tzinfo is None:
                claimed = claimed.replace(tzinfo=dt.timezone.utc)
            if (dt.datetime.now(dt.timezone.utc) - claimed).total_seconds() > 600 and delivery.attempt_id:
                self._mark_expired_attempt_unknown(operation_id, recipient_role, delivery.attempt_id)
                raise ConfirmationConflictError("Previous attempt expired with unknown outcome; do not resend it")
            raise ConfirmationConflictError(f"Recipient {recipient_role} is currently in progress")
        if delivery.status == "unknown":
            raise ConfirmationConflictError("Unknown delivery outcome needs separate manual reconciliation")
        if delivery.status not in {"prepared", "retryable_failure"}:
            raise ConfirmationConflictError(f"Delivery state {delivery.status} cannot be retried")
        if delivery.attempt_count >= 3:
            raise ConfirmationConflictError("Recipient reached the maximum of three attempts")
        if delivery.attempt_count == 0 and delivery.status != "prepared":
            raise ConfirmationConflictError("Delivery attempt history is inconsistent")
        if delivery.attempt_count > 0:
            if not reason or len(reason.strip()) < 5:
                raise ConfirmationConflictError("A retry reason is required")
            if not expected_attempt_id or expected_attempt_id != delivery.attempt_id:
                raise ConfirmationConflictError("Retry must name the current attempt ID")
        elif expected_attempt_id is not None:
            raise ConfirmationConflictError("No previous attempt exists for this recipient")

        message_id = delivery.message_id
        subject, plain, html_body, mime_bytes, rendered_message_id = render_n07_email_message(
            snapshot=event.snapshot,
            recipient_role=recipient_role,
            recipient_address=delivery.recipient_address,
            message_date=event.occurred_at,
            message_id=message_id,
            pdf_bytes=conf.pdf_bytes,
            attempt_id="stable",
        )
        current_content_hash = hashlib.sha256(
            "\0".join((subject, plain, html_body, rendered_message_id, delivery.recipient_address, conf.pdf_hash or ""))
            .encode("utf-8")
        ).hexdigest()
        if rendered_message_id != delivery.message_id or current_content_hash != delivery.content_hash:
            raise ConfirmationConflictError("Rendered message differs from the prepared confirmation")

        attempt_id = f"ATT-{uuid.uuid4().hex.upper()}"
        started_at = dt.datetime.now(dt.timezone.utc).isoformat()
        audit = ConfirmationAttemptAudit(
            attempt_id=attempt_id,
            recipient_role=recipient_role,
            actor=(actor or "coordinator")[:100],
            reason=reason.strip()[:500] if reason else None,
            previous_status=delivery.status,
            started_at=started_at,
        )
        claimed = self._claim_attempt(
            operation_id, event.event_id, event.snapshot_hash, recipient_role,
            expected_attempt_id, audit,
        )
        if claimed is None:
            raise ConfirmationConflictError("Another worker changed this recipient; reload status before retry")

        try:
            outcome = self.transport.submit(
                message_bytes=mime_bytes,
                envelope_recipient=delivery.recipient_address,
                message_id=message_id,
                attempt_id=attempt_id,
            )
            if outcome.state not in {"recorded_test", "blocked_dependency", "retryable_failure", "permanent_failure", "unknown"}:
                outcome = TransportSubmitOutcome(
                    state="unknown", evidence_source="invalid_transport_result",
                    error_code="INVALID_TRANSPORT_RESULT",
                )
        except Exception:
            logger.error("N07 transport raised during submission; outcome is unknown")
            outcome = TransportSubmitOutcome(
                state="unknown", evidence_source="transport_exception", error_code="TRANSPORT_OUTCOME_UNKNOWN"
            )
        outcome.error_details = None
        return self._finish_attempt(
            operation_id, event.event_id, event.snapshot_hash, recipient_role, attempt_id, outcome
        )

    def get_status(self, operation_id: str) -> StaffConfirmationStatusResponse:
        """Provide safe, masked status summary of confirmation deliveries for staff view."""
        op_doc = self._get_operation_doc(operation_id)
        raw_conf = op_doc.get("confirmation")
        if not raw_conf:
            raise ConfirmationNotFoundError(f"No confirmation record exists for operation {operation_id}")

        conf = BookingConfirmationRecord.model_validate(raw_conf)
        raw_event = op_doc.get("confirmed_event") or {}
        event = ConfirmedEvent.model_validate(raw_event)
        self._validate_source(operation_id, op_doc, event)
        now = dt.datetime.now(dt.timezone.utc)
        for role in ("guest", "hotel"):
            delivery = conf.deliveries.get(role)
            if delivery and delivery.status == "sending" and delivery.attempt_id and delivery.claimed_at:
                try:
                    claimed_at = dt.datetime.fromisoformat(delivery.claimed_at.replace("Z", "+00:00"))
                except ValueError:
                    claimed_at = now
                if claimed_at.tzinfo is None:
                    claimed_at = claimed_at.replace(tzinfo=dt.timezone.utc)
                if (now - claimed_at).total_seconds() > 600:
                    self._mark_expired_attempt_unknown(operation_id, role, delivery.attempt_id)
        if any(delivery.status == "sending" for delivery in conf.deliveries.values()):
            op_doc = self._get_operation_doc(operation_id)
            conf = BookingConfirmationRecord.model_validate(op_doc["confirmation"])

        guest_del = conf.deliveries.get("guest")
        hotel_del = conf.deliveries.get("hotel")

        if not guest_del or not hotel_del:
            raise ConfirmationConflictError("Incomplete deliveries record in confirmation")

        guest_summary = RecipientDeliveryStatusSummary(
            recipient_role="guest",
            recipient_address_masked=mask_email_address(guest_del.recipient_address),
            status=guest_del.status,
            attempt_count=guest_del.attempt_count,
            attempt_id=guest_del.attempt_id,
            claimed_at=guest_del.claimed_at,
            accepted_at=guest_del.accepted_at,
            error_code=guest_del.error_code,
        )

        hotel_summary = RecipientDeliveryStatusSummary(
            recipient_role="hotel",
            recipient_address_masked=mask_email_address(hotel_del.recipient_address),
            status=hotel_del.status,
            attempt_count=hotel_del.attempt_count,
            attempt_id=hotel_del.attempt_id,
            claimed_at=hotel_del.claimed_at,
            accepted_at=hotel_del.accepted_at,
            error_code=hotel_del.error_code,
        )

        return StaffConfirmationStatusResponse(
            booking_reference=conf.booking_reference,
            operation_id=operation_id,
            is_test=bool(raw_event.get("is_test", False)),
            event_id=conf.event_id,
            overall_status=self._overall_status(conf),
            guest_delivery=guest_summary,
            hotel_delivery=hotel_summary,
            updated_at=conf.updated_at,
        )
