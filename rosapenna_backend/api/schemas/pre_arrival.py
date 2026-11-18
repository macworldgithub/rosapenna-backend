"""Pydantic schemas for Ticket N08 — Pre-arrival scheduling and communication workflows.

Defines strict models for pre-arrival policies, server-owned eligible booking inputs,
persistent communication jobs, attempt records, and staff management endpoints.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
import hashlib
import re
from typing import Any, Dict, List, Literal, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def _valid_iana_timezone(tz_name: str) -> bool:
    try:
        ZoneInfo(tz_name)
        return True
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        return False


class PreArrivalPolicyDoc(BaseModel):
    """Authoritative pre-arrival scheduling policy definition."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    schema_version: int = 1
    policy_id: str = Field(min_length=1, max_length=100)
    version: int = Field(ge=1)
    environment: Literal["development", "test", "staging", "production"]
    approval_status: Literal["unresolved", "fixture", "approved"]
    approved_by: Optional[str] = Field(default=None, max_length=100)
    approved_at: Optional[dt.datetime] = None
    source: str = Field(min_length=1, max_length=200)
    effective_from: Optional[dt.date] = None
    effective_to: Optional[dt.date] = None
    timezone: str = Field(default="Europe/Dublin", max_length=50)
    local_send_time: str = Field(default="09:00", pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    balance_request_lead_days: Optional[int] = Field(default=None, ge=1, le=180)
    balance_due_lead_days: int = Field(default=42, ge=1, le=180)
    rooming_caddy_lead_days: int = Field(default=42, ge=1, le=180)
    late_booking_action: Literal["unresolved", "immediate", "adjusted"] = "unresolved"
    late_booking_offset_hours: int = Field(default=2, ge=0, le=72)
    dst_rule: Literal["standard", "strict_reject_nonexistent"] = "standard"
    template_version: str = Field(default="v1", max_length=50)
    sender_email: str = Field(default="reservations@rosapenna.invalid", max_length=254)
    sender_name: str = Field(default="Rosapenna Hotel & Golf Resort", max_length=100)
    reply_to: Optional[str] = Field(default=None, max_length=254)
    max_evidence_age_hours: int = Field(default=72, ge=1, le=720)

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, v: str) -> str:
        clean = v.strip()
        if not _valid_iana_timezone(clean):
            raise ValueError(f"Invalid IANA timezone: '{clean}'")
        return clean

    @model_validator(mode="after")
    def validate_approvals_and_dates(self) -> "PreArrivalPolicyDoc":
        if self.approval_status == "approved":
            if not self.approved_by or not self.approved_at:
                raise ValueError("Approved policy requires approved_by and approved_at")
        if self.effective_from and self.effective_to:
            if self.effective_to < self.effective_from:
                raise ValueError("effective_to cannot be earlier than effective_from")
        return self


class EligibleBookingInput(BaseModel):
    """Server-owned verified booking facts for schedule calculation and eligibility checks."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    booking_id: str = Field(min_length=1, max_length=100)
    booking_reference: str = Field(min_length=5, max_length=50)
    property_id: str = Field(default="ROSA", max_length=50)
    source_channel: Literal["online", "phone", "email"]
    revision: int = Field(default=1, ge=1)
    arrival_date: dt.date
    departure_date: dt.date
    confirmed_at: dt.datetime
    confirmation_status: str = Field(min_length=1, max_length=50)
    confirmation_evidence: Dict[str, Any] = Field(default_factory=dict)
    is_cancelled: bool = False
    has_discrepancy: bool = False
    in_recovery: bool = False
    lead_name: str = Field(min_length=1, max_length=200)
    lead_email: str = Field(min_length=3, max_length=254)
    lead_telephone: Optional[str] = None
    total_amount: Decimal = Field(default=Decimal("0.00"))
    verified_paid_total: Decimal = Field(default=Decimal("0.00"))
    currency: str = Field(default="EUR", max_length=3)
    balance_due: Decimal = Field(default=Decimal("0.00"))
    ledger_version: int = Field(default=1, ge=1)
    rooming_completed: bool = False
    caddy_completed: bool = False
    evidence_timestamp: dt.datetime

    @model_validator(mode="after")
    def validate_dates(self) -> "EligibleBookingInput":
        if self.departure_date <= self.arrival_date:
            raise ValueError("departure_date must be after arrival_date")
        return self

    def compute_eligibility_hash(self, reminder_type: Optional[str] = None) -> str:
        """Calculate deterministic SHA256 digest of relevant canonical eligibility facts.

        For rooming_and_caddy, financial amounts and ledger versions are excluded so that
        subsequent balance payments do not trigger false same-revision fact conflicts.
        """
        if reminder_type == "rooming_and_caddy":
            payload = (
                f"{self.booking_id}|{self.revision}|{self.arrival_date.isoformat()}|"
                f"{self.departure_date.isoformat()}|{self.confirmation_status.lower()}|"
                f"{int(self.is_cancelled)}|{int(self.has_discrepancy)}|"
                f"{int(self.in_recovery)}|{int(self.rooming_completed)}|{int(self.caddy_completed)}"
            )
        else:
            payload = (
                f"{self.booking_id}|{self.revision}|{self.arrival_date.isoformat()}|"
                f"{self.departure_date.isoformat()}|{self.confirmation_status.lower()}|"
                f"{self.total_amount:.2f}|{self.verified_paid_total:.2f}|{self.balance_due:.2f}|"
                f"{self.currency}|{int(self.is_cancelled)}|{int(self.has_discrepancy)}|"
                f"{int(self.in_recovery)}|{int(self.rooming_completed)}|{int(self.caddy_completed)}|"
                f"{self.ledger_version}"
            )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


JobStatus = Literal[
    "blocked_dependency",
    "scheduled",
    "sending",
    "recorded_test",
    "submitted",
    "failed",
    "unknown",
    "suppressed",
    "canceled_by_amendment",
]


class PreArrivalJobAttempt(BaseModel):
    """Audit record for a single dispatch attempt."""

    model_config = ConfigDict(extra="ignore")

    attempt_id: str
    started_at: dt.datetime
    ended_at: Optional[dt.datetime] = None
    actor: str
    outcome: str
    reason_code: Optional[str] = None
    provider_reference: Optional[str] = None
    error_details: Optional[str] = None


class PreArrivalJobDoc(BaseModel):
    """Canonical persistent reminder job in scheduled_booking_reminders (schema_version=2)."""

    model_config = ConfigDict(extra="ignore")

    schema_version: int = 2
    reminder_id: str = Field(description="Unique milestone identifier")
    environment: str
    test_run_id: Optional[str] = None
    property_id: str = "ROSA"
    booking_id: str
    booking_reference: str
    revision_number: int
    reminder_type: Literal["balance_due_first", "rooming_and_caddy"]
    title: str
    scheduled_date: str  # YYYY-MM-DD
    due_at: dt.datetime
    timezone: str
    policy_id: str
    policy_version: int
    template_version: str
    eligibility_hash: str
    balance_due_date: Optional[str] = None
    balance_due_amount: Optional[str] = None
    status: JobStatus = "scheduled"
    reason_code: Optional[str] = None
    attempt_count: int = 0
    next_attempt_at: Optional[dt.datetime] = None
    attempt_id: Optional[str] = None
    lease_expires_at: Optional[dt.datetime] = None
    message_hash: Optional[str] = None
    message_id: Optional[str] = None
    submission_started: bool = False
    submission_started_at: Optional[dt.datetime] = None
    prepared_at: Optional[dt.datetime] = None
    prepared_mime_bytes: Optional[bytes] = None
    prepared_message_date: Optional[str] = None
    rendered_content: Optional[Dict[str, Any]] = None
    provider_reference: Optional[str] = None
    delivery_result: Optional[Dict[str, Any]] = None
    attempts: List[PreArrivalJobAttempt] = Field(default_factory=list)
    created_at: dt.datetime
    updated_at: dt.datetime


# -------------------------------------------------------------------------
# Staff API Request & Response DTOs
# -------------------------------------------------------------------------


class StaffJobSummary(BaseModel):
    """Scoped summary of a pre-arrival job safe for staff display (no guest tokens/secrets)."""

    model_config = ConfigDict(extra="ignore")

    reminder_id: str
    booking_reference: str
    revision_number: int
    reminder_type: str
    title: str
    scheduled_date: str
    due_at: dt.datetime
    status: str
    reason_code: Optional[str] = None
    attempt_count: int
    next_attempt_at: Optional[dt.datetime] = None
    created_at: dt.datetime
    updated_at: dt.datetime


class StaffJobListResponse(BaseModel):
    """Staff response listing all scheduled/dispatched pre-arrival jobs for a booking."""

    model_config = ConfigDict(extra="ignore")

    booking_reference: str
    jobs: List[StaffJobSummary]
    total: int


class StaffReconcileRequest(BaseModel):
    """Payload to trigger idempotent server-side reconciliation for a booking."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    expected_revision: int = Field(ge=1)
    reason: str = Field(min_length=3, max_length=500)


class StaffReconcileResponse(BaseModel):
    """Response detailing the result of reconciliation."""

    model_config = ConfigDict(extra="ignore")

    booking_reference: str
    reconciled_jobs_count: int
    jobs: List[StaffJobSummary]


class StaffRetryJobRequest(BaseModel):
    """Payload to safely request a retry on a failed or reconciled unknown job."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    expected_revision: int = Field(ge=1)
    attempt_id: Optional[str] = Field(default=None, max_length=100)
    reason: str = Field(min_length=3, max_length=500)
    reconciliation_outcome: Optional[Literal["verified_non_acceptance", "resend_approved"]] = None


class StaffRetryJobResponse(BaseModel):
    """Response detailing the result of a retry request."""

    model_config = ConfigDict(extra="ignore")

    reminder_id: str
    status: str
    message: str
    next_attempt_at: Optional[dt.datetime] = None
