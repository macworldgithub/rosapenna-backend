"""Fail-closed staff authorization until the scoped external OIDC is configured."""

from dataclasses import dataclass

from fastapi import HTTPException


@dataclass(frozen=True)
class StaffPrincipal:
    """A verified external staff identity and its application roles."""

    issuer: str
    subject: str
    roles: frozenset[str]


def require_staff_roles(*required_roles: str):
    """Keep protected staff operations closed until OIDC validation is implemented."""
    if not required_roles:
        raise ValueError("A staff dependency must name one or more required roles")

    def dependency() -> StaffPrincipal:
        raise HTTPException(503, "External OIDC staff sign-in is not configured")

    return dependency
