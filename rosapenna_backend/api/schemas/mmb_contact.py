"""Pydantic schemas for Ticket M11 — Guest Phone Updates."""

from __future__ import annotations

import datetime as dt
import re
from typing import Literal, Optional
from pydantic import BaseModel, ConfigDict, Field, field_validator

# Allowed phone pattern: 6-30 chars, optional leading +, digits, spaces, hyphens, dots, parentheses
PHONE_REGEX = re.compile(r"^\+?[0-9\s().-]{6,30}$")


def normalize_phone_number(raw_value: str) -> str:
    """Normalize and validate phone numbers according to approved policy.

    Rejects invalid characters, control codes, and malformed inputs.
    Preserves international leading '+' and strips duplicate spacing/dashes.
    Does not guess country code if omitted.
    """
    if not raw_value or not isinstance(raw_value, str):
        raise ValueError("A non-empty phone number string is required.")

    stripped = raw_value.strip()
    if not PHONE_REGEX.match(stripped):
        raise ValueError(
            "Please enter a valid telephone number (e.g. +353 74 915 5000 or 074 915 5000)."
        )

    # Check digit count: standard E.164 / national telephony requires between 6 and 15 digits
    digits_only = re.sub(r"\D", "", stripped)
    if len(digits_only) < 6 or len(digits_only) > 16:
        raise ValueError("Telephone number must contain between 6 and 16 digits.")

    # Format cleanly: if starts with '+', keep '+', collapse multiple whitespace into single space
    cleaned = re.sub(r"[\s.-]+", " ", stripped).strip()
    return cleaned


def mask_phone_number(phone: Optional[str]) -> str:
    """Mask phone number for safe routine logging and public/read responses."""
    if not phone:
        return ""
    digits = re.sub(r"\D", "", phone)
    if len(digits) <= 4:
        return "***" + digits
    visible_tail = digits[-4:]
    return f"+...{visible_tail}"


class GuestPhoneUpdateRequest(BaseModel):
    """Guest request payload to update telephone contact on existing reservation."""
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    phone_number: str = Field(
        min_length=6,
        max_length=30,
        description="Updated telephone number in approved international or national format.",
    )
    expected_revision: int = Field(
        ge=1,
        description="Current booking linkage revision for optimistic concurrency protection.",
    )
    contact_version: Optional[str] = Field(
        default=None,
        max_length=100,
        description="Opaque version token of the contact snapshot being updated.",
    )

    @field_validator("phone_number")
    @classmethod
    def validate_phone(cls, v: str) -> str:
        return normalize_phone_number(v)


class GuestPhoneUpdateResponse(BaseModel):
    """Authoritative result of an authenticated lead guest phone update."""
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    operation_id: str
    booking_reference: str
    phone_number: str
    masked_phone: str
    booking_revision: int
    contact_version: str
    status: Literal["confirmed", "reconciliation_required", "conflict", "rejected"]
    verified_via_readback: bool = True
    staff_review_required: bool = False
    replayed: bool = False
    message: str
    updated_at: dt.datetime


class GuestPhoneReadResponse(BaseModel):
    """Authoritative snapshot of the lead guest contact telephone."""
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    booking_reference: str
    lead_guest_name: str
    phone_number: Optional[str] = None
    masked_phone: Optional[str] = None
    booking_revision: int
    contact_version: str
    source_status: Literal["confirmed", "unavailable", "pending_staff_review"] = "confirmed"
    read_at: dt.datetime
