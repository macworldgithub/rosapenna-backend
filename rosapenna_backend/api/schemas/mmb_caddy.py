"""Pydantic validation schemas for Ticket M12 — Per-Round Caddy Request Updates."""

from __future__ import annotations

import datetime as dt
from typing import List, Literal, Optional
from pydantic import BaseModel, ConfigDict, Field, model_validator


CADDY_FEE_PER_BAG = "90.00"
CADDY_NOTICE = (
    "Caddy requests are fulfilled by the Rosapenna Caddie Master on the day of play and are "
    "subject to availability. All caddy fees (€90 per bag) are payable directly at the Golf Pavilion on the day. "
    "Online requests carry €0.00 charge in package booking."
)


class CaddyBagPreference(BaseModel):
    """Specific preference for an individual golf bag or player."""
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    bag_index: int = Field(ge=1, le=4, description="1-indexed bag number within the tee time group")
    caddy_category: Literal["caddie", "forecaddie"] = Field(
        default="caddie",
        description="Requested caddy tier (caddie walks with single bag, forecaddie assists the group)",
    )
    player_name: Optional[str] = Field(
        default=None,
        max_length=100,
        description="Assigned player name if known from the booking roster",
    )
    notes: Optional[str] = Field(
        default=None,
        max_length=200,
        description="Specific preferences (e.g. experienced bag-carrier, local links knowledge)",
    )


class RoundCaddyUpdateRequest(BaseModel):
    """Guest submission to add or update caddy requests on a confirmed round."""
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    booking_reference: str = Field(min_length=5, max_length=40)
    round_id: str = Field(min_length=1, max_length=100)
    caddies_requested: int = Field(
        ge=0,
        le=4,
        description="Number of caddies requested (0 means cancel/remove caddy request)",
    )
    caddy_category: Optional[Literal["caddie", "forecaddie"]] = Field(
        default=None,
        description="Primary caddy category requested; required if caddies_requested > 0",
    )
    bag_preferences: List[CaddyBagPreference] = Field(
        default_factory=list,
        description="Optional bag-specific preferences for individual players",
    )
    general_notes: Optional[str] = Field(
        default=None,
        max_length=250,
        description="General notes for the caddie master",
    )
    expected_revision: int = Field(
        ge=1,
        description="Booking linkage revision for optimistic concurrency guard",
    )
    payment_terms: Literal["payable_on_the_day"] = "payable_on_the_day"

    @model_validator(mode="after")
    def validate_request_logic(self) -> "RoundCaddyUpdateRequest":
        if self.caddies_requested == 0:
            self.caddy_category = None
            self.bag_preferences = []
        else:
            if not self.caddy_category:
                raise ValueError("caddy_category is required when caddies_requested > 0")
            if len(self.bag_preferences) > self.caddies_requested:
                raise ValueError("Cannot supply more bag preferences than total caddies requested")
        return self


class RoundCaddySummary(BaseModel):
    """Snapshot of a confirmed golf round and its active caddy request status."""
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    round_id: str
    course_id: int
    course_name: str
    date: dt.date
    time: str
    holes: str
    players: int
    caddies_requested: int = 0
    caddy_category: Optional[Literal["caddie", "forecaddie"]] = None
    caddy_request_status: Literal["not_requested", "requested", "forwarded_to_caddie_master", "allocated_on_day"] = "not_requested"
    bag_preferences: List[CaddyBagPreference] = Field(default_factory=list)
    general_notes: Optional[str] = None
    online_charge: str = "0.00"
    estimated_day_fee: str = "0.00"
    notice: str = CADDY_NOTICE


class RoundCaddyUpdateResponse(BaseModel):
    """Authoritative response confirming BRS note sync and linkage update."""
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    operation_id: str
    booking_reference: str
    round_id: str
    caddies_requested: int
    caddy_category: Optional[Literal["caddie", "forecaddie"]] = None
    caddy_request_status: Literal["not_requested", "requested", "forwarded_to_caddie_master", "allocated_on_day"]
    bag_preferences: List[CaddyBagPreference] = Field(default_factory=list)
    general_notes: Optional[str] = None
    booking_revision: int
    online_charge: str = "0.00"
    estimated_day_fee: str = "0.00"
    verified_via_readback: bool = True
    replayed: bool = False
    message: str
    notice: str = CADDY_NOTICE
    updated_at: dt.datetime


class BookingCaddyRosterResponse(BaseModel):
    """Complete caddy request status for all rounds in an authenticated booking."""
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    booking_reference: str
    booking_revision: int
    rounds: List[RoundCaddySummary]
    general_notice: str = CADDY_NOTICE
    read_at: dt.datetime
