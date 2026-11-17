"""Domain service for Ticket N09 — Balance Payments and Reminders.

Provides strict calculation, bound payment requests, durable evidence ingestion,
monotonic canonical ledger updates, staff controls, and automatic reminder suppression.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
import hashlib
from typing import Any, Dict, List, Literal, Optional, Tuple
import uuid
from zoneinfo import ZoneInfo

from pymongo import ASCENDING
from pymongo.collection import Collection
from pymongo.database import Database

from rosapenna_backend.api.schemas.balance_payments import (
    AcceptedFinancialsSnapshot,
    BalancePaymentRequestDoc,
    BalanceSummaryResponse,
    PaymentEvidenceReceiptDoc,
    StaffBalanceAttentionItem,
    VerifiedBalanceEvidence,
    validate_price_string,
)
from rosapenna_backend.api.schemas.booking_linkage import (
    BookingLinkageResponse,
    PaymentLedgerEntry,
)
from rosapenna_backend.api.schemas.pre_arrival import PreArrivalPolicyDoc
from rosapenna_backend.core.logging import logger
from rosapenna_backend.integrations.payments.balance_gateway import (
    BalanceGatewayDisabledError,
    BalanceGatewayProtocol,
    BalanceGatewayTimeoutError,
    DisabledBalanceGateway,
)
from rosapenna_backend.services.booking_linkage import (
    BookingLinkageService,
    LinkageConflictError,
    LinkageNotFoundError,
)


class BalancePaymentError(Exception):
    """Base exception for balance payment domain errors."""


class BalanceNotFoundError(BalancePaymentError):
    """Raised when a requested booking or balance request is not found."""


class BalanceConflictError(BalancePaymentError):
    """Raised on concurrency, revision, or ledger version conflict."""


class BalanceAlreadyPaidError(BalancePaymentError):
    """Raised when an action is requested for an already-settled balance."""


class BalanceEvidenceTamperedError(BalancePaymentError):
    """Raised when incoming payment evidence exhibits tampering or duplicate collision."""


def _utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


class BalancePaymentService:
    """Manages balance calculation, bound requests, evidence receipts, and ledger updates."""

    def __init__(
        self,
        database: Optional[Database] = None,
        linkage_service: Optional[BookingLinkageService] = None,
        gateway: Optional[BalanceGatewayProtocol] = None,
        pre_arrival_service: Optional[Any] = None,
        environment: str = "test",
        property_id: str = "ROSA",
        test_run_id: Optional[str] = None,
        linkages_col_name: str = "mmb_booking_linkages",
        requests_col_name: str = "package_payments",
        receipts_col_name: str = "payment_evidence_receipts",
        policies_col_name: str = "pre_arrival_policies",
    ):
        self.db: Optional[Database] = database
        self.linkage_service = linkage_service
        self.gateway = gateway or DisabledBalanceGateway()
        self.pre_arrival_service = pre_arrival_service
        self.environment = environment.strip().lower()
        self.property_id = property_id.strip().upper()
        self.test_run_id = test_run_id

        self.linkages_col: Optional[Collection] = database[linkages_col_name] if database is not None else None
        self.requests_col: Optional[Collection] = database[requests_col_name] if database is not None else None
        self.receipts_col: Optional[Collection] = database[receipts_col_name] if database is not None else None
        self.policies_col: Optional[Collection] = database[policies_col_name] if database is not None else None

        if database is not None:
            self._init_indexes()

    def _init_indexes(self) -> None:
        """Create necessary scoped unique indexes for requests and evidence receipts."""
        if self.requests_col is not None:
            self.requests_col.create_index([("payment_id", ASCENDING)], unique=True, name="uniq_bal_payment_id")
            self.requests_col.create_index(
                [("environment", ASCENDING), ("property_id", ASCENDING), ("idempotency_key", ASCENDING)],
                unique=True,
                name="uniq_bal_idempotency",
            )
            self.requests_col.create_index(
                [("environment", ASCENDING), ("property_id", ASCENDING), ("booking_reference", ASCENDING), ("command_id", ASCENDING)],
                name="idx_bal_booking_command",
            )

        if self.receipts_col is not None:
            self.receipts_col.create_index([("receipt_id", ASCENDING)], unique=True, name="uniq_bal_receipt_id")
            self.receipts_col.create_index(
                [("environment", ASCENDING), ("property_id", ASCENDING), ("event_id", ASCENDING)],
                unique=True,
                name="uniq_bal_event_id",
            )
            # Partial unique index to prevent duplicate successful transactions across payments
            self.receipts_col.create_index(
                [("environment", ASCENDING), ("property_id", ASCENDING), ("transaction_id", ASCENDING)],
                unique=True,
                partialFilterExpression={"status": "completed"},
                name="uniq_bal_successful_transaction",
            )

    # -------------------------------------------------------------------------
    # Balance Summary & Pure Calculation
    # -------------------------------------------------------------------------

    def get_balance_summary(
        self,
        booking_reference: str,
        property_id: Optional[str] = None,
        now: Optional[dt.datetime] = None,
    ) -> BalanceSummaryResponse:
        """Read-only balance summary and status derivation for a confirmed booking."""
        if self.linkages_col is None:
            raise RuntimeError("Database connection is required to read balance summary")

        clock = now or _utc_now()
        clean_ref = booking_reference.strip().upper()
        prop = (property_id or self.property_id).strip().upper()

        linkage_doc = self.linkages_col.find_one({"booking_reference": clean_ref})
        if not linkage_doc:
            raise BalanceNotFoundError(f"Booking reference '{clean_ref}' was not found")

        stay = linkage_doc.get("opera_stay") or {}
        doc_prop = stay.get("hotel_id", prop).strip().upper()
        if doc_prop != prop:
            raise BalanceNotFoundError(f"Booking '{clean_ref}' does not belong to property '{prop}'")

        status = linkage_doc.get("status", "").lower()
        if status not in ("linked", "confirmed"):
            return BalanceSummaryResponse(
                booking_reference=clean_ref,
                property_id=prop,
                booking_revision=linkage_doc.get("revision", 1),
                ledger_version=linkage_doc.get("ledger_version", 1),
                currency=linkage_doc.get("financials", {}).get("currency", "EUR"),
                accepted_total="0.00",
                verified_deposit="0.00",
                verified_other_paid="0.00",
                total_verified_paid="0.00",
                balance_due="0.00",
                overpayment="0.00",
                collection_status="blocked_dependency",
                blocked_reason=f"Booking status is '{status}', not linked/confirmed",
            )

        # Financials derivation
        fin = linkage_doc.get("financials", {})
        accepted_fin = linkage_doc.get("accepted_financials")
        currency = (accepted_fin.get("currency") if accepted_fin else fin.get("currency", "EUR")).strip().upper()

        if accepted_fin and accepted_fin.get("accepted_total"):
            accepted_total = Decimal(str(accepted_fin["accepted_total"]))
        elif fin.get("total_amount"):
            accepted_total = Decimal(str(fin["total_amount"]))
        else:
            room_tot = Decimal(str(stay.get("total_amount", "0.00")))
            golf_tot = sum((Decimal(str(r.get("price", "0.00"))) for r in linkage_doc.get("brs_rounds", [])), Decimal("0.00"))
            accepted_total = room_tot + golf_tot

        payments = linkage_doc.get("payments", [])
        completed_payments = [p for p in payments if p.get("status") == "completed"]

        dep_paid = sum(
            (Decimal(str(p.get("amount", "0.00"))) for p in completed_payments if p.get("payment_type") == "deposit"),
            Decimal("0.00"),
        )
        bal_paid = sum(
            (Decimal(str(p.get("amount", "0.00"))) for p in completed_payments if p.get("payment_type") == "balance"),
            Decimal("0.00"),
        )
        other_paid = sum(
            (Decimal(str(p.get("amount", "0.00"))) for p in completed_payments if p.get("payment_type") in ("full", "amendment_balance")),
            Decimal("0.00"),
        )

        # In legacy bookings where payment_type was not set to deposit, treat non-balance, non-credit completed as deposit
        if dep_paid == Decimal("0.00") and bal_paid == Decimal("0.00") and other_paid > Decimal("0.00"):
            dep_paid = other_paid
            other_paid = Decimal("0.00")

        total_paid = dep_paid + bal_paid + other_paid
        raw_remaining = accepted_total - total_paid
        balance_due = max(Decimal("0.00"), raw_remaining)
        overpayment = max(Decimal("0.00"), -raw_remaining)

        # Resolve timing & policy
        policy = self._get_active_policy()
        balance_due_date: Optional[dt.date] = None
        first_request_date: Optional[dt.date] = None
        arr_date: Optional[dt.date] = None

        if stay.get("arrival_date"):
            arr_date = dt.date.fromisoformat(stay["arrival_date"]) if isinstance(stay["arrival_date"], str) else stay["arrival_date"]

        blocked_reason: Optional[str] = None
        if policy and arr_date:
            balance_due_date = arr_date - dt.timedelta(days=policy.balance_due_lead_days)
            if policy.balance_request_lead_days is not None:
                first_request_date = arr_date - dt.timedelta(days=policy.balance_request_lead_days)
            else:
                blocked_reason = "UNRESOLVED_BALANCE_TIMING"
        elif not policy:
            blocked_reason = "MISSING_PRE_ARRIVAL_POLICY"

        # Determine active request state
        active_req = linkage_doc.get("active_balance_request") or {}
        active_req_id = active_req.get("payment_id")
        active_req_state = active_req.get("state")

        # Determine collection status
        today_prop = clock.astimezone(ZoneInfo(policy.timezone if policy else "Europe/Dublin")).date()

        if balance_due == Decimal("0.00") and accepted_total > Decimal("0.00"):
            collection_status = "paid"
        elif active_req_state in ("unknown", "reconciliation_required"):
            collection_status = "reconciliation_required"
        elif blocked_reason:
            collection_status = "blocked_dependency"
        elif balance_due_date and today_prop >= balance_due_date:
            collection_status = "overdue"
        elif first_request_date and today_prop >= first_request_date:
            collection_status = "due"
        else:
            collection_status = "not_due"

        return BalanceSummaryResponse(
            booking_reference=clean_ref,
            property_id=prop,
            booking_revision=linkage_doc.get("revision", 1),
            ledger_version=linkage_doc.get("ledger_version", 1),
            currency=currency,
            accepted_total=validate_price_string(accepted_total),
            verified_deposit=validate_price_string(dep_paid),
            verified_other_paid=validate_price_string(bal_paid + other_paid),
            total_verified_paid=validate_price_string(total_paid),
            balance_due=validate_price_string(balance_due),
            overpayment=validate_price_string(overpayment),
            collection_status=collection_status,
            balance_due_date=balance_due_date,
            first_request_date=first_request_date,
            active_request_id=active_req_id,
            active_request_state=active_req_state,
            policy_id=policy.policy_id if policy else None,
            policy_version=policy.version if policy else None,
            blocked_reason=blocked_reason,
        )

    def _get_active_policy(self) -> Optional[PreArrivalPolicyDoc]:
        """Fetch active policy for scheduling and milestone definitions."""
        if self.policies_col is not None:
            doc = self.policies_col.find_one({"environment": self.environment}, sort=[("version", -1)])
            if doc:
                doc.pop("_id", None)
                return PreArrivalPolicyDoc(**doc)
        return None

    # -------------------------------------------------------------------------
    # Exact Request Creation & Binding
    # -------------------------------------------------------------------------

    def ensure_balance_request(
        self,
        booking_reference: str,
        expected_booking_revision: int,
        expected_ledger_version: int,
        command_id: str,
        actor: str,
        property_id: Optional[str] = None,
        now: Optional[dt.datetime] = None,
    ) -> BalancePaymentRequestDoc:
        """Ensure an exact, bound balance payment request exists under expected versions."""
        if self.linkages_col is None or self.requests_col is None:
            raise RuntimeError("Database connection is required to create balance requests")

        clock = now or _utc_now()
        clean_ref = booking_reference.strip().upper()
        prop = (property_id or self.property_id).strip().upper()

        summary = self.get_balance_summary(clean_ref, property_id=prop, now=clock)

        if summary.collection_status == "paid" or Decimal(summary.balance_due) <= Decimal("0.00"):
            raise BalanceAlreadyPaidError(
                f"Booking '{clean_ref}' balance is already settled (balance due: EUR {summary.balance_due})"
            )

        if summary.booking_revision != expected_booking_revision:
            raise BalanceConflictError(
                f"Booking revision conflict for '{clean_ref}': expected {expected_booking_revision}, current is {summary.booking_revision}"
            )

        if summary.ledger_version != expected_ledger_version:
            raise BalanceConflictError(
                f"Ledger version conflict for '{clean_ref}': expected {expected_ledger_version}, current is {summary.ledger_version}"
            )

        # Check for existing request with this command_id or active request
        existing_req_doc = self.requests_col.find_one({
            "environment": self.environment,
            "property_id": prop,
            "booking_reference": clean_ref,
            "command_id": command_id,
        })
        if existing_req_doc:
            if (
                existing_req_doc.get("amount") == summary.balance_due
                and existing_req_doc.get("currency") == summary.currency
                and existing_req_doc.get("booking_revision") == summary.booking_revision
                and existing_req_doc.get("ledger_version") == summary.ledger_version
            ):
                existing_req_doc.pop("_id", None)
                return BalancePaymentRequestDoc(**existing_req_doc)
            raise BalanceConflictError(f"Command ID '{command_id}' reused with conflicting balance parameters")

        # Check if booking already has a ready active balance request for this exact amount and currency
        if summary.active_request_id and summary.active_request_state in ("ready", "pending"):
            active_doc = self.requests_col.find_one({"payment_id": summary.active_request_id})
            if active_doc and active_doc.get("amount") == summary.balance_due and active_doc.get("currency") == summary.currency:
                active_doc.pop("_id", None)
                return BalancePaymentRequestDoc(**active_doc)

        linkage_doc = self.linkages_col.find_one({"booking_reference": clean_ref})
        assert linkage_doc is not None

        lead = linkage_doc.get("lead_guest") or {}
        payer_email = lead.get("email", "")
        payer_name = f"{lead.get('first_name', '')} {lead.get('last_name', '')}".strip() or "Valued Guest"
        accepted_fin = linkage_doc.get("accepted_financials")
        accepted_hash = (
            accepted_fin.get("accepted_snapshot_hash")
            if accepted_fin
            else hashlib.sha256(f"{clean_ref}|{summary.accepted_total}|{summary.currency}".encode("utf-8")).hexdigest()
        )

        idempotency_key = f"idemp_bal_{command_id}_{summary.ledger_version}"

        # 1. Conditional reservation on booking document
        cas_filter: Dict[str, Any] = {
            "booking_reference": clean_ref,
            "$and": [
                (
                    {"$or": [{"revision": 1}, {"revision": {"$exists": False}}]}
                    if expected_booking_revision == 1
                    else {"revision": expected_booking_revision}
                ),
                (
                    {"$or": [{"ledger_version": 1}, {"ledger_version": {"$exists": False}}]}
                    if expected_ledger_version == 1
                    else {"ledger_version": expected_ledger_version}
                ),
                {
                    "$or": [
                        {"active_balance_request": None},
                        {"active_balance_request": {"$exists": False}},
                        {"active_balance_request.state": {"$in": ["failed", "expired", "superseded", "verified_paid"]}},
                        {"active_balance_request.payment_id": summary.active_request_id},
                    ]
                },
            ],
        }
        reserve_result = self.linkages_col.update_one(
            cas_filter,
            {
                "$set": {
                    "active_balance_request": {
                        "payment_id": "creating",
                        "command_id": command_id,
                        "state": "creating",
                        "amount": summary.balance_due,
                        "currency": summary.currency,
                        "updated_at": clock,
                    },
                    "updated_at": clock,
                }
            },
        )
        if reserve_result.modified_count == 0:
            raise BalanceConflictError(
                f"Could not reserve balance request creation on '{clean_ref}' (concurrent modification or active request in progress)"
            )

        # 2. Invoke gateway
        try:
            req_doc = self.gateway.create_balance_request(
                booking_reference=clean_ref,
                property_id=prop,
                amount=summary.balance_due,
                currency=summary.currency,
                payer_email=payer_email,
                payer_name=payer_name,
                booking_revision=summary.booking_revision,
                ledger_version=summary.ledger_version,
                accepted_snapshot_hash=accepted_hash,
                command_id=command_id,
                idempotency_key=idempotency_key,
                now=clock,
            )
        except BalanceGatewayTimeoutError as exc:
            logger.warning("Gateway timed out creating balance request for %s: %s", clean_ref, exc)
            self.linkages_col.update_one(
                {"booking_reference": clean_ref},
                {
                    "$set": {
                        "active_balance_request.state": "unknown",
                        "active_balance_request.reason": str(exc),
                        "updated_at": clock,
                    }
                },
            )
            raise BalanceConflictError(f"Payment link creation timed out for '{clean_ref}'; request marked unknown") from exc
        except Exception as exc:
            logger.error("Gateway error creating balance request for %s: %s", clean_ref, exc)
            self.linkages_col.update_one(
                {"booking_reference": clean_ref},
                {
                    "$set": {
                        "active_balance_request.state": "failed",
                        "active_balance_request.reason": str(exc),
                        "updated_at": clock,
                    }
                },
            )
            raise BalancePaymentError(f"Gateway failed to create balance request: {exc}") from exc

        # 3. Persist request document and update booking active request state
        self.requests_col.insert_one(req_doc.model_dump(mode="json"))

        self.linkages_col.update_one(
            {"booking_reference": clean_ref},
            {
                "$set": {
                    "active_balance_request": {
                        "payment_id": req_doc.payment_id,
                        "command_id": command_id,
                        "state": "ready",
                        "amount": req_doc.amount,
                        "currency": req_doc.currency,
                        "hosted_url": req_doc.hosted_url,
                        "expires_at": req_doc.expires_at,
                        "updated_at": clock,
                    },
                    "updated_at": clock,
                }
            },
        )

        logger.info(
            "Created bound balance request %s for booking %s (amount: %s %s, actor: %s)",
            req_doc.payment_id, clean_ref, req_doc.currency, req_doc.amount, actor,
        )
        return req_doc

    # -------------------------------------------------------------------------
    # Evidence Ingestion & Monotonic Ledger Application
    # -------------------------------------------------------------------------

    def ingest_verified_evidence(
        self,
        evidence: VerifiedBalanceEvidence,
        actor: str = "webhook",
        now: Optional[dt.datetime] = None,
    ) -> Tuple[PaymentEvidenceReceiptDoc, BookingLinkageResponse]:
        """Atomically persist evidence receipt and apply payment to canonical booking ledger."""
        if self.receipts_col is None or self.linkages_col is None or self.linkage_service is None:
            raise RuntimeError("Database and linkage service are required for evidence ingestion")

        clock = now or _utc_now()
        clean_ref = evidence.booking_reference.strip().upper()

        # 1. Receipt deduplication and tampering check
        existing_receipt = self.receipts_col.find_one({
            "environment": evidence.environment,
            "property_id": evidence.property_id,
            "event_id": evidence.event_id,
        })
        if existing_receipt:
            if existing_receipt.get("raw_event_hash") == evidence.raw_event_hash:
                logger.info("Idempotent replay of already verified receipt %s", evidence.event_id)
                existing_receipt.pop("_id", None)
                receipt_obj = PaymentEvidenceReceiptDoc(**existing_receipt)
                linkage_doc = self.linkages_col.find_one({"booking_reference": clean_ref})
                assert linkage_doc is not None
                return receipt_obj, BookingLinkageResponse(**linkage_doc)
            raise BalanceEvidenceTamperedError(
                f"Event ID '{evidence.event_id}' arrived with altered hash; possible replay tampering"
            )

        # 2. Check for duplicate successful transaction ID
        if evidence.status == "completed":
            dup_txn = self.receipts_col.find_one({
                "environment": evidence.environment,
                "property_id": evidence.property_id,
                "transaction_id": evidence.transaction_id,
                "status": "completed",
            })
            if dup_txn:
                logger.warning(
                    "Transaction ID %s already claimed by receipt %s for payment %s",
                    evidence.transaction_id, dup_txn.get("receipt_id"), dup_txn.get("payment_id"),
                )
                raise BalanceEvidenceTamperedError(
                    f"Transaction ID '{evidence.transaction_id}' already applied to another payment"
                )

        receipt_doc = PaymentEvidenceReceiptDoc(
            receipt_id=evidence.receipt_id,
            event_id=evidence.event_id,
            transaction_id=evidence.transaction_id,
            payment_id=evidence.payment_id,
            booking_reference=clean_ref,
            property_id=evidence.property_id,
            environment=evidence.environment,
            purpose="balance",
            amount=evidence.amount,
            currency=evidence.currency,
            status=evidence.status,
            occurred_at=evidence.occurred_at,
            verified_at=evidence.verified_at,
            evidence_source=evidence.evidence_source,
            raw_event_hash=evidence.raw_event_hash,
            state="received",
            is_test=evidence.is_test,
            test_run_id=evidence.test_run_id,
            created_at=clock,
            applied_at=None,
            quarantine_reason=None,
        )
        self.receipts_col.insert_one(receipt_doc.model_dump(mode="json"))

        # 3. Apply to canonical booking ledger via BookingLinkageService
        entry = PaymentLedgerEntry(
            payment_id=evidence.payment_id,
            provider=evidence.evidence_source,
            payment_type="balance",
            amount=evidence.amount,
            currency=evidence.currency,
            status=evidence.status,
            provider_reference=evidence.transaction_id,
            verified_at=evidence.verified_at,
        )

        booking_response = self.linkage_service.record_payment(
            booking_reference=clean_ref,
            payment=entry,
            actor=actor,
        )

        # 4. Mark receipt applied
        self.receipts_col.update_one(
            {"receipt_id": evidence.receipt_id},
            {"$set": {"state": "applied", "applied_at": clock}},
        )
        receipt_doc.state = "applied"
        receipt_doc.applied_at = clock

        # 5. Update request projection
        if self.requests_col is not None:
            new_req_state = "verified_paid" if evidence.status == "completed" else "failed"
            self.requests_col.update_one(
                {"payment_id": evidence.payment_id},
                {
                    "$set": {
                        "state": new_req_state,
                        "paid_at": clock if evidence.status == "completed" else None,
                        "updated_at": clock,
                    }
                },
            )

        # 6. Update booking active request projection
        self.linkages_col.update_one(
            {"booking_reference": clean_ref},
            {
                "$set": {
                    "active_balance_request.state": "verified_paid" if evidence.status == "completed" else "failed",
                    "active_balance_request.updated_at": clock,
                }
            },
        )

        # 7. Trigger N08 pre-arrival reconciliation for truthful reminder suppression
        if self.pre_arrival_service is not None:
            try:
                self.pre_arrival_service.reconcile_booking(clean_ref, now=clock)
                logger.info("Triggered N08 pre-arrival reconciliation for booking %s post balance payment", clean_ref)
            except Exception as exc:
                logger.error("Error triggering N08 reconciliation for %s: %s", clean_ref, exc)

        logger.info(
            "Ingested balance evidence %s for booking %s (amount: %s %s, status: %s)",
            evidence.receipt_id, clean_ref, evidence.currency, evidence.amount, evidence.status,
        )
        return receipt_doc, booking_response

    # -------------------------------------------------------------------------
    # Staff Controls & Attention Queries
    # -------------------------------------------------------------------------

    def get_attention_list(
        self,
        property_id: Optional[str] = None,
        limit: int = 50,
        cursor: Optional[str] = None,
    ) -> List[StaffBalanceAttentionItem]:
        """Fetch bookings and requests requiring staff attention (overdue, failed, unknown, quarantined)."""
        if self.linkages_col is None:
            return []

        prop = (property_id or self.property_id).strip().upper()
        now = _utc_now()
        items: List[StaffBalanceAttentionItem] = []

        # Find bookings with active balance requests in unknown or failed state
        query: Dict[str, Any] = {
            "opera_stay.hotel_id": prop,
            "active_balance_request.state": {"$in": ["unknown", "failed", "reconciliation_required"]},
        }
        for doc in self.linkages_col.find(query).limit(limit):
            req = doc.get("active_balance_request", {})
            items.append(
                StaffBalanceAttentionItem(
                    booking_reference=doc["booking_reference"],
                    property_id=prop,
                    state=req.get("state", "unknown"),
                    amount=req.get("amount", "0.00"),
                    currency=req.get("currency", "EUR"),
                    reason=req.get("reason") or "Balance request in attention state",
                    updated_at=req.get("updated_at") or doc.get("updated_at") or now,
                )
            )

        # Find quarantined receipts
        if self.receipts_col is not None and len(items) < limit:
            q_receipts = self.receipts_col.find({
                "property_id": prop,
                "state": "quarantined",
            }).limit(limit - len(items))
            for r in q_receipts:
                items.append(
                    StaffBalanceAttentionItem(
                        booking_reference=r["booking_reference"],
                        property_id=prop,
                        state="quarantined_receipt",
                        amount=r["amount"],
                        currency=r["currency"],
                        reason=r.get("quarantine_reason") or "Payment evidence quarantined for staff review",
                        updated_at=r.get("created_at") or now,
                    )
                )

        return items

    def reconcile_request_state(
        self,
        payment_id: str,
        action: str,
        reason: str,
        actor: str,
        now: Optional[dt.datetime] = None,
    ) -> BalancePaymentRequestDoc:
        """Staff reconciliation of a balance payment request."""
        if self.requests_col is None or self.linkages_col is None:
            raise RuntimeError("Database connection is required for request reconciliation")

        clock = now or _utc_now()
        clean_id = payment_id.strip()

        req_doc = self.requests_col.find_one({"payment_id": clean_id})
        if not req_doc:
            raise BalanceNotFoundError(f"Payment request '{clean_id}' was not found")

        clean_ref = req_doc["booking_reference"]
        new_state = "reconciliation_required"
        if action == "mark_unknown":
            new_state = "unknown"
        elif action == "quarantine":
            new_state = "reconciliation_required"

        self.requests_col.update_one(
            {"payment_id": clean_id},
            {"$set": {"state": new_state, "reason": reason, "updated_at": clock}},
        )
        self.linkages_col.update_one(
            {"booking_reference": clean_ref, "active_balance_request.payment_id": clean_id},
            {"$set": {"active_balance_request.state": new_state, "active_balance_request.reason": reason, "updated_at": clock}},
        )

        refreshed = self.requests_col.find_one({"payment_id": clean_id})
        assert refreshed is not None
        refreshed.pop("_id", None)
        return BalancePaymentRequestDoc(**refreshed)

    # -------------------------------------------------------------------------
    # N08 Action Resolver
    # -------------------------------------------------------------------------

    def prepare_balance_action(
        self,
        booking_reference: str,
        expected_booking_revision: int,
        expected_ledger_version: int,
        command_id: str,
        now: Optional[dt.datetime] = None,
    ) -> Tuple[Optional[str], Optional[dt.datetime], Optional[str]]:
        """Internal helper for N08 pre-arrival job dispatch to acquire a ready hosted URL.

        Returns (hosted_url, expires_at, block_reason).
        """
        clock = now or _utc_now()
        clean_ref = booking_reference.strip().upper()

        summary = self.get_balance_summary(clean_ref, now=clock)
        if summary.collection_status == "paid" or Decimal(summary.balance_due) <= Decimal("0.00"):
            return None, None, "BALANCE_ALREADY_PAID"

        try:
            req = self.ensure_balance_request(
                booking_reference=clean_ref,
                expected_booking_revision=expected_booking_revision,
                expected_ledger_version=expected_ledger_version,
                command_id=command_id,
                actor="n08_scheduler",
                now=clock,
            )
            return req.hosted_url, req.expires_at, None
        except Exception as exc:
            logger.error("Failed to prepare balance action for N08 on %s: %s", clean_ref, exc)
            return None, None, f"PREPARE_BALANCE_ACTION_FAILED_{type(exc).__name__}"
