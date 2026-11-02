"""Schemas for Manage My Booking (MMB) release control and feature gating."""

from __future__ import annotations

from typing import Optional
from pydantic import BaseModel, ConfigDict, Field


class MmbStatusResponse(BaseModel):
    """Public status response describing whether Manage My Booking is active."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    enabled: bool = False
    message: str
    updated_at: Optional[str] = None
    environment: str = "production"


class StaffMmbGateStatusResponse(BaseModel):
    """Authorized staff status response with audit and written-approval details."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    enabled: bool
    updated_at: str
    updated_by: str
    written_request_reference: Optional[str] = None
    reason: Optional[str] = None
    environment: str
    audit_events_count: int = 0


class StaffMmbGateUpdateRequest(BaseModel):
    """Staff payload to enable or disable the MMB release gate with mandatory written request reference."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    enabled: bool = Field(description="Target gate state: True to enable, False to disable.")
    written_request_reference: str = Field(
        min_length=5,
        max_length=100,
        description="Mandatory formal written request reference (e.g. RSP-MMB-2026-ACTIVATE).",
    )
    reason: str = Field(
        min_length=5,
        max_length=500,
        description="Audit reason explaining why the state change was requested.",
    )
