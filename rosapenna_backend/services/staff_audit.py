"""Append-only audit events for staff-only operations."""

import uuid
from datetime import datetime, timezone

from pymongo import DESCENDING
from pymongo.collection import Collection


class StaffAuditLog:
    """Persist minimal staff actions without guest data or credentials."""

    def __init__(self, collection: Collection):
        self.collection = collection
        self.collection.create_index(
            [("actor_issuer", 1), ("actor_subject", 1), ("occurred_at", DESCENDING)],
            name="staff_actor_time",
        )
        self.collection.create_index(
            [("occurred_at", DESCENDING)], name="staff_event_time",
        )

    def record(self, principal, action: str, resource: str, outcome: str) -> None:
        """Append one event using the authenticated staff identity and route metadata."""
        self.collection.insert_one({
            "_id": uuid.uuid4().hex,
            "occurred_at": datetime.now(timezone.utc),
            "actor_issuer": principal.issuer,
            "actor_subject": principal.subject,
            "actor_roles": sorted(principal.roles),
            "action": action,
            "resource": resource,
            "outcome": outcome,
        })
