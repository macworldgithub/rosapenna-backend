"""Domain and validation schemas for Stay & Play package pricing, eligibility, and rules."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from rosapenna_backend.integrations.brs.tee_sheet import COURSES

PRICE_PATTERN = r"^\d{1,8}(?:\.\d{1,2})?$"


def _price(value: str) -> str:
    try:
        amount = Decimal(value)
    except (InvalidOperation, TypeError):
        raise ValueError("Invalid price amount") from None
    if not amount.is_finite() or amount < 0 or amount != amount.quantize(Decimal("0.01")):
        raise ValueError("Price must be a non-negative amount with at most two decimal places")
    return format(amount.quantize(Decimal("0.01")), ".2f")


class RoomSelectionQuoteRequest(BaseModel):
    """Room category selection for a package quote."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    room_category: str = Field(min_length=1, max_length=50)
    room_quantity: int = Field(default=1, ge=1, le=10)
    adults: int = Field(default=2, ge=1, le=20)
    children: int = Field(default=0, ge=0, le=10)


class GolfRoundQuoteRequest(BaseModel):
    """Golf round selection for a package quote."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    course_id: int = Field(gt=0, strict=True)
    date: dt.date
    time: str = Field(pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$", strict=True)
    players: int = Field(ge=1, le=4, strict=True)

    @field_validator("course_id")
    @classmethod
    def validate_course(cls, value: int) -> int:
        if value not in COURSES:
            raise ValueError(f"Unsupported course_id {value}. Supported: {list(COURSES.keys())}")
        return value


class PackageQuoteRequest(BaseModel):
    """Request payload to calculate a live, server-verified package quote."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    arrival_date: dt.date
    departure_date: dt.date
    rooms: List[RoomSelectionQuoteRequest] = Field(default_factory=list)
    golf_rounds: List[GolfRoundQuoteRequest] = Field(default_factory=list)
    existing_booking_intent_id: Optional[str] = Field(default=None, max_length=100)

    @model_validator(mode="after")
    def validate_stay_dates(self):
        if self.departure_date <= self.arrival_date:
            raise ValueError("departure_date must be strictly after arrival_date")
        return self


class ItemizedRoomQuote(BaseModel):
    """Itemized room pricing line item verified from Opera Cloud."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    room_category: str
    room_quantity: int
    nights: int
    rate_per_night: str
    subtotal: str
    rate_plan_code: str
    breakfast_included: bool = True
    available_rooms: int

    @field_validator("rate_per_night", "subtotal")
    @classmethod
    def normalize_prices(cls, value: str) -> str:
        return _price(value)


class ItemizedGolfRoundQuote(BaseModel):
    """Itemized golf green fee pricing line item verified from BRS Golf."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    course_id: int
    course_name: str
    date: dt.date
    time: str
    players: int
    rate_per_player: str
    subtotal: str
    available_slots: int

    @field_validator("rate_per_player", "subtotal")
    @classmethod
    def normalize_prices(cls, value: str) -> str:
        return _price(value)


class AmendmentPaymentSummary(BaseModel):
    """Summary of payments and price differences for booking amendments."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    current_total: str
    revised_total: str
    verified_paid: str
    amount_difference: str
    balance_due: str
    credit_amount: str = "0.00"

    @field_validator("current_total", "revised_total", "verified_paid", "balance_due", "credit_amount")
    @classmethod
    def normalize_prices(cls, value: str) -> str:
        return _price(value)


class PackageQuoteResponse(BaseModel):
    """Itemized, versioned quote response returned by the pricing engine."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    quote_id: str
    status: Literal["available", "ineligible", "unavailable"]
    rules_version: str
    arrival_date: dt.date
    departure_date: dt.date
    nights: int
    currency: str = "EUR"
    itemized_rooms: List[ItemizedRoomQuote] = Field(default_factory=list)
    itemized_golf: List[ItemizedGolfRoundQuote] = Field(default_factory=list)
    total_rooms_price: str = "0.00"
    total_golf_price: str = "0.00"
    total_package_price: str = "0.00"
    deposit_amount: str = "0.00"
    balance_amount: str = "0.00"
    balance_due_date: dt.date
    amendment_summary: Optional[AmendmentPaymentSummary] = None
    reserves_inventory: bool = False
    freshness_ttl_seconds: int = 120
    created_at: str
    errors: List[str] = Field(default_factory=list)


class MinimumRoundsRule(BaseModel):
    """Rule specifying minimum required golf rounds for a given stay length."""

    model_config = ConfigDict(extra="forbid")

    minimum_nights: int = Field(ge=1)
    maximum_nights: int = Field(ge=1)
    rounds: int = Field(ge=1)


class PackageRulesConfig(BaseModel):
    """Versioned package configuration governing caps, buffers, and stay rules."""

    model_config = ConfigDict(extra="forbid")

    version: str = "demo-2026-10-14-v1"
    effective_date: str = "2026-10-14"
    total_hotel_rooms: int = 70
    bookable_cap: int = 65
    category_buffers: Dict[str, int] = Field(
        default_factory=lambda: {
            "CLASSIC": 1,
            "DELUXE": 1,
            "BAYVIEW_SUITE": 1,
            "PENTHOUSE": 0,
        }
    )
    deposit_percent: int = 25
    balance_percent: int = 75
    balance_due_weeks: int = 6
    minimum_rounds: List[MinimumRoundsRule] = Field(
        default_factory=lambda: [
            MinimumRoundsRule(minimum_nights=2, maximum_nights=3, rounds=2),
            MinimumRoundsRule(minimum_nights=4, maximum_nights=5, rounds=3),
        ]
    )
    closures: List[Dict[str, Any]] = Field(default_factory=list)


class UpdatePackageRulesRequest(BaseModel):
    """Authorized staff request to update package configuration parameters."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    actor: str = Field(min_length=1, max_length=100)
    reason: str = Field(min_length=1, max_length=250)
    effective_date: dt.date
    bookable_cap: Optional[int] = Field(default=None, ge=1, le=70)
    category_buffers: Optional[Dict[str, int]] = None
    deposit_percent: Optional[int] = Field(default=None, ge=0, le=100)
    balance_due_weeks: Optional[int] = Field(default=None, ge=1, le=12)
