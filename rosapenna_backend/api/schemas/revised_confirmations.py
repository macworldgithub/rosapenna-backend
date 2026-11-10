"""Schemas for revised confirmations, updated PDF itineraries, and pre-arrival reminders (Ticket M15)."""

from __future__ import annotations

import datetime as dt
from typing import List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from rosapenna_backend.api.schemas.communications import EmailDeliveryResult


class ScheduledReminderItem(BaseModel):
    """Authoritative representation of a scheduled pre-arrival guest communication milestone."""

    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    reminder_id: str = Field(description="Unique milestone identifier, e.g. REM-REF-type-rev")
    booking_reference: str
    revision_number: int
    reminder_type: Literal["balance_due_first", "rooming_and_caddy", "concierge_check", "balance_due_final"]
    title: str
    scheduled_date: dt.date
    target_lead_time_days: int
    status: Literal["scheduled", "canceled_by_amendment", "suppressed", "dispatched", "failed"] = "scheduled"
    suppression_reason: Optional[str] = None
    dispatched_at: Optional[str] = None
    delivery_result: Optional[EmailDeliveryResult] = None
    created_at: dt.datetime
    updated_at: dt.datetime


class ScheduledReminderListResponse(BaseModel):
    """List of all scheduled, canceled, and dispatched communication milestones for a booking."""

    model_config = ConfigDict(extra="ignore")

    booking_reference: str
    active_revision: int
    arrival_date: Optional[dt.date] = None
    balance_due_date: Optional[dt.date] = None
    reminders: List[ScheduledReminderItem]


class RevisedConfirmationSendRequest(BaseModel):
    """Payload to request re-dispatch of an official revised booking confirmation."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    idempotency_key: Optional[str] = Field(default=None, max_length=100)
    recipient_override: Optional[str] = Field(default=None, max_length=254)


class RevisedConfirmationSendResponse(BaseModel):
    """Response detailing the outcome of a revised confirmation dispatch."""

    model_config = ConfigDict(extra="ignore")

    booking_reference: str
    revision_number: int
    delivery_result: EmailDeliveryResult


class DispatchReminderRequest(BaseModel):
    """Payload to trigger dispatch of a scheduled pre-arrival reminder."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    idempotency_key: Optional[str] = Field(default=None, max_length=100)


class DispatchReminderResponse(BaseModel):
    """Response detailing the outcome of a reminder dispatch or suppression."""

    model_config = ConfigDict(extra="ignore")

    reminder_id: str
    status: str
    message: str
    delivery_result: Optional[EmailDeliveryResult] = None
