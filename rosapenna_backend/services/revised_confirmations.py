"""Domain service for revised confirmations, PDF itineraries, and pre-arrival reminder workflows (Ticket M15)."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

from pymongo.collection import Collection
from pymongo.database import Database

from rosapenna_backend.api.schemas.communications import EmailDeliveryResult
from rosapenna_backend.api.schemas.revised_confirmations import (
    DispatchReminderResponse,
    ScheduledReminderItem,
    ScheduledReminderListResponse,
)
from rosapenna_backend.core.logging import logger
from rosapenna_backend.services.booking_linkage import BookingLinkageService, LinkageNotFoundError
from rosapenna_backend.services.email_delivery import EmailDeliveryService
from rosapenna_backend.services.pdf_itinerary import build_itinerary_pdf


def _utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


class RevisedConfirmationService:
    """Manages revised booking confirmations, PDF itinerary compilation, and durable pre-arrival reminders."""

    def __init__(
        self,
        database: Optional[Database] = None,
        linkage_service: Optional[BookingLinkageService] = None,
        email_service: Optional[EmailDeliveryService] = None,
        reminders_collection_name: str = "scheduled_booking_reminders",
    ):
        self.linkage_service = linkage_service or BookingLinkageService()
        self.email_service = email_service or EmailDeliveryService()
        self.collection: Optional[Collection] = (
            database[reminders_collection_name] if database is not None else None
        )
        self._in_memory_reminders: Dict[str, Dict[str, Any]] = {}

        if self.collection is not None:
            self._init_indexes()

    def _init_indexes(self) -> None:
        assert self.collection is not None
        self.collection.create_index([("reminder_id", 1)], unique=True, name="uniq_reminder_id")
        self.collection.create_index([("booking_reference", 1)], name="idx_rem_booking_ref")
        self.collection.create_index([("status", 1)], name="idx_rem_status")
        self.collection.create_index([("scheduled_date", 1)], name="idx_rem_scheduled_date")

    def get_canonical_booking_snapshot(self, booking_reference: str) -> Dict[str, Any]:
        """Fetch authoritative verified booking linkage record and format into a unified canonical snapshot.

        Used identically across PDF generation, email delivery, and dashboard rendering.
        """
        clean_ref = booking_reference.strip().upper()
        linkage = self.linkage_service.get_linkage(clean_ref)

        lead = linkage.lead_guest
        stay = linkage.opera_stay
        rounds = linkage.brs_rounds
        payments = linkage.payments
        fin = linkage.financials
        revision_num = getattr(linkage, "revision", getattr(linkage, "revision_number", 1))

        # Authoritative 42-day balance due date
        arr_date = stay.arrival_date if stay else None
        bal_due_date = None
        if arr_date:
            bal_due_date = arr_date - dt.timedelta(days=42)

        snapshot: Dict[str, Any] = {
            "booking_reference": clean_ref,
            "revision_number": revision_num,
            "revision": revision_num,
            "stage": "Confirmed" if linkage.status == "linked" else linkage.status.title(),
            "status": linkage.status,
            "channel": linkage.source_channel,
            "lead_guest": {
                "first_name": lead.first_name,
                "last_name": lead.last_name,
                "email": lead.email,
                "telephone": lead.telephone,
            },
            "stay": {
                "arrival_date": stay.arrival_date.isoformat() if stay else "",
                "departure_date": stay.departure_date.isoformat() if stay else "",
                "room_category": stay.room_category if stay else "Classic Room",
                "room_quantity": stay.room_quantity if stay else 1,
                "confirmation_number": stay.confirmation_number or stay.reservation_id if stay else "",
                "assigned_room_number": stay.assigned_room_number if stay else None,
                "occupant_names": stay.occupant_names if stay else [],
            } if stay else None,
            "opera_stay": {
                "arrival_date": stay.arrival_date.isoformat() if stay else "",
                "departure_date": stay.departure_date.isoformat() if stay else "",
                "room_category": stay.room_category if stay else "Classic Room",
                "room_quantity": stay.room_quantity if stay else 1,
                "confirmation_number": stay.confirmation_number or stay.reservation_id if stay else "",
                "occupant_names": stay.occupant_names if stay else [],
            } if stay else None,
            "golf_rounds": [
                {
                    "course_name": r.course_name,
                    "date": r.date.isoformat() if isinstance(r.date, dt.date) else str(r.date),
                    "time": r.time,
                    "players": r.players,
                    "holes": r.holes,
                    "caddy_request": (
                        r.caddy_request if isinstance(r.caddy_request, dict)
                        else (r.caddy_request.model_dump() if hasattr(r.caddy_request, "model_dump") else None)
                    ),
                    "caddy_request_status": (
                        f"Requested ({r.caddy_request.get('caddies_requested', 0) if isinstance(r.caddy_request, dict) else getattr(r.caddy_request, 'caddies_requested', 0)} "
                        f"{(r.caddy_request.get('caddy_category', 'caddie') if isinstance(r.caddy_request, dict) else getattr(r.caddy_request, 'caddy_category', 'caddie')).title()}(s))"
                        if (
                            (isinstance(r.caddy_request, dict) and r.caddy_request.get("caddies_requested", 0) > 0)
                            or (not isinstance(r.caddy_request, dict) and getattr(r.caddy_request, "caddies_requested", 0) > 0)
                        )
                        else (r.caddy_request_status or "None")
                    ),
                    "status": r.status,
                }
                for r in rounds
            ],
            "brs_rounds": [
                {
                    "course_name": r.course_name,
                    "date": r.date.isoformat() if isinstance(r.date, dt.date) else str(r.date),
                    "time": r.time,
                    "players": r.players,
                    "holes": r.holes,
                    "caddy_request": (
                        r.caddy_request if isinstance(r.caddy_request, dict)
                        else (r.caddy_request.model_dump() if hasattr(r.caddy_request, "model_dump") else None)
                    ),
                    "status": r.status,
                }
                for r in rounds
            ],
            "financials": {
                "total_amount": fin.get("total_amount", "0.00"),
                "total_price": fin.get("total_amount", "0.00"),
                "deposit_paid": fin.get("deposit_paid", "0.00"),
                "credit_amount": fin.get("credit_amount", "0.00"),
                "balance_due": fin.get("balance_due", "0.00"),
                "balance_due_date": bal_due_date.isoformat() if bal_due_date else None,
                "currency": fin.get("currency", "EUR"),
                "payment_status": fin.get("payment_status", "deposit_paid"),
                "payments_count": len(payments),
            },
            "payments": [p.model_dump() for p in payments],
            "links": {
                "opera_reservation": {
                    "arrival_date": stay.arrival_date.isoformat() if stay else "",
                    "departure_date": stay.departure_date.isoformat() if stay else "",
                    "room_category": stay.room_category if stay else "Classic Room",
                    "room_quantity": stay.room_quantity if stay else 1,
                    "confirmation_number": stay.confirmation_number or stay.reservation_id if stay else "",
                } if stay else None,
                "brs_rounds": [r.model_dump() for r in rounds],
                "payments": [p.model_dump() for p in payments],
            },
        }
        return snapshot

    def generate_revised_itinerary_pdf(self, booking_reference: str) -> Tuple[bytes, str]:
        """Generate authoritative binary PDF bytes for a booking matching its current revision."""
        clean_ref = booking_reference.strip().upper()
        snapshot = self.get_canonical_booking_snapshot(clean_ref)

        pdf_bytes = build_itinerary_pdf(snapshot)
        rev = snapshot.get("revision_number", 1)
        filename = f"Rosapenna-Revised-Itinerary-{clean_ref}-Rev{rev}.pdf" if rev > 0 else f"Rosapenna-Itinerary-{clean_ref}.pdf"
        return pdf_bytes, filename

    def send_revised_confirmation(
        self,
        booking_reference: str,
        recipient_override: Optional[str] = None,
        idempotency_key: Optional[str] = None,
        amendment_summary: Optional[str] = None,
    ) -> EmailDeliveryResult:
        """Dispatch revised confirmation email with newly rendered PDF attachment."""
        clean_ref = booking_reference.strip().upper()
        snapshot = self.get_canonical_booking_snapshot(clean_ref)

        rev = snapshot.get("revision_number", 1)
        delivery = self.email_service.send_revised_booking_confirmation(
            booking=snapshot,
            recipient_override=recipient_override,
            idempotency_key=idempotency_key,
            revision_number=rev,
            amendment_summary=amendment_summary,
        )
        return delivery

    def reconcile_and_reschedule_reminders(
        self,
        booking_reference: str,
        new_arrival_date: dt.date,
        new_revision: int,
        actor: str = "amendment_worker",
    ) -> List[ScheduledReminderItem]:
        """Reconcile and reschedule pre-arrival reminders following an amendment or stay date change.

        Cancels obsolete reminders from previous revisions and idempotently schedules new ones.
        """
        clean_ref = booking_reference.strip().upper()
        now = _utc_now()
        today = dt.date.today()

        # Retrieve current balance to check if balance reminders should be suppressed
        balance_due = Decimal("1.00")  # default assume balance due
        try:
            snapshot = self.get_canonical_booking_snapshot(clean_ref)
            balance_due = Decimal(str(snapshot.get("financials", {}).get("balance_due", "1.00")))
        except Exception:
            pass

        # 1. Define milestone schedules relative to arrival date
        # - balance_due_first: strictly 42 days (6 weeks) prior
        # - rooming_and_caddy: 42 days prior (or today if booked within 42 days)
        # - concierge_check: 14 days prior
        # - balance_due_final: 5 days prior
        milestones = [
            ("balance_due_first", "Initial Balance Payment Request (6 Weeks Prior)", 42),
            ("rooming_and_caddy", "Rooming List & Caddy Preferences (6 Weeks Prior)", 42),
            ("concierge_check", "Concierge Welcome & Itinerary Review (14 Days Prior)", 14),
            ("balance_due_final", "Final Balance Due Notice (5 Days Prior)", 5),
        ]

        # 2. Cancel obsolete reminders from prior revisions
        if self.collection is not None:
            self.collection.update_many(
                {
                    "booking_reference": clean_ref,
                    "status": "scheduled",
                    "revision_number": {"$lt": new_revision},
                },
                {
                    "$set": {
                        "status": "canceled_by_amendment",
                        "suppression_reason": f"Superseded by amendment revision #{new_revision} (Actor: {actor})",
                        "updated_at": now,
                    }
                },
            )
        else:
            for rem in self._in_memory_reminders.values():
                if (
                    rem["booking_reference"] == clean_ref
                    and rem["status"] == "scheduled"
                    and rem["revision_number"] < new_revision
                ):
                    rem["status"] = "canceled_by_amendment"
                    rem["suppression_reason"] = f"Superseded by amendment revision #{new_revision}"
                    rem["updated_at"] = now

        # 3. Insert or update new reminders for current revision
        result_items: List[ScheduledReminderItem] = []

        for r_type, title, lead_days in milestones:
            sched_date = new_arrival_date - dt.timedelta(days=lead_days)
            rem_id = f"REM-{clean_ref}-{r_type}-REV{new_revision}"

            # Check if should be suppressed immediately
            is_balance_type = r_type in ("balance_due_first", "balance_due_final")
            status = "scheduled"
            reason = None

            if is_balance_type and balance_due <= Decimal("0.00"):
                status = "suppressed"
                reason = "Balance already fully settled"
            elif sched_date < today:
                # Milestone already passed in calendar time
                status = "suppressed"
                reason = f"Milestone date ({sched_date}) has already elapsed"

            doc = {
                "reminder_id": rem_id,
                "booking_reference": clean_ref,
                "revision_number": new_revision,
                "reminder_type": r_type,
                "title": title,
                "scheduled_date": sched_date.isoformat(),
                "target_lead_time_days": lead_days,
                "status": status,
                "suppression_reason": reason,
                "dispatched_at": None,
                "delivery_result": None,
                "created_at": now,
                "updated_at": now,
            }

            if self.collection is not None:
                self.collection.update_one(
                    {"reminder_id": rem_id},
                    {"$setOnInsert": doc},
                    upsert=True,
                )
            else:
                if rem_id not in self._in_memory_reminders:
                    self._in_memory_reminders[rem_id] = doc

            item = ScheduledReminderItem(
                reminder_id=rem_id,
                booking_reference=clean_ref,
                revision_number=new_revision,
                reminder_type=r_type,  # type: ignore[arg-type]
                title=title,
                scheduled_date=sched_date,
                target_lead_time_days=lead_days,
                status=status,  # type: ignore[arg-type]
                suppression_reason=reason,
                created_at=now,
                updated_at=now,
            )
            result_items.append(item)

        logger.info(
            "Reconciled %d reminders for booking %s at revision %d (Arrival: %s)",
            len(result_items), clean_ref, new_revision, new_arrival_date,
        )
        return result_items

    def get_scheduled_reminders(self, booking_reference: str) -> ScheduledReminderListResponse:
        """Fetch all reminders for a booking, automatically scheduling if none exist."""
        clean_ref = booking_reference.strip().upper()
        snapshot = self.get_canonical_booking_snapshot(clean_ref)

        rev = snapshot.get("revision_number", 1)
        arr_str = snapshot.get("stay", {}).get("arrival_date") if snapshot.get("stay") else None
        arr_date = dt.date.fromisoformat(arr_str) if arr_str else None
        bal_due_str = snapshot.get("financials", {}).get("balance_due_date")
        bal_due_date = dt.date.fromisoformat(bal_due_str) if bal_due_str else None

        # Query existing reminders
        items: List[ScheduledReminderItem] = []
        if self.collection is not None:
            docs = list(self.collection.find({"booking_reference": clean_ref}).sort("scheduled_date", 1))
            for d in docs:
                sched_d = dt.date.fromisoformat(d["scheduled_date"]) if isinstance(d["scheduled_date"], str) else d["scheduled_date"]
                del_res = EmailDeliveryResult(**d["delivery_result"]) if d.get("delivery_result") else None
                items.append(
                    ScheduledReminderItem(
                        reminder_id=d["reminder_id"],
                        booking_reference=d["booking_reference"],
                        revision_number=d["revision_number"],
                        reminder_type=d["reminder_type"],
                        title=d["title"],
                        scheduled_date=sched_d,
                        target_lead_time_days=d["target_lead_time_days"],
                        status=d["status"],
                        suppression_reason=d.get("suppression_reason"),
                        dispatched_at=d.get("dispatched_at"),
                        delivery_result=del_res,
                        created_at=d.get("created_at", _utc_now()),
                        updated_at=d.get("updated_at", _utc_now()),
                    )
                )
        else:
            for d in self._in_memory_reminders.values():
                if d["booking_reference"] == clean_ref:
                    sched_d = dt.date.fromisoformat(d["scheduled_date"]) if isinstance(d["scheduled_date"], str) else d["scheduled_date"]
                    del_res = EmailDeliveryResult(**d["delivery_result"]) if d.get("delivery_result") else None
                    items.append(
                        ScheduledReminderItem(
                            reminder_id=d["reminder_id"],
                            booking_reference=d["booking_reference"],
                            revision_number=d["revision_number"],
                            reminder_type=d["reminder_type"],
                            title=d["title"],
                            scheduled_date=sched_d,
                            target_lead_time_days=d["target_lead_time_days"],
                            status=d["status"],
                            suppression_reason=d.get("suppression_reason"),
                            dispatched_at=d.get("dispatched_at"),
                            delivery_result=del_res,
                            created_at=d.get("created_at", _utc_now()),
                            updated_at=d.get("updated_at", _utc_now()),
                        )
                    )

        # If empty and booking has an arrival date, schedule baseline milestones
        if not items and arr_date:
            items = self.reconcile_and_reschedule_reminders(
                booking_reference=clean_ref,
                new_arrival_date=arr_date,
                new_revision=rev,
                actor="auto_initializer",
            )

        return ScheduledReminderListResponse(
            booking_reference=clean_ref,
            active_revision=rev,
            arrival_date=arr_date,
            balance_due_date=bal_due_date,
            reminders=items,
        )

    def dispatch_reminder(
        self,
        booking_reference: str,
        reminder_id: str,
        actor: str = "staff_dispatch",
        idempotency_key: Optional[str] = None,
    ) -> DispatchReminderResponse:
        """Truthfully evaluate and dispatch a scheduled reminder.

        Suppresses if booking is cancelled, balance is settled, or reminder is obsolete.
        """
        clean_ref = booking_reference.strip().upper()
        now = _utc_now()
        now_str = now.isoformat()

        # 1. Fetch reminder
        reminder_doc: Optional[Dict[str, Any]] = None
        if self.collection is not None:
            reminder_doc = self.collection.find_one({"reminder_id": reminder_id, "booking_reference": clean_ref})
        else:
            reminder_doc = self._in_memory_reminders.get(reminder_id)

        if not reminder_doc:
            raise LinkageNotFoundError(f"Scheduled reminder '{reminder_id}' was not found for booking {clean_ref}.")

        # Idempotent replay if already dispatched
        if reminder_doc.get("status") == "dispatched":
            del_res = EmailDeliveryResult(**reminder_doc["delivery_result"]) if reminder_doc.get("delivery_result") else None
            return DispatchReminderResponse(
                reminder_id=reminder_id,
                status="dispatched",
                message="Reminder has already been dispatched. Idempotent replay.",
                delivery_result=del_res,
            )

        # 2. Re-read current authoritative booking state before dispatch
        snapshot = self.get_canonical_booking_snapshot(clean_ref)
        booking_status = snapshot.get("status")
        balance_due = Decimal(str(snapshot.get("financials", {}).get("balance_due", "0.00")))
        current_rev = snapshot.get("revision_number", 1)

        # 3. Truthful suppression checks
        if booking_status in ("cancelled", "canceled"):
            reason = "Booking is cancelled; reminder delivery suppressed"
            self._update_reminder_status(reminder_id, "suppressed", reason, None)
            return DispatchReminderResponse(
                reminder_id=reminder_id,
                status="suppressed",
                message=reason,
            )

        if reminder_doc["revision_number"] < current_rev:
            reason = f"Reminder is obsolete (revision {reminder_doc['revision_number']} < current {current_rev}); suppressed"
            self._update_reminder_status(reminder_id, "canceled_by_amendment", reason, None)
            return DispatchReminderResponse(
                reminder_id=reminder_id,
                status="canceled_by_amendment",
                message=reason,
            )

        is_balance_type = reminder_doc["reminder_type"] in ("balance_due_first", "balance_due_final")
        if is_balance_type and balance_due <= Decimal("0.00"):
            reason = "Remaining balance is zero (fully paid); reminder delivery suppressed"
            self._update_reminder_status(reminder_id, "suppressed", reason, None)
            return DispatchReminderResponse(
                reminder_id=reminder_id,
                status="suppressed",
                message=reason,
            )

        # 4. Dispatch via Email Delivery Service
        idemp = idempotency_key or f"disp_{reminder_id}_{reminder_doc['revision_number']}"
        delivery = self.email_service.send_pre_arrival_reminder(
            booking=snapshot,
            reminder_type=reminder_doc["reminder_type"],
            idempotency_key=idemp,
        )

        final_status = "dispatched" if delivery.success else "failed"
        reason = None if delivery.success else delivery.error_message

        self._update_reminder_status(
            reminder_id=reminder_id,
            status=final_status,
            suppression_reason=reason,
            delivery_result=delivery.model_dump(),
            dispatched_at=now_str if delivery.success else None,
        )

        if not delivery.success:
            logger.warning(
                "Reminder %s delivery failed: %s. Staff alerted; booking remains intact.",
                reminder_id, delivery.error_message,
            )

        return DispatchReminderResponse(
            reminder_id=reminder_id,
            status=final_status,
            message="Reminder dispatched successfully" if delivery.success else f"Delivery failed: {delivery.error_message}",
            delivery_result=delivery,
        )

    def _update_reminder_status(
        self,
        reminder_id: str,
        status: str,
        suppression_reason: Optional[str],
        delivery_result: Optional[Dict[str, Any]],
        dispatched_at: Optional[str] = None,
    ) -> None:
        now = _utc_now()
        update_fields: Dict[str, Any] = {
            "status": status,
            "suppression_reason": suppression_reason,
            "updated_at": now,
        }
        if delivery_result is not None:
            update_fields["delivery_result"] = delivery_result
        if dispatched_at is not None:
            update_fields["dispatched_at"] = dispatched_at

        if self.collection is not None:
            self.collection.update_one({"reminder_id": reminder_id}, {"$set": update_fields})
        elif reminder_id in self._in_memory_reminders:
            self._in_memory_reminders[reminder_id].update(update_fields)
