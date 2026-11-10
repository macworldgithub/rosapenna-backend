"""Domain service for Ticket M13 — Special and Cancellation Requests.

Enforces:
- M13 AC 1: Request stored strictly against authenticated booking; no unsafe rendering or cross-booking access.
- M13 AC 2: Each request gets a unique durable ID (REQ-...) and 'pending_review' status; idempotency key prevents duplicate requests.
- M13 AC 3: Acknowledgment confirms pending review and is sent only after persistence succeeds.
- M13 AC 4: Routing through F12 email delivery records outcome; failure stays pending and alerts staff.
- M13 AC 5 & 6: Zero mutation to rooms, golf rounds, payments ledger, or inventory holds.
- M13 AC 7: Guest sees only their own dated request status; staff gets context without credential leakage.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import uuid
from typing import Any, Dict, List, Optional
from pymongo import ASCENDING
from pymongo.collection import Collection
from pymongo.database import Database

from rosapenna_backend.api.schemas.mmb_requests import (
    GuestRequestsListResponse,
    GuestSpecialRequestCreate,
    GuestSpecialRequestItem,
    GuestSpecialRequestResponse,
)
from rosapenna_backend.core.logging import logger
from rosapenna_backend.services.booking_linkage import (
    BookingLinkageResponse,
    BookingLinkageService,
    LinkageConflictError,
    LinkageNotFoundError,
)
from rosapenna_backend.services.email_delivery import EmailDeliveryResult, EmailDeliveryService


class MmbRequestError(Exception):
    """Base exception for MMB request operations."""

    def __init__(self, message: str, status_code: int = 400, detail_code: str = "request_error"):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.detail_code = detail_code


class MmbRequestNotFoundError(MmbRequestError):
    def __init__(self, message: str = "Booking record not found."):
        super().__init__(message, status_code=404, detail_code="request_not_found")


class MmbRequestConflictError(MmbRequestError):
    def __init__(self, message: str = "Request conflict. Please refresh."):
        super().__init__(message, status_code=409, detail_code="request_conflict")


def _utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


class MmbRequestService:
    """Service managing authenticated special and cancellation requests."""

    def __init__(
        self,
        database: Optional[Database] = None,
        linkage_service: Optional[BookingLinkageService] = None,
        email_service: Optional[EmailDeliveryService] = None,
        collection_name: str = "mmb_special_requests",
    ):
        self.linkage_service = linkage_service or BookingLinkageService(database=database)
        self.email_service = email_service or EmailDeliveryService()
        self.collection: Optional[Collection] = (
            database[collection_name] if database is not None else None
        )
        self._in_memory_records: Dict[str, Dict[str, Any]] = {}
        if self.collection is not None:
            self._init_indexes()

    def _init_indexes(self) -> None:
        assert self.collection is not None
        self.collection.create_index([("request_id", ASCENDING)], unique=True, name="uniq_guest_req_id")
        self.collection.create_index(
            [("booking_reference", ASCENDING), ("idempotency_hash", ASCENDING)],
            unique=True,
            name="uniq_booking_idemp_req",
        )

    def _hash_key(self, booking_reference: str, idempotency_key: str) -> str:
        raw = f"{booking_reference.strip().upper()}:{idempotency_key.strip()}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def get_guest_requests(self, booking_reference: str) -> GuestRequestsListResponse:
        """Retrieve all submitted requests for an authenticated booking."""
        clean_ref = booking_reference.strip().upper()
        try:
            linkage: BookingLinkageResponse = self.linkage_service.get_linkage(clean_ref)
        except LinkageNotFoundError as exc:
            raise MmbRequestNotFoundError(f"Booking reference '{clean_ref}' not found.") from exc

        items: List[GuestSpecialRequestItem] = []
        for p in linkage.pending_requests:
            items.append(
                GuestSpecialRequestItem(
                    request_id=p.request_id,
                    booking_reference=clean_ref,
                    request_type="cancellation_request" if "cancellation" in p.request_type else "special_request",
                    category="cancellation_inquiry" if "cancellation" in p.request_type else "general_inquiry",
                    request_text=p.description,
                    status=p.status,
                    submitted_at=p.submitted_at,
                    notes=p.notes,
                )
            )

        return GuestRequestsListResponse(
            booking_reference=clean_ref,
            booking_revision=linkage.revision,
            requests=items,
            read_at=_utc_now(),
        )

    def submit_request(
        self,
        booking_reference: str,
        request: GuestSpecialRequestCreate,
        idempotency_key: str,
        actor: str,
    ) -> GuestSpecialRequestResponse:
        """Atomically persist a special or cancellation request and dispatch F12 acknowledgement."""
        clean_ref = booking_reference.strip().upper()
        clean_key = idempotency_key.strip()
        if not clean_key or len(clean_key) < 8:
            raise MmbRequestError("A valid Idempotency-Key header (min 8 chars) is required.", status_code=400)

        if request.booking_reference.strip().upper() != clean_ref:
            raise MmbRequestError("Payload booking_reference does not match route path.", status_code=400)

        idemp_hash = self._hash_key(clean_ref, clean_key)

        # 1. Idempotency replay check
        existing_rec = None
        if self.collection is not None:
            existing_rec = self.collection.find_one({
                "booking_reference": clean_ref,
                "idempotency_hash": idemp_hash,
            })
        else:
            existing_rec = self._in_memory_records.get(f"{clean_ref}:{idemp_hash}")

        if existing_rec is not None:
            stored_req = existing_rec.get("request_payload")
            current_payload = request.model_dump(mode="json")
            if stored_req != current_payload:
                raise MmbRequestConflictError(
                    "Idempotency key previously used with different request parameters."
                )
            stored_res = existing_rec.get("response")
            if stored_res:
                logger.info("Replaying idempotent request submission for booking %s", clean_ref)
                res_obj = GuestSpecialRequestResponse(**stored_res)
                res_obj.replayed = True
                return res_obj

        # 2. Fetch authoritative booking
        try:
            linkage: BookingLinkageResponse = self.linkage_service.get_linkage(clean_ref)
        except LinkageNotFoundError as exc:
            raise MmbRequestNotFoundError(f"Booking reference '{clean_ref}' not found.") from exc

        if request.expected_revision is not None and linkage.revision != request.expected_revision:
            raise MmbRequestConflictError(
                f"Booking revision conflict: expected {request.expected_revision}, current is {linkage.revision}. Please refresh."
            )

        # 3. Create unique durable request record
        req_id = f"REQ-{uuid.uuid4().hex[:8].upper()}"
        now = _utc_now()

        item_dict = {
            "request_id": req_id,
            "request_type": request.request_type,
            "category": request.category,
            "description": request.request_text,
            "desired_dates": request.desired_dates,
            "desired_room_category": request.desired_room_category,
            "desired_golf_notes": request.desired_golf_notes,
            "status": "pending_review",
            "submitted_at": now.isoformat(),
        }

        # 4. Atomically persist to booking linkage pending_requests
        try:
            updated_linkage = self.linkage_service.add_pending_request(
                booking_reference=clean_ref,
                request_item=item_dict,
                actor=actor,
                expected_revision=request.expected_revision,
            )
        except LinkageConflictError as exc:
            raise MmbRequestConflictError(str(exc)) from exc

        # 5. Route through F12 email delivery service (send after persistence)
        delivery_res: Optional[EmailDeliveryResult] = None
        delivery_status = "simulated_sandbox"
        delivery_id = None
        try:
            booking_dict = {
                "booking_reference": clean_ref,
                "lead_guest": {
                    "first_name": linkage.lead_guest.first_name,
                    "last_name": linkage.lead_guest.last_name,
                    "email": linkage.lead_guest.email,
                },
            }
            delivery_res = self.email_service.send_special_request_acknowledgement(
                booking=booking_dict,
                request_text=request.request_text,
                category=request.category,
            )
            delivery_status = delivery_res.status
            delivery_id = delivery_res.delivery_id
        except Exception as exc:
            logger.error("Failed to dispatch email acknowledgement for request %s on %s: %s", req_id, clean_ref, exc)
            delivery_status = "failed"

        msg = (
            f"Your {request.request_type.replace('_', ' ')} has been recorded (Reference: {req_id}) "
            "and forwarded to our Reservations team. It is currently pending staff review. "
            "Confirmed dates, tee times, inventory, and payment ledger entries remain completely unchanged."
        )

        response = GuestSpecialRequestResponse(
            request_id=req_id,
            booking_reference=clean_ref,
            request_type=request.request_type,
            category=request.category,
            status="pending_review",
            submitted_at=now,
            booking_revision=updated_linkage.revision,
            replayed=False,
            inventory_mutated=False,
            payment_mutated=False,
            email_delivery_status=delivery_status,
            email_delivery_id=delivery_id,
            message=msg,
        )

        # 6. Record operation for idempotency deduplication
        self._record_operation(clean_ref, idemp_hash, req_id, request, response)
        return response

    def _record_operation(
        self,
        booking_reference: str,
        idempotency_hash: str,
        request_id: str,
        request: GuestSpecialRequestCreate,
        response: GuestSpecialRequestResponse,
    ) -> None:
        record = {
            "booking_reference": booking_reference,
            "idempotency_hash": idempotency_hash,
            "request_id": request_id,
            "request_payload": request.model_dump(mode="json"),
            "response": response.model_dump(mode="json"),
            "created_at": _utc_now(),
        }
        if self.collection is not None:
            self.collection.replace_one(
                {"booking_reference": booking_reference, "idempotency_hash": idempotency_hash},
                record,
                upsert=True,
            )
        else:
            self._in_memory_records[f"{booking_reference}:{idempotency_hash}"] = record
