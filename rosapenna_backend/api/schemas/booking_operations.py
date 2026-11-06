"""Validation and domain schemas for Ticket F14 — Safe Booking Operation Engine.

Guarantees durable operation state tracking, idempotent submissions, zero inventory
reservation before verified payment, post-payment revalidation, and authoritative reconciliation.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
import hashlib
from typing import Any, Dict, List, Literal, Optional
import uuid

from pydantic import BaseModel, ConfigDict, Field, field_validator


OperationType = Literal["new_booking", "amendment"]

OperationStage = Literal[
    "initialized",
    "payment_verified",
    "revalidation_passed",
    "hotel_written",
    "golf_written",
    "completed",
    "unconfirmed_reconciliation",
]


class ReadbackEvidence(BaseModel):
    """Authoritative readback verification evidence across external vendor APIs."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    opera_reservation_id: Optional[str] = Field(default=None, max_length=100)
    opera_confirmation_number: Optional[str] = Field(default=None, max_length=100)
    opera_verified: bool = False
    brs_round_ids: List[str] = Field(default_factory=list)
    brs_booking_ids: List[str] = Field(default_factory=list)
    brs_verified: bool = False
    verified_at: Optional[str] = None
    notes: Optional[str] = None


class StaffReconciliationAlert(BaseModel):
    """Actionable alert for hotel reservations staff. Strictly excludes sensitive payment cards/credentials."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    alert_id: str = Field(default_factory=lambda: f"ALT-{uuid.uuid4().hex[:8].upper()}")
    operation_id: str
    booking_reference: str
    failed_stage: str
    reason: str
    payment_id: Optional[str] = None
    payment_provider_ref: Optional[str] = None
    opera_reservation_id: Optional[str] = None
    brs_round_ids: List[str] = Field(default_factory=list)
    created_at: str = Field(default_factory=lambda: dt.datetime.now(dt.timezone.utc).isoformat())
    requires_human_review: bool = True


class StageHistoryEntry(BaseModel):
    """Immutable audit trail entry for operation state transitions."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    stage: OperationStage
    timestamp: str = Field(default_factory=lambda: dt.datetime.now(dt.timezone.utc).isoformat())
    actor: str = "operation_engine"
    details: Optional[str] = None


class BookingOperationRecord(BaseModel):
    """Durable state record for an atomic booking or amendment operation."""

    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    operation_id: str = Field(min_length=1, max_length=100)
    idempotency_key_hash: str = Field(min_length=1, max_length=100)
    operation_type: OperationType
    booking_reference: str = Field(min_length=1, max_length=50)
    quote_id: Optional[str] = None
    quote_version: Optional[str] = None
    amendment_offer_id: Optional[str] = None
    stage: OperationStage = "initialized"
    stage_history: List[StageHistoryEntry] = Field(default_factory=list)
    payment_id: Optional[str] = None
    payment_verified: bool = False
    vendor_references: Dict[str, Any] = Field(default_factory=dict)
    readback_evidence: Optional[ReadbackEvidence] = None
    staff_alert: Optional[StaffReconciliationAlert] = None
    email_delivery_id: Optional[str] = None
    created_at: str = Field(default_factory=lambda: dt.datetime.now(dt.timezone.utc).isoformat())
    updated_at: str = Field(default_factory=lambda: dt.datetime.now(dt.timezone.utc).isoformat())
    completed_at: Optional[str] = None


class ExecuteBookingOperationRequest(BaseModel):
    """Client request to safely commit a new booking or amendment operation."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    idempotency_key: str = Field(min_length=8, max_length=128)
    operation_type: OperationType = "new_booking"
    quote_id: Optional[str] = Field(default=None, max_length=100)
    payment_id: Optional[str] = Field(default=None, max_length=100)
    amendment_offer_id: Optional[str] = Field(default=None, max_length=100)
    booking_reference: Optional[str] = Field(default=None, max_length=50)


class ExecuteBookingOperationResponse(BaseModel):
    """Response detailing the durable result of the safe booking operation."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    operation_id: str
    booking_reference: str
    operation_type: OperationType
    stage: OperationStage
    status: Literal["confirmed", "unconfirmed_reconciliation", "pending_payment"]
    message: str
    readback_verified: bool = False
    staff_review_required: bool = False
    replayed: bool = False
    email_delivery_id: Optional[str] = None
    created_at: str
    completed_at: Optional[str] = None


def hash_idempotency_key(key: str) -> str:
    """Create a SHA-256 hash of the idempotency key for safe indexed lookup."""
    return hashlib.sha256(key.strip().encode("utf-8")).hexdigest()
