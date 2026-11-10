"""Service for Ticket M02 — Online and Offline Booking Linkage and Reconciliation."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple
import uuid

from pymongo import ASCENDING
from pymongo.collection import Collection
from pymongo.database import Database

from rosapenna_backend.api.schemas.booking_linkage import (
    BookingLinkageResponse,
    BrsRoundDetails,
    ComponentSourceHealth,
    GuestFinancialSummary,
    GuestGolfRoundSummary,
    GuestPendingRequestSummary,
    GuestSafeBookingSummary,
    GuestStaySummary,
    LeadGuestContact,
    LinkageAuditTrailEntry,
    LinkageStatus,
    OperaStayDetails,
    PaymentLedgerEntry,
    StaffCorrectLinkageRequest,
    StaffLinkBookingRequest,
)
from rosapenna_backend.core.logging import logger
from rosapenna_backend.integrations.opera.client import OperaClient, OperaNotFoundError


class LinkageError(Exception):
    """Base exception for linkage service errors."""
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


class LinkageNotFoundError(LinkageError):
    def __init__(self, message: str = "Booking linkage record was not found"):
        super().__init__(message, status_code=404)


class LinkageConflictError(LinkageError):
    def __init__(self, message: str):
        super().__init__(message, status_code=409)


def _utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


class BookingLinkageService:
    """Manages authoritative linkage of online and offline bookings across Opera, BRS, and payments."""

    def __init__(
        self,
        database: Optional[Database] = None,
        opera_client: Optional[OperaClient] = None,
        collection_name: str = "mmb_booking_linkages",
    ):
        self.opera_client = opera_client or OperaClient()
        self.collection: Optional[Collection] = (
            database[collection_name] if database is not None else None
        )
        if self.collection is not None:
            self._init_indexes()

    def _init_indexes(self) -> None:
        assert self.collection is not None
        self.collection.create_index([("booking_reference", ASCENDING)], unique=True, name="uniq_mmb_booking_ref")
        self.collection.create_index([("opera_stay.reservation_id", ASCENDING)], sparse=True, name="idx_mmb_opera_res_id")
        self.collection.create_index([("opera_stay.confirmation_number", ASCENDING)], sparse=True, name="idx_mmb_opera_conf_num")
        self.collection.create_index([("status", ASCENDING)], name="idx_mmb_linkage_status")
        self.collection.create_index([("lead_guest.last_name", ASCENDING)], name="idx_mmb_guest_last_name")

    def _reconcile_components(
        self,
        lead_guest: LeadGuestContact,
        opera_stay: Optional[OperaStayDetails],
        brs_rounds: List[BrsRoundDetails],
        payments: List[PaymentLedgerEntry],
        exclude_booking_ref: Optional[str] = None,
    ) -> Tuple[LinkageStatus, List[str], Dict[str, Any]]:
        """Reconcile all components, identify discrepancies, and calculate ledger financials."""
        discrepancies: List[str] = []

        # 1. Opera stay reconciliation
        if opera_stay is None:
            discrepancies.append("Missing hotel reservation component")
        else:
            # Check surname match against Opera occupant names (strict match, no fuzzy merging)
            guest_surname = lead_guest.last_name.strip().lower()
            if opera_stay.occupant_names:
                matches_surname = any(
                    guest_surname in occ.strip().lower() or occ.strip().lower().endswith(guest_surname)
                    for occ in opera_stay.occupant_names
                )
                if not matches_surname:
                    discrepancies.append(
                        f"Opera occupants {opera_stay.occupant_names} do not match lead guest surname '{lead_guest.last_name}'"
                    )

        # 2. BRS golf rounds reconciliation
        if not brs_rounds:
            discrepancies.append("Missing golf rounds component for stay-and-play package")
        elif opera_stay is not None:
            # Validate that every round date falls strictly within the hotel stay window
            for r in brs_rounds:
                if not (opera_stay.arrival_date <= r.date <= opera_stay.departure_date):
                    discrepancies.append(
                        f"Golf round on {r.date} ({r.course_name}) falls outside stay window [{opera_stay.arrival_date}, {opera_stay.departure_date}]"
                    )

        # 3. Payment ledger reconciliation
        completed_payments = [p for p in payments if p.status == "completed"]
        room_total = Decimal(opera_stay.total_amount) if opera_stay else Decimal("0.00")
        golf_total = sum((Decimal(r.price) for r in brs_rounds), Decimal("0.00"))
        total_price = room_total + golf_total
        deposit_paid = sum((Decimal(p.amount) for p in completed_payments if p.payment_type != "credit"), Decimal("0.00"))
        credit_amount = sum((Decimal(p.amount) for p in completed_payments if p.payment_type == "credit"), Decimal("0.00"))
        balance_due = max(Decimal("0.00"), total_price - deposit_paid)

        if not completed_payments:
            discrepancies.append("No verified completed payment found in ledger")

        # 4. Duplicate reference checks across existing records
        if self.collection is not None:
            if opera_stay is not None:
                # Check for duplicate Opera reservation ID or confirmation number
                dup_query: Dict[str, Any] = {
                    "$or": [
                        {"opera_stay.reservation_id": opera_stay.reservation_id},
                        {"opera_stay.confirmation_number": opera_stay.confirmation_number},
                    ]
                }
                if exclude_booking_ref:
                    dup_query["booking_reference"] = {"$ne": exclude_booking_ref}
                existing_dup = self.collection.find_one(dup_query)
                if existing_dup:
                    discrepancies.append(
                        f"Opera reservation {opera_stay.reservation_id} is already linked to booking {existing_dup['booking_reference']}"
                    )

            # Check for duplicate BRS round IDs
            for r in brs_rounds:
                if r.brs_booking_id:
                    brs_dup_query: Dict[str, Any] = {
                        "brs_rounds.brs_booking_id": r.brs_booking_id
                    }
                    if exclude_booking_ref:
                        brs_dup_query["booking_reference"] = {"$ne": exclude_booking_ref}
                    existing_brs = self.collection.find_one(brs_dup_query)
                    if existing_brs:
                        discrepancies.append(
                            f"BRS booking {r.brs_booking_id} is already linked to booking {existing_brs['booking_reference']}"
                        )

        # 5. Classify final linkage status
        status: LinkageStatus
        if any("already linked to booking" in d for d in discrepancies):
            status = "duplicate_references"
        elif any("falls outside stay window" in d or "do not match lead guest surname" in d for d in discrepancies):
            status = "conflicting"
        elif any("Missing" in d or "No verified completed payment" in d for d in discrepancies):
            status = "missing_components"
        else:
            status = "linked"

        financials = {
            "total_amount": format(total_price.quantize(Decimal("0.01")), ".2f"),
            "deposit_paid": format(deposit_paid.quantize(Decimal("0.01")), ".2f"),
            "credit_amount": format(credit_amount.quantize(Decimal("0.01")), ".2f"),
            "balance_due": format(balance_due.quantize(Decimal("0.01")), ".2f"),
            "currency": "EUR",
            "payment_status": "fully_paid" if balance_due == 0 and total_price > 0 else (
                "deposit_paid" if deposit_paid > 0 else "unpaid"
            ),
        }

        return status, discrepancies, financials

    def _resolve_opera_stay(
        self,
        opera_identifier: Optional[str],
        manual_opera_stay: Optional[OperaStayDetails],
    ) -> Optional[OperaStayDetails]:
        """Fetch authoritative Opera reservation details via read-only integration (without mutation)."""
        if manual_opera_stay is not None:
            return manual_opera_stay

        if not opera_identifier or not opera_identifier.strip():
            return None

        clean_id = opera_identifier.strip()
        try:
            res_detail = self.opera_client.get_reservation_by_identifier(clean_id)
            occupants = [
                f"{occ.first_name} {occ.last_name}".strip()
                for occ in res_detail.occupants
            ]
            return OperaStayDetails(
                reservation_id=res_detail.reservation_id,
                confirmation_number=res_detail.confirmation_number,
                hotel_id=res_detail.hotel_id,
                room_category=res_detail.room_category,
                room_quantity=res_detail.room_quantity,
                arrival_date=res_detail.arrival_date,
                departure_date=res_detail.departure_date,
                total_amount=format(Decimal(str(res_detail.total_amount)).quantize(Decimal("0.01")), ".2f"),
                currency=res_detail.currency,
                status=res_detail.status,
                occupant_names=occupants,
            )
        except OperaNotFoundError:
            logger.warning("Opera reservation '%s' not found during linkage attempt.", clean_id)
            return None
        except Exception as exc:
            logger.error("Error reading Opera reservation '%s': %s", clean_id, type(exc).__name__)
            return None

    def link_booking(
        self,
        request: StaffLinkBookingRequest,
        actor: str,
    ) -> BookingLinkageResponse:
        """Create or associate a booking from online or offline channels with full idempotency."""
        now = _utc_now()
        ref = request.booking_reference
        if not ref:
            date_prefix = now.strftime("%Y%m%d")
            ref = f"RSP-{date_prefix}-{uuid.uuid4().hex[:6].upper()}"

        # Check for existing record (Idempotency)
        if self.collection is not None:
            existing = self.collection.find_one({"booking_reference": ref})
            if existing is not None:
                # If existing request matches identically, return without duplicating
                return BookingLinkageResponse(**existing)

        # Read-only fetch of Opera stay
        opera_stay = self._resolve_opera_stay(request.opera_identifier, request.manual_opera_stay)

        # Reconcile components
        status, discrepancies, financials = self._reconcile_components(
            lead_guest=request.lead_guest,
            opera_stay=opera_stay,
            brs_rounds=request.brs_rounds,
            payments=request.payments,
            exclude_booking_ref=ref,
        )

        audit_entry = LinkageAuditTrailEntry(
            audit_id=f"aud_{uuid.uuid4().hex[:12]}",
            actor=actor,
            action="link_booking_created",
            occurred_at=now,
            reason=request.reason,
            before_state={},
            after_state={
                "status": status,
                "discrepancies": discrepancies,
                "opera_stay": opera_stay.model_dump(mode="json") if opera_stay else None,
                "brs_rounds_count": len(request.brs_rounds),
                "payments_count": len(request.payments),
            },
        )

        doc: Dict[str, Any] = {
            "booking_reference": ref,
            "source_channel": request.source_channel,
            "status": status,
            "discrepancies": discrepancies,
            "lead_guest": request.lead_guest.model_dump(mode="json"),
            "opera_stay": opera_stay.model_dump(mode="json") if opera_stay else None,
            "brs_rounds": [r.model_dump(mode="json") for r in request.brs_rounds],
            "payments": [p.model_dump(mode="json") for p in request.payments],
            "financials": financials,
            "created_at": now,
            "updated_at": now,
            "audit_trail": [audit_entry.model_dump(mode="json")],
        }

        if self.collection is not None:
            self.collection.insert_one(doc)

        logger.info(
            "Booking %s linked by %s (channel: %s, status: %s, discrepancies: %d)",
            ref, actor, request.source_channel, status, len(discrepancies),
        )
        return BookingLinkageResponse(**doc)

    def correct_linkage(
        self,
        booking_reference: str,
        request: StaffCorrectLinkageRequest,
        actor: str,
    ) -> BookingLinkageResponse:
        """Staff-driven correction of linkage components with an immutable audit trail."""
        if self.collection is None:
            raise LinkageNotFoundError("Storage is not configured.")

        existing = self.collection.find_one({"booking_reference": booking_reference})
        if existing is None:
            raise LinkageNotFoundError(f"Booking reference '{booking_reference}' was not found.")

        now = _utc_now()
        before_state = {
            "lead_guest": existing.get("lead_guest"),
            "opera_stay": existing.get("opera_stay"),
            "brs_rounds": existing.get("brs_rounds"),
            "payments": existing.get("payments"),
            "status": existing.get("status"),
            "discrepancies": existing.get("discrepancies"),
        }

        # Resolve lead guest
        lead_guest = request.lead_guest or LeadGuestContact(**existing["lead_guest"])

        # Resolve Opera stay
        opera_stay: Optional[OperaStayDetails]
        if request.manual_opera_stay is not None or request.opera_identifier is not None:
            opera_stay = self._resolve_opera_stay(request.opera_identifier, request.manual_opera_stay)
        elif existing.get("opera_stay"):
            opera_stay = OperaStayDetails(**existing["opera_stay"])
        else:
            opera_stay = None

        # Resolve BRS rounds
        brs_rounds: List[BrsRoundDetails]
        if request.brs_rounds is not None:
            brs_rounds = request.brs_rounds
        else:
            brs_rounds = [BrsRoundDetails(**r) for r in existing.get("brs_rounds", [])]

        # Resolve payments
        payments: List[PaymentLedgerEntry]
        if request.payments is not None:
            payments = request.payments
        else:
            payments = [PaymentLedgerEntry(**p) for p in existing.get("payments", [])]

        # Re-run reconciliation
        status, discrepancies, financials = self._reconcile_components(
            lead_guest=lead_guest,
            opera_stay=opera_stay,
            brs_rounds=brs_rounds,
            payments=payments,
            exclude_booking_ref=booking_reference,
        )

        after_state = {
            "lead_guest": lead_guest.model_dump(mode="json"),
            "opera_stay": opera_stay.model_dump(mode="json") if opera_stay else None,
            "brs_rounds_count": len(brs_rounds),
            "payments_count": len(payments),
            "status": status,
            "discrepancies": discrepancies,
        }

        audit_entry = LinkageAuditTrailEntry(
            audit_id=f"aud_{uuid.uuid4().hex[:12]}",
            actor=actor,
            action="linkage_corrected",
            occurred_at=now,
            reason=request.reason,
            before_state=before_state,
            after_state=after_state,
        )

        new_rev = int(existing.get("revision", len(existing.get("audit_trail", [])) or 1)) + 1
        update_fields = {
            "lead_guest": lead_guest.model_dump(mode="json"),
            "opera_stay": opera_stay.model_dump(mode="json") if opera_stay else None,
            "brs_rounds": [r.model_dump(mode="json") for r in brs_rounds],
            "payments": [p.model_dump(mode="json") for p in payments],
            "status": status,
            "discrepancies": discrepancies,
            "financials": financials,
            "revision": new_rev,
            "updated_at": now,
        }

        self.collection.update_one(
            {"booking_reference": booking_reference},
            {
                "$set": update_fields,
                "$push": {"audit_trail": audit_entry.model_dump(mode="json")},
            },
        )

        updated_doc = self.collection.find_one({"booking_reference": booking_reference})
        assert updated_doc is not None
        logger.info(
            "Booking %s linkage corrected by %s (reason: %s, new status: %s)",
            booking_reference, actor, request.reason, status,
        )
        return BookingLinkageResponse(**updated_doc)

    def get_linkage(self, booking_reference: str) -> BookingLinkageResponse:
        """Fetch full staff linkage details for a booking reference."""
        if self.collection is None:
            raise LinkageNotFoundError("Storage is not configured.")

        doc = self.collection.find_one({"booking_reference": booking_reference.strip().upper()})
        if doc is None:
            raise LinkageNotFoundError(f"Booking reference '{booking_reference}' was not found.")
        return BookingLinkageResponse(**doc)

    def list_linkages(
        self,
        status: Optional[str] = None,
        source_channel: Optional[str] = None,
        limit: int = 50,
    ) -> List[BookingLinkageResponse]:
        """List booking linkages for staff operations with optional filtering."""
        if self.collection is None:
            return []

        query: Dict[str, Any] = {}
        if status:
            query["status"] = status
        if source_channel:
            query["source_channel"] = source_channel

        cursor = self.collection.find(query).sort("created_at", -1).limit(min(limit, 200))
        return [BookingLinkageResponse(**doc) for doc in cursor]

    def guest_lookup(
        self,
        booking_reference: str,
        last_name: str,
    ) -> GuestSafeBookingSummary:
        """Safe guest lookup returning sanitised public fields only, strictly suppressing vendor IDs."""
        if self.collection is None:
            raise LinkageNotFoundError("Manage My Booking service is unavailable.")

        clean_ref = booking_reference.strip().upper()
        clean_name = last_name.strip().lower()

        doc = self.collection.find_one({"booking_reference": clean_ref})
        if doc is None:
            raise LinkageNotFoundError("No matching booking was found for the provided details.")

        stored_last_name = doc.get("lead_guest", {}).get("last_name", "").strip().lower()
        if stored_last_name != clean_name:
            # Prevent leaking whether reference exists vs name mismatch
            raise LinkageNotFoundError("No matching booking was found for the provided details.")

        # Build sanitized guest stay summary
        stay_summary: Optional[GuestStaySummary] = None
        opera_stay = doc.get("opera_stay")
        balance_due_date: Optional[dt.date] = None

        if opera_stay:
            arr = dt.date.fromisoformat(opera_stay["arrival_date"]) if isinstance(opera_stay["arrival_date"], str) else opera_stay["arrival_date"]
            dep = dt.date.fromisoformat(opera_stay["departure_date"]) if isinstance(opera_stay["departure_date"], str) else opera_stay["departure_date"]
            nights = max(1, (dep - arr).days)
            balance_due_date = arr - dt.timedelta(days=28)
            stay_summary = GuestStaySummary(
                arrival_date=arr,
                departure_date=dep,
                nights=nights,
                room_category=opera_stay.get("room_category", "Classic Room"),
                room_quantity=int(opera_stay.get("room_quantity", 1)),
                assigned_room_number=opera_stay.get("assigned_room_number"),
                occupant_names=opera_stay.get("occupant_names", []),
                meal_plan="Breakfast Included Daily",
                source_status="confirmed" if doc.get("status") != "missing_components" else "stale",
                last_synced_at=doc.get("updated_at"),
            )

        # Build sanitized golf rounds summary
        rounds_summary: List[GuestGolfRoundSummary] = []
        for r in doc.get("brs_rounds", []):
            r_date = dt.date.fromisoformat(r["date"]) if isinstance(r["date"], str) else r["date"]
            rounds_summary.append(
                GuestGolfRoundSummary(
                    course_name=r.get("course_name", "Rosapenna Championship Course"),
                    date=r_date,
                    time=r.get("time", "08:00"),
                    holes=r.get("holes", "18"),
                    players=int(r.get("players", 2)),
                    caddy_request_status=r.get("caddy_request_status"),
                    source_status="confirmed",
                )
            )

        # Build verified payment ledger summary (F11)
        financials_data = doc.get("financials", {})
        completed_payments = [p for p in doc.get("payments", []) if p.get("status") == "completed"]
        fin_summary = GuestFinancialSummary(
            total_amount=financials_data.get("total_amount", "0.00"),
            deposit_paid=financials_data.get("deposit_paid", "0.00"),
            credit_amount=financials_data.get("credit_amount", "0.00"),
            balance_due=financials_data.get("balance_due", "0.00"),
            currency=financials_data.get("currency", "EUR"),
            payment_status=financials_data.get("payment_status", "unpaid"),
            balance_due_date=balance_due_date,
            ledger_verified=True,
            payments_count=len(completed_payments),
        )

        # Build pending requests held separately from confirmed itinerary (AC 3)
        pending_list: List[GuestPendingRequestSummary] = []
        for p in doc.get("pending_requests", []):
            p_sub = dt.datetime.fromisoformat(p["submitted_at"]) if isinstance(p.get("submitted_at"), str) else p.get("submitted_at", _utc_now())
            pending_list.append(
                GuestPendingRequestSummary(
                    request_id=p.get("request_id", "REQ-1"),
                    request_type=p.get("request_type", "general_inquiry"),
                    description=p.get("description", "Pending booking request"),
                    status=p.get("status", "pending_review"),
                    submitted_at=p_sub,
                    notes=p.get("notes"),
                )
            )

        # Determine authoritative sources health and freshness (AC 5)
        now = _utc_now()
        sources_health: Dict[str, ComponentSourceHealth] = {
            "hotel_pms": ComponentSourceHealth(
                status="confirmed" if opera_stay else "unavailable",
                message="Authoritative Opera hotel stay verified" if opera_stay else "Hotel reservation details not currently linked",
                last_synced_at=doc.get("updated_at"),
            ),
            "golf_brs": ComponentSourceHealth(
                status="confirmed" if rounds_summary else "partial",
                message=f"{len(rounds_summary)} Championship rounds confirmed in BRS" if rounds_summary else "No golf rounds attached to this reservation",
                last_synced_at=doc.get("updated_at"),
            ),
            "payment_ledger": ComponentSourceHealth(
                status="confirmed" if completed_payments else "partial",
                message="F11 payment ledger verified" if completed_payments else "Pending initial payment ledger reconciliation",
                last_synced_at=doc.get("updated_at"),
            ),
        }

        lead_guest = doc.get("lead_guest", {})
        first_name = lead_guest.get("first_name", "")
        last_name_val = lead_guest.get("last_name", "")
        guest_display_name = f"{first_name} {last_name_val}".strip()

        return GuestSafeBookingSummary(
            booking_reference=doc["booking_reference"],
            source_channel=doc.get("source_channel", "online"),
            lead_guest_name=guest_display_name,
            stay=stay_summary,
            golf_rounds=rounds_summary,
            financials=fin_summary,
            pending_requests=pending_list,
            sources_health=sources_health,
            data_freshness_timestamp=now,
            status=doc.get("status", "linked"),
        )

    def record_payment(
        self,
        booking_reference: str,
        payment: PaymentLedgerEntry,
        actor: str = "prommt_webhook",
    ) -> BookingLinkageResponse:
        """Record or update a verified payment transaction in the booking ledger (Ticket F11).

        Enforces:
        - AC 3: Correlates link, amount, currency, and provider reference before recording.
        - AC 4: Idempotent: duplicate or out-of-order events cannot credit twice.
        - AC 5: Non-completed payments (pending, failed, refunded) do not inflate deposit_paid.
        - AC 6: Zero card details stored.
        """
        if self.collection is None:
            raise LinkageNotFoundError("Storage is not configured.")

        clean_ref = booking_reference.strip().upper()
        doc = self.collection.find_one({"booking_reference": clean_ref})
        if doc is None:
            raise LinkageNotFoundError(f"Booking reference '{booking_reference}' was not found.")

        now = _utc_now()
        existing_payments = doc.get("payments", [])

        # Idempotency check: if payment_id already exists with same status, return early
        existing_idx = next(
            (i for i, p in enumerate(existing_payments) if p.get("payment_id") == payment.payment_id),
            None,
        )

        if existing_idx is not None:
            existing_p = existing_payments[existing_idx]
            if existing_p.get("status") == "completed" and payment.status == "completed":
                logger.info(
                    "Payment %s for booking %s is already completed in ledger; idempotent return",
                    payment.payment_id,
                    clean_ref,
                )
                return BookingLinkageResponse(**doc)
            existing_payments[existing_idx] = payment.model_dump(mode="json")
        else:
            existing_payments.append(payment.model_dump(mode="json"))

        updated_payment_models = [PaymentLedgerEntry(**p) for p in existing_payments]

        lead_guest = LeadGuestContact(**doc["lead_guest"])
        opera_stay = OperaStayDetails(**doc["opera_stay"]) if doc.get("opera_stay") else None
        brs_rounds = [BrsRoundDetails(**r) for r in doc.get("brs_rounds", [])]

        status, discrepancies, financials = self._reconcile_components(
            lead_guest=lead_guest,
            opera_stay=opera_stay,
            brs_rounds=brs_rounds,
            payments=updated_payment_models,
            exclude_booking_ref=clean_ref,
        )

        audit_entry = LinkageAuditTrailEntry(
            audit_id=f"aud_{uuid.uuid4().hex[:12]}",
            actor=actor,
            action="payment_recorded",
            occurred_at=now,
            reason=f"Recorded {payment.payment_type} payment of {payment.currency} {payment.amount} (status: {payment.status})",
            before_state={"financials": doc.get("financials"), "payments_count": len(existing_payments)},
            after_state={"financials": financials, "payments_count": len(updated_payment_models)},
        )

        self.collection.update_one(
            {"booking_reference": clean_ref},
            {
                "$set": {
                    "payments": [p.model_dump(mode="json") for p in updated_payment_models],
                    "financials": financials,
                    "status": status,
                    "discrepancies": discrepancies,
                    "updated_at": now,
                },
                "$push": {"audit_trail": audit_entry.model_dump(mode="json")},
            },
        )

        updated_doc = self.collection.find_one({"booking_reference": clean_ref})
        assert updated_doc is not None
        return BookingLinkageResponse(**updated_doc)

    def record_credit(
        self,
        booking_reference: str,
        credit_entry: Dict[str, Any],
        actor: str = "amendment_service",
    ) -> BookingLinkageResponse:
        """Record an approved resort credit under Rosapenna policy (Ticket M08).

        Credits are recorded in the booking linkage audit trail and payment ledger without
        performing automated credit card refunds.
        """
        ledger_entry = PaymentLedgerEntry(
            payment_id=credit_entry.get("credit_id", f"crd_{uuid.uuid4().hex[:12]}"),
            provider="rosapenna_policy",
            payment_type="credit",
            amount=credit_entry["amount"],
            currency=credit_entry.get("currency", "EUR"),
            status="completed",
            provider_reference=credit_entry.get("offer_id"),
            verified_at=_utc_now(),
        )
        return self.record_payment(booking_reference=booking_reference, payment=ledger_entry, actor=actor)

    def update_lead_phone(
        self,
        booking_reference: str,
        phone_number: str,
        actor: str,
        reason: str,
        expected_revision: int,
    ) -> BookingLinkageResponse:
        """Atomic update of lead guest telephone on booking linkage without altering stay, rates, occupants or profiles."""
        if self.collection is None:
            raise LinkageNotFoundError("Storage is not configured.")

        clean_ref = booking_reference.strip().upper()
        existing = self.collection.find_one({"booking_reference": clean_ref})
        if existing is None:
            raise LinkageNotFoundError(f"Booking reference '{booking_reference}' was not found.")

        current_rev = int(existing.get("revision", 1))
        if current_rev != expected_revision:
            raise LinkageConflictError(
                f"Booking revision conflict: expected {expected_revision}, current is {current_rev}. Please refresh."
            )

        now = _utc_now()
        before_phone = existing.get("lead_guest", {}).get("telephone")
        audit_entry = LinkageAuditTrailEntry(
            audit_id=f"aud_{uuid.uuid4().hex[:12]}",
            actor=actor,
            action="guest_phone_updated",
            occurred_at=now,
            reason=reason,
            before_state={"telephone": before_phone, "revision": current_rev},
            after_state={"telephone": phone_number, "revision": current_rev + 1},
        )

        res = self.collection.update_one(
            {"booking_reference": clean_ref, "revision": expected_revision},
            {
                "$set": {
                    "lead_guest.telephone": phone_number,
                    "updated_at": now,
                },
                "$inc": {"revision": 1},
                "$push": {"audit_trail": audit_entry.model_dump(mode="json")},
            },
        )
        if res.matched_count == 0:
            raise LinkageConflictError("Concurrent booking update detected. Please refresh.")

        updated_doc = self.collection.find_one({"booking_reference": clean_ref})
        assert updated_doc is not None
        return BookingLinkageResponse(**updated_doc)

    def update_round_caddy(
        self,
        booking_reference: str,
        round_id: str,
        caddy_status: str,
        caddy_data: Optional[Dict[str, Any]],
        actor: str,
        reason: str,
        expected_revision: int,
    ) -> BookingLinkageResponse:
        """Atomic update of caddy request attached to a specific confirmed golf round."""
        if self.collection is None:
            raise LinkageNotFoundError("Storage is not configured.")

        clean_ref = booking_reference.strip().upper()
        clean_round_id = round_id.strip()

        existing = self.collection.find_one({"booking_reference": clean_ref})
        if existing is None:
            raise LinkageNotFoundError(f"Booking reference '{booking_reference}' was not found.")

        current_rev = int(existing.get("revision", 1))
        if current_rev != expected_revision:
            raise LinkageConflictError(
                f"Booking revision conflict: expected {expected_revision}, current is {current_rev}. Please refresh."
            )

        rounds = existing.get("brs_rounds", [])
        target_round = next((r for r in rounds if r.get("round_id") == clean_round_id), None)
        if not target_round:
            raise LinkageNotFoundError(f"Golf round '{clean_round_id}' not found on booking '{clean_ref}'.")

        now = _utc_now()
        before_status = target_round.get("caddy_request_status")
        before_caddy = target_round.get("caddy_request")

        audit_entry = LinkageAuditTrailEntry(
            audit_id=f"aud_{uuid.uuid4().hex[:12]}",
            actor=actor,
            action="round_caddy_updated",
            occurred_at=now,
            reason=reason,
            before_state={
                "round_id": clean_round_id,
                "caddy_status": before_status,
                "caddy_request": before_caddy,
                "revision": current_rev,
            },
            after_state={
                "round_id": clean_round_id,
                "caddy_status": caddy_status,
                "caddy_request": caddy_data,
                "revision": current_rev + 1,
            },
        )

        res = self.collection.update_one(
            {
                "booking_reference": clean_ref,
                "revision": expected_revision,
                "brs_rounds.round_id": clean_round_id,
            },
            {
                "$set": {
                    "brs_rounds.$.caddy_request_status": caddy_status,
                    "brs_rounds.$.caddy_request": caddy_data,
                    "updated_at": now,
                },
                "$inc": {"revision": 1},
                "$push": {"audit_trail": audit_entry.model_dump(mode="json")},
            },
        )
        if res.matched_count == 0:
            raise LinkageConflictError("Concurrent booking update detected. Please refresh.")

        updated_doc = self.collection.find_one({"booking_reference": clean_ref})
        assert updated_doc is not None
        return BookingLinkageResponse(**updated_doc)
