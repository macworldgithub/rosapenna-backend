"""Pydantic validation schemas for Ticket M13 — Special and Cancellation Requests."""

from __future__ import annotations

import datetime as dt
from typing import List, Literal, Optional
from pydantic import BaseModel, ConfigDict, Field


SpecialRequestCategory = Literal[
    "dietary",
    "room_preference",
    "golf_preference",
    "accessibility",
    "cancellation_inquiry",
    "general_inquiry",
    "other",
]

RequestType = Literal["special_request", "cancellation_request"]

RequestReviewStatus = Literal[
    "pending_review",
    "under_review",
    "needs_information",
    "rejected",
    "approved_awaiting_implementation",
    "completed",
    "approved",
    "declined",
    "cancelled",
]


class GuestSpecialRequestCreate(BaseModel):
    """Guest submission for a special request or cancellation inquiry."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    booking_reference: str = Field(min_length=5, max_length=40)
    request_type: RequestType = Field(
        default="special_request",
        description="Whether this is an inquiry/special request or an explicit cancellation inquiry",
    )
    category: SpecialRequestCategory = Field(
        default="general_inquiry",
        description="Subject classification of the request",
    )
    request_text: str = Field(
        min_length=5,
        max_length=1500,
        description="Guest's free-text notes or inquiry details",
    )
    desired_dates: Optional[str] = Field(
        default=None,
        max_length=100,
        description="Optional preferred arrival/departure shift if live options did not fit",
    )
    desired_room_category: Optional[str] = Field(
        default=None,
        max_length=100,
        description="Optional preferred room category preference",
    )
    desired_golf_notes: Optional[str] = Field(
        default=None,
        max_length=200,
        description="Optional preferred course or tee-time preference",
    )
    expected_revision: Optional[int] = Field(
        default=None,
        ge=1,
        description="Optional booking linkage revision for optimistic concurrency guard",
    )


class GuestSpecialRequestItem(BaseModel):
    """Item record of a stored guest request."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    request_id: str = Field(description="Unique durable request ID")
    booking_reference: str = Field(description="Booking reference")
    request_type: RequestType
    category: SpecialRequestCategory
    request_text: str
    desired_dates: Optional[str] = None
    desired_room_category: Optional[str] = None
    desired_golf_notes: Optional[str] = None
    status: RequestReviewStatus = "pending_review"
    submitted_at: dt.datetime
    email_delivery_id: Optional[str] = None
    email_delivery_status: Optional[str] = None


class GuestSpecialRequestResponse(BaseModel):
    """Authoritative response confirming durable storage of a special or cancellation request."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    request_id: str
    booking_reference: str
    request_type: RequestType
    category: SpecialRequestCategory
    status: RequestReviewStatus = "pending_review"
    submitted_at: dt.datetime
    booking_revision: int
    replayed: bool = False
    inventory_mutated: bool = False
    payment_mutated: bool = False
    email_delivery_status: str
    email_delivery_id: Optional[str] = None
    message: str
    notice: str = (
        "Your request has been recorded and is pending hotel reservations staff review. "
        "Confirmed stay arrangements, tee times, inventory, and payment ledger entries remain completely unchanged."
    )


class GuestRequestsListResponse(BaseModel):
    """List of all submitted requests for an authenticated booking."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    booking_reference: str
    booking_revision: int
    requests: List[GuestSpecialRequestItem]
    read_at: dt.datetime
