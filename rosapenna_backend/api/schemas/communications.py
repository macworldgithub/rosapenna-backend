"""Validation and domain schemas for email notifications and communications."""

from __future__ import annotations

from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation
import hashlib
import json
import re
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class SendEmailRequest(BaseModel):
    """Request payload to dispatch an email notification."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    booking_intent_id: str = Field(min_length=1, max_length=100)
    template: Literal[
        "booking_confirmation",
        "revised_booking_confirmation",
        "special_request_acknowledgement",
        "staff_request_outcome",
        "one_time_code",
        "balance_reminder",
        "pre_arrival_reminder",
    ]
    recipient_email: Optional[str] = Field(default=None, max_length=254)
    recipient_name: Optional[str] = Field(default=None, max_length=120)
    otp_code: Optional[str] = Field(default=None, min_length=4, max_length=10)
    special_request_text: Optional[str] = Field(default=None, max_length=1000)
    idempotency_key: Optional[str] = Field(default=None, max_length=100)


class EmailDeliveryResult(BaseModel):
    """Response record detailing the outcome of an email dispatch operation."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    success: bool
    delivery_id: str
    template: str
    recipient: str
    hotel_notified: bool = False
    pdf_attached: bool = False
    subject: str
    status: Literal["sent", "queued", "simulated_sandbox", "failed"]
    sent_at: str
    error_message: Optional[str] = None
    idempotency_replayed: bool = False


class SpecialRequestCreate(BaseModel):
    """Special request submission by guest; does not alter room or golf inventory."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    booking_intent_id: str = Field(min_length=1, max_length=100)
    request_text: str = Field(min_length=5, max_length=1000)
    category: Literal["dietary", "room_preference", "accessibility", "other"] = "other"


# ==============================================================================
# Ticket N07: Structured Booking Confirmation Models
# ==============================================================================

ConfirmationDeliveryState = Literal[
    "blocked_dependency",
    "prepared",
    "sending",
    "recorded_test",
    "accepted_by_transport",
    "retryable_failure",
    "permanent_failure",
    "unknown",
    "stale_snapshot",
]


class SnapshotLeadGuest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    first_name: str = Field(min_length=1, max_length=100)
    last_name: str = Field(min_length=1, max_length=100)
    email: str = Field(min_length=3, max_length=254)
    telephone: Optional[str] = Field(default=None, max_length=50)

    @field_validator("first_name", "last_name", "email", "telephone")
    @classmethod
    def validate_header_safe_text(cls, value: Optional[str]) -> Optional[str]:
        if value is not None and any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError("contact fields cannot contain control characters")
        return value

    @field_validator("email")
    @classmethod
    def validate_email(cls, value: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,63}@[A-Za-z0-9](?:[A-Za-z0-9.-]{0,61}[A-Za-z0-9])?\.[A-Za-z]{2,63}", value):
            raise ValueError("email must contain one valid mailbox address")
        local, domain = value.rsplit("@", 1)
        if ".." in local or ".." in domain or local.endswith("."):
            raise ValueError("email mailbox is malformed")
        return value


class SnapshotRoom(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    room_group_id: str = Field(min_length=1, max_length=50)
    room_category: str = Field(min_length=1, max_length=50)
    room_quantity: int = Field(ge=1, le=10)
    occupant_names: List[str] = Field(default_factory=list, max_length=20)

    @field_validator("room_group_id", "room_category", "occupant_names")
    @classmethod
    def reject_control_characters(cls, value: Any) -> Any:
        values = value if isinstance(value, list) else [value]
        if any(
            isinstance(item, str) and any(ord(char) < 32 or ord(char) == 127 for char in item)
            for item in values
        ):
            raise ValueError("room details cannot contain control characters")
        return value


class SnapshotStay(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    arrival_date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    departure_date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    rooms: List[SnapshotRoom] = Field(min_length=1, max_length=10)

    @field_validator("departure_date")
    @classmethod
    def validate_dates(cls, v: str, info: Any) -> str:
        arrival = info.data.get("arrival_date")
        try:
            departure = date.fromisoformat(v)
            arrival_date = date.fromisoformat(arrival) if arrival else None
        except ValueError as exc:
            raise ValueError("stay dates must be valid calendar dates") from exc
        if arrival_date and departure <= arrival_date:
            raise ValueError("departure_date must be after arrival_date")
        return v

    @field_validator("arrival_date")
    @classmethod
    def validate_arrival_date(cls, value: str) -> str:
        try:
            date.fromisoformat(value)
        except ValueError as exc:
            raise ValueError("arrival_date must be a valid calendar date") from exc
        return value


class SnapshotGolfRound(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    round_key: str = Field(min_length=1, max_length=50)
    course_name: str = Field(min_length=1, max_length=100)
    date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    tee_time: str = Field(pattern=r"^\d{2}:\d{2}$")
    timezone: str = Field(default="Europe/Dublin", max_length=50)
    players: int = Field(ge=1, le=4)
    holes: int = Field(ge=9, le=18)
    provider_booking_ids: List[str] = Field(default_factory=list, max_length=10)
    golfer_names: List[str] = Field(default_factory=list, max_length=4)

    @field_validator("date")
    @classmethod
    def validate_round_date(cls, value: str) -> str:
        try:
            date.fromisoformat(value)
        except ValueError as exc:
            raise ValueError("golf round date must be a valid calendar date") from exc
        return value

    @field_validator("tee_time")
    @classmethod
    def validate_tee_time(cls, value: str) -> str:
        try:
            time.fromisoformat(value)
        except ValueError as exc:
            raise ValueError("tee_time must be a valid 24-hour time") from exc
        return value

    @field_validator("round_key", "course_name", "timezone", "golfer_names")
    @classmethod
    def reject_control_characters(cls, value: Any) -> Any:
        values = value if isinstance(value, list) else [value]
        if any(
            isinstance(item, str) and any(ord(char) < 32 or ord(char) == 127 for char in item)
            for item in values
        ):
            raise ValueError("golf details cannot contain control characters")
        return value


class SnapshotFinancials(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    currency: str = Field(pattern=r"^[A-Z]{3}$")
    total_amount: str = Field(pattern=r"^(?:0|[1-9]\d{0,9})\.\d{2}$")
    deposit_paid: str = Field(pattern=r"^(?:0|[1-9]\d{0,9})\.\d{2}$")
    verified_paid_total: str = Field(pattern=r"^(?:0|[1-9]\d{0,9})\.\d{2}$")
    balance_due: str = Field(pattern=r"^(?:0|[1-9]\d{0,9})\.\d{2}$")
    balance_due_date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")

    @model_validator(mode="after")
    def validate_financials(self) -> "SnapshotFinancials":
        total = Decimal(self.total_amount)
        deposit = Decimal(self.deposit_paid)
        paid = Decimal(self.verified_paid_total)
        balance = Decimal(self.balance_due)
        if paid > total:
            raise ValueError("verified_paid_total cannot exceed total_amount")
        if deposit > paid:
            raise ValueError("deposit_paid cannot exceed verified_paid_total")
        if balance != total - paid:
            raise ValueError("balance_due must equal total_amount minus verified_paid_total")
        try:
            date.fromisoformat(self.balance_due_date)
        except ValueError as exc:
            raise ValueError("balance_due_date must be a valid calendar date") from exc
        return self


class SnapshotPayment(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    payment_purpose: Literal["deposit", "balance", "full"]
    payment_id: str = Field(min_length=1, max_length=100)
    amount: str = Field(pattern=r"^(?:0|[1-9]\d{0,9})\.\d{2}$")
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    verified_at: str = Field(min_length=1, max_length=50)
    receipt_reference: Optional[str] = Field(default=None, max_length=100)

    @field_validator("verified_at")
    @classmethod
    def validate_verified_at(cls, value: str) -> str:
        try:
            timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("verified_at must be a valid timestamp") from exc
        if timestamp.tzinfo is None:
            raise ValueError("verified_at must include a time zone")
        return value

    @field_validator("amount")
    @classmethod
    def require_positive_payment(cls, value: str) -> str:
        if Decimal(value) <= Decimal("0.00"):
            raise ValueError("verified payment amount must be greater than zero")
        return value


class SnapshotContent(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    template_version: str = Field(default="v1.0-draft", max_length=50)
    pdf_layout_version: str = Field(default="v1.0-draft", max_length=50)
    policy_text_version: str = Field(default="v1.0-standard", max_length=50)
    policy_text: Optional[str] = Field(default=None, max_length=4000)


class BookingConfirmationSnapshot(BaseModel):
    """Immutable snapshot of confirmed booking facts for N07 rendering."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    booking_reference: str = Field(min_length=1, max_length=50, pattern=r"^[A-Z0-9][A-Z0-9-]*$")
    booking_revision: int = Field(ge=1)
    confirmed_at: str = Field(min_length=1, max_length=50)
    environment: Literal["development", "test", "staging", "production"]
    is_test: bool = False
    simulation_run_id: Optional[str] = Field(default=None, max_length=100)
    accepted_quote_hash: str = Field(pattern=r"^[A-Fa-f0-9]{64}$")
    rules_version: str = Field(min_length=1, max_length=50)
    terms_version: str = Field(min_length=1, max_length=50)

    lead_guest: SnapshotLeadGuest
    stay: SnapshotStay
    golf_rounds: List[SnapshotGolfRound] = Field(default_factory=list, max_length=60)
    financials: SnapshotFinancials
    payments: List[SnapshotPayment] = Field(default_factory=list, max_length=20)
    content: SnapshotContent = Field(default_factory=SnapshotContent)

    @model_validator(mode="after")
    def validate_snapshot_links_and_ledger(self) -> "BookingConfirmationSnapshot":
        try:
            confirmed_at = datetime.fromisoformat(self.confirmed_at.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("confirmed_at must be a valid timestamp") from exc
        if confirmed_at.tzinfo is None:
            raise ValueError("confirmed_at must include a time zone")
        if self.is_test and not self.simulation_run_id:
            raise ValueError("test snapshots require a simulation_run_id")

        arrival = date.fromisoformat(self.stay.arrival_date)
        departure = date.fromisoformat(self.stay.departure_date)
        if not self.golf_rounds:
            raise ValueError("a stay-and-play confirmation requires at least one verified golf round")
        for golf_round in self.golf_rounds:
            round_date = date.fromisoformat(golf_round.date)
            if not arrival <= round_date <= departure:
                raise ValueError("golf rounds must fall within the confirmed stay dates")

        receipt_refs: set[str] = set()
        payment_ids: set[str] = set()
        paid_total = Decimal("0.00")
        deposit_total = Decimal("0.00")
        for payment in self.payments:
            if payment.currency != self.financials.currency:
                raise ValueError("payment currency must match the booking currency")
            if not payment.receipt_reference:
                raise ValueError("verified payments require a receipt_reference")
            if payment.receipt_reference in receipt_refs:
                raise ValueError("payment receipt references must be unique")
            if payment.payment_id in payment_ids:
                raise ValueError("payment IDs must be unique")
            receipt_refs.add(payment.receipt_reference)
            payment_ids.add(payment.payment_id)
            amount = Decimal(payment.amount)
            paid_total += amount
            if payment.payment_purpose.strip().lower() == "deposit":
                deposit_total += amount

        if paid_total != Decimal(self.financials.verified_paid_total):
            raise ValueError("verified payments must sum to verified_paid_total")
        if deposit_total != Decimal(self.financials.deposit_paid):
            raise ValueError("deposit payments must sum to deposit_paid")
        if self.is_test and not self.content.policy_text:
            raise ValueError("test snapshots require the approved test policy text")
        return self

    def compute_hash(self) -> str:
        """Deterministic SHA-256 hash of canonical snapshot JSON."""
        dumped = self.model_dump(mode="json")
        canonical = json.dumps(dumped, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class ConfirmedEvent(BaseModel):
    """Canonical confirmed event emitted upon N06 booking completion."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    schema_version: Literal[1] = 1
    event_id: str = Field(min_length=1, max_length=100)
    event_type: Literal["booking.confirmed"] = "booking.confirmed"
    operation_id: str = Field(min_length=1, max_length=100)
    booking_reference: str = Field(min_length=1, max_length=50)
    booking_revision: int = Field(ge=1)
    occurred_at: str = Field(min_length=1, max_length=50)
    environment: Literal["development", "test", "staging", "production"]
    evidence_source: str = Field(min_length=1, max_length=50)
    is_test: bool = False
    simulation_run_id: Optional[str] = Field(default=None, max_length=100)
    snapshot_hash: str = Field(min_length=1, max_length=100)
    snapshot: BookingConfirmationSnapshot
    evidence_refs: Dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_event_snapshot_identity(self) -> "ConfirmedEvent":
        snapshot = self.snapshot
        if self.booking_reference != snapshot.booking_reference:
            raise ValueError("event and snapshot booking references must match")
        if self.booking_revision != snapshot.booking_revision:
            raise ValueError("event and snapshot booking revisions must match")
        if self.occurred_at != snapshot.confirmed_at:
            raise ValueError("event time must match the confirmed snapshot time")
        if self.environment != snapshot.environment:
            raise ValueError("event and snapshot environments must match")
        if self.is_test != snapshot.is_test:
            raise ValueError("event and snapshot test flags must match")
        if self.simulation_run_id != snapshot.simulation_run_id:
            raise ValueError("event and snapshot run IDs must match")
        if not re.fullmatch(r"[A-Fa-f0-9]{64}", self.snapshot_hash):
            raise ValueError("snapshot_hash must be a SHA-256 hex digest")
        return self


class RecipientDeliveryState(BaseModel):
    """Delivery record for a single fixed recipient role."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    recipient_role: Literal["guest", "hotel"]
    recipient_address: str = Field(min_length=3, max_length=254)
    message_id: str = Field(min_length=1, max_length=254)
    content_hash: str = Field(min_length=1, max_length=100)
    status: ConfirmationDeliveryState = "prepared"
    attempt_count: int = Field(default=0, ge=0)
    attempt_id: Optional[str] = Field(default=None, max_length=100)
    claimed_at: Optional[str] = None
    accepted_at: Optional[str] = None
    error_code: Optional[str] = None
    error_details: Optional[str] = None
    provider_reference: Optional[str] = None

    @field_validator("recipient_address")
    @classmethod
    def validate_test_recipient(cls, value: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,63}@[A-Za-z0-9](?:[A-Za-z0-9.-]{0,61}[A-Za-z0-9])?\.invalid", value):
            raise ValueError("confirmation recipient must be one .invalid test mailbox")
        local, domain = value.rsplit("@", 1)
        if ".." in local or ".." in domain or local.endswith("."):
            raise ValueError("confirmation recipient mailbox is malformed")
        return value


class ConfirmationAttemptAudit(BaseModel):
    """Bounded audit data for one recipient submission attempt."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    attempt_id: str = Field(min_length=1, max_length=100)
    recipient_role: Literal["guest", "hotel"]
    actor: str = Field(min_length=1, max_length=100)
    reason: Optional[str] = Field(default=None, max_length=500)
    previous_status: ConfirmationDeliveryState
    started_at: str = Field(min_length=1, max_length=50)


class BookingConfirmationRecord(BaseModel):
    """Saved state of N07 confirmation rendering and deliveries."""

    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    event_id: str = Field(min_length=1, max_length=100)
    operation_id: str = Field(min_length=1, max_length=100)
    booking_reference: str = Field(min_length=1, max_length=50)
    snapshot_hash: str = Field(min_length=1, max_length=100)
    overall_status: ConfirmationDeliveryState = "prepared"
    deliveries: Dict[str, RecipientDeliveryState] = Field(default_factory=dict)
    attempt_history: List[ConfirmationAttemptAudit] = Field(default_factory=list, max_length=6)
    pdf_hash: Optional[str] = None
    pdf_filename: Optional[str] = None
    pdf_bytes: Optional[bytes] = None
    created_at: str = Field(...)
    updated_at: str = Field(...)


class RecipientDeliveryStatusSummary(BaseModel):
    """Sanitized recipient status view for staff responses."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    recipient_role: Literal["guest", "hotel"]
    recipient_address_masked: str
    status: ConfirmationDeliveryState
    attempt_count: int
    attempt_id: Optional[str] = None
    claimed_at: Optional[str] = None
    accepted_at: Optional[str] = None
    error_code: Optional[str] = None


class StaffConfirmationStatusResponse(BaseModel):
    """Staff view of confirmation delivery progress."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    booking_reference: str
    operation_id: str
    is_test: bool
    event_id: str
    overall_status: ConfirmationDeliveryState
    guest_delivery: RecipientDeliveryStatusSummary
    hotel_delivery: RecipientDeliveryStatusSummary
    updated_at: str


class StaffConfirmationRetryRequest(BaseModel):
    """Staff request to retry delivery to one failed role."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    recipient_role: Literal["guest", "hotel"]
    expected_event_id: str = Field(min_length=1, max_length=100)
    expected_attempt_id: Optional[str] = Field(default=None, max_length=100)
    reason: str = Field(min_length=5, max_length=500)


class StaffConfirmationRetryResponse(BaseModel):
    """Response detailing staff retry execution."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    operation_id: str
    recipient_role: Literal["guest", "hotel"]
    attempt_id: str
    status: ConfirmationDeliveryState
    message: str
