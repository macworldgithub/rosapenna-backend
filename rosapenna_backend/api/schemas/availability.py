"""Availability and tee-sheet API models."""

from datetime import date, datetime
from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from rosapenna_backend.config import settings
from rosapenna_backend.integrations.brs.navigation import validate_brs_url
from rosapenna_backend.integrations.brs.tee_sheet import COURSES

class AvailableTimesRequest(BaseModel):
    model_config = ConfigDict(json_schema_extra={
        "example": {
            "url": settings.default_url,
            "course_name": "Old Tom Morris Links",
            "holes": "18 Holes",
            "desired_date": "2026-07-26",
            "earliest_time": "07:00",
            "latest_time": "15:00",
            "headless": True,
            "debug": False,
        }
    })

    url: str = Field(settings.default_url, max_length=2048, description="Full URL of the Rosapenna BRS tee-sheet page.")
    course_name: str = Field("Old Tom Morris Links", max_length=120, description="Course label as shown on the site.")
    holes: str = Field("18 Holes", max_length=40, description="Holes option label.")
    desired_date: Optional[str] = Field(None, pattern=r"^\d{4}-\d{2}-\d{2}$", description="Date to check, formatted 'YYYY-MM-DD'.")
    earliest_time: Optional[str] = Field(None, pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$", description="Lower bound 'HH:MM' for the Time range slider.")
    latest_time: Optional[str] = Field(None, pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$", description="Upper bound 'HH:MM' for the Time range slider.")
    headless: bool = Field(True, description="Run the browser headlessly.")
    debug: bool = Field(False, description="Dump extra diagnostic state on failure.")

    @field_validator("url")
    @classmethod
    def approved_brs_page(cls, value: str) -> str:
        return validate_brs_url(value)

class PlayerOption(BaseModel):
    model_config = ConfigDict(json_schema_extra={
        "example": {"id": "opt-2ball-0730", "label": "2 Players - €180.00", "player_count": 2, "price": "€180.00"}
    })

    id: str = Field(..., description="DOM element id of this option's button.")
    label: str = Field(..., description="Raw visible label, e.g. '2 Players - €180.00'.")
    player_count: Optional[int] = Field(None, description="Parsed number of players.")
    price: Optional[str] = Field(None, description="Parsed price.")

class TimeSlot(BaseModel):
    model_config = ConfigDict(json_schema_extra={
        "example": {
            "time": "07:30",
            "time_button_id": "teetime-0730",
            "player_options": [{"id": "opt-2ball-0730", "label": "2 Players - €180.00", "player_count": 2, "price": "€180.00"}],
        }
    })

    time: str = Field(..., description="Tee time, e.g. '07:30'.")
    time_button_id: Optional[str] = Field(None, description="DOM element id of the time button.")
    player_options: List[PlayerOption] = Field(default_factory=list, description="Player-count/price combinations.")

class RateSection(BaseModel):
    model_config = ConfigDict(json_schema_extra={
        "example": {"rate_name": "Visitor Rate", "times": []}
    })

    rate_name: Optional[str] = Field(None, description="Name of the rate/package.")
    times: List[TimeSlot] = Field(default_factory=list, description="Tee times under this rate.")

class AvailableTimesResponse(BaseModel):
    model_config = ConfigDict(json_schema_extra={
        "example": {
            "course_name": "Old Tom Morris Links",
            "holes": "18 Holes",
            "date": "2026-07-26",
            "date_selected": True,
            "sections": [],
            "log": ["ok: select course", "ok: found 12 time slot(s) across 1 section(s)"],
        }
    })

    course_name: str = Field(..., description="Course echoed from request.")
    holes: str = Field(..., description="Holes echoed from request.")
    date: Optional[str] = Field(None, description="Date echoed from request.")
    date_selected: Optional[bool] = Field(None, description="Whether the calendar click was verified.")
    sections: List[RateSection] = Field(..., description="Available tee times grouped by rate.")
    log: List[str] = Field(..., description="Step-by-step log lines.")

class QuoteSelection(BaseModel):
    date: date
    course_id: int
    tee_time_id: int = Field(gt=0)
    green_fee_id: int = Field(gt=0)
    players: int = Field(ge=1, le=4)
    price: str = Field(max_length=30)
    currency: str = Field(pattern=r"^[A-Z]{3}$")


class BrsSlotRate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    rate_id: int
    package_name: str
    players: int = Field(ge=1, le=4)
    price_per_player: str = Field(pattern=r"^\d{1,8}(?:\.\d{1,2})?$")
    total_price: str = Field(pattern=r"^\d{1,8}(?:\.\d{1,2})?$")
    currency: str = "EUR"
    num_holes: str = "18"
    type: str = "Visitor"


class BrsTeeSlot(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    slot_id: int
    time: str = Field(pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    holes: str = "18"
    available_capacity: int = Field(ge=0, le=4)
    is_available: bool
    rates: List[BrsSlotRate] = Field(default_factory=list)
    inventory_id: str


class BrsCourseAvailabilityResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    course_id: int
    course_name: str
    tenant: str
    date: date
    status: str  # "available", "no_availability", "unconfigured_tenant", "stale_snapshot"
    available_slots_count: int
    slots: List[BrsTeeSlot] = Field(default_factory=list)
    retrieved_at: datetime
    freshness_ttl_seconds: int = 60
    reserves_inventory: bool = False
    source: str  # "brs_api", "fixture_fallback"
