"""Validation and domain schemas for email notifications and communications."""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


class SendEmailRequest(BaseModel):
    """Request payload to dispatch an email notification."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    booking_intent_id: str = Field(min_length=1, max_length=100)
    template: Literal[
        "booking_confirmation",
        "special_request_acknowledgement",
        "one_time_code",
        "balance_reminder",
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
