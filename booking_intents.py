"""Compatibility exports for booking-intent schemas and service."""

from rosapenna_backend.api.schemas.booking_intents import (
    BookingIntentAction, BookingIntentCreate, LeadGuest, QuoteSelection, RevalidatedQuote,
)
from rosapenna_backend.services.booking_intents import (
    BookingIntentStore, IntentConflict, IntentNotFound, public_intent,
)

__all__ = [
    "BookingIntentAction", "BookingIntentCreate", "BookingIntentStore", "IntentConflict",
    "IntentNotFound", "LeadGuest", "QuoteSelection", "RevalidatedQuote", "public_intent",
]
