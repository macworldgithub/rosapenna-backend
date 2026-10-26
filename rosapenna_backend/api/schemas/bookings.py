"""Booking and session API request/response models."""

from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from rosapenna_backend.config import settings
from rosapenna_backend.integrations.brs.navigation import validate_brs_url

class PersonalInfo(BaseModel):
    model_config = ConfigDict(json_schema_extra={
        "example": {
            "first_name": "Aoife",
            "last_name": "Byrne",
            "country": "Ireland",
            "email": "aoife.byrne@example.com",
            "telephone": "+353 87 123 4567",
            "mobile": "+353 87 123 4567",
            "handicap": "14.2",
            "club": "Portsalon Golf Club",
            "cdh_number": "1234567",
            "special_requirements": "",
        }
    })

    first_name: str = Field(..., max_length=100, description="Golfer's first name.")
    last_name: str = Field(..., max_length=100, description="Golfer's last (family) name.")
    country: str = Field(..., max_length=100, description="Country of residence, matched against the site's country dropdown.")
    email: str = Field(..., max_length=254, description="Contact email address.")
    telephone: str = Field(..., max_length=40, description="Primary telephone number, including country code.")
    mobile: Optional[str] = Field("", max_length=40, description="Mobile number, if different from telephone. Optional.")
    handicap: str = Field(..., max_length=16, description="Official golf handicap index, e.g. '14.2'.")
    club: str = Field(..., max_length=160, description="Home golf club name.")
    cdh_number: Optional[str] = Field("", max_length=40, description="CDH reference number. Optional.")
    special_requirements: Optional[str] = Field("", max_length=1000, description="Free-text notes for the course. Optional.")

class AdditionalPlayer(BaseModel):
    model_config = ConfigDict(json_schema_extra={
        "example": {
            "name": "Ciaran Doyle",
            "handicap": "9.4",
            "club": "Rosapenna Golf Club",
            "cdh_number": "7654321",
        }
    })

    name: str = Field(..., max_length=200, description="Full name of the additional player.")
    handicap: str = Field(..., max_length=16, description="This player's golf handicap index.")
    club: str = Field(..., max_length=160, description="This player's home golf club.")
    cdh_number: Optional[str] = Field("", max_length=40, description="This player's CDH reference number. Optional.")

class MarketingConsent(BaseModel):
    model_config = ConfigDict(json_schema_extra={
        "example": {"email": True, "sms": False, "post": False, "telephone": False}
    })

    email: bool = Field(False, description="Opt in to marketing emails.")
    sms: bool = Field(False, description="Opt in to marketing SMS messages.")
    post: bool = Field(False, description="Opt in to marketing by post.")
    telephone: bool = Field(False, description="Opt in to marketing phone calls.")

class CardDetails(BaseModel):
    model_config = ConfigDict(json_schema_extra={
        "example": {
            "card_number": "4242424242424242",
            "expiry": "1228",
            "cvv": "123",
        }
    })

    card_number: str = Field(..., min_length=12, max_length=19, pattern=r"^\d+$", description="Card number digits only, no spaces, e.g. '4242424242424242'.")
    expiry: str = Field(..., min_length=4, max_length=4, pattern=r"^(0[1-9]|1[0-2])\d{2}$", description="Expiry as MMYY, e.g. '1228' for Dec 2028.")
    cvv: str = Field(..., min_length=3, max_length=4, pattern=r"^\d+$", description="3 or 4 digit CVV/CVC.")

class BookingRequest(BaseModel):
    model_config = ConfigDict(json_schema_extra={
        "example": {
            "url": settings.default_url,
            "course_name": "Old Tom Morris Links",
            "holes": "18 Holes",
            "desired_date": "2026-07-26",
            "earliest_time": "07:00",
            "latest_time": "15:00",
            "desired_time": "07:30",
            "player_count": 2,
            "personal_info": {
                "first_name": "Aoife",
                "last_name": "Byrne",
                "country": "Ireland",
                "email": "aoife.byrne@example.com",
                "telephone": "+353 87 123 4567",
                "mobile": "+353 87 123 4567",
                "handicap": "14.2",
                "club": "Portsalon Golf Club",
                "cdh_number": "1234567",
                "special_requirements": "",
            },
            "additional_players": [
                {"name": "Ciaran Doyle", "handicap": "9.4", "club": "Rosapenna Golf Club", "cdh_number": "7654321"}
            ],
            "marketing_consent": {"email": True, "sms": False, "post": False, "telephone": False},
            "accept_terms": True,
            "card_details": {
                "card_number": "4242424242424242",
                "expiry": "1228",
                "cvv": "123",
            },
            "auto_pay": False,
            "debug": True,
        }
    })

    url: str = Field(settings.default_url, max_length=2048, description="Full URL of the Rosapenna BRS tee-sheet page.")
    course_name: str = Field("Old Tom Morris Links", max_length=120, description="Course label as shown on the site.")
    holes: str = Field("18 Holes", max_length=40, description="Holes option label, e.g. '18 Holes' or '9 Holes'.")
    desired_date: Optional[str] = Field(None, pattern=r"^\d{4}-\d{2}-\d{2}$", description="Date to book, formatted 'YYYY-MM-DD'.")
    earliest_time: Optional[str] = Field(None, pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$", description="Lower bound 'HH:MM' for the Time range slider.")
    latest_time: Optional[str] = Field(None, pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$", description="Upper bound 'HH:MM' for the Time range slider.")
    desired_time: str = Field(..., pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$", description="The exact tee time to book, e.g. '07:30'.")
    player_count: int = Field(1, ge=1, le=4, description="Number of players (1-4).")
    personal_info: PersonalInfo = Field(..., description="Lead player details.")
    additional_players: List[AdditionalPlayer] = Field(default_factory=list, max_length=3, description="Details for players 2-4.")
    marketing_consent: MarketingConsent = Field(default_factory=MarketingConsent, description="Marketing consent checkboxes.")
    accept_terms: bool = Field(True, description="Whether to tick the terms & conditions checkbox.")
    card_details: Optional[CardDetails] = Field(
        None,
        description="Card details to fill into Stripe automatically. If omitted, the browser is left open for manual card entry.",
    )
    auto_pay: bool = Field(
        False,
        description="If True AND card_details are supplied, click the Pay button automatically after filling card details. "
                    "Defaults to False — set True only when you are confident the form is correct.",
    )
    debug: bool = Field(settings.debug_screenshots, description="Save screenshots + text dumps after each major step.")

    @field_validator("url")
    @classmethod
    def approved_brs_page(cls, value: str) -> str:
        return validate_brs_url(value)

class BookingResponse(BaseModel):
    model_config = ConfigDict(json_schema_extra={
        "example": {
            "session_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
            "status": "filled",
            "message": "Form filled successfully. Browser is open — complete payment manually.",
            "log": ["ok: select course", "ok: selected date 2026-07-26 (input now reads 'Sunday 26th July')"],
        }
    })

    session_id: str = Field(..., description="Unique ID for this booking session.")
    status: str = Field(..., description="One of: 'filled', 'error', 'running'.")
    message: str = Field(..., description="Human-readable summary.")
    log: List[str] = Field(..., description="Step-by-step log lines.")

class SessionStatusResponse(BaseModel):
    model_config = ConfigDict(json_schema_extra={
        "example": {
            "session_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
            "status": "filled",
            "created_at": 1753600000.0,
            "log": ["ok: select course"],
            "error": None,
        }
    })

    session_id: str = Field(..., description="The session ID being queried.")
    status: str = Field(..., description="One of: 'running', 'filled', 'error', 'closed'.")
    created_at: float = Field(..., description="Unix timestamp when the session was created.")
    log: List[str] = Field(..., description="Step-by-step log lines.")
    error: Optional[str] = Field(None, description="Error message, present only when status is 'error'.")
