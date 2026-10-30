"""Role-protected staff operations with environment-configured staff credentials and dev bypass."""

from __future__ import annotations

import hmac
from dataclasses import dataclass
from typing import Optional

from fastapi import Header, HTTPException

from rosapenna_backend.config import settings


@dataclass(frozen=True)
class StaffPrincipal:
    """An authenticated staff identity and its application roles."""

    issuer: str
    subject: str
    roles: frozenset[str]


def require_staff_roles(*required_roles: str):
    """Authorize staff operations using configured STAFF_API_KEY with dev bypass support."""
    if not required_roles:
        raise ValueError("A staff dependency must name one or more required roles")

    def dependency(
        x_staff_token: Optional[str] = Header(default=None, alias="X-Staff-Token")
    ) -> StaffPrincipal:
        # Development mode bypass (if enabled by environment setting)
        if settings.environment.strip().lower() != "production" and settings.security_bypass_in_dev:
            if x_staff_token and settings.staff_api_key and not hmac.compare_digest(x_staff_token, settings.staff_api_key):
                raise HTTPException(status_code=401, detail="Missing or invalid X-Staff-Token header")
            return StaffPrincipal(
                issuer="local_dev",
                subject="developer",
                roles=frozenset(required_roles),
            )

        if not settings.staff_api_key:
            raise HTTPException(status_code=503, detail="Staff authentication is not configured")

        if not x_staff_token or not hmac.compare_digest(x_staff_token, settings.staff_api_key):
            raise HTTPException(status_code=401, detail="Missing or invalid X-Staff-Token header")

        return StaffPrincipal(
            issuer="rosapenna_staff_auth",
            subject="staff_operator",
            roles=frozenset(required_roles),
        )

    return dependency

