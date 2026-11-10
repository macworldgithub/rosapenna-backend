"""Validation and domain schemas for Ticket M16 \u2014 Action History and Recovery View.

Supports privacy-filtered guest action timelines, authoritative staff audit history,
exception tracking (paid-but-unconfirmed, partial writes, notification failures),
and safe operation engine recovery retries.
"""

from __future__ import annotations

import datetime as dt
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


TimelineEventCategory = Literal[
    "booking_created",
    "amendment_offer",
    "amendment_consent",
    "amendment_commit",
    "occupant_update",
    "contact_update",
    "caddy_update",
    "guest_request",
    "staff_request_outcome",
    "payment_recorded",
    "reminder_scheduled",
    "reminder_dispatched",
    "confirmation_sent",
    "exception_alert",
    "recovery_action",
]


class TimelineEvent(BaseModel):
    """A single time-ordered event in a booking's action history."""

    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    event_id: str = Field(description="Unique identifier for the event")
    timestamp: dt.datetime
    category: TimelineEventCategory
    summary: str
    details: Optional[str] = None
    booking_revision: Optional[int] = None
    operation_id: Optional[str] = None
    actor_type: Literal["guest", "staff", "system"] = "guest"
    actor_label: str = Field(
        default="Guest",
        description="Privacy-safe actor label (e.g. 'Lead Guest', 'Rosapenna Reservations Staff', 'Automated System')",
    )
    status: Optional[str] = None
    vendor_readback_verified: Optional[bool] = None
    is_exception: bool = False
    metadata: Dict[str, Any] = Field(default_factory=dict)


class GuestActionHistoryResponse(BaseModel):
    """Privacy-filtered time-ordered action history response for authenticated guests."""

    model_config = ConfigDict(extra="ignore")

    booking_reference: str
    current_revision: int
    lead_guest_name: str
    arrival_date: Optional[str] = None
    events: List[TimelineEvent]
    generated_at: dt.datetime


class StaffActionHistoryResponse(BaseModel):
    """Full-fidelity time-ordered action history and audit log for authorized staff."""

    model_config = ConfigDict(extra="ignore")

    booking_reference: str
    current_revision: int
    lead_guest_name: str
    linkage_status: str
    open_exceptions_count: int
    events: List[TimelineEvent]
    generated_at: dt.datetime


class RecoveryExceptionItem(BaseModel):
    """An actionable exception or failure item requiring staff review or recovery."""

    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    exception_id: str
    booking_reference: str
    operation_id: Optional[str] = None
    exception_type: Literal[
        "unconfirmed_reconciliation",
        "partial_vendor_write",
        "payment_unmatched",
        "notification_failed",
        "staff_alert",
    ]
    failed_stage: Optional[str] = None
    reason: str
    payment_verified: bool = False
    payment_id: Optional[str] = None
    payment_amount: Optional[str] = None
    payment_provider_ref: Optional[str] = None
    opera_reservation_id: Optional[str] = None
    opera_readback_verified: bool = False
    brs_round_ids: List[str] = Field(default_factory=list)
    brs_readback_verified: bool = False
    can_retry_automatically: bool = True
    safe_next_action: str
    original_booking_intact: bool = True
    created_at: dt.datetime
    updated_at: dt.datetime


class StaffRecoveryExceptionsListResponse(BaseModel):
    """List of all actionable MMB recovery exceptions across bookings for staff."""

    model_config = ConfigDict(extra="ignore")

    total_exceptions: int
    unconfirmed_reconciliations_count: int
    notification_failures_count: int
    exceptions: List[RecoveryExceptionItem]
    read_at: dt.datetime


class StaffRecoveryRetryRequest(BaseModel):
    """Request payload to safely retry a halted operation without duplicating side-effects."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    idempotency_key: Optional[str] = Field(default=None, max_length=100)
    staff_notes: Optional[str] = Field(default=None, max_length=1000)
    force_readback_recheck_only: bool = Field(
        default=False,
        description="If True, only checks whether vendor writes were already committed externally without writing again.",
    )


class StaffRecoveryRetryResponse(BaseModel):
    """Response detailing the safe recovery execution outcome."""

    model_config = ConfigDict(extra="ignore")

    operation_id: str
    booking_reference: str
    outcome: Literal["recovered_completed", "still_unconfirmed", "requires_manual_reconciliation"]
    stage: str
    message: str
    readback_verified: bool = False
    new_booking_revision: Optional[int] = None
    email_delivery_id: Optional[str] = None
    replayed: bool = False
    updated_at: dt.datetime
