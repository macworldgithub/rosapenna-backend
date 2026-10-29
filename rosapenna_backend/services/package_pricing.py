"""Server-side package pricing, eligibility, and rules engine."""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal
from typing import Any, Dict, List, Optional

from pymongo.database import Database

from rosapenna_backend.api.schemas.packages import (
    AmendmentPaymentSummary,
    ItemizedGolfRoundQuote,
    ItemizedRoomQuote,
    MinimumRoundsRule,
    PackageQuoteRequest,
    PackageQuoteResponse,
    PackageRulesConfig,
    UpdatePackageRulesRequest,
)
from rosapenna_backend.core.logging import logger
from rosapenna_backend.integrations.brs.tee_sheet import COURSES
from rosapenna_backend.integrations.opera.client import OperaClient, OperaIntegrationError
from rosapenna_backend.services.booking_intents import BookingIntentStore, IntentNotFound
from rosapenna_backend.services.tee_sheets import TeesheetCache


class PackagePricingService:
    """Calculates live, server-verified package quotes and manages rules configuration."""

    def __init__(
        self,
        opera_client: OperaClient,
        teesheet_cache: TeesheetCache,
        booking_store: BookingIntentStore,
        database: Optional[Database] = None,
    ):
        self.opera_client = opera_client
        self.teesheet_cache = teesheet_cache
        self.booking_store = booking_store
        self.db = database
        self.rules_collection = database["package_rules"] if database is not None else None
        self._cached_rules: Optional[PackageRulesConfig] = None

    def get_rules(self) -> PackageRulesConfig:
        """Retrieve active package rules configuration."""
        if self._cached_rules is not None:
            return self._cached_rules

        if self.rules_collection is not None:
            doc = self.rules_collection.find_one({"is_active": True}, sort=[("created_at", -1)])
            if doc:
                doc.pop("_id", None)
                doc.pop("is_active", None)
                self._cached_rules = PackageRulesConfig.model_validate(doc)
                return self._cached_rules

        # Default rules baseline
        self._cached_rules = PackageRulesConfig()
        return self._cached_rules

    def update_rules(self, request: UpdatePackageRulesRequest) -> PackageRulesConfig:
        """Update package rules configuration with an immutable audit record."""
        current = self.get_rules()
        now_str = dt.datetime.now(dt.timezone.utc).isoformat()
        new_version = f"rules-{request.effective_date.strftime('%Y%m%d')}-{uuid.uuid4().hex[:4]}"

        new_dict = current.model_dump()
        new_dict["version"] = new_version
        new_dict["effective_date"] = request.effective_date.isoformat()

        if request.bookable_cap is not None:
            new_dict["bookable_cap"] = request.bookable_cap
        if request.category_buffers is not None:
            new_dict["category_buffers"] = request.category_buffers
        if request.deposit_percent is not None:
            new_dict["deposit_percent"] = request.deposit_percent
            new_dict["balance_percent"] = 100 - request.deposit_percent
        if request.balance_due_weeks is not None:
            new_dict["balance_due_weeks"] = request.balance_due_weeks

        updated_rules = PackageRulesConfig.model_validate(new_dict)

        if self.rules_collection is not None:
            # Archive old active configs
            self.rules_collection.update_many({"is_active": True}, {"$set": {"is_active": False}})
            record = updated_rules.model_dump()
            record["is_active"] = True
            record["created_at"] = now_str
            record["actor"] = request.actor
            record["reason"] = request.reason
            self.rules_collection.insert_one(record)

        self._cached_rules = updated_rules
        logger.info(
            "Package rules updated to %s by %s (effective %s, reason: %s)",
            new_version,
            request.actor,
            request.effective_date,
            request.reason,
        )
        return updated_rules

    def calculate_quote(self, request: PackageQuoteRequest) -> PackageQuoteResponse:
        """Calculate a live, itemized package quote combining Opera room rates and BRS golf rates."""
        rules = self.get_rules()
        quote_id = f"QTE-{dt.datetime.now(dt.timezone.utc).strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:6]}"
        now_str = dt.datetime.now(dt.timezone.utc).isoformat()
        nights = (request.departure_date - request.arrival_date).days
        balance_due_date = request.arrival_date - dt.timedelta(weeks=rules.balance_due_weeks)

        # 1. Eligibility Check: Require accommodation
        if not request.rooms:
            return PackageQuoteResponse(
                quote_id=quote_id,
                status="ineligible",
                rules_version=rules.version,
                arrival_date=request.arrival_date,
                departure_date=request.departure_date,
                nights=nights,
                balance_due_date=balance_due_date,
                created_at=now_str,
                errors=["Stay & Play packages require accommodation and golf. For golf-only, please use standalone tee times."],
            )

        # 2. Eligibility Check: Stay length
        if nights < 2:
            return PackageQuoteResponse(
                quote_id=quote_id,
                status="ineligible",
                rules_version=rules.version,
                arrival_date=request.arrival_date,
                departure_date=request.departure_date,
                nights=nights,
                balance_due_date=balance_due_date,
                created_at=now_str,
                errors=["Stay & Play packages require a minimum stay of 2 nights."],
            )
        if nights > 5:
            return PackageQuoteResponse(
                quote_id=quote_id,
                status="ineligible",
                rules_version=rules.version,
                arrival_date=request.arrival_date,
                departure_date=request.departure_date,
                nights=nights,
                balance_due_date=balance_due_date,
                created_at=now_str,
                errors=["Stays longer than 5 nights require bespoke staff review. Please contact Reservations."],
            )

        # 3. Eligibility Check: Minimum golf rounds
        required_rounds = 0
        for rule in rules.minimum_rounds:
            if rule.minimum_nights <= nights <= rule.maximum_nights:
                required_rounds = rule.rounds
                break

        if len(request.golf_rounds) < required_rounds:
            return PackageQuoteResponse(
                quote_id=quote_id,
                status="ineligible",
                rules_version=rules.version,
                arrival_date=request.arrival_date,
                departure_date=request.departure_date,
                nights=nights,
                balance_due_date=balance_due_date,
                created_at=now_str,
                errors=[f"Stays of {nights} nights require at least {required_rounds} golf rounds (got {len(request.golf_rounds)})."],
            )

        # 4. Live Room Pricing & Category Buffer Check (Opera Cloud)
        total_requested_rooms = sum(r.room_quantity for r in request.rooms)
        if total_requested_rooms > rules.bookable_cap:
            return PackageQuoteResponse(
                quote_id=quote_id,
                status="unavailable",
                rules_version=rules.version,
                arrival_date=request.arrival_date,
                departure_date=request.departure_date,
                nights=nights,
                balance_due_date=balance_due_date,
                created_at=now_str,
                errors=[f"Requested room count ({total_requested_rooms}) exceeds bookable room cap ({rules.bookable_cap})."],
            )

        try:
            total_adults = sum(r.adults for r in request.rooms)
            total_children = sum(r.children for r in request.rooms)
            opera_avail = self.opera_client.get_room_availability(
                arrival_date=request.arrival_date,
                departure_date=request.departure_date,
                adults=max(1, total_adults),
                children=total_children,
            )
        except OperaIntegrationError as exc:
            return PackageQuoteResponse(
                quote_id=quote_id,
                status="unavailable",
                rules_version=rules.version,
                arrival_date=request.arrival_date,
                departure_date=request.departure_date,
                nights=nights,
                balance_due_date=balance_due_date,
                created_at=now_str,
                errors=[f"Live room availability could not be verified: {exc.message}"],
            )

        categories_by_code = {c.room_category.upper(): c for c in opera_avail.categories}
        itemized_rooms: List[ItemizedRoomQuote] = []
        total_rooms_price = Decimal("0.00")

        for req_room in request.rooms:
            code = req_room.room_category.upper()
            cat = categories_by_code.get(code)
            if not cat:
                return PackageQuoteResponse(
                    quote_id=quote_id,
                    status="unavailable",
                    rules_version=rules.version,
                    arrival_date=request.arrival_date,
                    departure_date=request.departure_date,
                    nights=nights,
                    balance_due_date=balance_due_date,
                    created_at=now_str,
                    errors=[f"Room category '{req_room.room_category}' is not available for requested stay."],
                )

            buffer = rules.category_buffers.get(code, 0)
            net_available = max(0, cat.available_rooms - buffer)
            if net_available < req_room.room_quantity:
                return PackageQuoteResponse(
                    quote_id=quote_id,
                    status="unavailable",
                    rules_version=rules.version,
                    arrival_date=request.arrival_date,
                    departure_date=request.departure_date,
                    nights=nights,
                    balance_due_date=balance_due_date,
                    created_at=now_str,
                    errors=[
                        f"Insufficient inventory for '{cat.category_name}' (available: {cat.available_rooms}, buffer: {buffer}, requested: {req_room.room_quantity})."
                    ],
                )

            # Extract published/verified rate from Opera
            if not cat.rates:
                return PackageQuoteResponse(
                    quote_id=quote_id,
                    status="unavailable",
                    rules_version=rules.version,
                    arrival_date=request.arrival_date,
                    departure_date=request.departure_date,
                    nights=nights,
                    balance_due_date=balance_due_date,
                    created_at=now_str,
                    errors=[f"No rate plans published for room category '{cat.category_name}'."],
                )

            rate_plan = cat.rates[0]
            rate_per_night = (Decimal(str(rate_plan.total_amount)) / Decimal(nights)).quantize(Decimal("0.01"))
            subtotal = (Decimal(str(rate_plan.total_amount)) * Decimal(req_room.room_quantity)).quantize(Decimal("0.01"))
            total_rooms_price += subtotal

            itemized_rooms.append(
                ItemizedRoomQuote(
                    room_category=cat.room_category,
                    room_quantity=req_room.room_quantity,
                    nights=nights,
                    rate_per_night=format(rate_per_night, ".2f"),
                    subtotal=format(subtotal, ".2f"),
                    rate_plan_code=rate_plan.rate_plan_code,
                    breakfast_included=True,
                    available_rooms=cat.available_rooms,
                )
            )

        # 5. Live Golf Pricing Check (BRS Golf)
        itemized_golf: List[ItemizedGolfRoundQuote] = []
        total_golf_price = Decimal("0.00")

        for round_req in request.golf_rounds:
            if not (request.arrival_date <= round_req.date <= request.departure_date):
                return PackageQuoteResponse(
                    quote_id=quote_id,
                    status="ineligible",
                    rules_version=rules.version,
                    arrival_date=request.arrival_date,
                    departure_date=request.departure_date,
                    nights=nights,
                    balance_due_date=balance_due_date,
                    created_at=now_str,
                    errors=[
                        f"Golf round date {round_req.date} falls outside stay window ({request.arrival_date} to {request.departure_date})."
                    ],
                )

            course_cfg = COURSES.get(round_req.course_id)
            if not course_cfg:
                return PackageQuoteResponse(
                    quote_id=quote_id,
                    status="ineligible",
                    rules_version=rules.version,
                    arrival_date=request.arrival_date,
                    departure_date=request.departure_date,
                    nights=nights,
                    balance_due_date=balance_due_date,
                    created_at=now_str,
                    errors=[f"Unsupported course_id {round_req.course_id}."],
                )

            try:
                brs_avail = self.teesheet_cache.get_course_availability(
                    course_id=round_req.course_id,
                    date_str=round_req.date.isoformat(),
                    players=round_req.players,
                    time_from=round_req.time,
                    time_to=round_req.time,
                )
            except Exception as exc:
                return PackageQuoteResponse(
                    quote_id=quote_id,
                    status="unavailable",
                    rules_version=rules.version,
                    arrival_date=request.arrival_date,
                    departure_date=request.departure_date,
                    nights=nights,
                    balance_due_date=balance_due_date,
                    created_at=now_str,
                    errors=[f"Could not verify tee time on {course_cfg['name']}: {str(exc)}"],
                )

            matching_slots = [
                s for s in brs_avail.slots
                if s.time == round_req.time and s.available_players >= round_req.players
            ]
            if not matching_slots:
                return PackageQuoteResponse(
                    quote_id=quote_id,
                    status="unavailable",
                    rules_version=rules.version,
                    arrival_date=request.arrival_date,
                    departure_date=request.departure_date,
                    nights=nights,
                    balance_due_date=balance_due_date,
                    created_at=now_str,
                    errors=[
                        f"Tee time at {round_req.time} on {round_req.date} on {course_cfg['name']} does not have capacity for {round_req.players} players."
                    ],
                )

            slot = matching_slots[0]
            # Extract player green fee from slot rates or fallback to baseline
            rate_per_player = Decimal("180.00")
            if slot.rates:
                rate_per_player = Decimal(slot.rates[0].price_per_player)
            subtotal = (rate_per_player * Decimal(round_req.players)).quantize(Decimal("0.01"))
            total_golf_price += subtotal

            itemized_golf.append(
                ItemizedGolfRoundQuote(
                    course_id=round_req.course_id,
                    course_name=course_cfg["name"],
                    date=round_req.date,
                    time=round_req.time,
                    players=round_req.players,
                    rate_per_player=format(rate_per_player, ".2f"),
                    subtotal=format(subtotal, ".2f"),
                    available_slots=slot.available_players,
                )
            )

        # 6. Sum Package Totals
        total_package = total_rooms_price + total_golf_price
        deposit_amount = (total_package * Decimal(rules.deposit_percent) / Decimal(100)).quantize(Decimal("0.01"))
        balance_amount = (total_package - deposit_amount).quantize(Decimal("0.01"))

        # 7. For Amendments: Compute delta and balance from F05 verified ledger
        amendment_summary = None
        if request.existing_booking_intent_id:
            try:
                intent = self.booking_store.get(request.existing_booking_intent_id)
                current_total = Decimal("0.00")
                if "quote" in intent and isinstance(intent["quote"], dict) and "selected" in intent["quote"]:
                    selected_q = intent["quote"]["selected"]
                    amt = selected_q.get("amount") or selected_q.get("price") or "0.00"
                    current_total = Decimal(str(amt))
                elif "selection" in intent and "price" in intent["selection"]:
                    current_total = Decimal(str(intent["selection"]["price"]))

                if current_total == Decimal("0.00"):
                    opera_amt = (
                        Decimal(str(intent.get("links", {}).get("opera_reservation", {}).get("total_amount", "0.00")))
                        if intent.get("links", {}).get("opera_reservation")
                        else Decimal("0.00")
                    )
                    rounds_amt = sum(
                        Decimal(str(r.get("price", "0.00")))
                        for r in intent.get("links", {}).get("brs_rounds", [])
                    )
                    if opera_amt + rounds_amt > Decimal("0.00"):
                        current_total = opera_amt + rounds_amt
                completed_payments = [
                    Decimal(p["amount"])
                    for p in intent.get("links", {}).get("payments", [])
                    if p.get("status") == "completed"
                ]
                verified_paid = sum(completed_payments, Decimal("0.00"))
                difference = total_package - current_total
                balance_due = max(Decimal("0.00"), total_package - verified_paid)
                credit_amount = max(Decimal("0.00"), verified_paid - total_package)

                amendment_summary = AmendmentPaymentSummary(
                    current_total=format(current_total, ".2f"),
                    revised_total=format(total_package, ".2f"),
                    verified_paid=format(verified_paid, ".2f"),
                    amount_difference=format(difference, ".2f"),
                    balance_due=format(balance_due, ".2f"),
                    credit_amount=format(credit_amount, ".2f"),
                )
            except IntentNotFound:
                return PackageQuoteResponse(
                    quote_id=quote_id,
                    status="ineligible",
                    rules_version=rules.version,
                    arrival_date=request.arrival_date,
                    departure_date=request.departure_date,
                    nights=nights,
                    balance_due_date=balance_due_date,
                    created_at=now_str,
                    errors=[f"Existing booking intent {request.existing_booking_intent_id} not found."],
                )

        return PackageQuoteResponse(
            quote_id=quote_id,
            status="available",
            rules_version=rules.version,
            arrival_date=request.arrival_date,
            departure_date=request.departure_date,
            nights=nights,
            currency="EUR",
            itemized_rooms=itemized_rooms,
            itemized_golf=itemized_golf,
            total_rooms_price=format(total_rooms_price, ".2f"),
            total_golf_price=format(total_golf_price, ".2f"),
            total_package_price=format(total_package, ".2f"),
            deposit_amount=format(deposit_amount, ".2f"),
            balance_amount=format(balance_amount, ".2f"),
            balance_due_date=balance_due_date,
            amendment_summary=amendment_summary,
            reserves_inventory=False,
            freshness_ttl_seconds=120,
            created_at=now_str,
            errors=[],
        )
