"""Validation models for BRS tee time booking writes and amendments."""

from __future__ import annotations

import datetime as dt
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from rosapenna_backend.integrations.brs.tee_sheet import COURSES

PRICE_PATTERN = r"^\d{1,8}(?:\.\d{1,2})?$"


class CaddyRequestPayload(BaseModel):
    """Caddy request attached to a golf round.

    Per CIP-SOW-RSP-001 and project scope, caddies are strictly availability
    requests payable on the day at the resort. Online charge is always 0.00.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    caddies_requested: int = Field(default=0, ge=0, le=4)
    caddy_category: Literal["caddie", "forecaddie"] | None = Field(
        default=None,
        description="Type of caddy requested; must be provided if caddies_requested > 0",
    )
    notes: str | None = Field(
        default=None,
        max_length=250,
        description="Specific guest requests or preferences for the caddie master",
    )
    payment_terms: Literal["payable_on_the_day"] = "payable_on_the_day"
    online_charge: str = "0.00"
    status: Literal["requested", "not_requested"] = "requested"

    @model_validator(mode="after")
    def validate_caddy_fields(self):
        if self.caddies_requested == 0:
            self.status = "not_requested"
            self.caddy_category = None
        else:
            self.status = "requested"
            if self.caddy_category is None:
                raise ValueError("caddy_category is required when caddies_requested > 0")
        return self


class CreateBrsRoundRequest(BaseModel):
    """Request payload to create a BRS round linked to a booking intent."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    booking_intent_id: str = Field(min_length=1, max_length=100)
    idempotency_key: str = Field(min_length=1, max_length=100)
    round_id: str | None = Field(default=None, max_length=50)
    course_id: int = Field(gt=0, strict=True)
    date: dt.date
    time: str = Field(pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$", strict=True)
    holes: str = Field(default="18", max_length=40)
    players: int = Field(ge=1, le=4, strict=True)
    price: str = Field(pattern=PRICE_PATTERN, max_length=11)
    currency: str = Field(default="EUR", pattern=r"^[A-Z]{3}$")
    package_name: str | None = Field(default=None, max_length=120)
    caddy_request: CaddyRequestPayload | None = None

    @field_validator("course_id")
    @classmethod
    def validate_course(cls, value: int) -> int:
        if value not in COURSES:
            raise ValueError(f"Unsupported course_id {value}. Supported: {list(COURSES.keys())}")
        return value


class UpdateBrsRoundRequest(BaseModel):
    """Request payload to amend a BRS round linked to a booking intent."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    booking_intent_id: str = Field(min_length=1, max_length=100)
    idempotency_key: str = Field(min_length=1, max_length=100)
    round_id: str = Field(min_length=1, max_length=50)
    new_course_id: int | None = Field(default=None, gt=0)
    new_date: dt.date | None = None
    new_time: str | None = Field(default=None, pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    new_players: int | None = Field(default=None, ge=1, le=4)
    new_price: str | None = Field(default=None, pattern=PRICE_PATTERN)
    caddy_request: CaddyRequestPayload | None = None

    @field_validator("new_course_id")
    @classmethod
    def validate_new_course(cls, value: int | None) -> int | None:
        if value is not None and value not in COURSES:
            raise ValueError(f"Unsupported course_id {value}. Supported: {list(COURSES.keys())}")
        return value


class ReleaseBrsRoundRequest(BaseModel):
    """Request to release/cancel an old BRS round after amendment is verified."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    booking_intent_id: str = Field(min_length=1, max_length=100)
    idempotency_key: str = Field(min_length=1, max_length=100)
    round_id: str = Field(min_length=1, max_length=50)
    reason: str = Field(default="Replaced during amendment", max_length=200)


class BrsRoundWriteResult(BaseModel):
    """Response returned upon successful creation or amendment of a BRS round."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    success: bool = True
    round_id: str
    brs_booking_id: str
    booking_reference: str
    course_id: int
    course_name: str
    date: dt.date
    time: str
    holes: str
    players: int
    price: str
    currency: str
    status: str
    caddy_request: CaddyRequestPayload | None = None
    verified_via_readback: bool = True
    source: Literal["brs_live", "verified_sandbox", "idempotent_replay"]
    created_at: str
    idempotency_replayed: bool = False


class BrsRoundReleaseResult(BaseModel):
    """Response returned upon successful release/cancellation of a BRS round."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    success: bool = True
    round_id: str
    status: Literal["Released", "Cancelled"] = "Released"
    released_at: str
    source: Literal["brs_live", "verified_sandbox"]
