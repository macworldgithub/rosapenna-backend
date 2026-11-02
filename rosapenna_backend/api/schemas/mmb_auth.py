"""Pydantic schemas for Ticket M03 — Booking Lookup and One-Time Security Code (OTP)."""

from __future__ import annotations

import datetime as dt
from typing import Optional
from pydantic import BaseModel, ConfigDict, Field


BOOKING_REF_PATTERN = r"^[A-Za-z0-9\-]{5,40}$"
OTP_CODE_PATTERN = r"^\d{6}$"


class RequestOtpRequest(BaseModel):
    """Guest request to generate and dispatch a one-time verification code."""
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    booking_reference: str = Field(
        min_length=5,
        max_length=40,
        pattern=BOOKING_REF_PATTERN,
        description="Canonical booking reference (e.g. RSP-20270514-XXXXXX).",
    )
    last_name: str = Field(
        min_length=1,
        max_length=100,
        description="Lead guest surname as recorded on the reservation.",
    )


class RequestOtpResponse(BaseModel):
    """Generic response disclosing zero existence or contact details to prevent enumeration."""
    model_config = ConfigDict(extra="forbid")

    success: bool = True
    message: str = (
        "If the booking details match our records, a one-time verification code "
        "has been sent to the email address on file."
    )
    cooldown_seconds: int = Field(
        default=60,
        description="Minimum seconds before another code request is accepted for this booking.",
    )


class VerifyOtpRequest(BaseModel):
    """Guest submission of the 6-digit one-time verification code."""
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    booking_reference: str = Field(
        min_length=5,
        max_length=40,
        pattern=BOOKING_REF_PATTERN,
        description="Canonical booking reference.",
    )
    code: str = Field(
        min_length=6,
        max_length=6,
        pattern=OTP_CODE_PATTERN,
        description="6-digit numeric verification code.",
    )


class MmbSessionResponse(BaseModel):
    """Authenticated guest session token scoped strictly to the verified booking."""
    model_config = ConfigDict(extra="forbid")

    session_token: str = Field(description="Opaque, high-entropy bearer token for MMB requests.")
    booking_reference: str = Field(description="Booking reference authorized for this session.")
    token_type: str = "bearer"
    expires_at: dt.datetime = Field(description="UTC timestamp when this session token expires.")


class MmbLogoutResponse(BaseModel):
    """Confirmation of session revocation."""
    model_config = ConfigDict(extra="forbid")

    success: bool = True
    message: str = "You have been successfully logged out."


class GuestSessionPrincipal(BaseModel):
    """Server-side principal resolved from an authenticated guest session."""
    model_config = ConfigDict(extra="forbid")

    session_token: str
    booking_reference: str
    lead_guest_email: str
    expires_at: dt.datetime
