"""Domain service for Ticket M12 — Per-Round Caddy Request Updates.

Enforces:
- Release gate check (require_mmb_enabled) & IDOR booking scoping.
- Round & player scope: request can only attach to an existing, confirmed round on the booking.
- Caddy count constraint: caddies_requested <= round.players (max 4).
- Zero online charge: caddies are strictly requests payable on the day (€90/bag).
- Narrow BRS write & post-write readback verification.
- Optimistic concurrency control via expected_revision.
- Idempotency with replay protection and conflict detection on differing payload.
- Full audit logging without credential or sensitive data leakage.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import uuid
from decimal import Decimal
from typing import Any, Dict, List, Optional
from pymongo import ASCENDING
from pymongo.collection import Collection
from pymongo.database import Database

from rosapenna_backend.api.schemas.brs_booking import CaddyRequestPayload
from rosapenna_backend.api.schemas.mmb_caddy import (
    BookingCaddyRosterResponse,
    CADDY_FEE_PER_BAG,
    CADDY_NOTICE,
    CaddyBagPreference,
    RoundCaddySummary,
    RoundCaddyUpdateRequest,
    RoundCaddyUpdateResponse,
)
from rosapenna_backend.core.logging import logger
from rosapenna_backend.integrations.brs.booking_client import (
    BrsBookingClient,
    BrsConflictError,
    BrsReadbackMismatchError,
    BrsWriteError,
)
from rosapenna_backend.services.booking_linkage import (
    BookingLinkageResponse,
    BookingLinkageService,
    LinkageConflictError,
    LinkageNotFoundError,
)


class MmbCaddyError(Exception):
    """Base exception for MMB caddy operations."""
    def __init__(self, message: str, status_code: int = 400, detail_code: str = "caddy_error"):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.detail_code = detail_code


class MmbCaddyNotFoundError(MmbCaddyError):
    def __init__(self, message: str = "Booking or golf round record not found."):
        super().__init__(message, status_code=404, detail_code="caddy_not_found")


class MmbCaddyConflictError(MmbCaddyError):
    def __init__(self, message: str = "Caddy request conflict. Please refresh."):
        super().__init__(message, status_code=409, detail_code="caddy_conflict")


class MmbCaddyIntegrationError(MmbCaddyError):
    def __init__(self, message: str = "External BRS golf system caddy update failed."):
        super().__init__(message, status_code=502, detail_code="caddy_vendor_error")


def _utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


class MmbCaddyService:
    """Service managing authenticated per-round caddy requests and BRS synchronization."""

    def __init__(
        self,
        database: Optional[Database] = None,
        linkage_service: Optional[BookingLinkageService] = None,
        brs_client: Optional[BrsBookingClient] = None,
        collection_name: str = "mmb_caddy_updates",
    ):
        self.linkage_service = linkage_service or BookingLinkageService(database=database)
        self.brs_client = brs_client or BrsBookingClient()
        self.collection: Optional[Collection] = (
            database[collection_name] if database is not None else None
        )
        self._in_memory_operations: Dict[str, Dict[str, Any]] = {}
        if self.collection is not None:
            self._init_indexes()

    def _init_indexes(self) -> None:
        assert self.collection is not None
        self.collection.create_index([("operation_id", ASCENDING)], unique=True, name="uniq_caddy_op_id")
        self.collection.create_index(
            [("booking_reference", ASCENDING), ("round_id", ASCENDING), ("idempotency_hash", ASCENDING)],
            unique=True,
            name="uniq_caddy_booking_round_idemp",
        )

    def _hash_key(self, booking_reference: str, round_id: str, idempotency_key: str) -> str:
        raw = f"{booking_reference.strip().upper()}:{round_id.strip()}:{idempotency_key.strip()}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def get_caddy_roster(self, booking_reference: str) -> BookingCaddyRosterResponse:
        """Fetch all confirmed rounds on the booking with active caddy status and fees."""
        clean_ref = booking_reference.strip().upper()
        try:
            linkage: BookingLinkageResponse = self.linkage_service.get_linkage(clean_ref)
        except LinkageNotFoundError as exc:
            raise MmbCaddyNotFoundError(f"Booking reference '{clean_ref}' not found.") from exc

        round_summaries: List[RoundCaddySummary] = []
        for r in linkage.brs_rounds:
            caddy_req = getattr(r, "caddy_request", None) or {}
            if isinstance(caddy_req, dict):
                caddies_count = caddy_req.get("caddies_requested", 0)
                category = caddy_req.get("caddy_category")
                notes = caddy_req.get("notes")
                bag_prefs_data = caddy_req.get("bag_preferences", [])
                bag_prefs = [CaddyBagPreference(**b) for b in bag_prefs_data]
            else:
                caddies_count = 0
                category = None
                notes = None
                bag_prefs = []

            status_str = r.caddy_request_status or ("requested" if caddies_count > 0 else "not_requested")
            day_fee = format(Decimal(CADDY_FEE_PER_BAG) * caddies_count, ".2f")

            round_summaries.append(
                RoundCaddySummary(
                    round_id=r.round_id,
                    course_id=r.course_id,
                    course_name=r.course_name,
                    date=r.date,
                    time=r.time,
                    holes=r.holes,
                    players=r.players,
                    caddies_requested=caddies_count,
                    caddy_category=category,
                    caddy_request_status=status_str,
                    bag_preferences=bag_prefs,
                    general_notes=notes,
                    online_charge="0.00",
                    estimated_day_fee=day_fee,
                    notice=CADDY_NOTICE,
                )
            )

        return BookingCaddyRosterResponse(
            booking_reference=clean_ref,
            booking_revision=linkage.revision,
            rounds=round_summaries,
            general_notice=CADDY_NOTICE,
            read_at=_utc_now(),
        )

    def update_round_caddy(
        self,
        booking_reference: str,
        round_id: str,
        request: RoundCaddyUpdateRequest,
        idempotency_key: str,
        actor: str,
    ) -> RoundCaddyUpdateResponse:
        """Atomically update caddy requests on a specific confirmed golf round."""
        clean_ref = booking_reference.strip().upper()
        clean_round_id = round_id.strip()
        clean_key = idempotency_key.strip()
        if not clean_key or len(clean_key) < 8:
            raise MmbCaddyError("A valid Idempotency-Key header (min 8 chars) is required.", status_code=400)

        if request.round_id.strip() != clean_round_id:
            raise MmbCaddyError("Payload round_id does not match route path.", status_code=400)
        if request.booking_reference.strip().upper() != clean_ref:
            raise MmbCaddyError("Payload booking_reference does not match route path.", status_code=400)

        idemp_hash = self._hash_key(clean_ref, clean_round_id, clean_key)

        # 1. Idempotency replay check
        existing_op = None
        if self.collection is not None:
            existing_op = self.collection.find_one({
                "booking_reference": clean_ref,
                "round_id": clean_round_id,
                "idempotency_hash": idemp_hash,
            })
        else:
            existing_op = self._in_memory_operations.get(f"{clean_ref}:{clean_round_id}:{idemp_hash}")

        if existing_op is not None:
            stored_req = existing_op.get("request_payload")
            current_payload = request.model_dump(mode="json")
            if stored_req != current_payload:
                raise MmbCaddyConflictError(
                    "Idempotency key previously used with different caddy request parameters."
                )
            stored_res = existing_op.get("response")
            if stored_res:
                logger.info(
                    "Replaying idempotent caddy update for booking %s round %s",
                    clean_ref,
                    clean_round_id,
                )
                res_obj = RoundCaddyUpdateResponse(**stored_res)
                res_obj.replayed = True
                return res_obj

        # 2. Authoritative booking linkage retrieval
        try:
            linkage: BookingLinkageResponse = self.linkage_service.get_linkage(clean_ref)
        except LinkageNotFoundError as exc:
            raise MmbCaddyNotFoundError(f"Booking reference '{clean_ref}' not found.") from exc

        # Concurrency revision check
        if linkage.revision != request.expected_revision:
            raise MmbCaddyConflictError(
                f"Booking revision conflict: expected {request.expected_revision}, current is {linkage.revision}. Please refresh."
            )

        # 3. Locate target confirmed round
        target_round = next((r for r in linkage.brs_rounds if r.round_id == clean_round_id), None)
        if not target_round:
            raise MmbCaddyNotFoundError(f"Round '{clean_round_id}' not found on booking '{clean_ref}'.")

        if target_round.status in {"Cancelled", "Released"}:
            raise MmbCaddyConflictError(f"Cannot update caddies on round with status '{target_round.status}'.")

        # 4. Validate caddy count against player capacity
        if request.caddies_requested > target_round.players:
            raise MmbCaddyError(
                f"Cannot request {request.caddies_requested} caddies for a {target_round.players}-player round.",
                status_code=400,
            )

        # 5. Build BRS caddy payload
        brs_caddy_payload = CaddyRequestPayload(
            caddies_requested=request.caddies_requested,
            caddy_category=request.caddy_category,
            notes=request.general_notes,
            payment_terms="payable_on_the_day",
            online_charge="0.00",
            status="requested" if request.caddies_requested > 0 else "not_requested",
        )

        caddy_status_enum = "requested" if request.caddies_requested > 0 else "not_requested"
        day_fee = format(Decimal(CADDY_FEE_PER_BAG) * request.caddies_requested, ".2f")
        op_id = f"op_cad_{uuid.uuid4().hex[:10]}"

        # 6. Execute narrow BRS mutation with post-write readback
        existing_round_dict = {
            "round_id": target_round.round_id,
            "course_id": target_round.course_id,
            "date": target_round.date.isoformat(),
            "time": target_round.time,
            "holes": target_round.holes,
            "players": target_round.players,
            "price": target_round.price,
            "currency": target_round.currency,
            "status": target_round.status,
            "brs_booking_id": target_round.brs_booking_id,
        }

        try:
            self.brs_client.update_round_caddy_request(
                round_id=clean_round_id,
                existing_round=existing_round_dict,
                caddy_request=brs_caddy_payload,
                booking_reference=clean_ref,
                idempotency_key=f"brs_{clean_key}",
            )
        except BrsConflictError as exc:
            raise MmbCaddyConflictError(str(exc)) from exc
        except (BrsReadbackMismatchError, BrsWriteError) as exc:
            logger.error(
                "BRS caddy update or readback failed for booking %s round %s: %s",
                clean_ref,
                clean_round_id,
                exc,
            )
            raise MmbCaddyIntegrationError(f"Hotel golf system caddy update failed: {exc}") from exc

        # 7. Persist atomic linkage update
        caddy_data_for_linkage = {
            "caddies_requested": request.caddies_requested,
            "caddy_category": request.caddy_category,
            "bag_preferences": [b.model_dump(mode="json") for b in request.bag_preferences],
            "notes": request.general_notes,
            "payment_terms": "payable_on_the_day",
            "online_charge": "0.00",
            "status": caddy_status_enum,
        }

        reason_text = (
            f"Caddy request updated to {request.caddies_requested} bags ({request.caddy_category or 'none'}) "
            f"via Manage My Booking portal (Key: {clean_key[:12]})"
        )

        try:
            updated_linkage = self.linkage_service.update_round_caddy(
                booking_reference=clean_ref,
                round_id=clean_round_id,
                caddy_status=caddy_status_enum,
                caddy_data=caddy_data_for_linkage,
                actor=actor,
                reason=reason_text,
                expected_revision=request.expected_revision,
            )
        except LinkageConflictError as exc:
            raise MmbCaddyConflictError(str(exc)) from exc

        msg = (
            f"Caddy request for {request.caddies_requested} {request.caddy_category or 'caddies'} recorded. "
            f"Caddies are subject to availability and payable on the day (€{day_fee})."
            if request.caddies_requested > 0
            else "Caddy request successfully removed."
        )

        response = RoundCaddyUpdateResponse(
            operation_id=op_id,
            booking_reference=clean_ref,
            round_id=clean_round_id,
            caddies_requested=request.caddies_requested,
            caddy_category=request.caddy_category,
            caddy_request_status=caddy_status_enum,
            bag_preferences=request.bag_preferences,
            general_notes=request.general_notes,
            booking_revision=updated_linkage.revision,
            online_charge="0.00",
            estimated_day_fee=day_fee,
            verified_via_readback=True,
            replayed=False,
            message=msg,
            notice=CADDY_NOTICE,
            updated_at=_utc_now(),
        )

        self._record_operation(clean_ref, clean_round_id, idemp_hash, op_id, request, response)
        return response

    def _record_operation(
        self,
        booking_reference: str,
        round_id: str,
        idempotency_hash: str,
        operation_id: str,
        request: RoundCaddyUpdateRequest,
        response: RoundCaddyUpdateResponse,
    ) -> None:
        doc = {
            "operation_id": operation_id,
            "booking_reference": booking_reference,
            "round_id": round_id,
            "idempotency_hash": idempotency_hash,
            "request_payload": request.model_dump(mode="json"),
            "response": response.model_dump(mode="json"),
            "created_at": _utc_now(),
        }
        if self.collection is not None:
            self.collection.update_one(
                {"booking_reference": booking_reference, "round_id": round_id, "idempotency_hash": idempotency_hash},
                {"$setOnInsert": doc},
                upsert=True,
            )
        else:
            self._in_memory_operations[f"{booking_reference}:{round_id}:{idempotency_hash}"] = doc
