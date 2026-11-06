"""Pydantic schemas for Ticket M05 — Live Amendment Options and Alternatives."""

from __future__ import annotations

import datetime as dt
from typing import Any, Dict, List, Literal, Optional
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from rosapenna_backend.api.schemas.booking_linkage import ComponentSourceHealth, GuestSafeBookingSummary

PRICE_PATTERN = r"^[+-]?\d{1,8}(?:\.\d{1,2})?$"


class ProposedGolfRoundSearch(BaseModel):
    """Candidate round search parameters."""
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    course_name: str = Field(min_length=1, max_length=100)
    date: dt.date
    preferred_time_window: Optional[str] = Field(default=None, max_length=50)  # e.g., "morning", "afternoon", "10:00"
    players: int = Field(default=2, ge=1, le=4)
    holes: str = Field(default="18", max_length=10)


class MmbAmendmentSearchRequest(BaseModel):
    """Guest search request for proposed booking amendments."""
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    booking_reference: str = Field(
        min_length=5,
        max_length=40,
        pattern=r"^[A-Za-z0-9\-]+$",
        description="Canonical booking reference.",
    )
    proposed_arrival_date: Optional[dt.date] = None
    proposed_departure_date: Optional[dt.date] = None
    proposed_room_category: Optional[str] = Field(default=None, max_length=100)
    proposed_room_quantity: Optional[int] = Field(default=None, ge=1, le=5)
    proposed_golf_rounds: Optional[List[ProposedGolfRoundSearch]] = None

    @model_validator(mode="after")
    def validate_dates(self) -> "MmbAmendmentSearchRequest":
        if self.proposed_arrival_date and self.proposed_departure_date:
            if self.proposed_departure_date <= self.proposed_arrival_date:
                raise ValueError("proposed_departure_date must be after proposed_arrival_date.")
        return self


class ProposedStayOption(BaseModel):
    """Evaluated stay option for an amendment alternative."""
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    arrival_date: dt.date
    departure_date: dt.date
    nights: int
    room_category: str
    room_quantity: int
    is_available: bool
    own_allocation_retained: bool = False
    nightly_rate: str
    stay_total: str
    source_freshness: str = "Live Opera Read"


class ProposedGolfRoundOption(BaseModel):
    """Evaluated golf round option for an amendment alternative."""
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    course_name: str
    date: dt.date
    time: str
    players: int
    holes: str = "18"
    round_price_total: str
    is_available: bool
    own_allocation_retained: bool = False
    source_freshness: str = "Live BRS Read"


class AmendmentPricingComparison(BaseModel):
    """Financial impact comparison between original and amended itineraries."""
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    original_package_total: str
    new_package_total: str
    price_delta: str
    deposit_already_paid: str
    revised_balance_due: str
    currency: str = "EUR"


class AmendmentOptionAlternative(BaseModel):
    """Complete, independently checked available Stay & Play amendment alternative."""
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    option_id: str
    title: str
    description: str
    stay_option: ProposedStayOption
    golf_options: List[ProposedGolfRoundOption]
    pricing_comparison: AmendmentPricingComparison
    eligibility_checks: Dict[str, Any]
    is_complete_package: bool
    unavailability_reasons: List[str] = Field(default_factory=list)


class MmbAmendmentSearchResponse(BaseModel):
    """Live search response containing complete verified amendment alternatives."""
    model_config = ConfigDict(extra="forbid")

    booking_reference: str
    search_id: str
    searched_at: dt.datetime
    current_itinerary: GuestSafeBookingSummary
    is_complete_itinerary_available: bool
    alternatives: List[AmendmentOptionAlternative]
    sources_freshness: Dict[str, ComponentSourceHealth]
    warnings: List[str] = Field(default_factory=list)
    reserves_inventory: bool = False  # Strictly False per AC 6


class AmendmentOfferCreateRequest(BaseModel):
    """Guest request to generate an immutable amendment offer snapshot from a candidate alternative."""
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    booking_reference: str = Field(
        min_length=5,
        max_length=40,
        pattern=r"^[A-Za-z0-9\-]+$",
        description="Authoritative booking reference.",
    )
    option_id: str = Field(min_length=1, max_length=50)
    proposed_stay: ProposedStayOption
    proposed_golf_rounds: List[ProposedGolfRoundOption] = Field(default_factory=list)


class AmendmentOfferAcceptRequest(BaseModel):
    """Guest consent and acceptance of an immutable amendment offer."""
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    offer_id: str = Field(min_length=5, max_length=100)
    booking_reference: str = Field(
        min_length=5,
        max_length=40,
        pattern=r"^[A-Za-z0-9\-]+$",
        description="Authoritative booking reference.",
    )
    terms_version: str = Field(min_length=1, max_length=50)
    accepted_terms: bool
    occupant_review_acknowledged: bool = False
    occupant_review_version: Optional[str] = Field(default=None, max_length=100)


class AmendmentOfferSnapshot(BaseModel):
    """Immutable snapshot of an evaluated amendment offer presented to the lead guest."""
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    offer_id: str
    booking_reference: str
    booking_revision: int
    option_id: str
    current_itinerary: GuestSafeBookingSummary
    proposed_stay: ProposedStayOption
    proposed_golf_rounds: List[ProposedGolfRoundOption]
    pricing_comparison: AmendmentPricingComparison
    credit_amount: str = "0.00"
    amount_due_now: str = "0.00"
    balance_due_date: dt.date
    amendment_fee: str = "0.00"
    requires_occupant_review: bool = False
    occupant_review_acknowledged: bool = False
    occupant_review_version: Optional[str] = None
    terms_version: str = "terms-v1-20261014"
    status: Literal["pending_consent", "accepted", "expired", "invalidated"] = "pending_consent"
    expires_at: dt.datetime
    created_at: dt.datetime
    accepted_at: Optional[dt.datetime] = None
    reserves_inventory: bool = False  # Strictly False per AC 6


class AmendmentRevalidationRequest(BaseModel):
    """Guest request to atomically revalidate an accepted amendment offer before payment or commit."""
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    offer_id: str = Field(min_length=5, max_length=100)
    booking_reference: str = Field(
        min_length=5,
        max_length=40,
        pattern=r"^[A-Za-z0-9\-]+$",
        description="Authoritative booking reference.",
    )
    idempotency_key: Optional[str] = Field(default=None, max_length=100)
    # Controllable fault simulation hooks for non-production tests
    simulate_sold_out_room: bool = False
    simulate_unavailable_tee: bool = False
    simulate_rate_change: Optional[str] = None


class AmendmentRevalidationComponentCheck(BaseModel):
    """Result of an individual subsystem revalidation check."""
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    component: Literal[
        "gate",
        "session",
        "booking_revision",
        "offer_lifecycle",
        "hotel_rooms",
        "golf_rounds",
        "pricing_ledger",
    ]
    status: Literal["passed", "failed", "stale"]
    message: str
    checked_at: dt.datetime


class AmendmentRevalidationResponse(BaseModel):
    """Authoritative result of final amendment revalidation."""
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    revalidation_id: str
    offer_id: str
    booking_reference: str
    is_valid: bool
    revalidation_status: Literal[
        "valid",
        "offer_expired",
        "revision_conflict",
        "room_unavailable",
        "tee_time_unavailable",
        "price_changed",
        "ledger_changed",
        "gate_disabled",
        "unaccepted_offer",
        "stale_data",
    ]
    summary_message: str
    checks: List[AmendmentRevalidationComponentCheck] = Field(default_factory=list)
    refreshed_offer: Optional[AmendmentOfferSnapshot] = None
    revalidated_at: dt.datetime
    reserves_inventory: bool = False  # Strictly False per AC 3 & AC 6
    idempotency_key: Optional[str] = None

AmendmentPaymentActionType = Literal["payment_required", "zero_due", "credit_generated"]
AmendmentSettlementStatus = Literal[
    "payment_link_created",
    "zero_due_authorized",
    "credit_recorded",
    "paid_unconfirmed",
]


class AmendmentPaymentInitiateRequest(BaseModel):
    """Guest request to initiate payment or settlement for an accepted, revalidated amendment offer (Ticket M08)."""
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    booking_reference: str = Field(
        min_length=5,
        max_length=40,
        pattern=r"^[A-Za-z0-9\-]+$",
        description="Authoritative booking reference.",
    )
    offer_id: str = Field(min_length=5, max_length=100)
    revalidation_id: str = Field(min_length=5, max_length=100)
    idempotency_key: Optional[str] = Field(default=None, max_length=100)


class AmendmentPaymentInitiateResponse(BaseModel):
    """Authoritative result of amendment settlement initiation (Ticket M08)."""
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    booking_reference: str
    offer_id: str
    revalidation_id: str
    action_type: AmendmentPaymentActionType
    settlement_status: AmendmentSettlementStatus
    payment_id: Optional[str] = None
    payment_url: Optional[str] = None
    amount_due_now: str
    credit_amount: str
    currency: str = "EUR"
    expires_at: Optional[dt.datetime] = None
    message: str
    reserves_inventory: bool = False  # Strictly False per AC 6
    idempotency_key: Optional[str] = None
    created_at: dt.datetime
