"""Persistence and state transitions for online booking intents."""

from __future__ import annotations

import uuid
import hashlib
import json
from datetime import datetime, timezone

from pymongo import ASCENDING, ReturnDocument
from pymongo.collection import Collection
from pymongo.errors import DuplicateKeyError

from rosapenna_backend.api.schemas.booking_intents import (
    MAX_ACTIONS,
    BookingIntentAction,
    BookingIntentCreate,
    OperaReservationPayload,
)
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
    result["booking_reference"] = document.get("booking_reference")
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
        self.collection.create_index(
            [("booking_reference", ASCENDING)],
            sparse=True,
            name="idx_booking_intent_reference",
        )
        self.collection.create_index(
            [("links.opera_reservation.reservation_id", ASCENDING)],
            sparse=True,
            name="idx_booking_intent_opera_res",
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
        booking_ref = f"RSP-{selected.date.strftime('%Y%m%d')}-{uuid.uuid4().hex[:6].upper()}"
        document = {
            "_id": intent_id,
            "booking_reference": booking_ref,
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
        elif request.action == "link_opera_reservation":
            assert request.opera_payload is not None
            opera_data = request.opera_payload.model_dump(mode="json")
            existing_opera = document.get("links", {}).get("opera_reservation")
            if existing_opera and existing_opera.get("reservation_id") == opera_data["reservation_id"]:
                evidence["idempotent_replay"] = True
            evidence["opera_reservation"] = opera_data
            update["links.opera_reservation"] = opera_data
            update["reservation_state"] = "reserved"
        elif request.action == "link_brs_round":
            assert request.brs_payload is not None
            brs_data = request.brs_payload.model_dump(mode="json")
            existing_rounds = document.get("links", {}).get("brs_rounds", [])
            updated_rounds = [r for r in existing_rounds if r.get("round_id") != brs_data["round_id"]]
            updated_rounds.append(brs_data)
            evidence["brs_round"] = brs_data
            update["links.brs_rounds"] = updated_rounds
        elif request.action == "record_payment":
            assert request.payment_payload is not None
            pay_data = request.payment_payload.model_dump(mode="json")
            existing_payments = document.get("links", {}).get("payments", [])
            updated_payments = [p for p in existing_payments if p.get("payment_id") != pay_data["payment_id"]]
            updated_payments.append(pay_data)
            evidence["payment"] = pay_data
            update["links.payments"] = updated_payments
            if pay_data["status"] == "completed":
                p_state = "deposit_paid" if pay_data["payment_type"] == "deposit" else "fully_paid"
                update["payment_state"] = p_state
                if document.get("reservation_state") in {"reserved", "confirmed"} and document.get("links", {}).get("opera_reservation"):
                    next_stage = "confirmed"
                    update["reservation_state"] = "confirmed"
            elif pay_data["status"] == "failed":
                update["payment_state"] = "payment_failed"
        elif request.action == "confirm_booking":
            next_stage = "confirmed"
            update["reservation_state"] = "confirmed"
        elif request.action == "mark_recovery_required":
            next_stage = "recovery_required"

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
        if action == "link_opera_reservation":
            return stage, "opera_integration"
        if action == "link_brs_round":
            return stage, "brs_integration"
        if action == "record_payment":
            return stage, "payment_integration"
        if action == "confirm_booking":
            return "confirmed", "booking_orchestration"
        if action == "mark_recovery_required":
            return "recovery_required", "staff_recovery"
        raise IntentConflict("This booking intent cannot accept that action in its current stage")

    def get_by_reference(self, reference_or_id: str) -> dict:
        clean = reference_or_id.strip()
        query = {
            "$or": [
                {"_id": clean},
                {"booking_reference": clean},
                {"links.opera_reservation.reservation_id": clean},
                {"links.opera_reservation.confirmation_number": clean},
            ]
        }
        document = self.collection.find_one(query)
        if document is None:
            raise IntentNotFound("Booking record was not found")
        return public_intent(document)

    def link_opera_reservation(
        self,
        intent_id: str,
        expected_revision: int,
        opera_payload: OperaReservationPayload,
    ) -> tuple[dict, bool]:
        """Link an authoritative Opera reservation payload to the booking intent."""
        action = BookingIntentAction(
            action_id=uuid.uuid4(),
            expected_revision=expected_revision,
            action="link_opera_reservation",
            opera_payload=opera_payload,
        )
        return self.append_action(intent_id, action)

    def link_brs_round(
        self,
        intent_id: str,
        expected_revision: int,
        brs_payload: BrsRoundPayload,
        action_id: uuid.UUID | None = None,
    ) -> tuple[dict, bool]:
        """Link an authoritative BRS golf round payload to the booking intent."""
        action = BookingIntentAction(
            action_id=action_id or uuid.uuid4(),
            expected_revision=expected_revision,
            action="link_brs_round",
            brs_payload=brs_payload,
        )
        return self.append_action(intent_id, action)

    def record_payment(
        self,
        intent_id: str,
        expected_revision: int,
        payment_payload: PaymentRecordPayload,
        action_id: uuid.UUID | None = None,
    ) -> tuple[dict, bool]:
        """Record an authoritative payment transaction in the booking ledger."""
        action = BookingIntentAction(
            action_id=action_id or uuid.uuid4(),
            expected_revision=expected_revision,
            action="record_payment",
            payment_payload=payment_payload,
        )
        return self.append_action(intent_id, action)

    def mark_recovery_required(
        self,
        intent_id: str,
        expected_revision: int,
        detail_code: str = "upstream_response_error",
    ) -> tuple[dict, bool]:
        """Mark booking intent as requiring manual staff recovery after an ambiguous failure."""
        action = BookingIntentAction(
            action_id=uuid.uuid4(),
            expected_revision=expected_revision,
            action="mark_recovery_required",
            detail_code=detail_code,
        )
        return self.append_action(intent_id, action)
