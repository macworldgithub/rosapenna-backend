"""N10 input and booking rules for the isolated rooming sandbox."""

import datetime as dt
import re
import unicodedata
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator, model_validator

Identifier = Annotated[str, StringConstraints(pattern=r"^[a-zA-Z0-9_-]{1,64}$")]
SecretToken = Annotated[str, StringConstraints(pattern=r"^[a-zA-Z0-9_-]{43}$")]


class RoomingModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, hide_input_in_errors=True)


def plain_text(value: str) -> str:
    if any(unicodedata.category(character) in {"Cc", "Cs"} for character in value):
        raise ValueError("Text contains an unsupported character")
    return value


class Interval(RoomingModel):
    start: dt.date
    end: dt.date

    @field_validator("start", "end", mode="before")
    @classmethod
    def iso_date(cls, value):
        if not isinstance(value, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
            raise ValueError("Use dates in YYYY-MM-DD format")
        return value

    @model_validator(mode="after")
    def ordered(self):
        if not 0 < (self.end - self.start).days <= 90:
            raise ValueError("Enter a valid date interval")
        return self

    def covers(self, other: "Interval") -> bool:
        return self.start <= other.start and other.end <= self.end

    def nights(self):
        return (self.start + dt.timedelta(days=index) for index in range((self.end - self.start).days))


class Group(Interval):
    id: Identifier
    label: str = Field(min_length=1, max_length=80)
    total_limit: int = Field(strict=True, ge=1, le=20)
    child_limit: int = Field(strict=True, ge=0, le=20)
    adult_limit: int = Field(strict=True, ge=0, le=20)


class Slot(Interval):
    id: Identifier
    category: Literal["adult", "child"]


class Fixture(Interval):
    reference: Identifier
    lead_email: Annotated[str, StringConstraints(pattern=r"^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9.-]+\.invalid$", max_length=254)]
    lead_version: Identifier
    confirmed: bool = Field(strict=True)
    groups: list[Group] = Field(min_length=1, max_length=10)
    slots: list[Slot] = Field(min_length=1, max_length=20)

    @model_validator(mode="after")
    def valid_mapping(self):
        for items in (self.groups, self.slots):
            if len({item.id for item in items}) != len(items) or not all(self.covers(item) for item in items):
                raise ValueError("Invalid booking mapping")
        return self


class InviteExchange(RoomingModel):
    invite_token: SecretToken


class Occupant(Interval):
    id: Identifier
    group_id: Identifier
    first_name: str = Field(min_length=1, max_length=100)
    last_name: str = Field(min_length=1, max_length=100)

    _safe_names = field_validator("first_name", "last_name")(plain_text)


class SharingRequest(Interval):
    group_id: Identifier
    notes: str = Field(min_length=1, max_length=200)

    _safe_notes = field_validator("notes")(plain_text)


class RoomingSnapshot(RoomingModel):
    occupants: list[Occupant] = Field(min_length=1, max_length=20)
    sharing_requests: list[SharingRequest] = Field(max_length=40)


class Submission(RoomingSnapshot):
    version: int = Field(strict=True, ge=1)
    request_id: Identifier
    reviewed: Literal[True]

    @field_validator("reviewed", mode="before")
    @classmethod
    def checked_review(cls, value):
        if value is not True:
            raise ValueError("Review the rooming list before you submit it")
        return value
