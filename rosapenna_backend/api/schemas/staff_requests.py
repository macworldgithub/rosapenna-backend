"""Pydantic validation schemas for Ticket M14 — Staff Request Outcomes."""

from __future__ import annotations

import datetime as dt
from typing import Any, Dict, List, Literal, Optional
from pydantic import BaseModel, ConfigDict, Field


StaffRequestOutcomeStatus = Literal[
    "needs_information",
    "rejected",
    "approved_awaiting_implementation",
    "completed",
]

StaffRequestExecutionType = Literal[
    "informational_only",
    "f14_operation",
    "vendor_readback_verified",
]


class StaffRequestHistoryEntry(BaseModel):
    """Immutable audit entry for a staff request status transition."""

    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    history_id: str
    actor: str
    timestamp: dt.datetime
    prior_status: str
    new_status: str
    reason: str
    internal_notes: Optional[str] = None
    guest_message: Optional[str] = None
    execution_type: Optional[str] = None
    f14_operation_id: Optional[str] = None
    vendor_readback_ref: Optional[str] = None
    cancellation_authorization_ref: Optional[str] = None
    f12_delivery_id: Optional[str] = None
    f12_delivery_status: Optional[str] = None


class StaffRequestTransitionCreate(BaseModel):
    """Staff request to transition status of a guest pending request."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    target_status: StaffRequestOutcomeStatus = Field(
        description="Target outcome status (needs_information, rejected, approved_awaiting_implementation, completed)"
    )
    reason: str = Field(
        min_length=5,
        max_length=500,
        description="Mandatory staff reason for this outcome transition",
    )
    internal_notes: Optional[str] = Field(
        default=None,
        max_length=1000,
        description="Optional staff-internal notes not shown to the guest",
    )
    guest_message: Optional[str] = Field(
        default=None,
        max_length=1000,
        description="Optional message forwarded to guest in the F12 outcome notification",
    )
    execution_type: Optional[StaffRequestExecutionType] = Field(
        default=None,
        description="For 'completed' status: whether this was informational or verified via F14 / vendor readback",
    )
    f14_operation_id: Optional[str] = Field(
        default=None,
        max_length=100,
        description="F14 operation ID if completed via coordinated booking amendment writes",
    )
    vendor_readback_ref: Optional[str] = Field(
        default=None,
        max_length=200,
        description="External Opera or BRS reservation readback confirmation identifier",
    )
    cancellation_authorization_ref: Optional[str] = Field(
        default=None,
        max_length=200,
        description="Mandatory signed authorization reference if marking a cancellation inquiry completed",
    )
    expected_revision: Optional[int] = Field(
        default=None,
        ge=1,
        description="Booking linkage revision guard for optimistic concurrency control",
    )


class StaffRequestItem(BaseModel):
    """Detailed view of a guest request for authorized staff review."""

    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    request_id: str
    booking_reference: str
    request_type: str
    category: str
    request_text: str
    desired_dates: Optional[str] = None
    desired_room_category: Optional[str] = None
    desired_golf_notes: Optional[str] = None
    status: str
    submitted_at: dt.datetime
    last_updated_at: Optional[dt.datetime] = None
    lead_guest_name: Optional[str] = None
    lead_guest_email: Optional[str] = None
    notes: Optional[str] = None
    history: List[StaffRequestHistoryEntry] = Field(default_factory=list)
    f12_delivery_id: Optional[str] = None
    f12_delivery_status: Optional[str] = None
    booking_revision: int = 1


class StaffRequestTransitionResponse(BaseModel):
    """Response confirming durable recording of a staff outcome transition."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    request_id: str
    booking_reference: str
    prior_status: str
    new_status: str
    transition_recorded_at: dt.datetime
    booking_revision: int
    f12_delivery_status: str
    f12_delivery_id: Optional[str] = None
    replayed: bool = False
    message: str
    notice: str = (
        "Staff request outcome recorded with immutable audit history. "
        "Approval alone does not claim completed; implementation requires verified vendor readback."
    )


class StaffRequestListResponse(BaseModel):
    """List of requests across bookings for staff dashboard."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    total: int
    requests: List[StaffRequestItem]
    read_at: dt.datetime
