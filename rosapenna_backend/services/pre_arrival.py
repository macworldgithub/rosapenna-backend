"""Domain service for Ticket N08 — Pre-arrival scheduling and communication workflows.

Provides durable, channel-agnostic scheduling for confirmed bookings across online,
phone, and email sources. Enforces strict policy validation, atomic conditional claims,
truthful suppression, staff recovery, and bounded repair scans.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
import logging
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Tuple
import uuid
from zoneinfo import ZoneInfo

from pymongo.collection import Collection
from pymongo.database import Database

from rosapenna_backend.api.schemas.pre_arrival import (
    EligibleBookingInput,
    PreArrivalJobAttempt,
    PreArrivalJobDoc,
    PreArrivalPolicyDoc,
)
from rosapenna_backend.core.logging import logger
from rosapenna_backend.services.mmb_gate import MmbGateService
from rosapenna_backend.services.email_delivery import (
    ConfirmationTransportProtocol,
    DisabledConfirmationTransport,
    RecordingConfirmationTransport,
    TransportSubmitOutcome,
    render_n08_pre_arrival_message,
)


class PreArrivalPolicyError(Exception):
    """Raised when a pre-arrival policy is invalid, missing, or unapproved."""


class PreArrivalConflictError(Exception):
    """Raised when schedule state, booking revision, or concurrency check conflicts."""


class PreArrivalNotFoundError(Exception):
    """Raised when a requested booking or job is not found."""


def _utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _resolve_send_time(
    send_date: dt.date,
    local_time_str: str,
    tz: ZoneInfo,
    dst_rule: str = "standard",
) -> dt.datetime:
    """Combine local date and send time in property timezone, resolving DST and converting to UTC."""
    parts = local_time_str.split(":")
    hour = int(parts[0])
    minute = int(parts[1])

    local_dt = dt.datetime(
        year=send_date.year,
        month=send_date.month,
        day=send_date.day,
        hour=hour,
        minute=minute,
        tzinfo=tz,
    )

    if dst_rule == "strict_reject_nonexistent":
        # Check if local_dt is in a DST spring-forward gap
        # By converting to UTC and back to property timezone:
        utc_dt = local_dt.astimezone(dt.timezone.utc)
        roundtrip = utc_dt.astimezone(tz)
        if roundtrip.hour != hour or roundtrip.minute != minute or roundtrip.date() != send_date:
            raise ValueError(f"Local send time {local_time_str} is nonexistent on {send_date} due to DST transition")
        return utc_dt

    return local_dt.astimezone(dt.timezone.utc)


class PreArrivalService:
    """Manages pre-arrival scheduling, evidence verification, atomic job execution, and staff recovery."""

    MAX_ATTEMPTS = 3

    def __init__(
        self,
        database: Optional[Database] = None,
        transport: Optional[ConfirmationTransportProtocol] = None,
        environment: str = "test",
        property_id: str = "ROSA",
        test_run_id: Optional[str] = None,
        mmb_gate: Optional[MmbGateService] = None,
        balance_service: Optional[Any] = None,
        reminders_collection_name: str = "scheduled_booking_reminders",
        policies_collection_name: str = "pre_arrival_policies",
        worker_state_collection_name: str = "pre_arrival_worker_state",
        linkages_collection_name: str = "mmb_booking_linkages",
        init_indexes: bool = True,
    ):
        self.db: Optional[Database] = database
        self.environment = environment.strip().lower()
        self.property_id = property_id.strip().upper()
        self.test_run_id = test_run_id
        self.mmb_gate = mmb_gate or MmbGateService(database=database)
        self.balance_service = balance_service

        self.reminders_col: Optional[Collection] = (
            database[reminders_collection_name] if database is not None else None
        )
        self.policies_col: Optional[Collection] = (
            database[policies_collection_name] if database is not None else None
        )
        self.worker_state_col: Optional[Collection] = (
            database[worker_state_collection_name] if database is not None else None
        )
        self.linkages_col: Optional[Collection] = (
            database[linkages_collection_name] if database is not None else None
        )

        self.transport = transport or DisabledConfirmationTransport()

        if self.reminders_col is not None and init_indexes:
            try:
                self._init_indexes()
            except Exception as exc:
                logger.error("Failed to initialize pre-arrival indexes: %s", exc)
                raise RuntimeError(f"Pre-arrival index readiness failure: {exc}") from exc

    def _init_indexes(self) -> None:
        """Create required indexes for pre-arrival scheduling and worker performance."""
        assert self.reminders_col is not None
        assert self.policies_col is not None

        # Compound unique constraint prevents duplicate jobs for same booking, event, and revision
        self.reminders_col.create_index(
            [
                ("environment", 1),
                ("property_id", 1),
                ("booking_id", 1),
                ("revision_number", 1),
                ("reminder_type", 1),
            ],
            unique=True,
            partialFilterExpression={"schema_version": 2},
            name="uniq_n08_job_key",
        )
        self.reminders_col.create_index([("reminder_id", 1)], unique=True, name="uniq_n08_reminder_id")
        self.reminders_col.create_index(
            [("environment", 1), ("property_id", 1), ("status", 1), ("next_attempt_at", 1), ("due_at", 1)],
            name="idx_n08_due_work",
        )
        self.reminders_col.create_index(
            [("environment", 1), ("property_id", 1), ("booking_id", 1), ("revision_number", 1)],
            name="idx_n08_booking_hist",
        )
        self.policies_col.create_index(
            [("environment", 1), ("policy_id", 1), ("version", 1)],
            unique=True,
            name="uniq_n08_policy_version",
        )

    # -------------------------------------------------------------------------
    # Policy Management & Validation
    # -------------------------------------------------------------------------

    def save_policy(self, policy: PreArrivalPolicyDoc) -> None:
        """Store or update a policy document, ensuring approved policies are immutable."""
        if self.policies_col is None:
            raise RuntimeError("Database connection is required to persist policies")

        existing = self.policies_col.find_one({
            "environment": policy.environment,
            "policy_id": policy.policy_id,
            "version": policy.version,
        })
        if existing and existing.get("approval_status") == "approved":
            raise PreArrivalPolicyError("Approved policy versions are strictly immutable and cannot be updated")

        self.policies_col.update_one(
            {
                "environment": policy.environment,
                "policy_id": policy.policy_id,
                "version": policy.version,
            },
            {"$set": policy.model_dump(mode="json")},
            upsert=True,
        )

    def get_policy(
        self,
        policy_id: Optional[str] = None,
        version: Optional[int] = None,
    ) -> PreArrivalPolicyDoc:
        """Fetch the active or requested policy doc."""
        if self.policies_col is not None:
            query: Dict[str, Any] = {"environment": self.environment}
            if policy_id:
                query["policy_id"] = policy_id
            if version:
                query["version"] = version

            doc = self.policies_col.find_one(query, sort=[("version", -1)])
            if doc:
                doc.pop("_id", None)
                return PreArrivalPolicyDoc(**doc)

        raise PreArrivalPolicyError(f"No policy found for environment '{self.environment}'")

    # -------------------------------------------------------------------------
    # Pure Milestone Calculation
    # -------------------------------------------------------------------------

    def calculate_milestones(
        self,
        booking: EligibleBookingInput,
        policy: PreArrivalPolicyDoc,
        now: Optional[dt.datetime] = None,
    ) -> List[Dict[str, Any]]:
        """Calculate scheduled pre-arrival milestones according to explicit policy rules.

        Deterministic function with injected clock. Generates:
          1. balance_due_first (42 or 56 days prior, or blocked if unresolved)
          2. rooming_and_caddy (strictly 42 days prior)
        """
        clock = now or _utc_now()
        tz = ZoneInfo(policy.timezone)
        clock_prop = clock.astimezone(tz)
        today_prop = clock_prop.date()

        milestones: List[Dict[str, Any]] = []

        bal_due_date_str = (booking.arrival_date - dt.timedelta(days=policy.balance_due_lead_days)).isoformat()
        bal_due_amount_str = f"{booking.balance_due:.2f}"
        rc_sched_date_str = (booking.arrival_date - dt.timedelta(days=policy.rooming_caddy_lead_days)).isoformat()

        # Enforce approved & effective policy check
        is_policy_valid = (
            policy.approval_status == "approved"
            and (policy.effective_from is None or today_prop >= policy.effective_from)
            and (policy.effective_to is None or today_prop <= policy.effective_to)
        )
        if not is_policy_valid:
            bal_reason = "UNRESOLVED_BALANCE_TIMING" if policy.balance_request_lead_days is None else "UNAPPROVED_OR_EXPIRED_POLICY"
            rc_reason = "UNRESOLVED_LATE_BOOKING_POLICY" if policy.late_booking_action == "unresolved" else "UNAPPROVED_OR_EXPIRED_POLICY"
            milestones.append({
                "reminder_type": "balance_due_first",
                "title": "Initial Balance Payment Request",
                "scheduled_date": bal_due_date_str,
                "due_at": clock,
                "timezone": policy.timezone,
                "balance_due_date": bal_due_date_str,
                "balance_due_amount": bal_due_amount_str,
                "status": "blocked_dependency",
                "reason_code": bal_reason,
            })
            milestones.append({
                "reminder_type": "rooming_and_caddy",
                "title": f"Rooming List & Caddy Preferences ({policy.rooming_caddy_lead_days} Days Prior)",
                "scheduled_date": rc_sched_date_str,
                "due_at": clock,
                "timezone": policy.timezone,
                "balance_due_date": bal_due_date_str,
                "balance_due_amount": bal_due_amount_str,
                "status": "blocked_dependency",
                "reason_code": rc_reason,
            })
            return milestones

        # ---------------------------------------------------------------------
        # Milestone 1: Balance Due First Request
        # ---------------------------------------------------------------------

        if policy.balance_request_lead_days is None:
            # Unresolved timing blocks schedule activation
            milestones.append({
                "reminder_type": "balance_due_first",
                "title": "Initial Balance Payment Request",
                "scheduled_date": bal_due_date_str,
                "due_at": clock,
                "timezone": policy.timezone,
                "balance_due_date": bal_due_date_str,
                "balance_due_amount": bal_due_amount_str,
                "status": "blocked_dependency",
                "reason_code": "UNRESOLVED_BALANCE_TIMING",
            })
        else:
            bal_lead = policy.balance_request_lead_days
            bal_sched_date = booking.arrival_date - dt.timedelta(days=bal_lead)
            bal_due_at = _resolve_send_time(bal_sched_date, policy.local_send_time, tz, policy.dst_rule)

            bal_status: str = "scheduled"
            bal_reason: Optional[str] = None

            if booking.balance_due <= Decimal("0.00"):
                bal_status = "suppressed"
                bal_reason = "BALANCE_ALREADY_PAID"
            elif today_prop >= booking.arrival_date:
                bal_status = "suppressed"
                bal_reason = "ARRIVAL_DATE_ELAPSED"
            elif bal_sched_date < today_prop or bal_due_at < clock:
                # Milestone date already elapsed (late booking or past milestone)
                if policy.late_booking_action == "unresolved":
                    bal_status = "blocked_dependency"
                    bal_reason = "UNRESOLVED_LATE_BOOKING_POLICY"
                elif policy.late_booking_action == "immediate":
                    bal_due_at = max(clock, booking.confirmed_at)
                    bal_status = "scheduled"
                    bal_reason = "LATE_BOOKING_IMMEDIATE"
                elif policy.late_booking_action == "adjusted":
                    bal_due_at = max(clock, booking.confirmed_at + dt.timedelta(hours=policy.late_booking_offset_hours))
                    bal_status = "scheduled"
                    bal_reason = "LATE_BOOKING_ADJUSTED"

            milestones.append({
                "reminder_type": "balance_due_first",
                "title": f"Initial Balance Payment Request ({bal_lead} Days Prior)",
                "scheduled_date": bal_sched_date.isoformat(),
                "due_at": bal_due_at,
                "timezone": policy.timezone,
                "balance_due_date": bal_due_date_str,
                "balance_due_amount": bal_due_amount_str,
                "status": bal_status,
                "reason_code": bal_reason,
            })

        # ---------------------------------------------------------------------
        # Milestone 2: Rooming List and Caddy Preferences
        # ---------------------------------------------------------------------
        rc_lead = policy.rooming_caddy_lead_days
        rc_sched_date = booking.arrival_date - dt.timedelta(days=rc_lead)
        rc_due_at = _resolve_send_time(rc_sched_date, policy.local_send_time, tz, policy.dst_rule)

        rc_status: str = "scheduled"
        rc_reason: Optional[str] = None

        if booking.rooming_completed and booking.caddy_completed:
            rc_status = "suppressed"
            rc_reason = "ROOMING_CADDY_ALREADY_COMPLETED"
        elif today_prop >= booking.arrival_date:
            rc_status = "suppressed"
            rc_reason = "ARRIVAL_DATE_ELAPSED"
        elif rc_sched_date < today_prop or rc_due_at < clock:
            if policy.late_booking_action == "unresolved":
                rc_status = "blocked_dependency"
                rc_reason = "UNRESOLVED_LATE_BOOKING_POLICY"
            elif policy.late_booking_action == "immediate":
                rc_due_at = max(clock, booking.confirmed_at)
                rc_status = "scheduled"
                rc_reason = "LATE_BOOKING_IMMEDIATE"
            elif policy.late_booking_action == "adjusted":
                rc_due_at = max(clock, booking.confirmed_at + dt.timedelta(hours=policy.late_booking_offset_hours))
                rc_status = "scheduled"
                rc_reason = "LATE_BOOKING_ADJUSTED"

        milestones.append({
            "reminder_type": "rooming_and_caddy",
            "title": f"Rooming List & Caddy Preferences ({rc_lead} Days Prior)",
            "scheduled_date": rc_sched_date.isoformat(),
            "due_at": rc_due_at,
            "timezone": policy.timezone,
            "balance_due_date": bal_due_date_str,
            "balance_due_amount": bal_due_amount_str,
            "status": rc_status,
            "reason_code": rc_reason,
        })

        return milestones

    # -------------------------------------------------------------------------
    # Server-owned Booking Adapter
    # -------------------------------------------------------------------------

    def adapt_linkage_to_eligible_booking(
        self,
        linkage_doc: Dict[str, Any],
        now: Optional[dt.datetime] = None,
    ) -> Tuple[Optional[EligibleBookingInput], Optional[str]]:
        """Validate and adapt an untrusted database linkage document into an EligibleBookingInput."""
        clock = now or _utc_now()
        ref = linkage_doc.get("booking_reference", "")
        status = linkage_doc.get("status", "")
        discrepancies = linkage_doc.get("discrepancies") or []
        lead = linkage_doc.get("lead_guest") or {}
        stay = linkage_doc.get("opera_stay") or {}
        fin = linkage_doc.get("financials") or {}
        rev = linkage_doc.get("revision", linkage_doc.get("revision_number", 1))
        channel = linkage_doc.get("source_channel", "online")

        if status.lower() not in ("linked", "confirmed"):
            return None, f"LINKAGE_STATUS_NOT_LINKED_{status.upper()}"
        if linkage_doc.get("in_recovery") is True:
            return None, "BOOKING_IN_RECOVERY"
        if discrepancies:
            return None, "LINKAGE_HAS_DISCREPANCIES"
        if not stay.get("reservation_id"):
            return None, "MISSING_HOTEL_RESERVATION_ID"
        if not stay.get("arrival_date") or not stay.get("departure_date"):
            return None, "MISSING_STAY_DATES"
        if not lead.get("email"):
            return None, "MISSING_LEAD_EMAIL"

        rounds = linkage_doc.get("brs_rounds")
        if rounds is not None and not rounds and channel == "online":
            return None, "MISSING_CONFIRMED_GOLF_ROUNDS"

        # Check financial fields
        if "balance_due" not in fin or "total_amount" not in fin:
            return None, "MISSING_FINANCIAL_EVIDENCE"

        try:
            total_amount = Decimal(str(fin.get("total_amount", "0.00")))
            verified_paid = Decimal(str(fin.get("verified_paid_total", fin.get("deposit_paid", "0.00"))))
            balance_due = Decimal(str(fin.get("balance_due", "0.00")))
            if total_amount < Decimal("0.00") or verified_paid < Decimal("0.00") or balance_due < Decimal("0.00"):
                return None, "NEGATIVE_FINANCIAL_AMOUNTS"
            arr_date = (
                dt.date.fromisoformat(stay["arrival_date"])
                if isinstance(stay["arrival_date"], str)
                else stay["arrival_date"]
            )
            dep_date = (
                dt.date.fromisoformat(stay["departure_date"])
                if isinstance(stay["departure_date"], str)
                else stay["departure_date"]
            )
            if dep_date <= arr_date:
                return None, "INVALID_STAY_DATES"
        except Exception:
            return None, "MALFORMED_FINANCIAL_OR_DATE_FIELDS"

        is_canc = status.lower() in ("cancelled", "canceled")
        conf_status = "confirmed" if status in ("linked", "confirmed") else status

        # Check rooming and caddy completion flags
        rooming_done = bool(stay.get("occupant_names") and len(stay.get("occupant_names")) >= stay.get("room_quantity", 1))
        caddy_done = all(r.get("caddy_request_status") in ("Requested", "None", "Fulfilled", "declined") for r in rounds) if rounds else True

        confirmed_at = linkage_doc.get("created_at") or clock
        if isinstance(confirmed_at, str):
            confirmed_at = dt.datetime.fromisoformat(confirmed_at.replace("Z", "+00:00"))

        input_data = EligibleBookingInput(
            booking_id=ref,
            booking_reference=ref,
            property_id=stay.get("hotel_id", "ROSA"),
            source_channel=channel,
            revision=rev,
            arrival_date=arr_date,
            departure_date=dep_date,
            confirmed_at=confirmed_at,
            confirmation_status=conf_status,
            confirmation_evidence={"linkage_status": status, "discrepancies": len(discrepancies)},
            is_cancelled=is_canc,
            has_discrepancy=bool(discrepancies),
            in_recovery=bool(linkage_doc.get("in_recovery", False)),
            lead_name=f"{lead.get('first_name', '')} {lead.get('last_name', '')}".strip() or "Valued Guest",
            lead_email=lead.get("email", ""),
            lead_telephone=lead.get("telephone"),
            total_amount=total_amount,
            verified_paid_total=verified_paid,
            currency=fin.get("currency", "EUR"),
            balance_due=balance_due,
            ledger_version=int(linkage_doc.get("ledger_version", 1)),
            rooming_completed=rooming_done,
            caddy_completed=caddy_done,
            evidence_timestamp=clock,
        )
        return input_data, None

    # -------------------------------------------------------------------------
    # Durable Reconciliation
    # -------------------------------------------------------------------------

    def reconcile_booking(
        self,
        booking_reference: str,
        expected_revision: Optional[int] = None,
        actor: str = "scheduler",
        now: Optional[dt.datetime] = None,
        policy: Optional[PreArrivalPolicyDoc] = None,
    ) -> List[PreArrivalJobDoc]:
        """Reconcile pre-arrival reminders for a booking against current evidence and policy.

        Cancels obsolete pending reminders from prior revisions and idempotently creates
        new reminders using atomic MongoDB upserts.
        """
        if self.reminders_col is None or self.linkages_col is None:
            raise RuntimeError("Database connection is required for pre-arrival reconciliation")

        clock = now or _utc_now()
        clean_ref = booking_reference.strip().upper()

        linkage_doc = self.linkages_col.find_one({"booking_reference": clean_ref})
        if not linkage_doc:
            raise PreArrivalNotFoundError(f"Booking linkage '{clean_ref}' was not found")

        current_rev = linkage_doc.get("revision", linkage_doc.get("revision_number", 1))
        if expected_revision is not None and expected_revision != current_rev:
            raise PreArrivalConflictError(
                f"Booking revision conflict: expected {expected_revision}, but current is {current_rev}"
            )

        active_policy = policy or self.get_policy()
        booking_input, block_reason = self.adapt_linkage_to_eligible_booking(linkage_doc, now=clock)

        if not booking_input:
            logger.warning("Booking %s is ineligible for scheduling: %s", clean_ref, block_reason)
            # Suppress any existing scheduled jobs
            self.reminders_col.update_many(
                {
                    "environment": self.environment,
                    "booking_reference": clean_ref,
                    "status": "scheduled",
                },
                {
                    "$set": {
                        "status": "blocked_dependency",
                        "reason_code": block_reason,
                        "updated_at": clock,
                    }
                },
            )
            return self.get_jobs_for_booking(clean_ref)

        # Check if booking is cancelled: suppress pending jobs
        if booking_input.is_cancelled:
            self.reminders_col.update_many(
                {
                    "environment": self.environment,
                    "booking_reference": clean_ref,
                    "status": {"$in": ["scheduled", "blocked_dependency", "failed"]},
                },
                {
                    "$set": {
                        "status": "suppressed",
                        "reason_code": "BOOKING_CANCELLED",
                        "updated_at": clock,
                    }
                },
            )
            return self.get_jobs_for_booking(clean_ref)

        eligibility_hash = booking_input.compute_eligibility_hash()

        # Check for same-revision schedule conflicts:
        # If an existing job for this revision has a different arrival date or eligibility hash
        existing_jobs = list(self.reminders_col.find({
            "environment": self.environment,
            "booking_reference": clean_ref,
            "revision_number": current_rev,
        }))
        for ej in existing_jobs:
            target_hash = booking_input.compute_eligibility_hash(reminder_type=ej.get("reminder_type"))
            if ej.get("eligibility_hash") and ej.get("eligibility_hash") != target_hash:
                # If balance is fully paid, suppress pending balance reminder rather than raising schedule conflict
                if booking_input.balance_due <= Decimal("0.00") and ej.get("reminder_type") == "balance_due_first":
                    continue
                # If stay dates changed without revision bump, raise schedule conflict
                lead_days = (
                    active_policy.balance_request_lead_days or 42
                    if ej.get("reminder_type") == "balance_due_first"
                    else active_policy.rooming_caddy_lead_days
                )
                expected_sched_date = (booking_input.arrival_date - dt.timedelta(days=lead_days)).isoformat()
                if ej.get("scheduled_date") != expected_sched_date:
                    raise PreArrivalConflictError(
                        f"Schedule conflict: Booking {clean_ref} facts changed on revision {current_rev} without revision bump."
                    )

        # 1. Cancel obsolete jobs from prior revisions
        self.reminders_col.update_many(
            {
                "environment": self.environment,
                "booking_reference": clean_ref,
                "revision_number": {"$lt": current_rev},
                "status": {"$in": ["scheduled", "blocked_dependency", "failed"]},
            },
            {
                "$set": {
                    "status": "canceled_by_amendment",
                    "reason_code": f"SUPERSEDED_BY_REVISION_{current_rev}",
                    "updated_at": clock,
                }
            },
        )

        # 2. Calculate milestones
        milestones = self.calculate_milestones(booking_input, active_policy, now=clock)

        # 3. Idempotently insert/update jobs
        for m in milestones:
            r_type = m["reminder_type"]
            due_at = m["due_at"]
            rem_id = f"REM-N08-{clean_ref}-{r_type}-R{current_rev}"
            milestone_hash = booking_input.compute_eligibility_hash(reminder_type=r_type)

            filter_query = {
                "environment": self.environment,
                "property_id": booking_input.property_id,
                "booking_id": clean_ref,
                "revision_number": current_rev,
                "reminder_type": r_type,
            }
            existing = self.reminders_col.find_one(filter_query)
            if existing:
                if existing.get("status") in ("scheduled", "blocked_dependency", "failed") and m["status"] == "suppressed":
                    self.reminders_col.update_one(
                        {"_id": existing["_id"]},
                        {
                            "$set": {
                                "status": "suppressed",
                                "reason_code": m["reason_code"],
                                "updated_at": clock,
                            }
                        },
                    )
                elif existing.get("status") == "blocked_dependency" and m["status"] == "scheduled":
                    self.reminders_col.update_one(
                        {"_id": existing["_id"]},
                        {
                            "$set": {
                                "status": "scheduled",
                                "due_at": due_at,
                                "next_attempt_at": due_at,
                                "reason_code": m["reason_code"],
                                "eligibility_hash": milestone_hash,
                                "policy_id": active_policy.policy_id,
                                "policy_version": active_policy.version,
                                "template_version": active_policy.template_version,
                                "updated_at": clock,
                            }
                        },
                    )
            else:
                job_doc = {
                    "schema_version": 2,
                    "reminder_id": rem_id,
                    "environment": self.environment,
                    "test_run_id": self.test_run_id,
                    "property_id": booking_input.property_id,
                    "booking_id": clean_ref,
                    "booking_reference": clean_ref,
                    "revision_number": current_rev,
                    "reminder_type": r_type,
                    "title": m["title"],
                    "scheduled_date": m["scheduled_date"],
                    "due_at": due_at,
                    "timezone": m["timezone"],
                    "policy_id": active_policy.policy_id,
                    "policy_version": active_policy.version,
                    "template_version": active_policy.template_version,
                    "eligibility_hash": milestone_hash,
                    "balance_due_date": m.get("balance_due_date"),
                    "balance_due_amount": m.get("balance_due_amount"),
                    "status": m["status"],
                    "reason_code": m["reason_code"],
                    "attempt_count": 0,
                    "next_attempt_at": due_at if m["status"] == "scheduled" else None,
                    "attempt_id": None,
                    "lease_expires_at": None,
                    "message_hash": None,
                    "message_id": None,
                    "submission_started": False,
                    "submission_started_at": None,
                    "prepared_at": None,
                    "prepared_mime_bytes": None,
                    "prepared_message_date": None,
                    "rendered_content": None,
                    "provider_reference": None,
                    "delivery_result": None,
                    "attempts": [],
                    "created_at": clock,
                    "updated_at": clock,
                }
                self.reminders_col.insert_one(job_doc)

        logger.info(
            "Reconciled N08 pre-arrival reminders for %s at revision %d (Actor: %s)",
            clean_ref, current_rev, actor,
        )
        return self.get_jobs_for_booking(clean_ref)

    # -------------------------------------------------------------------------
    # Atomic Claim and Dispatch
    # -------------------------------------------------------------------------

    def recover_expired_claims(self, now: Optional[dt.datetime] = None, limit: int = 50) -> int:
        """Recover jobs whose worker leases expired while sending into actionable unknown state."""
        if self.reminders_col is None:
            return 0
        clock = now or _utc_now()
        query: Dict[str, Any] = {
            "environment": self.environment,
            "property_id": self.property_id,
            "status": "sending",
            "lease_expires_at": {"$lte": clock},
        }
        if self.environment == "test" or self.test_run_id:
            query["test_run_id"] = self.test_run_id

        cursor = self.reminders_col.find(query).limit(limit)
        count = 0
        for doc in cursor:
            attempt_id = doc.get("attempt_id") or "UNKNOWN"
            res = self.reminders_col.update_one(
                {
                    "_id": doc["_id"],
                    "status": "sending",
                    "lease_expires_at": doc.get("lease_expires_at"),
                },
                {
                    "$set": {
                        "status": "unknown",
                        "reason_code": "CLAIM_LEASE_EXPIRED",
                        "submission_started": False,
                        "updated_at": clock,
                    },
                    "$push": {
                        "attempts": {
                            "attempt_id": attempt_id,
                            "attempt_number": doc.get("attempt_count", 1),
                            "started_at": doc.get("updated_at", clock),
                            "ended_at": clock,
                            "actor": "claim_lease_recovery",
                            "outcome": "unknown",
                            "reason_code": "LEASE_EXPIRED_BEFORE_FINALIZATION",
                            "error_details": "Worker crashed or lease expired while in sending status",
                        }
                    },
                },
            )
            if res.modified_count == 1:
                count += 1
        return count

    def claim_due_jobs(
        self,
        now: Optional[dt.datetime] = None,
        batch_size: int = 50,
        lease_minutes: int = 15,
    ) -> List[PreArrivalJobDoc]:
        """Atomically claim up to batch_size due pre-arrival jobs for processing."""
        if self.reminders_col is None:
            raise RuntimeError("Database connection is required to claim jobs")

        clock = now or _utc_now()
        lease_expires = clock + dt.timedelta(minutes=lease_minutes)

        # Recover expired worker leases before selecting due work
        self.recover_expired_claims(now=clock)

        # Match due jobs waiting for dispatch or retry
        query: Dict[str, Any] = {
            "environment": self.environment,
            "property_id": self.property_id,
            "status": {"$in": ["scheduled", "failed"]},
            "$or": [
                {"next_attempt_at": {"$lte": clock}},
                {"next_attempt_at": None, "due_at": {"$lte": clock}},
            ],
            "$and": [
                {"$or": [{"lease_expires_at": None}, {"lease_expires_at": {"$lte": clock}}]},
                {"attempt_count": {"$lt": self.MAX_ATTEMPTS}},
            ],
        }
        if self.environment == "test" or self.test_run_id:
            query["test_run_id"] = self.test_run_id

        claimed_jobs: List[PreArrivalJobDoc] = []
        candidate_cursor = self.reminders_col.find(query).sort("due_at", 1).limit(batch_size)

        for candidate in candidate_cursor:
            new_attempt_id = f"ATT-{uuid.uuid4().hex[:12]}"
            doc = self.reminders_col.find_one_and_update(
                {
                    "_id": candidate["_id"],
                    "status": candidate["status"],
                    "lease_expires_at": candidate.get("lease_expires_at"),
                },
                {
                    "$set": {
                        "status": "sending",
                        "attempt_id": new_attempt_id,
                        "lease_expires_at": lease_expires,
                        "submission_started": False,
                        "updated_at": clock,
                    },
                    "$inc": {"attempt_count": 1},
                },
                return_document=True,
            )
            if doc is not None:
                doc.pop("_id", None)
                claimed_jobs.append(PreArrivalJobDoc(**doc))

        return claimed_jobs

    def dispatch_claimed_job(
        self,
        job: PreArrivalJobDoc,
        now: Optional[dt.datetime] = None,
    ) -> PreArrivalJobDoc:
        """Render, submit, and record the outcome of a claimed pre-arrival job."""
        if self.reminders_col is None or self.linkages_col is None:
            raise RuntimeError("Database connection is required to dispatch jobs")

        clock = now or _utc_now()
        attempt_id = job.attempt_id or f"ATT-{uuid.uuid4().hex[:12]}"

        # 1. Reload booking linkage and policy
        clean_ref = job.booking_reference
        linkage_doc = self.linkages_col.find_one({"booking_reference": clean_ref})
        if not linkage_doc:
            self._finalize_job_state(
                job.reminder_id,
                attempt_id,
                "failed",
                "BOOKING_NOT_FOUND",
                clock,
                error_details="Booking linkage not found at dispatch time",
            )
            return self.get_job_by_id(job.reminder_id)

        current_rev = linkage_doc.get("revision", linkage_doc.get("revision_number", 1))
        if current_rev > job.revision_number:
            # Superseded by amendment
            self._finalize_job_state(
                job.reminder_id,
                attempt_id,
                "canceled_by_amendment",
                f"SUPERSEDED_BY_REVISION_{current_rev}",
                clock,
            )
            return self.get_job_by_id(job.reminder_id)

        booking_input, reason = self.adapt_linkage_to_eligible_booking(linkage_doc, now=clock)
        if not booking_input:
            self._finalize_job_state(
                job.reminder_id,
                attempt_id,
                "blocked_dependency",
                reason or "INELIGIBLE_AT_DISPATCH",
                clock,
            )
            return self.get_job_by_id(job.reminder_id)

        # Arrival elapsed check: If stay arrived or departed, suppress
        if clock.date() >= booking_input.arrival_date:
            self._finalize_job_state(
                job.reminder_id,
                attempt_id,
                "suppressed",
                "ARRIVAL_DATE_ELAPSED",
                clock,
            )
            return self.get_job_by_id(job.reminder_id)

        if booking_input.is_cancelled:
            self._finalize_job_state(
                job.reminder_id,
                attempt_id,
                "suppressed",
                "BOOKING_CANCELLED",
                clock,
            )
            return self.get_job_by_id(job.reminder_id)

        if job.reminder_type == "balance_due_first" and booking_input.balance_due <= Decimal("0.00"):
            self._finalize_job_state(
                job.reminder_id,
                attempt_id,
                "suppressed",
                "BALANCE_ALREADY_PAID",
                clock,
            )
            return self.get_job_by_id(job.reminder_id)

        if job.reminder_type == "rooming_and_caddy" and booking_input.rooming_completed and booking_input.caddy_completed:
            self._finalize_job_state(
                job.reminder_id,
                attempt_id,
                "suppressed",
                "ROOMING_CADDY_ALREADY_COMPLETED",
                clock,
            )
            return self.get_job_by_id(job.reminder_id)

        # Same-revision canonical facts changed check
        current_hash = booking_input.compute_eligibility_hash(reminder_type=job.reminder_type)
        if job.eligibility_hash and job.eligibility_hash != current_hash:
            self._finalize_job_state(
                job.reminder_id,
                attempt_id,
                "blocked_dependency",
                "SAME_REVISION_FACTS_CHANGED_CONFLICT",
                clock,
                error_details="Booking stay or financial facts changed without revision increment",
            )
            return self.get_job_by_id(job.reminder_id)

        try:
            policy = self.get_policy(policy_id=job.policy_id, version=job.policy_version)
        except PreArrivalPolicyError as exc:
            self._finalize_job_state(
                job.reminder_id,
                attempt_id,
                "blocked_dependency",
                "POLICY_UNAVAILABLE",
                clock,
                error_details=str(exc),
            )
            return self.get_job_by_id(job.reminder_id)

        # 2. Render or reuse frozen message content
        is_test_env = self.environment in ("test", "development")
        stored_doc = self.reminders_col.find_one({"reminder_id": job.reminder_id})
        if stored_doc and stored_doc.get("prepared_mime_bytes"):
            mime_bytes = stored_doc["prepared_mime_bytes"]
            msg_id = stored_doc.get("message_id")
            msg_hash = stored_doc.get("message_hash")
        else:
            action_url: Optional[str] = None
            if job.reminder_type == "balance_due_first" and self.balance_service is not None:
                action_url, _, block_reason = self.balance_service.prepare_balance_action(
                    booking_reference=clean_ref,
                    expected_booking_revision=job.revision_number,
                    expected_ledger_version=booking_input.ledger_version,
                    command_id=f"cmd_n08_{job.reminder_id}_{job.attempt_count}",
                    now=clock,
                )
                if block_reason == "BALANCE_ALREADY_PAID":
                    self._finalize_job_state(
                        job.reminder_id,
                        attempt_id,
                        "suppressed",
                        "BALANCE_ALREADY_PAID",
                        clock,
                    )
                    return self.get_job_by_id(job.reminder_id)
                elif block_reason:
                    self._finalize_job_state(
                        job.reminder_id,
                        attempt_id,
                        "blocked_dependency",
                        block_reason,
                        clock,
                    )
                    return self.get_job_by_id(job.reminder_id)

            try:
                subject, plain, html_body, mime_bytes, msg_id, msg_hash = render_n08_pre_arrival_message(
                    booking=booking_input,
                    reminder_type=job.reminder_type,
                    policy=policy,
                    attempt_id=attempt_id,
                    action_url=action_url,
                    mmb_enabled=self.mmb_gate.is_enabled(),
                    message_id=job.message_id,
                    message_date=clock.isoformat(),
                    is_test=is_test_env,
                )
            except Exception as exc:
                logger.error("Failed to render N08 message for %s: %s", job.reminder_id, exc)
                self._finalize_job_state(
                    job.reminder_id,
                    attempt_id,
                    "failed",
                    "RENDER_FAILED",
                    clock,
                    error_details=str(exc),
                )
                return self.get_job_by_id(job.reminder_id)

            self.reminders_col.update_one(
                {"reminder_id": job.reminder_id, "status": "sending", "attempt_id": attempt_id},
                {
                    "$set": {
                        "message_id": msg_id,
                        "message_hash": msg_hash,
                        "prepared_at": clock,
                        "prepared_mime_bytes": mime_bytes,
                        "prepared_message_date": clock.isoformat(),
                    }
                },
            )

        # 3. One-time submission transition to protect against double submit
        sub_trans = self.reminders_col.update_one(
            {
                "reminder_id": job.reminder_id,
                "revision_number": job.revision_number,
                "status": "sending",
                "attempt_id": attempt_id,
                "submission_started": {"$ne": True},
            },
            {
                "$set": {
                    "submission_started": True,
                    "submission_started_at": clock,
                    "updated_at": clock,
                }
            },
        )
        if sub_trans.matched_count != 1:
            logger.warning("Submission transition skipped for %s (already submitted or claim lost)", job.reminder_id)
            return self.get_job_by_id(job.reminder_id)

        # 4. Submit to transport
        try:
            outcome: TransportSubmitOutcome = self.transport.submit(
                message_bytes=mime_bytes,
                envelope_recipient=booking_input.lead_email,
                message_id=msg_id,
                attempt_id=attempt_id,
            )
        except Exception as exc:
            logger.error("Transport execution failed for %s: %s", job.reminder_id, exc)
            outcome = TransportSubmitOutcome(
                state="unknown",
                evidence_source="transport_exception",
                error_code="TRANSPORT_EXCEPTION",
                error_details=str(exc),
            )

        # 5. Map transport outcome to final state
        final_state = outcome.state
        reason_code = outcome.error_code

        next_attempt_at: Optional[dt.datetime] = None
        if final_state in ("retryable_failure", "failed"):
            final_state = "failed"
            # Exponential backoff: 1 min, 5 min, 30 min capped at 3 attempts
            delays = [1, 5, 30]
            if job.attempt_count < 3:
                delay_min = delays[min(job.attempt_count - 1, len(delays) - 1)]
                next_attempt_at = clock + dt.timedelta(minutes=delay_min)

        self._finalize_job_state(
            job.reminder_id,
            attempt_id,
            final_state,
            reason_code,
            clock,
            provider_ref=outcome.provider_reference,
            error_details=outcome.error_details,
            next_attempt_at=next_attempt_at,
        )

        return self.get_job_by_id(job.reminder_id)

    def _finalize_job_state(
        self,
        reminder_id: str,
        attempt_id: str,
        status: str,
        reason_code: Optional[str],
        now: dt.datetime,
        provider_ref: Optional[str] = None,
        error_details: Optional[str] = None,
        next_attempt_at: Optional[dt.datetime] = None,
    ) -> None:
        """Write job completion outcome atomically with stale worker protection."""
        assert self.reminders_col is not None

        attempt_doc = {
            "attempt_id": attempt_id,
            "started_at": now,
            "ended_at": now,
            "actor": "pre_arrival_worker",
            "outcome": status,
            "reason_code": reason_code,
            "provider_reference": provider_ref,
            "error_details": error_details,
        }

        update_fields: Dict[str, Any] = {
            "status": status,
            "reason_code": reason_code,
            "provider_reference": provider_ref,
            "next_attempt_at": next_attempt_at,
            "lease_expires_at": None,
            "updated_at": now,
        }

        # Safe conditional write requires match on reminder_id, sending status, and matching attempt_id
        res = self.reminders_col.update_one(
            {
                "reminder_id": reminder_id,
                "status": "sending",
                "attempt_id": attempt_id,
            },
            {
                "$set": update_fields,
                "$push": {"attempts": attempt_doc},
            },
        )
        if res.matched_count == 0:
            logger.warning(
                "Stale worker completion write ignored for job %s (Attempt: %s)",
                reminder_id, attempt_id,
            )

    # -------------------------------------------------------------------------
    # Missed-Hook Bounded Repair Scan
    # -------------------------------------------------------------------------

    def scan_and_repair_missed_hooks(
        self,
        limit: int = 100,
        now: Optional[dt.datetime] = None,
    ) -> int:
        """Perform a bounded pagination scan of linkages to repair any missed amendment hooks."""
        if self.linkages_col is None or self.worker_state_col is None:
            return 0

        clock = now or _utc_now()
        state_key = {"environment": self.environment, "property_id": "ROSA"}
        state_doc = self.worker_state_col.find_one(state_key) or {}
        last_id = state_doc.get("last_scanned_booking_reference")

        query: Dict[str, Any] = {}
        if last_id:
            query["booking_reference"] = {"$gt": last_id}

        cursor = self.linkages_col.find(query).sort("booking_reference", 1).limit(limit)
        items = list(cursor)

        reconciled_count = 0
        new_last_id = last_id

        for item in items:
            ref = item.get("booking_reference")
            if ref:
                try:
                    self.reconcile_booking(ref, actor="missed_hook_scanner", now=clock)
                    reconciled_count += 1
                except Exception as exc:
                    logger.warning("Error repairing missed hook for %s: %s", ref, exc)
                new_last_id = ref

        # If items is empty or fewer than limit, reset cursor so next run scans from start
        if len(items) < limit:
            new_last_id = None

        self.worker_state_col.update_one(
            state_key,
            {
                "$set": {
                    "last_scanned_booking_reference": new_last_id,
                    "last_scan_at": clock,
                    "updated_at": clock,
                }
            },
            upsert=True,
        )

        return reconciled_count

    # -------------------------------------------------------------------------
    # Staff Recovery Operations
    # -------------------------------------------------------------------------

    def get_jobs_for_booking(self, booking_reference: str) -> List[PreArrivalJobDoc]:
        """Fetch all pre-arrival reminder jobs for a specific booking."""
        if self.reminders_col is None:
            return []
        clean_ref = booking_reference.strip().upper()
        docs = list(self.reminders_col.find({
            "environment": self.environment,
            "booking_reference": clean_ref,
        }).sort("scheduled_date", 1))

        results: List[PreArrivalJobDoc] = []
        for d in docs:
            d.pop("_id", None)
            results.append(PreArrivalJobDoc(**d))
        return results

    def get_job_by_id(self, reminder_id: str) -> PreArrivalJobDoc:
        """Fetch single job doc by reminder ID."""
        if self.reminders_col is None:
            raise PreArrivalNotFoundError("No database connection available")
        clean_id = reminder_id.strip()
        doc = self.reminders_col.find_one({
            "environment": self.environment,
            "reminder_id": clean_id,
        })
        if not doc:
            raise PreArrivalNotFoundError(f"Job '{clean_id}' was not found")
        doc.pop("_id", None)
        return PreArrivalJobDoc(**doc)

    def retry_job(
        self,
        reminder_id: str,
        expected_revision: int,
        reason: str,
        actor: str = "staff_retry",
        reconciliation_outcome: Optional[str] = None,
        attempt_id: Optional[str] = None,
        now: Optional[dt.datetime] = None,
    ) -> PreArrivalJobDoc:
        """Safely re-enqueue a failed or reconciled unknown job for dispatch."""
        job = self.get_job_by_id(reminder_id)
        clock = now or _utc_now()

        if job.revision_number != expected_revision:
            raise PreArrivalConflictError(
                f"Job revision {job.revision_number} does not match expected {expected_revision}"
            )

        if job.status not in ("failed", "unknown"):
            raise PreArrivalConflictError(
                f"Cannot retry a job with status '{job.status}'. Only 'failed' or reconciled 'unknown' jobs can be retried."
            )

        if job.status == "unknown" and reconciliation_outcome != "verified_non_acceptance":
            raise PreArrivalConflictError(
                "Retrying an unknown job requires explicit verified_non_acceptance reconciliation outcome"
            )

        assert self.reminders_col is not None
        audit_entry = {
            "attempt_id": f"STAFF-RETRY-{uuid.uuid4().hex[:8]}",
            "started_at": clock,
            "ended_at": clock,
            "actor": actor,
            "outcome": "re_enqueued",
            "reason_code": "STAFF_AUTHORIZED_RETRY",
            "error_details": reason,
        }

        query: Dict[str, Any] = {
            "environment": self.environment,
            "property_id": self.property_id,
            "reminder_id": reminder_id,
            "revision_number": expected_revision,
            "status": job.status,
            "attempt_count": {"$lt": self.MAX_ATTEMPTS},
        }
        if self.test_run_id:
            query["test_run_id"] = self.test_run_id
        if attempt_id and job.attempt_id:
            query["attempt_id"] = attempt_id

        res = self.reminders_col.update_one(
            query,
            {
                "$set": {
                    "status": "scheduled",
                    "reason_code": "STAFF_RETRY_SCHEDULED",
                    "next_attempt_at": clock,
                    "submission_started": False,
                    "lease_expires_at": None,
                    "updated_at": clock,
                },
                "$push": {"attempts": audit_entry},
            },
        )
        if res.matched_count != 1:
            raise PreArrivalConflictError(
                "Job status changed or attempt cap reached; reload current state."
            )

        return self.get_job_by_id(reminder_id)
