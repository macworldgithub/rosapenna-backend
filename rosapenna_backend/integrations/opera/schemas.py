"""Pydantic schemas for Oracle Opera Cloud / OHIP requests and responses."""

import datetime as dt
from typing import List, Optional
from pydantic import BaseModel, Field, model_validator


class OperaOAuthTokenResponse(BaseModel):
    access_token: str
    token_type: str = "Bearer"
    expires_in: int = 3600


class RoomRatePlan(BaseModel):
    rate_plan_code: str
    rate_plan_name: str
    total_amount: float
    currency: str = "EUR"
    breakfast_included: bool = True


class RoomCategoryAvailability(BaseModel):
    room_category: str  # e.g., "CLASSIC", "DELUXE", "BAYVIEW", "PENTHOUSE"
    category_name: str  # e.g., "Classic Room", "Deluxe Room", etc.
    available_rooms: int = Field(ge=0)
    rates: List[RoomRatePlan] = Field(default_factory=list)


class RoomAvailabilityResponse(BaseModel):
    hotel_id: str
    arrival_date: dt.date
    departure_date: dt.date
    adults: int = Field(1, ge=1)
    children: int = Field(0, ge=0)
    source: str = "opera_cloud"  # "opera_cloud" or "fixture_fallback"
    retrieved_at: dt.datetime = Field(default_factory=lambda: dt.datetime.now(dt.timezone.utc))
    categories: List[RoomCategoryAvailability] = Field(default_factory=list)


class RoomOccupant(BaseModel):
    first_name: str = Field(min_length=1, max_length=100)
    last_name: str = Field(min_length=1, max_length=100)
    is_primary: bool = False


class DateSharingAssignment(BaseModel):
    model_config = {
        "extra": "forbid",
        "str_strip_whitespace": True,
    }

    date: dt.date
    occupant_names: List[str] = Field(min_length=1, max_length=4)
    bed_type_preference: Optional[str] = Field(default="TWIN", pattern=r"^(TWIN|DOUBLE|SINGLE)$")
    notes: Optional[str] = Field(default=None, max_length=200)


class CreateReservationRequest(BaseModel):
    model_config = {
        "extra": "forbid",
        "str_strip_whitespace": True,
    }

    booking_intent_id: str = Field(min_length=1, max_length=100, description="Linked MongoDB booking intent ID")
    arrival_date: dt.date
    departure_date: dt.date
    room_category: str = Field(min_length=1, max_length=50, description="e.g., CLASSIC, DELUXE, BAYVIEW, PENTHOUSE")
    room_quantity: int = Field(default=1, ge=1, le=10, description="Number of rooms requested")
    room_group_id: Optional[str] = Field(default=None, max_length=100, description="Group/block ID for multi-room booking")
    rate_plan_code: str = Field(default="BB_STANDARD", max_length=50)
    adults: int = Field(default=2, ge=1, le=10)
    children: int = Field(default=0, ge=0, le=10)
    occupants: List[RoomOccupant] = Field(min_length=1, max_length=10)
    sharing_assignments: List[DateSharingAssignment] = Field(default_factory=list, description="Date-specific room sharing assignments")
    guest_phone: Optional[str] = Field(default=None, max_length=40)
    guest_email: Optional[str] = Field(default=None, max_length=120)
    special_requests: Optional[str] = Field(default=None, max_length=500)
    idempotency_key: Optional[str] = Field(default=None, max_length=120)

    @model_validator(mode="after")
    def validate_dates_and_room_safety(self) -> "CreateReservationRequest":
        if self.departure_date <= self.arrival_date:
            raise ValueError("departure_date must be strictly after arrival_date.")
        # Ensure room_number is never assigned
        if hasattr(self, "room_number") or hasattr(self, "assignedRoomNumber"):
            raise ValueError("Physical room numbers must not be assigned via client writes.")
        # Validate sharing assignment dates
        for sa in self.sharing_assignments:
            if not (self.arrival_date <= sa.date <= self.departure_date):
                raise ValueError(f"Sharing assignment date {sa.date} must be within stay window [{self.arrival_date}, {self.departure_date}].")
        return self


class UpdateReservationRequest(BaseModel):
    model_config = {
        "extra": "forbid",
        "str_strip_whitespace": True,
    }

    reservation_id: str = Field(min_length=1, max_length=100)
    expected_revision: Optional[int] = Field(default=None, ge=1, description="Version/revision for conflict detection")
    arrival_date: Optional[dt.date] = None
    departure_date: Optional[dt.date] = None
    room_category: Optional[str] = Field(default=None, max_length=50)
    room_quantity: Optional[int] = Field(default=None, ge=1, le=10)
    room_group_id: Optional[str] = Field(default=None, max_length=100)
    occupants: Optional[List[RoomOccupant]] = Field(default=None, max_length=10)
    sharing_assignments: Optional[List[DateSharingAssignment]] = None
    guest_phone: Optional[str] = Field(default=None, max_length=40)
    guest_email: Optional[str] = Field(default=None, max_length=120)
    special_requests: Optional[str] = Field(default=None, max_length=500)
    idempotency_key: Optional[str] = Field(default=None, max_length=120)

    @model_validator(mode="after")
    def validate_amendment_safety(self) -> "UpdateReservationRequest":
        if self.arrival_date and self.departure_date:
            if self.departure_date <= self.arrival_date:
                raise ValueError("departure_date must be strictly after arrival_date.")
        if hasattr(self, "room_number") or hasattr(self, "assignedRoomNumber"):
            raise ValueError("Physical room numbers must not be assigned via client writes.")
        return self


class ReservationWriteResult(BaseModel):
    reservation_id: str
    confirmation_number: str
    status: str
    hotel_id: str
    arrival_date: dt.date
    departure_date: dt.date
    room_category: str
    room_quantity: int = 1
    room_group_id: Optional[str] = None
    occupants: List[RoomOccupant]
    sharing_assignments: List[DateSharingAssignment] = Field(default_factory=list)
    total_amount: float
    currency: str = "EUR"
    source: str  # "opera_cloud" or "fixture_sandbox"
    verified_via_readback: bool = True
    idempotency_replayed: bool = False
    created_at: dt.datetime = Field(default_factory=lambda: dt.datetime.now(dt.timezone.utc))


class RoomReservationDetail(BaseModel):
    reservation_id: str
    confirmation_number: Optional[str] = None
    hotel_id: str
    status: str  # "Reserved", "Confirmed", "CheckedIn", "CheckedOut", "Cancelled"
    arrival_date: dt.date
    departure_date: dt.date
    room_category: str
    room_quantity: int = 1
    room_group_id: Optional[str] = None
    room_number: Optional[str] = None  # Note: Hotel-owned allocation; None until assigned
    occupants: List[RoomOccupant] = Field(default_factory=list)
    sharing_assignments: List[DateSharingAssignment] = Field(default_factory=list)
    total_amount: float
    currency: str = "EUR"
    source: str = "opera_cloud"
    retrieved_at: dt.datetime = Field(default_factory=lambda: dt.datetime.now(dt.timezone.utc))
