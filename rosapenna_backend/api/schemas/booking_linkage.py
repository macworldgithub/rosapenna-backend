"""Pydantic schemas for Ticket M02 — Online and Offline Booking Linkage."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Literal, Optional
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


PRICE_PATTERN = r"^\d{1,8}(?:\.\d{1,2})?$"
PHONE_PATTERN = r"^[+\d\s().-]{6,40}$"


def _price(value: str | float | int) -> str:
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, TypeError):
        raise ValueError("Invalid price value") from None
    if not amount.is_finite() or amount < 0 or amount != amount.quantize(Decimal("0.01")):
        raise ValueError("Price must be a non-negative amount with at most two decimal places")
    return format(amount.quantize(Decimal("0.01")), ".2f")


class LeadGuestContact(BaseModel):
    """Lead guest contact details."""
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    first_name: str = Field(min_length=1, max_length=100)
    last_name: str = Field(min_length=1, max_length=100)
    email: str = Field(min_length=3, max_length=254)
    telephone: str = Field(min_length=6, max_length=40, pattern=PHONE_PATTERN)

    @field_validator("email")
    @classmethod
    def normalize_email(cls, v: str) -> str:
        clean = v.strip().lower()
        if "@" not in clean or "." not in clean:
            raise ValueError("Enter a valid email address")
        return clean


class OperaStayDetails(BaseModel):
    """Authoritative Opera hotel stay details."""
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    reservation_id: str = Field(min_length=1, max_length=100)
    confirmation_number: Optional[str] = Field(default=None, max_length=100)
    hotel_id: str = Field(default="ROSA", max_length=50)
    room_category: str = Field(min_length=1, max_length=50)
    room_quantity: int = Field(default=1, ge=1, le=10)
    arrival_date: dt.date
    departure_date: dt.date
    total_amount: str = Field(pattern=PRICE_PATTERN, max_length=11)
    currency: str = Field(default="EUR", pattern=r"^[A-Z]{3}$")
    status: str = Field(default="Reserved", max_length=50)
    occupant_names: List[str] = Field(default_factory=list)
    assigned_room_number: Optional[str] = Field(default=None, max_length=50)

    @field_validator("total_amount")
    @classmethod
    def normalize_total_amount(cls, v: str) -> str:
        return _price(v)

    @model_validator(mode="after")
    def validate_dates(self) -> "OperaStayDetails":
        if self.departure_date <= self.arrival_date:
            raise ValueError("departure_date must be after arrival_date.")
        return self


class BrsRoundDetails(BaseModel):
    """Authoritative BRS golf round details."""
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    round_id: str = Field(min_length=1, max_length=100)
    course_id: int = Field(gt=0)
    course_name: str = Field(min_length=1, max_length=100)
    date: dt.date
    time: str = Field(pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    holes: str = Field(default="18", max_length=40)
    players: int = Field(ge=1, le=4)
    brs_booking_id: Optional[str] = Field(default=None, max_length=100)
    price: str = Field(pattern=PRICE_PATTERN, max_length=11)
    currency: str = Field(default="EUR", pattern=r"^[A-Z]{3}$")
    status: str = Field(default="Reserved", max_length=50)
    caddy_request_status: Optional[str] = Field(default=None, max_length=100)

    @field_validator("price")
    @classmethod
    def normalize_round_price(cls, v: str) -> str:
        return _price(v)


class PaymentLedgerEntry(BaseModel):
    """Verified payment transaction in the booking ledger."""
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    payment_id: str = Field(min_length=1, max_length=100)
    provider: str = Field(min_length=1, max_length=50)
    payment_type: Literal["deposit", "balance", "full", "amendment_balance", "credit"]
    amount: str = Field(pattern=PRICE_PATTERN, max_length=11)
    currency: str = Field(default="EUR", pattern=r"^[A-Z]{3}$")
    status: Literal["pending", "completed", "failed", "refunded", "paid_unconfirmed"]
    provider_reference: Optional[str] = Field(default=None, max_length=150)
    verified_at: Optional[dt.datetime] = None

    @field_validator("amount")
    @classmethod
    def normalize_payment_amount(cls, v: str) -> str:
        return _price(v)


LinkageStatus = Literal["linked", "missing_components", "conflicting", "duplicate_references"]


class StaffLinkBookingRequest(BaseModel):
    """Request payload for staff to create or associate an existing booking."""
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    booking_reference: Optional[str] = Field(
        default=None,
        pattern=r"^RSP-\d{8}-[A-Z0-9]{4,10}$",
        description="Optional existing canonical booking reference; generated if omitted.",
    )
    source_channel: Literal["online", "phone", "email"]
    lead_guest: LeadGuestContact
    opera_identifier: Optional[str] = Field(
        default=None,
        max_length=100,
        description="Opera reservation ID or confirmation number.",
    )
    manual_opera_stay: Optional[OperaStayDetails] = None
    brs_rounds: List[BrsRoundDetails] = Field(default_factory=list)
    payments: List[PaymentLedgerEntry] = Field(default_factory=list)
    reason: str = Field(min_length=5, max_length=500, description="Mandatory reason for staff association or import.")


class StaffCorrectLinkageRequest(BaseModel):
    """Staff request to correct components of an existing linkage with audit justification."""
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    reason: str = Field(min_length=5, max_length=500, description="Mandatory explanation for linkage correction.")
    lead_guest: Optional[LeadGuestContact] = None
    opera_identifier: Optional[str] = Field(default=None, max_length=100)
    manual_opera_stay: Optional[OperaStayDetails] = None
    brs_rounds: Optional[List[BrsRoundDetails]] = None
    payments: Optional[List[PaymentLedgerEntry]] = None


class LinkageAuditTrailEntry(BaseModel):
    """Immutable audit trail of linkage actions and corrections."""
    model_config = ConfigDict(extra="forbid")

    audit_id: str
    actor: str
    action: str
    occurred_at: dt.datetime
    reason: str
    before_state: Dict[str, Any]
    after_state: Dict[str, Any]


class BookingLinkageResponse(BaseModel):
    """Detailed staff view of an online or offline linked booking record."""
    model_config = ConfigDict(extra="ignore")

    booking_reference: str
    source_channel: Literal["online", "phone", "email"]
    status: LinkageStatus
    discrepancies: List[str] = Field(default_factory=list)
    lead_guest: LeadGuestContact
    opera_stay: Optional[OperaStayDetails] = None
    brs_rounds: List[BrsRoundDetails] = Field(default_factory=list)
    payments: List[PaymentLedgerEntry] = Field(default_factory=list)
    financials: Dict[str, Any] = Field(default_factory=dict)
    revision: int = 1
    created_at: dt.datetime
    updated_at: dt.datetime
    audit_trail: List[LinkageAuditTrailEntry] = Field(default_factory=list)


class GuestBookingLookupRequest(BaseModel):
    """Public lookup request for guest portal access."""
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    booking_reference: str = Field(
        min_length=5,
        max_length=40,
        pattern=r"^[A-Za-z0-9\-]+$",
        description="Booking reference provided in the confirmation email.",
    )
    last_name: str = Field(min_length=1, max_length=100, description="Lead guest surname.")


class GuestStaySummary(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    arrival_date: dt.date
    departure_date: dt.date
    nights: int
    room_category: str
    room_quantity: int
    assigned_room_number: Optional[str] = None  # None indicates "Assigned upon check-in"
    occupant_names: List[str] = Field(default_factory=list)
    meal_plan: str = "Breakfast Included Daily"
    source_status: Literal["confirmed", "unavailable", "stale"] = "confirmed"
    last_synced_at: Optional[dt.datetime] = None


class GuestGolfRoundSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    course_name: str
    date: dt.date
    time: str
    holes: str = "18"
    players: int
    caddy_request_status: Optional[str] = None
    source_status: Literal["confirmed", "unavailable", "stale"] = "confirmed"


class GuestFinancialSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    total_amount: str
    deposit_paid: str
    balance_due: str
    currency: str = "EUR"
    payment_status: str
    balance_due_date: Optional[dt.date] = None
    ledger_verified: bool = True
    payments_count: int = 0
    credit_amount: Optional[str] = "0.00"


class GuestPendingRequestSummary(BaseModel):
    """Guest amendment or inquiry request held separately from confirmed itinerary."""
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    request_id: str
    request_type: str
    description: str
    status: Literal["pending_review", "under_review", "approved", "declined"] = "pending_review"
    submitted_at: dt.datetime
    notes: Optional[str] = None


class ComponentSourceHealth(BaseModel):
    """Authoritative source connectivity and freshness status."""
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    status: Literal["confirmed", "unavailable", "stale", "partial"]
    message: str
    last_synced_at: Optional[dt.datetime] = None


class GuestSafeBookingSummary(BaseModel):
    """Safe, guest-facing booking summary with zero vendor IDs or internal database IDs exposed."""
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    booking_reference: str
    source_channel: str
    lead_guest_name: str
    stay: Optional[GuestStaySummary] = None
    golf_rounds: List[GuestGolfRoundSummary] = Field(default_factory=list)
    financials: GuestFinancialSummary
    pending_requests: List[GuestPendingRequestSummary] = Field(default_factory=list)
    sources_health: Dict[str, ComponentSourceHealth] = Field(default_factory=dict)
    data_freshness_timestamp: dt.datetime
    status: str
