"""Persistence and state transitions for online booking intents."""

from __future__ import annotations

import uuid
import hashlib
import json
from datetime import datetime, timezone

from pymongo import ASCENDING, ReturnDocument
from pymongo.collection import Collection
from pymongo.errors import DuplicateKeyError

from rosapenna_backend.api.schemas.booking_intents import MAX_ACTIONS, BookingIntentAction, BookingIntentCreate
from rosapenna_backend.integrations.brs.tee_sheet import COURSES

class IntentConflict(ValueError):
    pass

class IntentNotFound(LookupError):
    pass

def _fingerprint(value: dict) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

def _utc_now() -> datetime:
    return datetime.now(timezone.utc)

def _event_fingerprint(action: BookingIntentAction) -> str:
    return _fingerprint(action.model_dump(mode="json", exclude={"expected_revision"}))

def public_intent(document: dict) -> dict:
    """Return only the supported API contract, never internal hashes or Mongo fields."""
    result = {
        key: value for key, value in document.items()
        if key not in {"_id", "idempotency_key_hash", "create_fingerprint"}
    }
    result["booking_intent_id"] = document["_id"]
    result["actions"] = [
        {key: value for key, value in action.items() if key != "request_fingerprint"}
        for action in document.get("actions", [])
    ]
    return result

class BookingIntentStore:
    """Append-only action history with optimistic revisions and unique idempotency."""

    def __init__(self, collection: Collection):
        self.collection = collection
        self.collection.create_index(
            [("idempotency_key_hash", ASCENDING)],
            unique=True,
            name="uniq_booking_intent_idempotency",
        )

    def create(self, idempotency_key: uuid.UUID, request: BookingIntentCreate) -> tuple[dict, bool]:
        request_data = request.model_dump(mode="json")
        request_fingerprint = _fingerprint(request_data)
        key_hash = hashlib.sha256(idempotency_key.bytes).hexdigest()
        existing = self.collection.find_one({"idempotency_key_hash": key_hash})
        if existing:
            if existing["create_fingerprint"] != request_fingerprint:
                raise IntentConflict("This idempotency key was already used for different booking details")
            return public_intent(existing), False

        now = _utc_now()
        selected = request.selection
        course = COURSES[selected.course_id]
        quote_evidence = {
            "version": 1,
            "source": "website_submitted_quote",
            "amount": selected.price,
            "currency": selected.currency,
            "captured_at": now,
        }
        intent_id = f"bki_{uuid.uuid4().hex}"
        document = {
            "_id": intent_id,
            "idempotency_key_hash": key_hash,
            "create_fingerprint": request_fingerprint,
            "source": "website_checkout",
            "selection": {
                "date": selected.date.isoformat(),
                "course_id": selected.course_id,
                "course_name": course["name"],
                "tee_time_id": selected.tee_time_id,
                "time": selected.time,
                "holes": selected.holes,
                "package_name": selected.package_name,
                "green_fee_id": selected.green_fee_id,
                "players": selected.players,
            },
            "lead_guest": request.lead_guest.model_dump(mode="json"),
            "quote": {"version": 1, "selected": quote_evidence, "live_revalidated": None},
            "links": {"opera_reservation": None, "brs_rounds": [], "payments": []},
            "stage": "draft",
            "payment_state": "not_started",
            "reservation_state": "not_confirmed",
            "created_at": now,
            "updated_at": now,
            "last_action_at": now,
            "revision": 1,
            "actions": [{
                "action_id": str(uuid.uuid4()),
                "action": "draft_created",
                "source": "website_checkout",
                "occurred_at": now,
                "stage": "draft",
                "revision": 1,
                "evidence": {"quote_version": 1, "quote_source": "website_submitted_quote"},
            }],
        }
        try:
            self.collection.insert_one(document)
            return public_intent(document), True
        except DuplicateKeyError:
            existing = self.collection.find_one({"idempotency_key_hash": key_hash})
            if existing and existing.get("create_fingerprint") == request_fingerprint:
                return public_intent(existing), False
            if existing:
                raise IntentConflict("This idempotency key was already used for different booking details") from None
            raise IntentConflict("Booking intent identifier collision; please retry with the same request key") from None

    def get(self, intent_id: str) -> dict:
        document = self.collection.find_one({"_id": intent_id})
        if document is None:
            raise IntentNotFound("Booking intent was not found")
        return public_intent(document)

    def append_action(self, intent_id: str, request: BookingIntentAction) -> tuple[dict, bool]:
        action_id = str(request.action_id)
        action_fingerprint = _event_fingerprint(request)
        document = self.collection.find_one({"_id": intent_id})
        if document is None:
            raise IntentNotFound("Booking intent was not found")

        previous = next((item for item in document.get("actions", []) if item["action_id"] == action_id), None)
        if previous is not None:
            if previous.get("request_fingerprint") != action_fingerprint:
                raise IntentConflict("This action identifier was already used for different evidence")
            return public_intent(document), False

        if document["revision"] != request.expected_revision:
            raise IntentConflict("Booking intent revision changed; reload it before applying this update")
        if len(document.get("actions", [])) >= MAX_ACTIONS:
            raise IntentConflict("Booking intent history reached its safe limit; staff recovery is required")

        current_stage = document["stage"]
        next_stage, source = self._next_stage(document, request)
        now = _utc_now()
        evidence: dict = {}
        update: dict = {}
        if request.status_code is not None:
            evidence["status_code"] = request.status_code
        if request.detail_code is not None:
            evidence["detail_code"] = request.detail_code
        if request.action == "live_revalidation_passed":
            quote_version = document["quote"]["version"] + 1
            revalidated = {
                "version": quote_version,
                "source": "live_teesheet_revalidation",
                "amount": request.quote.price,
                "currency": request.quote.currency,
                "observed_at": now,
            }
            evidence["quote"] = revalidated.copy()
            update["quote.version"] = quote_version
            update["quote.live_revalidated"] = revalidated

        next_revision = request.expected_revision + 1
        event = {
            "action_id": action_id,
            "action": request.action,
            "source": source,
            "occurred_at": now,
            "stage": next_stage,
            "revision": next_revision,
            "evidence": evidence,
            "request_fingerprint": action_fingerprint,
        }
        filter_query = {
            "_id": intent_id,
            "revision": request.expected_revision,
            "stage": current_stage,
            "actions.action_id": {"$ne": action_id},
        }
        set_values = {"stage": next_stage, "updated_at": now, "last_action_at": now, **update}
        updated = self.collection.find_one_and_update(
            filter_query,
            {"$set": set_values, "$inc": {"revision": 1}, "$push": {"actions": event}},
            return_document=ReturnDocument.AFTER,
        )
        if updated is not None:
            return public_intent(updated), True

        latest = self.collection.find_one({"_id": intent_id})
        if latest is None:
            raise IntentNotFound("Booking intent was not found")
        repeated = next((item for item in latest.get("actions", []) if item["action_id"] == action_id), None)
        if repeated is not None and repeated.get("request_fingerprint") == action_fingerprint:
            return public_intent(latest), False
        raise IntentConflict("Booking intent revision changed; reload it before applying this update")

    @staticmethod
    def _next_stage(document: dict, request: BookingIntentAction) -> tuple[str, str]:
        stage = document["stage"]
        action = request.action
        retry_after_429 = (
            stage == "checkout_preparation_failed"
            and bool(document.get("actions"))
            and document["actions"][-1].get("action") == "checkout_preparation_failed"
            and document["actions"][-1].get("evidence", {}).get("status_code") == 429
        )
        if action == "live_revalidation_passed" and stage in {
            "draft", "quote_revalidation_failed", "quote_revalidation_unavailable",
            "quote_revalidated", "checkout_preparation_blocked",
        } | ({"checkout_preparation_failed"} if retry_after_429 else set()):
            return "quote_revalidated", "booking_backend"
        if action == "live_revalidation_failed" and stage in {
            "draft", "quote_revalidation_failed", "quote_revalidation_unavailable",
            "quote_revalidated", "checkout_preparation_blocked",
        } | ({"checkout_preparation_failed"} if retry_after_429 else set()):
            return "quote_revalidation_failed", "booking_backend"
        if action == "live_revalidation_unavailable" and stage in {
            "draft", "quote_revalidation_failed", "quote_revalidation_unavailable",
            "quote_revalidated", "checkout_preparation_blocked",
        } | ({"checkout_preparation_failed"} if retry_after_429 else set()):
            return "quote_revalidation_unavailable", "booking_backend"
        if action == "checkout_preparation_started":
            if stage in {"quote_revalidated", "checkout_preparation_blocked"} or retry_after_429:
                return "checkout_preparation_pending", "booking_backend"
        if action == "checkout_preparation_response_received" and stage == "checkout_preparation_pending":
            return "checkout_preparation_response_received", "brs_checkout"
        if action == "checkout_preparation_failed" and stage == "checkout_preparation_pending":
            return "checkout_preparation_failed", "brs_checkout"
        if action == "checkout_preparation_blocked" and stage in {
            "quote_revalidated", "checkout_preparation_pending",
        }:
            source = "local_checkout_cooldown" if request.detail_code == "local_cooldown" else "backend_configuration"
            return "checkout_preparation_blocked", source
        if action == "checkout_preparation_outcome_unknown" and stage == "checkout_preparation_pending":
            return "recovery_required", "brs_checkout"
        raise IntentConflict("This booking intent cannot accept that action in its current stage")
