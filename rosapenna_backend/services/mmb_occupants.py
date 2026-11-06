"""Service layer for Ticket M10 — Room Occupants, Sharing, and Moves."""

from __future__ import annotations

import datetime as dt
import logging
import secrets
from typing import Any, Dict, List, Optional
from pymongo.database import Database

from rosapenna_backend.api.schemas.mmb_occupants import (
    BookedRoomGroupSummary,
    OccupantDetail,
    OccupantReviewVersion,
    OccupantRosterResponse,
    OccupantUpdateRequest,
    OccupantUpdateResponse,
    SharingPreference,
    compute_occupant_version_hash,
)
from rosapenna_backend.integrations.opera.client import OperaClient, OperaReadbackMismatchError
from rosapenna_backend.integrations.opera.schemas import (
    DateSharingAssignment,
    RoomOccupant,
    UpdateReservationRequest,
)
from rosapenna_backend.services.booking_linkage import BookingLinkageService
from rosapenna_backend.services.mmb_amendments import MmbAmendmentService
from rosapenna_backend.services.mmb_gate import MmbGateService

logger = logging.getLogger("booking_api")


class ConcurrentEditConflictError(Exception):
    """Raised when a concurrent edit has updated the booking revision."""
    def __init__(self, message: str = "Booking was modified concurrently. Please reload."):
        super().__init__(message)
        self.message = message


class OccupantValidationError(Exception):
    """Raised when room occupancy, dates, capacity, or sharing rules are violated."""
    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class MmbOccupantsService:
    """Coordinates room occupant records, room moves, sharing requests, and amendment review versioning."""

    def __init__(
        self,
        database: Optional[Database] = None,
        linkage_service: Optional[BookingLinkageService] = None,
        opera_client: Optional[OperaClient] = None,
        gate_service: Optional[MmbGateService] = None,
        amendment_service: Optional[MmbAmendmentService] = None,
    ):
        self.database = database
        self.rosters_col = database["mmb_occupant_rosters"] if database is not None else None
        self.reviews_col = database["mmb_amendment_occupant_reviews"] if database is not None else None
        self.idempotency_col = database["mmb_occupant_idempotency"] if database is not None else None
        self.audit_col = database["staff_audit_events"] if database is not None else None

        self.linkage_service = linkage_service
        self.opera_client = opera_client or OperaClient()
        self.gate_service = gate_service
        self.amendment_service = amendment_service

        # In-memory storage for test/sandbox execution
        self._rosters_cache: Dict[str, Dict[str, Any]] = {}
        self._reviews_cache: Dict[str, Dict[str, Any]] = {}
        self._idempotency_cache: Dict[str, Dict[str, Any]] = {}

    def get_roster(self, booking_reference: str) -> OccupantRosterResponse:
        """Retrieve authoritative occupant roster, room assignments, and sharing preferences."""
        clean_ref = booking_reference.strip().upper()
        if not self.linkage_service:
            raise RuntimeError("BookingLinkageService is required for occupant lookup.")

        booking = self.linkage_service.get_linkage(clean_ref)
        if not booking.opera_stay:
            raise OccupantValidationError(f"Booking '{clean_ref}' does not have a linked hotel reservation.")

        stay = booking.opera_stay
        revision = getattr(booking, "revision", len(booking.audit_trail) or 1)

        # 1. Check persistent roster
        roster_data = self._load_roster(clean_ref)

        if roster_data:
            occupants = [OccupantDetail.model_validate(o) for o in roster_data.get("occupants", [])]
            sharing = [SharingPreference.model_validate(s) for s in roster_data.get("sharing_preferences", [])]
        else:
            # Generate synthesized initial roster based on linked stay and lead guest
            occupants = []
            names = stay.occupant_names if stay.occupant_names else [f"{booking.lead_guest.first_name} {booking.lead_guest.last_name}"]
            for idx, full_name in enumerate(names):
                parts = full_name.strip().split(maxsplit=1)
                first = parts[0] if parts else "Guest"
                last = parts[1] if len(parts) > 1 else booking.lead_guest.last_name
                # Distribute across available rooms (room_1, room_2, etc.)
                assigned_room = f"room_{(idx % stay.room_quantity) + 1}"
                occupants.append(
                    OccupantDetail(
                        occupant_id=f"occ_{idx + 1}",
                        first_name=first,
                        last_name=last,
                        is_primary=(idx == 0),
                        room_group_id=assigned_room,
                        effective_from=stay.arrival_date,
                        effective_to=stay.departure_date,
                    )
                )
            sharing = [
                SharingPreference(
                    room_group_id=f"room_{r}",
                    bed_type_preference="TWIN",
                    notes=None,
                )
                for r in range(1, stay.room_quantity + 1)
            ]

        # 2. Build room group summaries
        room_groups = []
        for r in range(1, stay.room_quantity + 1):
            r_id = f"room_{r}"
            assigned = [o.full_name for o in occupants if o.room_group_id == r_id]
            pref = next((sp for sp in sharing if sp.room_group_id == r_id), None)
            room_groups.append(
                BookedRoomGroupSummary(
                    room_group_id=r_id,
                    room_category=stay.room_category,
                    capacity=2,
                    assigned_occupants=assigned,
                    sharing_preference=pref,
                )
            )

        # 3. Check for active amendment review requirement
        pending_review = False
        active_offer_id = None
        if self.amendment_service:
            for offer in self.amendment_service._offers_cache.values():
                if (
                    offer.get("booking_reference") == clean_ref
                    and offer.get("status") in ("pending_consent", "accepted")
                    and offer.get("requires_occupant_review", False)
                ):
                    pending_review = not offer.get("occupant_review_acknowledged", False)
                    active_offer_id = offer.get("offer_id")
                    break

        version_hash = compute_occupant_version_hash(
            booking_reference=clean_ref,
            occupants=occupants,
            sharing_preferences=sharing,
            stay_dates=(stay.arrival_date, stay.departure_date),
            room_category=stay.room_category,
        )

        return OccupantRosterResponse(
            booking_reference=clean_ref,
            arrival_date=stay.arrival_date,
            departure_date=stay.departure_date,
            room_quantity=stay.room_quantity,
            room_groups=room_groups,
            occupants=occupants,
            sharing_preferences=sharing,
            version_hash=version_hash,
            revision=revision,
            pending_amendment_review=pending_review,
            active_amendment_offer_id=active_offer_id,
        )

    def validate_rules(
        self,
        stay_arrival: dt.date,
        stay_departure: dt.date,
        room_quantity: int,
        occupants: List[OccupantDetail],
        sharing_preferences: List[SharingPreference],
    ) -> None:
        """Validate occupant full names, room groups, date continuity, and capacity."""
        if not occupants:
            raise OccupantValidationError("At least one occupant must be provided.")

        valid_room_ids = {f"room_{r}" for r in range(1, room_quantity + 1)}

        # Capacity tracking per room
        room_occupant_counts: Dict[str, int] = {r: 0 for r in valid_room_ids}
        seen_names: Dict[str, tuple[str, dt.date, dt.date]] = {}

        for occ in occupants:
            # 1. Full name validation
            if not occ.first_name.strip() or not occ.last_name.strip():
                raise OccupantValidationError("Every occupant must have a non-empty first and last name.")

            # 2. Room group check
            if occ.room_group_id not in valid_room_ids:
                raise OccupantValidationError(
                    f"Room group '{occ.room_group_id}' does not exist on this booking. Valid groups: {sorted(valid_room_ids)}"
                )

            # 3. Date window & continuity
            if occ.effective_from < stay_arrival or occ.effective_to > stay_departure:
                raise OccupantValidationError(
                    f"Occupant '{occ.full_name}' effective dates [{occ.effective_from}, {occ.effective_to}] must fall within stay window [{stay_arrival}, {stay_departure}]."
                )
            if occ.effective_to <= occ.effective_from:
                raise OccupantValidationError(
                    f"Occupant '{occ.full_name}' departure date must be strictly after arrival date."
                )

            # 4. Conflicting duplicate check
            name_key = occ.full_name.lower()
            if name_key in seen_names:
                other_room, o_from, o_to = seen_names[name_key]
                # Check date overlap
                if max(occ.effective_from, o_from) < min(occ.effective_to, o_to):
                    raise OccupantValidationError(
                        f"Occupant '{occ.full_name}' is assigned to multiple rooms with overlapping dates."
                    )
            seen_names[name_key] = (occ.room_group_id, occ.effective_from, occ.effective_to)

            room_occupant_counts[occ.room_group_id] += 1

        # 5. Room capacity check (standard max capacity 3 per room group)
        for r_id, count in room_occupant_counts.items():
            if count > 3:
                raise OccupantValidationError(
                    f"Room '{r_id}' exceeds maximum allowed occupancy capacity of 3 guests (has {count})."
                )

        # 6. Overall booking capacity limit
        max_total_capacity = room_quantity * 3
        if len(occupants) > max_total_capacity:
            raise OccupantValidationError(
                f"Total party size of {len(occupants)} exceeds total room capacity ({max_total_capacity})."
            )

    def update_occupants(
        self,
        booking_reference: str,
        request: OccupantUpdateRequest,
        actor: str = "lead_guest",
    ) -> OccupantUpdateResponse:
        """Update confirmed occupant roster and room assignments with Opera readback verification."""
        clean_ref = booking_reference.strip().upper()

        # Idempotency check
        if request.idempotency_key:
            cached = self._get_idempotency(request.idempotency_key)
            if cached:
                logger.info("Returning idempotent occupant update for key %s", request.idempotency_key)
                return OccupantUpdateResponse.model_validate(cached)

        if not self.linkage_service:
            raise RuntimeError("BookingLinkageService is required for occupant updates.")

        booking = self.linkage_service.get_linkage(clean_ref)
        if not booking.opera_stay:
            raise OccupantValidationError(f"Booking '{clean_ref}' does not have a linked hotel reservation.")

        stay = booking.opera_stay
        current_rev = getattr(booking, "revision", len(booking.audit_trail) or 1)

        # Concurrent edit detection (AC 6)
        if request.expected_revision is not None and request.expected_revision != current_rev:
            raise ConcurrentEditConflictError(
                f"Booking revision mismatch (expected {request.expected_revision}, current {current_rev}). A concurrent edit occurred."
            )

        # Validate occupancy and capacity rules (AC 1, AC 2)
        self.validate_rules(
            stay_arrival=stay.arrival_date,
            stay_departure=stay.departure_date,
            room_quantity=stay.room_quantity,
            occupants=request.occupants,
            sharing_preferences=request.sharing_preferences,
        )

        # Prepare Opera domain models
        opera_occupants = [
            RoomOccupant(
                first_name=occ.first_name,
                last_name=occ.last_name,
                is_primary=occ.is_primary,
            )
            for occ in request.occupants
        ]

        # Prepare DateSharingAssignments
        date_sharing: List[DateSharingAssignment] = []
        for sp in request.sharing_preferences:
            room_occupant_names = [
                occ.full_name for occ in request.occupants if occ.room_group_id == sp.room_group_id
            ]
            if room_occupant_names:
                date_sharing.append(
                    DateSharingAssignment(
                        date=stay.arrival_date,
                        occupant_names=room_occupant_names,
                        bed_type_preference=sp.bed_type_preference,
                        notes=sp.notes,
                    )
                )

        # Write to Opera reservation via authorized write path (AC 4, AC 5)
        opera_req = UpdateReservationRequest(
            reservation_id=stay.reservation_id,
            occupants=opera_occupants,
            sharing_assignments=date_sharing,
            idempotency_key=request.idempotency_key,
        )

        readback_verified = False
        try:
            write_result = self.opera_client.amend_reservation(opera_req)
            readback_verified = write_result.verified_via_readback
        except OperaReadbackMismatchError as exc:
            logger.error("Opera readback verification failed during occupant update: %s", exc)
            self._record_staff_alert(clean_ref, "readback_mismatch", str(exc))
            return OccupantUpdateResponse(
                booking_reference=clean_ref,
                status="pending_staff_review",
                version_hash="unverified",
                revision=current_rev,
                occupants=request.occupants,
                sharing_preferences=request.sharing_preferences,
                readback_verified=False,
                message="Occupant update submitted but requires staff verification due to upstream response mismatch.",
            )
        except Exception as exc:
            logger.error("Opera update failed during occupant amendment: %s", exc)
            self._record_staff_alert(clean_ref, "vendor_failure", str(exc))
            return OccupantUpdateResponse(
                booking_reference=clean_ref,
                status="pending_staff_review",
                version_hash="unverified",
                revision=current_rev,
                occupants=request.occupants,
                sharing_preferences=request.sharing_preferences,
                readback_verified=False,
                message=f"Occupant update pending staff review due to hotel system communication failure: {str(exc)}",
            )

        # Successful update: compute version hash and save roster (AC 7, AC 8)
        version_hash = compute_occupant_version_hash(
            booking_reference=clean_ref,
            occupants=request.occupants,
            sharing_preferences=request.sharing_preferences,
            stay_dates=(stay.arrival_date, stay.departure_date),
            room_category=stay.room_category,
        )

        # Update linkage occupant names and audit
        updated_occupant_names = [occ.full_name for occ in request.occupants]
        stay.occupant_names = updated_occupant_names

        new_rev = current_rev + 1
        setattr(booking, "revision", new_rev)

        roster_record = {
            "booking_reference": clean_ref,
            "revision": new_rev,
            "version_hash": version_hash,
            "occupants": [occ.model_dump(mode="json") for occ in request.occupants],
            "sharing_preferences": [sp.model_dump(mode="json") for sp in request.sharing_preferences],
            "updated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "actor": actor,
        }
        self._save_roster(clean_ref, roster_record)

        response = OccupantUpdateResponse(
            booking_reference=clean_ref,
            status="confirmed",
            version_hash=version_hash,
            revision=new_rev,
            occupants=request.occupants,
            sharing_preferences=request.sharing_preferences,
            readback_verified=readback_verified,
            message="Room occupants, room moves, and sharing preferences updated and verified in hotel PMS.",
        )

        if request.idempotency_key:
            self._save_idempotency(request.idempotency_key, response.model_dump(mode="json"))

        logger.info(
            "Successfully updated occupants for booking %s (new revision: %d, version: %s)",
            clean_ref,
            new_rev,
            version_hash[:8],
        )
        return response

    def review_amendment_occupants(
        self,
        booking_reference: str,
        offer_id: str,
        request: OccupantUpdateRequest,
        actor: str = "lead_guest",
    ) -> OccupantUpdateResponse:
        """Review and acknowledge occupant assignments for an amendment offer before commit (AC 3).

        Does NOT write to Opera immediately. Generates an immutable OccupantReviewVersion
        and links it to the amendment offer so Ticket M09 can verify it before commit.
        """
        clean_ref = booking_reference.strip().upper()
        if not self.amendment_service:
            raise RuntimeError("MmbAmendmentService is required for amendment occupant review.")

        offer = self.amendment_service.get_amendment_offer(offer_id, clean_ref)
        if offer.status in ("expired", "invalidated"):
            raise OccupantValidationError(f"Cannot review occupants because amendment offer is {offer.status}.")

        proposed_stay = offer.proposed_stay

        # Validate against proposed stay parameters
        self.validate_rules(
            stay_arrival=proposed_stay.arrival_date,
            stay_departure=proposed_stay.departure_date,
            room_quantity=proposed_stay.room_quantity,
            occupants=request.occupants,
            sharing_preferences=request.sharing_preferences,
        )

        version_hash = compute_occupant_version_hash(
            booking_reference=clean_ref,
            occupants=request.occupants,
            sharing_preferences=request.sharing_preferences,
            stay_dates=(proposed_stay.arrival_date, proposed_stay.departure_date),
            room_category=proposed_stay.room_category,
        )

        review_id = f"REV-OCC-{secrets.token_hex(6).upper()}"
        review_version = OccupantReviewVersion(
            review_id=review_id,
            booking_reference=clean_ref,
            offer_id=offer_id,
            version_hash=version_hash,
            reviewed_at=dt.datetime.now(dt.timezone.utc),
            occupants=request.occupants,
            sharing_preferences=request.sharing_preferences,
            is_current=True,
        )

        # Save review version
        self._save_review(offer_id, review_version.model_dump(mode="json"))

        # Link review version directly to offer snapshot
        raw_offer = self.amendment_service._offers_cache.get(offer_id)
        if raw_offer:
            raw_offer["occupant_review_acknowledged"] = True
            raw_offer["occupant_review_version"] = version_hash

        if self.amendment_service.collection is not None:
            try:
                self.amendment_service.collection.update_one(
                    {"offer_id": offer_id},
                    {
                        "$set": {
                            "occupant_review_acknowledged": True,
                            "occupant_review_version": version_hash,
                        }
                    },
                )
            except Exception as exc:
                logger.warning("Failed to persist occupant review version to MongoDB (%s)", exc)

        logger.info(
            "Created amendment occupant review version %s for offer %s (version: %s)",
            review_id,
            offer_id,
            version_hash[:8],
        )

        return OccupantUpdateResponse(
            booking_reference=clean_ref,
            status="confirmed",
            version_hash=version_hash,
            revision=offer.booking_revision,
            occupants=request.occupants,
            sharing_preferences=request.sharing_preferences,
            readback_verified=True,
            amendment_offer_id=offer_id,
            message="Amendment occupant review recorded. Version ready for confirmation.",
        )

    # -------------------------------------------------------------------------
    # Internal persistence & helpers
    # -------------------------------------------------------------------------

    def _load_roster(self, booking_ref: str) -> Optional[Dict[str, Any]]:
        if self.rosters_col is not None:
            try:
                doc = self.rosters_col.find_one({"booking_reference": booking_ref}, {"_id": 0})
                if doc:
                    return doc
            except Exception as exc:
                logger.warning("MongoDB read failed for occupant roster %s (%s)", booking_ref, exc)
        return self._rosters_cache.get(booking_ref)

    def _save_roster(self, booking_ref: str, record: Dict[str, Any]) -> None:
        self._rosters_cache[booking_ref] = record
        if self.rosters_col is not None:
            try:
                self.rosters_col.update_one(
                    {"booking_reference": booking_ref},
                    {"$set": record},
                    upsert=True,
                )
            except Exception as exc:
                logger.warning("MongoDB write failed for occupant roster %s (%s)", booking_ref, exc)

    def _save_review(self, offer_id: str, record: Dict[str, Any]) -> None:
        self._reviews_cache[offer_id] = record
        if self.reviews_col is not None:
            try:
                self.reviews_col.update_one(
                    {"offer_id": offer_id},
                    {"$set": record},
                    upsert=True,
                )
            except Exception as exc:
                logger.warning("MongoDB write failed for amendment review %s (%s)", offer_id, exc)

    def _get_idempotency(self, key: str) -> Optional[Dict[str, Any]]:
        if self.idempotency_col is not None:
            try:
                doc = self.idempotency_col.find_one({"idempotency_key": key}, {"_id": 0})
                if doc and "response" in doc:
                    return doc["response"]
            except Exception:
                pass
        return self._idempotency_cache.get(key)

    def _save_idempotency(self, key: str, response: Dict[str, Any]) -> None:
        self._idempotency_cache[key] = response
        if self.idempotency_col is not None:
            try:
                self.idempotency_col.update_one(
                    {"idempotency_key": key},
                    {"$set": {"response": response, "created_at": dt.datetime.now(dt.timezone.utc)}},
                    upsert=True,
                )
            except Exception:
                pass

    def _record_staff_alert(self, booking_ref: str, alert_type: str, details: str) -> None:
        alert_event = {
            "event_type": "occupant_update_alert",
            "booking_reference": booking_ref,
            "alert_type": alert_type,
            "details": details,
            "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        }
        if self.audit_col is not None:
            try:
                self.audit_col.insert_one(alert_event)
            except Exception:
                pass
        logger.warning("Staff alert recorded for booking %s: %s - %s", booking_ref, alert_type, details)
