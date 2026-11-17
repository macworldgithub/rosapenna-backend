"""Pydantic schemas for Ticket N09 — Balance Payments and Reminders.

Defines strict models for accepted financial snapshots, balance calculations,
bound payment requests, immutable payment evidence receipts, staff controls,
and simulation requests. Money values are strictly validated decimal strings;
no floats are permitted.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal, InvalidOperation
import hashlib
from typing import Any, Dict, List, Literal, Optional
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

PRICE_PATTERN = r"^\d{1,8}(?:\.\d{1,2})?$"


def validate_price_string(value: str | Decimal | int) -> str:
    """Validate and format a monetary amount as a non-negative string with 2 decimal places."""
    try:
        val_str = str(value).strip()
        if "e" in val_str.lower():
            raise ValueError("Scientific notation not allowed for prices")
        amount = Decimal(val_str)
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError("Invalid price value") from None

    if not amount.is_finite() or amount < Decimal("0.00"):
        raise ValueError("Price must be a non-negative finite amount")
    if amount != amount.quantize(Decimal("0.01")):
        raise ValueError("Price cannot exceed two decimal places")
    return format(amount.quantize(Decimal("0.01")), ".2f")


class AcceptedFinancialsSnapshot(BaseModel):
    """Authoritative snapshot of accepted booking financials."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    schema_version: int = 1
    booking_revision: int = Field(ge=1)
    accepted_total: str = Field(pattern=PRICE_PATTERN, max_length=11)
    currency: str = Field(default="EUR", pattern=r"^[A-Z]{3}$")
    accepted_snapshot_hash: str = Field(min_length=16, max_length=64)
    accepted_at: dt.datetime
    evidence_reference: str = Field(min_length=1, max_length=150)
    source: Literal["online_quote", "staff_import", "opera_verified"]

    @field_validator("accepted_total")
    @classmethod
    def normalize_accepted_total(cls, v: str) -> str:
        return validate_price_string(v)

    @classmethod
    def compute_hash(
        cls,
        booking_reference: str,
        booking_revision: int,
        accepted_total: str,
        currency: str,
        source: str,
        evidence_reference: str,
    ) -> str:
        payload = f"{booking_reference.strip().upper()}|{booking_revision}|{validate_price_string(accepted_total)}|{currency.strip().upper()}|{source.strip()}|{evidence_reference.strip()}"
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


BalanceCollectionStatus = Literal[
    "not_due",
    "due",
    "overdue",
    "paid",
    "reconciliation_required",
    "blocked_dependency",
]


class BalanceSummaryResponse(BaseModel):
    """Detailed read-only balance summary and collection status for a booking."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    booking_reference: str
    property_id: str
    booking_revision: int
    ledger_version: int
    currency: str
    accepted_total: str
    verified_deposit: str
    verified_other_paid: str
    total_verified_paid: str
    balance_due: str
    overpayment: str
    collection_status: BalanceCollectionStatus
    balance_due_date: Optional[dt.date] = None
    first_request_date: Optional[dt.date] = None
    active_request_id: Optional[str] = None
    active_request_state: Optional[str] = None
    policy_id: Optional[str] = None
    policy_version: Optional[int] = None
    blocked_reason: Optional[str] = None


BalanceRequestState = Literal[
    "creating",
    "ready",
    "pending",
    "verified_paid",
    "failed",
    "expired",
    "superseded",
    "unknown",
    "reconciliation_required",
]


class BalancePaymentRequestDoc(BaseModel):
    """Durable balance payment request representation."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    payment_id: str = Field(min_length=8, max_length=100)
    idempotency_key: str = Field(min_length=8, max_length=100)
    command_id: str = Field(min_length=8, max_length=100)
    booking_reference: str = Field(min_length=5, max_length=50)
    property_id: str = Field(default="ROSA", max_length=50)
    environment: str
    booking_revision: int = Field(ge=1)
    ledger_version: int = Field(ge=1)
    accepted_snapshot_hash: str = Field(min_length=16, max_length=64)
    amount: str = Field(pattern=PRICE_PATTERN, max_length=11)
    currency: str = Field(default="EUR", pattern=r"^[A-Z]{3}$")
    purpose: Literal["balance"] = "balance"
    payer_email: str = Field(min_length=3, max_length=254)
    payer_name: str = Field(min_length=1, max_length=200)
    provider: str = "simulation"
    provider_account_id: Optional[str] = None
    provider_request_id: Optional[str] = None
    hosted_url: Optional[str] = None
    state: BalanceRequestState = "creating"
    reason: Optional[str] = None
    attempts: int = 0
    created_at: dt.datetime
    expires_at: dt.datetime
    updated_at: dt.datetime
    paid_at: Optional[dt.datetime] = None
    is_test: bool = True
    test_run_id: Optional[str] = None

    @field_validator("amount")
    @classmethod
    def normalize_amount(cls, v: str) -> str:
        return validate_price_string(v)


class VerifiedBalanceEvidence(BaseModel):
    """Strictly verified payment evidence from trusted gateway or simulation adapter."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    receipt_id: str = Field(min_length=8, max_length=100)
    event_id: str = Field(min_length=4, max_length=100)
    transaction_id: str = Field(min_length=4, max_length=100)
    payment_id: str = Field(min_length=8, max_length=100)
    booking_reference: str = Field(min_length=5, max_length=50)
    property_id: str = Field(default="ROSA", max_length=50)
    environment: str
    purpose: Literal["balance"] = "balance"
    amount: str = Field(pattern=PRICE_PATTERN, max_length=11)
    currency: str = Field(default="EUR", pattern=r"^[A-Z]{3}$")
    status: Literal["completed", "failed", "pending", "refunded"]
    occurred_at: dt.datetime
    verified_at: dt.datetime
    evidence_source: Literal["simulation", "prommt_verified"]
    raw_event_hash: str = Field(min_length=16, max_length=64)
    is_test: bool = True
    test_run_id: Optional[str] = None

    @field_validator("amount")
    @classmethod
    def normalize_amount(cls, v: str) -> str:
        return validate_price_string(v)


class PaymentEvidenceReceiptDoc(BaseModel):
    """Durable stored evidence receipt in payment_evidence_receipts journal."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    receipt_id: str = Field(min_length=8, max_length=100)
    event_id: str = Field(min_length=4, max_length=100)
    transaction_id: str = Field(min_length=4, max_length=100)
    payment_id: str = Field(min_length=8, max_length=100)
    booking_reference: str = Field(min_length=5, max_length=50)
    property_id: str = Field(default="ROSA", max_length=50)
    environment: str
    purpose: Literal["balance"] = "balance"
    amount: str = Field(pattern=PRICE_PATTERN, max_length=11)
    currency: str = Field(default="EUR", pattern=r"^[A-Z]{3}$")
    status: Literal["completed", "failed", "pending", "refunded"]
    occurred_at: dt.datetime
    verified_at: dt.datetime
    evidence_source: str
    raw_event_hash: str = Field(min_length=16, max_length=64)
    state: Literal["received", "applied", "quarantined"]
    is_test: bool = True
    test_run_id: Optional[str] = None
    created_at: dt.datetime
    applied_at: Optional[dt.datetime] = None
    quarantine_reason: Optional[str] = None


class StaffCreateBalanceRequest(BaseModel):
    """Staff payload to request a bound balance payment link."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    expected_booking_revision: int = Field(ge=1)
    expected_ledger_version: int = Field(ge=1)
    command_id: str = Field(min_length=8, max_length=100)
    reason: str = Field(min_length=5, max_length=500)


class StaffReconcileBalanceRequest(BaseModel):
    """Staff payload to trigger request reconciliation."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    expected_request_version: Optional[int] = None
    reason: str = Field(min_length=5, max_length=500)
    action: Literal["check_status", "mark_unknown", "quarantine"] = "check_status"


class StaffBalanceAttentionItem(BaseModel):
    """Item for staff attention query."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    booking_reference: str
    property_id: str
    state: str
    amount: str
    currency: str
    reason: str
    updated_at: dt.datetime


class DevSimulateBalancePaymentRequest(BaseModel):
    """Test/dev simulation payload for executing deterministically signed balance payment events."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    scenario: Literal[
        "success",
        "failure",
        "pending",
        "expired",
        "delayed_success",
        "duplicate",
        "mismatch_amount",
        "creation_timeout",
    ] = "success"
    test_run_id: str = Field(min_length=4, max_length=100)
    signature: Optional[str] = None
