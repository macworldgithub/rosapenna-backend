"""Service layer for Manage My Booking (MMB) independent release control."""

from __future__ import annotations

import datetime as dt
from typing import Any, Dict, Optional

from pymongo.collection import Collection
from pymongo.database import Database
from pymongo.errors import PyMongoError

from rosapenna_backend.api.schemas.mmb_gate import StaffMmbGateUpdateRequest
from rosapenna_backend.api.staff_security import StaffPrincipal
from rosapenna_backend.config import Settings, settings as default_settings
from rosapenna_backend.core.logging import logger
from rosapenna_backend.services.staff_audit import StaffAuditLog

GATE_DOCUMENT_ID = "mmb_release_gate"
DEFAULT_OFF_REASON = "Manage My Booking is disabled by default pending explicit written activation."


class MmbGateService:
    """Manages the server-enforced, persistent release gate for Manage My Booking."""

    def __init__(
        self,
        database: Optional[Database] = None,
        settings: Optional[Settings] = None,
    ):
        self.database = database
        self.settings = settings or default_settings
        self._memory_state: Dict[str, Any] = {
            "_id": GATE_DOCUMENT_ID,
            "enabled": False,
            "updated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "updated_by": "system_default",
            "written_request_reference": None,
            "reason": DEFAULT_OFF_REASON,
            "environment": self.settings.environment,
        }

        if self.database is not None:
            try:
                self.collection: Optional[Collection] = self.database["mmb_release_gate"]
            except Exception as exc:
                logger.warning("Could not access mmb_release_gate collection: %s", exc)
                self.collection = None
        else:
            self.collection = None

    def is_enabled(self) -> bool:
        """Evaluate the authoritative server gate. Defaults strictly to False (fail closed)."""
        if self.collection is not None:
            try:
                doc = self.collection.find_one({"_id": GATE_DOCUMENT_ID})
                if doc:
                    return bool(doc.get("enabled", False))
                return False
            except PyMongoError as err:
                logger.warning("MongoDB read failed for MMB release gate (%s); failing closed", err)
                return False
        return bool(self._memory_state.get("enabled", False))

    def get_public_status(self) -> Dict[str, Any]:
        """Public status summary safe for guest-facing responses without data leakage."""
        active = self.is_enabled()
        message = (
            "Manage My Booking is active."
            if active
            else "Manage My Booking is currently disabled pending hotel written activation."
        )
        updated_at = None
        if self.collection is not None:
            try:
                doc = self.collection.find_one({"_id": GATE_DOCUMENT_ID})
                if doc and "updated_at" in doc:
                    updated_at = doc["updated_at"]
            except Exception:
                pass
        if not updated_at:
            updated_at = self._memory_state.get("updated_at")

        return {
            "enabled": active,
            "message": message,
            "updated_at": updated_at,
            "environment": self.settings.environment,
        }

    def get_staff_status(self) -> Dict[str, Any]:
        """Detailed gate state and written approval references for authorized staff."""
        state = dict(self._memory_state)
        audit_count = 0

        if self.collection is not None:
            try:
                doc = self.collection.find_one({"_id": GATE_DOCUMENT_ID})
                if doc:
                    state = {
                        "enabled": bool(doc.get("enabled", False)),
                        "updated_at": doc.get("updated_at", self._memory_state["updated_at"]),
                        "updated_by": doc.get("updated_by", "system"),
                        "written_request_reference": doc.get("written_request_reference"),
                        "reason": doc.get("reason", DEFAULT_OFF_REASON),
                        "environment": doc.get("environment", self.settings.environment),
                    }
            except Exception as exc:
                logger.warning("Failed to fetch MMB gate state from MongoDB: %s", exc)

        if self.database is not None:
            try:
                audit_coll = self.database["staff_audit_events"]
                audit_count = audit_coll.count_documents({"resource": GATE_DOCUMENT_ID})
            except Exception:
                pass

        return {
            "enabled": bool(state.get("enabled", False)),
            "updated_at": state.get("updated_at", dt.datetime.now(dt.timezone.utc).isoformat()),
            "updated_by": state.get("updated_by", "system_default"),
            "written_request_reference": state.get("written_request_reference"),
            "reason": state.get("reason"),
            "environment": state.get("environment", self.settings.environment),
            "audit_events_count": audit_count,
        }

    def update_gate(
        self,
        request: StaffMmbGateUpdateRequest,
        principal: StaffPrincipal,
        audit: StaffAuditLog,
    ) -> Dict[str, Any]:
        """Update the MMB gate state with mandatory written request reference and audit logging."""
        now = dt.datetime.now(dt.timezone.utc).isoformat()
        actor_name = f"{principal.subject}@{principal.issuer}"

        update_doc = {
            "_id": GATE_DOCUMENT_ID,
            "enabled": request.enabled,
            "updated_at": now,
            "updated_by": actor_name,
            "written_request_reference": request.written_request_reference.strip(),
            "reason": request.reason.strip(),
            "environment": self.settings.environment,
        }

        self._memory_state = dict(update_doc)

        if self.collection is not None:
            try:
                self.collection.update_one(
                    {"_id": GATE_DOCUMENT_ID},
                    {"$set": update_doc},
                    upsert=True,
                )
            except PyMongoError as exc:
                logger.error("Failed to persist MMB gate update to MongoDB: %s", exc)
                raise

        # Record immutable staff audit log
        action_name = "mmb.gate.enable" if request.enabled else "mmb.gate.disable"
        audit.record(
            principal=principal,
            action=action_name,
            resource=GATE_DOCUMENT_ID,
            outcome=f"enabled={request.enabled}, ref={request.written_request_reference}",
        )

        logger.info(
            "MMB release gate updated to enabled=%s by %s (ref: %s)",
            request.enabled,
            actor_name,
            request.written_request_reference,
        )

        return self.get_staff_status()
