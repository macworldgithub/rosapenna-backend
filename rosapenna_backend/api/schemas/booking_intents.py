"""Validation models for persisted website booking intents."""

from __future__ import annotations

import re
import uuid
from datetime import date as Date
from decimal import Decimal, InvalidOperation
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from rosapenna_backend.api.schemas.brs_booking import CaddyRequestPayload
from rosapenna_backend.integrations.brs.tee_sheet import COURSES

MAX_ACTIONS = 200
PRICE_PATTERN = r"^\d{1,8}(?:\.\d{1,2})?$"
PHONE_PATTERN = r"^[+\d\s().-]{6,40}$"

def _price(value: str) -> str:
    try:
        amount = Decimal(value)
    except (InvalidOperation, TypeError):
        raise ValueError("Invalid price") from None
    if not amount.is_finite() or amount < 0 or amount != amount.quantize(Decimal("0.01")):
        raise ValueError("Price must be a non-negative amount with at most two decimal places")
    return format(amount.quantize(Decimal("0.01")), ".2f")

class QuoteSelection(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    date: Date
    course_id: int = Field(gt=0, strict=True)
    tee_time_id: int = Field(gt=0, strict=True)
    green_fee_id: int = Field(gt=0, strict=True)
    time: str = Field(pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$", strict=True)
    holes: str = Field(min_length=1, max_length=40, strict=True)
    package_name: str = Field(min_length=1, max_length=120, strict=True)
    players: int = Field(ge=1, le=4, strict=True)
    price: str = Field(pattern=PRICE_PATTERN, max_length=11, strict=True)
    currency: str = Field(pattern=r"^[A-Z]{3}$", strict=True)

    @field_validator("price")
    @classmethod
    def normalize_price(cls, value: str) -> str:
        return _price(value)

    @field_validator("course_id")
    @classmethod
    def known_course(cls, value: int) -> int:
        if value not in COURSES:
            raise ValueError("Course is not configured for this checkout")
        return value

class LeadGuest(BaseModel):
    """Only the lead guest contact needed by this checkout is retained."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    first_name: str = Field(min_length=1, max_length=100, strict=True)
    last_name: str = Field(min_length=1, max_length=100, strict=True)
    email: str = Field(min_length=3, max_length=254, strict=True)
    telephone: str = Field(min_length=6, max_length=40, pattern=PHONE_PATTERN, strict=True)

    @field_validator("email")
    @classmethod
    def valid_email(cls, value: str) -> str:
        if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", value):
            raise ValueError("Enter a valid email address")
        return value.lower()

    @field_validator("telephone")
    @classmethod
    def valid_telephone(cls, value: str) -> str:
        if len(re.sub(r"\D", "", value)) < 6:
            raise ValueError("Enter a valid telephone number")
        return value

class BookingIntentCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    selection: QuoteSelection
    lead_guest: LeadGuest

class RevalidatedQuote(BaseModel):
    model_config = ConfigDict(extra="forbid")

    price: str = Field(pattern=PRICE_PATTERN, max_length=11, strict=True)
    currency: str = Field(pattern=r"^[A-Z]{3}$", strict=True)

    @field_validator("price")
    @classmethod
    def normalize_price(cls, value: str) -> str:
        return _price(value)


class OperaReservationPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    reservation_id: str = Field(min_length=1, max_length=100)
    confirmation_number: str | None = Field(default=None, max_length=100)
    hotel_id: str = Field(default="ROSA", max_length=50)
    room_category: str = Field(min_length=1, max_length=50)
    room_quantity: int = Field(default=1, ge=1, le=10)
    room_group_id: str | None = Field(default=None, max_length=100)
    arrival_date: Date
    departure_date: Date
    total_amount: str = Field(pattern=PRICE_PATTERN, max_length=11)
    currency: str = Field(default="EUR", pattern=r"^[A-Z]{3}$")
    status: str = Field(default="Reserved", max_length=50)

    @field_validator("total_amount")
    @classmethod
    def normalize_price(cls, value: str) -> str:
        return _price(value)


class BrsRoundPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    round_id: str = Field(min_length=1, max_length=50)
    course_id: int = Field(gt=0, strict=True)
    course_name: str = Field(min_length=1, max_length=100)
    date: Date
    time: str = Field(pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$", strict=True)
    holes: str = Field(default="18", max_length=40)
    players: int = Field(ge=1, le=4, strict=True)
    brs_booking_id: str | None = Field(default=None, max_length=100)
    booking_reference: str | None = Field(default=None, max_length=100)
    price: str = Field(pattern=PRICE_PATTERN, max_length=11)
    currency: str = Field(default="EUR", pattern=r"^[A-Z]{3}$")
    status: str = Field(default="Reserved", max_length=50)
    caddy_request: CaddyRequestPayload | None = None

    @field_validator("price")
    @classmethod
    def normalize_price(cls, value: str) -> str:
        return _price(value)


class PaymentRecordPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    payment_id: str = Field(min_length=1, max_length=100)
    provider: str = Field(min_length=1, max_length=50)
    payment_type: Literal["deposit", "balance", "full"]
    amount: str = Field(pattern=PRICE_PATTERN, max_length=11)
    currency: str = Field(default="EUR", pattern=r"^[A-Z]{3}$")
    status: Literal["pending", "completed", "failed", "refunded"]
    provider_reference: str | None = Field(default=None, max_length=150)

    @field_validator("amount")
    @classmethod
    def normalize_price(cls, value: str) -> str:
        return _price(value)


IntentActionName = Literal[
    "live_revalidation_passed",
    "live_revalidation_failed",
    "live_revalidation_unavailable",
    "checkout_preparation_started",
    "checkout_preparation_response_received",
    "checkout_preparation_failed",
    "checkout_preparation_blocked",
    "checkout_preparation_outcome_unknown",
    "link_opera_reservation",
    "link_brs_round",
    "record_payment",
    "confirm_booking",
    "mark_recovery_required",
]

class BookingIntentAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action_id: uuid.UUID
    expected_revision: int = Field(ge=1, strict=True)
    action: IntentActionName
    quote: RevalidatedQuote | None = None
    opera_payload: OperaReservationPayload | None = None
    brs_payload: BrsRoundPayload | None = None
    payment_payload: PaymentRecordPayload | None = None
    status_code: int | None = Field(default=None, ge=100, le=599, strict=True)
    detail_code: Literal[
        "quote_or_availability_changed",
        "backend_unavailable",
        "local_cooldown",
        "configuration_missing",
        "upstream_response_error",
        "upstream_outcome_unknown",
    ] | None = None

    @model_validator(mode="after")
    def validate_action_evidence(self):
        if self.action == "live_revalidation_passed":
            if self.quote is None or self.status_code is not None or self.detail_code is not None:
                raise ValueError("Successful revalidation requires only quote evidence")
        elif self.action == "live_revalidation_failed":
            if self.status_code != 409 or self.quote is not None or self.detail_code != "quote_or_availability_changed":
                raise ValueError("A revalidation conflict requires HTTP 409 evidence")
        elif self.action == "live_revalidation_unavailable":
            if self.status_code is None or self.status_code == 409 or self.quote is not None or self.detail_code != "backend_unavailable":
                raise ValueError("An unavailable revalidation requires its HTTP response status")
        elif self.action == "checkout_preparation_started":
            if any(value is not None for value in (self.quote, self.status_code, self.detail_code)):
                raise ValueError("Checkout start does not accept outcome evidence")
        elif self.action == "checkout_preparation_response_received":
            if self.status_code is None or not 200 <= self.status_code < 300 or self.quote is not None or self.detail_code is not None:
                raise ValueError("A preparation response requires a successful HTTP status")
        elif self.action == "checkout_preparation_failed":
            if self.status_code is None or 200 <= self.status_code < 300 or self.quote is not None or self.detail_code != "upstream_response_error":
                raise ValueError("A preparation failure requires a non-success response status")
        elif self.action == "checkout_preparation_blocked":
            local_cooldown = self.status_code == 429 and self.detail_code == "local_cooldown"
            missing_configuration = self.status_code == 503 and self.detail_code == "configuration_missing"
            if not (local_cooldown or missing_configuration) or self.quote is not None:
                raise ValueError("A locally blocked request requires cooldown or configuration evidence")
        elif self.action == "checkout_preparation_outcome_unknown":
            if self.status_code is not None or self.quote is not None or self.detail_code != "upstream_outcome_unknown":
                raise ValueError("An unknown outcome must not claim an upstream response")
        elif self.action == "link_opera_reservation":
            if self.opera_payload is None:
                raise ValueError("link_opera_reservation requires opera_payload")
        elif self.action == "link_brs_round":
            if self.brs_payload is None:
                raise ValueError("link_brs_round requires brs_payload")
        elif self.action == "record_payment":
            if self.payment_payload is None:
                raise ValueError("record_payment requires payment_payload")
        return self
