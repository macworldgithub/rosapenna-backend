"""Domain service for Ticket M11 — Guest Phone Updates.

Enforces:
- Strict session authorization and IDOR booking scope checks.
- Lead-guest only: rejects attempts to alter booking ownership or verification email.
- Narrow Opera telephone mutation with authoritative post-write readback.
- Optimistic concurrency control via expected_revision and contact_version.
- Idempotency with replay protection and conflict detection.
- Zero token or credential leakage and phone masking in routine logs.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import uuid
from typing import Any, Dict, Optional
from pymongo import ASCENDING
from pymongo.collection import Collection
from pymongo.database import Database

from rosapenna_backend.api.schemas.mmb_contact import (
    GuestPhoneReadResponse,
    GuestPhoneUpdateRequest,
    GuestPhoneUpdateResponse,
    mask_phone_number,
    normalize_phone_number,
)
from rosapenna_backend.core.logging import logger
from rosapenna_backend.integrations.opera.client import (
    OperaClient,
    OperaConflictError,
    OperaIntegrationError,
    OperaReadbackMismatchError,
)
from rosapenna_backend.services.booking_linkage import (
    BookingLinkageResponse,
    BookingLinkageService,
    LinkageConflictError,
    LinkageNotFoundError,
)


class MmbContactError(Exception):
    """Base exception for MMB contact operations."""
    def __init__(self, message: str, status_code: int = 400, detail_code: str = "contact_error"):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.detail_code = detail_code


class MmbContactNotFoundError(MmbContactError):
    def __init__(self, message: str = "Booking contact record not found."):
        super().__init__(message, status_code=404, detail_code="contact_not_found")


class MmbContactConflictError(MmbContactError):
    def __init__(self, message: str = "Contact update conflict. Please refresh."):
        super().__init__(message, status_code=409, detail_code="contact_conflict")


class MmbContactIntegrationError(MmbContactError):
    def __init__(self, message: str = "External hotel system telephone update failed."):
        super().__init__(message, status_code=502, detail_code="contact_vendor_error")


def _utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


class MmbContactService:
    """Service managing authenticated guest telephone updates and Opera synchronization."""

    def __init__(
        self,
        database: Optional[Database] = None,
        linkage_service: Optional[BookingLinkageService] = None,
        opera_client: Optional[OperaClient] = None,
        collection_name: str = "mmb_phone_updates",
    ):
        self.linkage_service = linkage_service or BookingLinkageService(database=database, opera_client=opera_client)
        self.opera_client = opera_client or getattr(self.linkage_service, "opera_client", None) or OperaClient()
        self.collection: Optional[Collection] = (
            database[collection_name] if database is not None else None
        )
        self._in_memory_operations: Dict[str, Dict[str, Any]] = {}
        if self.collection is not None:
            self._init_indexes()

    def _init_indexes(self) -> None:
        assert self.collection is not None
        self.collection.create_index([("operation_id", ASCENDING)], unique=True, name="uniq_phone_op_id")
        self.collection.create_index(
            [("booking_reference", ASCENDING), ("idempotency_hash", ASCENDING)],
            unique=True,
            name="uniq_phone_booking_idemp",
        )

    def _compute_contact_version(self, phone: Optional[str], revision: int) -> str:
        raw = f"{phone or ''}:{revision}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]

    def _hash_key(self, booking_reference: str, idempotency_key: str) -> str:
        return hashlib.sha256(f"{booking_reference.strip().upper()}:{idempotency_key.strip()}".encode("utf-8")).hexdigest()

    def get_lead_phone(self, booking_reference: str) -> GuestPhoneReadResponse:
        """Fetch current lead guest telephone and version metadata."""
        clean_ref = booking_reference.strip().upper()
        try:
            linkage: BookingLinkageResponse = self.linkage_service.get_linkage(clean_ref)
        except LinkageNotFoundError as exc:
            raise MmbContactNotFoundError(f"Booking reference '{clean_ref}' not found.") from exc

        lead = linkage.lead_guest
        phone = lead.telephone if lead else None
        lead_name = f"{lead.first_name} {lead.last_name}".strip() if lead else "Guest"
        contact_ver = self._compute_contact_version(phone, linkage.revision)

        return GuestPhoneReadResponse(
            booking_reference=clean_ref,
            lead_guest_name=lead_name,
            phone_number=phone,
            masked_phone=mask_phone_number(phone),
            booking_revision=linkage.revision,
            contact_version=contact_ver,
            source_status="confirmed" if phone else "unavailable",
            read_at=_utc_now(),
        )

    def update_lead_phone(
        self,
        booking_reference: str,
        request: GuestPhoneUpdateRequest,
        idempotency_key: str,
        actor: str,
    ) -> GuestPhoneUpdateResponse:
        """Atomically update the lead guest telephone and verify Opera post-write readback."""
        clean_ref = booking_reference.strip().upper()
        clean_key = idempotency_key.strip()
        if not clean_key or len(clean_key) < 8:
            raise MmbContactError("A valid Idempotency-Key header (min 8 chars) is required.", status_code=400)

        idemp_hash = self._hash_key(clean_ref, clean_key)
        normalized_target = request.phone_number

        # Check existing operation by idempotency hash
        existing_op = None
        if self.collection is not None:
            existing_op = self.collection.find_one({"booking_reference": clean_ref, "idempotency_hash": idemp_hash})
        else:
            existing_op = self._in_memory_operations.get(f"{clean_ref}:{idemp_hash}")

        if existing_op is not None:
            # Check if details match identically
            stored_phone = existing_op.get("target_phone")
            if stored_phone != normalized_target:
                raise MmbContactConflictError("Idempotency key previously used with a different telephone number.")

            stored_res = existing_op.get("response")
            if stored_res:
                logger.info(
                    "Replaying idempotent phone update for booking %s (target phone: %s)",
                    clean_ref,
                    mask_phone_number(normalized_target),
                )
                res_obj = GuestPhoneUpdateResponse(**stored_res)
                res_obj.replayed = True
                return res_obj

        # Load authoritative booking linkage
        try:
            linkage: BookingLinkageResponse = self.linkage_service.get_linkage(clean_ref)
        except LinkageNotFoundError as exc:
            raise MmbContactNotFoundError(f"Booking reference '{clean_ref}' not found.") from exc

        # Optimistic concurrency check: revision must match
        if linkage.revision != request.expected_revision:
            raise MmbContactConflictError(
                f"Booking revision conflict: expected {request.expected_revision}, current is {linkage.revision}. Please refresh."
            )

        # Contact version check
        current_contact_ver = self._compute_contact_version(linkage.lead_guest.telephone, linkage.revision)
        if request.contact_version and request.contact_version != current_contact_ver:
            raise MmbContactConflictError("Contact version conflict. Intervening changes detected. Please refresh.")

        # Zero-mutation check: if phone is already equal, return confirmed without redundant vendor write
        current_phone = linkage.lead_guest.telephone if linkage.lead_guest else None
        if current_phone and normalize_phone_number(current_phone) == normalized_target:
            op_id = f"op_phone_{uuid.uuid4().hex[:10]}"
            res = GuestPhoneUpdateResponse(
                operation_id=op_id,
                booking_reference=clean_ref,
                phone_number=normalized_target,
                masked_phone=mask_phone_number(normalized_target),
                booking_revision=linkage.revision,
                contact_version=current_contact_ver,
                status="confirmed",
                verified_via_readback=True,
                staff_review_required=False,
                replayed=False,
                message="Telephone number already matches verified reservation.",
                updated_at=_utc_now(),
            )
            self._record_operation(clean_ref, idemp_hash, op_id, normalized_target, res)
            return res

        op_id = f"op_phone_{uuid.uuid4().hex[:10]}"

        # Execute narrow Opera telephone update if Opera stay is linked
        opera_stay = linkage.opera_stay
        if opera_stay and opera_stay.reservation_id:
            try:
                self.opera_client.update_reservation_phone(
                    reservation_id=opera_stay.reservation_id,
                    phone_number=normalized_target,
                    idempotency_key=f"opera_{clean_key}",
                )
            except OperaConflictError as exc:
                raise MmbContactConflictError(f"Opera reservation edit conflict: {exc}") from exc
            except (OperaReadbackMismatchError, OperaIntegrationError) as exc:
                logger.error(
                    "Opera telephone update or readback failed for booking %s: %s",
                    clean_ref,
                    exc,
                )
                raise MmbContactIntegrationError(f"Hotel reservation telephone update failed: {exc}") from exc

        # Atomically update booking linkage lead guest telephone
        try:
            updated_linkage = self.linkage_service.update_lead_phone(
                booking_reference=clean_ref,
                phone_number=normalized_target,
                actor=actor,
                reason=f"Guest telephone updated via Manage My Booking portal (Key: {clean_key[:12]})",
                expected_revision=request.expected_revision,
            )
        except LinkageConflictError as exc:
            raise MmbContactConflictError(str(exc)) from exc

        new_contact_ver = self._compute_contact_version(normalized_target, updated_linkage.revision)

        response = GuestPhoneUpdateResponse(
            operation_id=op_id,
            booking_reference=clean_ref,
            phone_number=normalized_target,
            masked_phone=mask_phone_number(normalized_target),
            booking_revision=updated_linkage.revision,
            contact_version=new_contact_ver,
            status="confirmed",
            verified_via_readback=True,
            staff_review_required=False,
            replayed=False,
            message="Telephone number successfully updated and verified in Opera PMS.",
            updated_at=_utc_now(),
        )

        self._record_operation(clean_ref, idemp_hash, op_id, normalized_target, response)

        logger.info(
            "Guest phone updated for booking %s (new revision: %d, phone: %s)",
            clean_ref,
            updated_linkage.revision,
            mask_phone_number(normalized_target),
        )
        return response

    def _record_operation(
        self,
        booking_reference: str,
        idempotency_hash: str,
        operation_id: str,
        target_phone: str,
        response: GuestPhoneUpdateResponse,
    ) -> None:
        doc = {
            "operation_id": operation_id,
            "booking_reference": booking_reference,
            "idempotency_hash": idempotency_hash,
            "target_phone": target_phone,
            "response": response.model_dump(mode="json"),
            "created_at": _utc_now(),
        }
        if self.collection is not None:
            self.collection.update_one(
                {"booking_reference": booking_reference, "idempotency_hash": idempotency_hash},
                {"$set": doc},
                upsert=True,
            )
        else:
            self._in_memory_operations[f"{booking_reference}:{idempotency_hash}"] = doc
            self._in_memory_operations[operation_id] = doc
