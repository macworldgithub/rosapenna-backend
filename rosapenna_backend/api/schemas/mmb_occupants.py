"""Pydantic schemas for Ticket M10 — Room Occupants, Sharing, and Moves."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from typing import Any, Dict, List, Literal, Optional
from pydantic import BaseModel, ConfigDict, Field, model_validator


BedPreferenceType = Literal["TWIN", "DOUBLE", "SINGLE"]
OccupantUpdateStatus = Literal["confirmed", "pending_staff_review", "conflict", "invalidated"]


class OccupantDetail(BaseModel):
    """Individual occupant in a booked room group with effective stay dates."""
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    occupant_id: Optional[str] = Field(default=None, max_length=100)
    first_name: str = Field(min_length=1, max_length=100)
    last_name: str = Field(min_length=1, max_length=100)
    is_primary: bool = False
    room_group_id: str = Field(min_length=1, max_length=50, description="Logical room group identifier, e.g. room_1, room_2")
    effective_from: dt.date
    effective_to: dt.date

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}".strip()

    @model_validator(mode="after")
    def validate_dates(self) -> "OccupantDetail":
        if self.effective_to <= self.effective_from:
            raise ValueError("effective_to must be strictly after effective_from")
        return self


class SharingPreference(BaseModel):
    """Guest sharing request / bed preference per room group."""
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    room_group_id: str = Field(min_length=1, max_length=50)
    bed_type_preference: BedPreferenceType = "TWIN"
    notes: Optional[str] = Field(default=None, max_length=300)
    sharing_with: Optional[str] = Field(default=None, max_length=200)


class BookedRoomGroupSummary(BaseModel):
    """Summary of a booked room group (without physical room number)."""
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    room_group_id: str
    room_category: str
    capacity: int = Field(default=2, ge=1, le=4)
    assigned_occupants: List[str] = Field(default_factory=list)
    sharing_preference: Optional[SharingPreference] = None


class OccupantUpdateRequest(BaseModel):
    """Request by lead guest to update occupants, bed preferences, or room assignments."""
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    booking_reference: str = Field(min_length=5, max_length=40, pattern=r"^[A-Za-z0-9\-]+$")
    expected_revision: Optional[int] = Field(default=None, ge=1, description="Version/revision for conflict detection")
    occupants: List[OccupantDetail] = Field(min_length=1, max_length=20)
    sharing_preferences: List[SharingPreference] = Field(default_factory=list)
    idempotency_key: Optional[str] = Field(default=None, max_length=120)
    amendment_offer_id: Optional[str] = Field(default=None, max_length=100, description="Optional amendment offer ID if reviewing for an amendment")

    @model_validator(mode="after")
    def validate_no_physical_room_numbers(self) -> "OccupantUpdateRequest":
        # Disallow physical room assignment from guest inputs
        raw_dict = self.model_dump()
        for k in raw_dict.keys():
            if "physical_room" in k or k in ("room_number", "assigned_room_number", "assignedRoomNumber"):
                raise ValueError("Physical room numbers must not be assigned by guest controls.")
        return self


class OccupantReviewVersion(BaseModel):
    """Immutable reviewed occupant and sharing version token for amendment linkage (M09)."""
    model_config = ConfigDict(extra="forbid")

    review_id: str
    booking_reference: str
    offer_id: Optional[str] = None
    version_hash: str
    reviewed_at: dt.datetime
    occupants: List[OccupantDetail]
    sharing_preferences: List[SharingPreference]
    is_current: bool = True


class OccupantUpdateResponse(BaseModel):
    """Response returned upon occupant update or review."""
    model_config = ConfigDict(extra="forbid")

    booking_reference: str
    status: OccupantUpdateStatus
    version_hash: str
    revision: int
    occupants: List[OccupantDetail]
    sharing_preferences: List[SharingPreference]
    readback_verified: bool
    unassigned_physical_room_notice: str = "Physical room numbers are assigned by hotel reception upon check-in. They are not assigned in advance."
    amendment_offer_id: Optional[str] = None
    message: str


class OccupantRosterResponse(BaseModel):
    """Safe guest roster view for Manage Booking."""
    model_config = ConfigDict(extra="forbid")

    booking_reference: str
    arrival_date: dt.date
    departure_date: dt.date
    room_quantity: int
    room_groups: List[BookedRoomGroupSummary]
    occupants: List[OccupantDetail]
    sharing_preferences: List[SharingPreference]
    unassigned_physical_room_notice: str = "Physical room numbers are assigned by hotel reception upon check-in. They are not assigned in advance."
    version_hash: str
    revision: int
    pending_amendment_review: bool = False
    active_amendment_offer_id: Optional[str] = None


def compute_occupant_version_hash(
    booking_reference: str,
    occupants: List[OccupantDetail],
    sharing_preferences: List[SharingPreference],
    stay_dates: tuple[dt.date, dt.date],
    room_category: str,
) -> str:
    """Deterministically compute SHA-256 hash representing the exact occupant and rooming state."""
    canonical_data = {
        "booking_reference": booking_reference.upper(),
        "arrival_date": stay_dates[0].isoformat(),
        "departure_date": stay_dates[1].isoformat(),
        "room_category": room_category.upper(),
        "occupants": sorted(
            [
                {
                    "first_name": occ.first_name.strip().title(),
                    "last_name": occ.last_name.strip().title(),
                    "room_group_id": occ.room_group_id,
                    "effective_from": occ.effective_from.isoformat(),
                    "effective_to": occ.effective_to.isoformat(),
                    "is_primary": occ.is_primary,
                }
                for occ in occupants
            ],
            key=lambda x: (x["room_group_id"], x["last_name"], x["first_name"]),
        ),
        "sharing_preferences": sorted(
            [
                {
                    "room_group_id": sp.room_group_id,
                    "bed_type_preference": sp.bed_type_preference,
                    "notes": sp.notes or "",
                    "sharing_with": sp.sharing_with or "",
                }
                for sp in sharing_preferences
            ],
            key=lambda x: x["room_group_id"],
        ),
    }
    dumped = json.dumps(canonical_data, sort_keys=True)
    return hashlib.sha256(dumped.encode("utf-8")).hexdigest()
