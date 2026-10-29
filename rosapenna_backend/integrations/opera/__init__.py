"""Oracle Opera Cloud / OHIP integration package."""

from rosapenna_backend.integrations.opera.client import OperaClient, OperaIntegrationError
from rosapenna_backend.integrations.opera.schemas import (
    RoomAvailabilityResponse,
    RoomCategoryAvailability,
    RoomOccupant,
    RoomRatePlan,
    RoomReservationDetail,
)

__all__ = [
    "OperaClient",
    "OperaIntegrationError",
    "RoomAvailabilityResponse",
    "RoomCategoryAvailability",
    "RoomOccupant",
    "RoomRatePlan",
    "RoomReservationDetail",
]
