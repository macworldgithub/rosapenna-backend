"""Pydantic schemas for Prommt API integration (Ticket F11)."""

from __future__ import annotations

import datetime as dt
from typing import Any, Dict, List, Literal, Optional, Union
from pydantic import BaseModel, ConfigDict, Field, field_validator

PRICE_PATTERN = r"^[+-]?\d{1,8}(?:\.\d{1,2})?$"


def _price(value: Union[str, float, int]) -> str:
    return f"{float(value):.2f}"


class PrommtCreatePaymentRequest(BaseModel):
    """Payload to create a payment request via Prommt POST /api/v1/paymentrequests."""
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, populate_by_name=True)

    amount: float = Field(gt=0, description="Amount to be paid by the customer.")
    currency: str = Field(default="EUR", pattern=r"^[A-Z]{3}$")
    channel: Literal["LINK", "EMAIL", "SMS", "WEBSITE"] = "LINK"
    payer_email: str = Field(alias="payerEmail", min_length=3, max_length=254)
    payer_first_name: Optional[str] = Field(default=None, alias="payerFirstName", max_length=100)
    payer_last_name: Optional[str] = Field(default=None, alias="payerLastName", max_length=100)
    payer_mobile: Optional[str] = Field(default=None, alias="payerMobile", max_length=40)
    description: Optional[str] = Field(default=None, max_length=250)
    external_id: Optional[str] = Field(default=None, alias="externalId", max_length=100)
    redirect_url: Optional[str] = Field(default=None, alias="redirectUrl", max_length=500)
    payment_methods: List[str] = Field(default_factory=lambda: ["CARD"], alias="paymentMethods")
    location_id: Optional[int] = Field(default=None, alias="locationId")

    @field_validator("payer_email")
    @classmethod
    def validate_email(cls, v: str) -> str:
        clean = v.strip().lower()
        if "@" not in clean or "." not in clean:
            raise ValueError("Enter a valid email address.")
        return clean


class PrommtPaymentRequestResponse(BaseModel):
    """Response returned when a payment request is created."""
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    resource_id: str = Field(alias="resourceId")
    status: str = Field(default="CREATED")
    message: Optional[str] = None
    payment_url: Optional[str] = Field(default=None, alias="url")


class PrommtPaymentLinkResponse(BaseModel):
    """Response from GET /api/v1/paymentrequests/{paymentRequestId}/link."""
    model_config = ConfigDict(extra="ignore")

    url: str


class PrommtPaymentDetailsResponse(BaseModel):
    """Details returned from GET /api/v1/paymentrequests/{paymentRequestId}."""
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    id: str
    status: str
    amount: float
    currency: str = "EUR"
    payer_email: Optional[str] = Field(default=None, alias="payerEmail")
    channel: Optional[str] = None
    paid_at: Optional[str] = Field(default=None, alias="paidAt")


class PrommtWebhookPayload(BaseModel):
    """Webhook event sent by Prommt on transaction state changes."""
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    event_id: str = Field(alias="eventId")
    type: str = Field(default="PAYMENT_REQUEST")
    timestamp: Union[int, str]
    event: str  # PAID_SUCCESS, PAID_FAILED, EXPIRED, CANCELED, SENT, etc.
    value: Optional[str] = None
    resource_id: str = Field(alias="resourceId")
    client_id: Optional[str] = Field(default=None, alias="clientId")
    mode: str = Field(default="TEST")
    external_id: Optional[str] = Field(default=None, alias="externalId")
    group_id: Optional[str] = Field(default=None, alias="groupId")
    amount: Optional[float] = None
    currency: Optional[str] = "EUR"
