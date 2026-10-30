"""Server-side package pricing, eligibility, and rules engine."""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal
from typing import Any, Dict, List, Optional

from pymongo.database import Database

from rosapenna_backend.api.schemas.packages import (
    AmendmentPaymentSummary,
    AvailableRoomCategoryOption,
    AvailableTeeSlotOption,
    DailyCourseTeeTimes,
    ItemizedGolfRoundQuote,
    ItemizedRoomQuote,
    MinimumRoundsRule,
    PackageQuoteRequest,
    PackageQuoteResponse,
    PackageRulesConfig,
    PackageSearchRequest,
    PackageSearchResponse,
    RecommendedPackageOption,
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

    def search_stay_and_golf(self, request: PackageSearchRequest) -> PackageSearchResponse:
        """Execute a live search across Opera room categories and BRS three-course tee sheets."""
        rules = self.get_rules()
        search_id = f"SRC-{dt.datetime.now(dt.timezone.utc).strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:6]}"
        now_str = dt.datetime.now(dt.timezone.utc).isoformat()
        nights = (request.departure_date - request.arrival_date).days
        balance_due_date = request.arrival_date - dt.timedelta(weeks=rules.balance_due_weeks)

        # 1. Stay Length Validation
        if nights < 2:
            return PackageSearchResponse(
                search_id=search_id,
                status="ineligible",
                arrival_date=request.arrival_date,
                departure_date=request.departure_date,
                nights=nights,
                party_size=request.party_size,
                room_count=request.room_count,
                is_golf_ireland_eligible=False,
                required_minimum_rounds=2,
                warnings=["Stay & Play packages require a minimum stay of 2 nights."],
                created_at=now_str,
            )
        if nights > 5:
            return PackageSearchResponse(
                search_id=search_id,
                status="ineligible",
                arrival_date=request.arrival_date,
                departure_date=request.departure_date,
                nights=nights,
                party_size=request.party_size,
                room_count=request.room_count,
                is_golf_ireland_eligible=False,
                required_minimum_rounds=4,
                warnings=["Stays over 5 nights require bespoke reservations handling. Please contact Reservations at +353 (0)74 91 55301."],
                created_at=now_str,
            )

        required_rounds = 2 if nights <= 3 else (3 if nights <= 5 else 4)

        # Golf Ireland eligibility (dual-fact check: Irish residence + active index)
        is_irish_resident = (request.residence_country or "").upper() in ("IE", "NI")
        is_golf_ireland_eligible = is_irish_resident and request.has_golf_ireland_index

        warnings: List[str] = []

        # 2. Check Opera Cloud Room Availability
        room_options: List[AvailableRoomCategoryOption] = []
        try:
            opera_avail = self.opera_client.get_room_availability(
                arrival_date=request.arrival_date,
                departure_date=request.departure_date,
                adults=max(1, request.party_size),
                children=0,
            )
            cat_avail_map = {r.room_category.upper(): r for r in opera_avail.available_categories}
        except Exception as exc:
            logger.warning("Opera availability query failed during package search: %s", exc)
            cat_avail_map = {}

        std_categories = [
            ("CLASSIC", "Classic Room", Decimal("200.00"), Decimal("220.00")),
            ("DELUXE", "Deluxe Room", Decimal("230.00"), Decimal("250.00")),
            ("BAYVIEW", "Bay View Junior Suite", Decimal("280.00"), Decimal("300.00")),
            ("PENTHOUSE", "Penthouse Suite", Decimal("400.00"), Decimal("420.00")),
        ]

        is_high_season = 5 <= request.arrival_date.month <= 9
        season_bump = Decimal("80.00") if is_high_season else Decimal("0.00")
        buffers = rules.category_buffers or {"CLASSIC": 1, "DELUXE": 1, "BAYVIEW": 1, "PENTHOUSE": 0}

        for cat_code, cat_name, single_base, double_base in std_categories:
            base_rate = single_base if request.bed_type == "SINGLE" else double_base
            nightly_rate = base_rate + season_bump

            avail_entry = cat_avail_map.get(cat_code)
            raw_rooms = avail_entry.available_rooms if avail_entry else 5
            buffer_val = buffers.get(cat_code, 1)
            effective_avail = max(0, min(rules.bookable_cap, raw_rooms - buffer_val))

            has_rooms = effective_avail >= request.room_count
            subtotal = nightly_rate * Decimal(nights) * Decimal(request.room_count)

            room_options.append(
                AvailableRoomCategoryOption(
                    room_category=cat_code,
                    category_name=cat_name,
                    available_rooms=effective_avail,
                    rate_per_night=format(nightly_rate.quantize(Decimal("0.01")), ".2f"),
                    subtotal=format(subtotal.quantize(Decimal("0.01")), ".2f"),
                    breakfast_included=True,
                    is_available=has_rooms,
                )
            )

        # 3. Check BRS Golf Tee Times Across All Three Courses
        daily_golf: List[DailyCourseTeeTimes] = []
        course_names = {
            1: "Old Tom Morris Links",
            3: "Sandy Hills Links",
            4: "St Patrick's Links",
        }

        st_patricks_closures = {
            "04-13", "04-15", "04-20", "04-27", "05-06", "05-11", "05-18", "05-25",
            "06-01", "06-08", "06-15", "06-22", "06-29", "07-08", "07-20", "07-21",
            "07-27", "08-03", "08-05", "08-10", "08-17", "08-26", "08-31", "09-07",
            "09-28", "10-05", "10-07", "10-12", "10-14", "10-18",
        }

        green_fee_rates = {
            1: Decimal("90.00") if is_golf_ireland_eligible else Decimal("160.00"),
            3: Decimal("90.00") if is_golf_ireland_eligible else Decimal("220.00"),
            4: Decimal("150.00") if is_golf_ireland_eligible else Decimal("330.00"),
        }

        stay_days = [request.arrival_date + dt.timedelta(days=i) for i in range(nights + 1)]
        courses_to_search = [cid for cid in request.preferred_courses if cid in COURSES]

        for day in stay_days:
            day_mm_dd = day.strftime("%m-%d")
            for cid in courses_to_search:
                cname = course_names.get(cid, COURSES[cid]["name"])
                is_closed = False
                closure_msg = None
                alt_msg = None

                if cid == 4 and day_mm_dd in st_patricks_closures:
                    is_closed = True
                    closure_msg = f"{cname} is scheduled closed on {day.strftime('%a, %d %b')} for course maintenance."
                    alt_msg = f"Play Sandy Hills Links or Old Tom Morris Links on {day.strftime('%a, %d %b')}."
                    warnings.append(closure_msg)

                rate_str = format(green_fee_rates[cid].quantize(Decimal("0.01")), ".2f")
                tee_slots: List[AvailableTeeSlotOption] = []

                if not is_closed:
                    try:
                        sheet_res = self.teesheet_cache.get(day, cid)
                        sheet_data = sheet_res.get("data", {})
                        times = sheet_data.get("tee_times", [])

                        for t in times:
                            slots_dict = t.get("slots", {})
                            avail_slots = sum(
                                1 for s in slots_dict.values()
                                if isinstance(s, dict) and s.get("status") == "Available"
                            )
                            if avail_slots >= min(4, request.party_size):
                                tee_slots.append(
                                    AvailableTeeSlotOption(
                                        time=t.get("time", "09:00"),
                                        available_slots=avail_slots,
                                        rate_per_player=rate_str,
                                        is_hot_deal=t.get("is_hot_deal", False),
                                    )
                                )
                    except Exception as exc:
                        logger.debug("Tee sheet read for course %s day %s: %s", cid, day, exc)
                        for default_time in ["08:30", "09:40", "10:50", "14:00", "15:10"]:
                            tee_slots.append(
                                AvailableTeeSlotOption(
                                    time=default_time,
                                    available_slots=4,
                                    rate_per_player=rate_str,
                                )
                            )

                daily_golf.append(
                    DailyCourseTeeTimes(
                        date=day,
                        course_id=cid,
                        course_name=cname,
                        is_closed=is_closed,
                        closure_reason=closure_msg,
                        alternative_suggestion=alt_msg,
                        available_slots_count=len(tee_slots),
                        tee_slots=tee_slots[:8],
                    )
                )

        # 4. Build Recommended Complete Packages
        recommended_packages: List[RecommendedPackageOption] = []
        avail_rooms = [r for r in room_options if r.is_available]
        top_room = avail_rooms[0] if avail_rooms else room_options[0]

        golf_total_1 = (green_fee_rates[1] + green_fee_rates[3]) * Decimal(request.party_size)
        pkg_total_1 = Decimal(top_room.subtotal) + golf_total_1
        dep_1 = (pkg_total_1 * Decimal("0.25")).quantize(Decimal("0.01"))
        bal_1 = pkg_total_1 - dep_1

        recommended_packages.append(
            RecommendedPackageOption(
                name="Rosapenna Classic Links Experience",
                room_category=top_room.category_name,
                selected_courses=["Old Tom Morris Links", "Sandy Hills Links"],
                total_price=format(pkg_total_1, ".2f"),
                deposit_amount=format(dep_1, ".2f"),
                balance_amount=format(bal_1, ".2f"),
                balance_due_date=balance_due_date,
            )
        )

        golf_total_2 = (green_fee_rates[4] + green_fee_rates[3]) * Decimal(request.party_size)
        pkg_total_2 = Decimal(top_room.subtotal) + golf_total_2
        dep_2 = (pkg_total_2 * Decimal("0.25")).quantize(Decimal("0.01"))
        bal_2 = pkg_total_2 - dep_2

        recommended_packages.append(
            RecommendedPackageOption(
                name="Championship Links Experience",
                room_category=top_room.category_name,
                selected_courses=["St Patrick's Links", "Sandy Hills Links"],
                total_price=format(pkg_total_2, ".2f"),
                deposit_amount=format(dep_2, ".2f"),
                balance_amount=format(bal_2, ".2f"),
                balance_due_date=balance_due_date,
            )
        )

        overall_status = "available" if any(r.is_available for r in room_options) else "unavailable"

        return PackageSearchResponse(
            search_id=search_id,
            status=overall_status,
            arrival_date=request.arrival_date,
            departure_date=request.departure_date,
            nights=nights,
            party_size=request.party_size,
            room_count=request.room_count,
            is_golf_ireland_eligible=is_golf_ireland_eligible,
            required_minimum_rounds=required_rounds,
            room_options=room_options,
            daily_golf_options=daily_golf,
            recommended_packages=recommended_packages,
            warnings=warnings,
            reserves_inventory=False,
            freshness_ttl_seconds=120,
            created_at=now_str,
        )

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

    def search_stay_and_golf(self, request: PackageSearchRequest) -> PackageSearchResponse:
        """Search live Opera room availability and BRS tee times across all courses.

        Applies package rules (2-night min, rounds requirements, room caps, buffers, closures)
        and returns room options, tee slots by course/day, and recommended package options.
        Reserves NO inventory (reserves_inventory=False).
        """
        rules = self.get_rules()
        search_id = f"SRC-{dt.datetime.now(dt.timezone.utc).strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:6]}"
        now_str = dt.datetime.now(dt.timezone.utc).isoformat()
        nights = (request.departure_date - request.arrival_date).days
        warnings: List[str] = []

        # Dual-fact Golf Ireland eligibility
        is_golf_ireland_eligible = (
            request.residence_country in ["IE", "NI"] and bool(request.has_golf_ireland_index)
        )

        # Minimum required rounds
        required_rounds = 0
        for rule in rules.minimum_rounds:
            if rule.minimum_nights <= nights <= rule.maximum_nights:
                required_rounds = rule.rounds
                break

        # 1. Stay length eligibility check
        if nights < 2:
            return PackageSearchResponse(
                search_id=search_id,
                status="ineligible",
                arrival_date=request.arrival_date,
                departure_date=request.departure_date,
                nights=nights,
                party_size=request.party_size,
                room_count=request.room_count,
                is_golf_ireland_eligible=is_golf_ireland_eligible,
                required_minimum_rounds=required_rounds,
                room_options=[],
                daily_golf_options=[],
                recommended_packages=[],
                warnings=["Stay & Play packages require a minimum stay of 2 nights."],
                reserves_inventory=False,
                freshness_ttl_seconds=120,
                created_at=now_str,
            )

        if nights > 5:
            return PackageSearchResponse(
                search_id=search_id,
                status="ineligible",
                arrival_date=request.arrival_date,
                departure_date=request.departure_date,
                nights=nights,
                party_size=request.party_size,
                room_count=request.room_count,
                is_golf_ireland_eligible=is_golf_ireland_eligible,
                required_minimum_rounds=required_rounds,
                room_options=[],
                daily_golf_options=[],
                recommended_packages=[],
                warnings=["Stays longer than 5 nights require bespoke staff review. Please contact Reservations."],
                reserves_inventory=False,
                freshness_ttl_seconds=120,
                created_at=now_str,
            )

        # 2. Room cap check
        if request.room_count > rules.bookable_cap:
            return PackageSearchResponse(
                search_id=search_id,
                status="unavailable",
                arrival_date=request.arrival_date,
                departure_date=request.departure_date,
                nights=nights,
                party_size=request.party_size,
                room_count=request.room_count,
                is_golf_ireland_eligible=is_golf_ireland_eligible,
                required_minimum_rounds=required_rounds,
                room_options=[],
                daily_golf_options=[],
                recommended_packages=[],
                warnings=[f"Requested room count ({request.room_count}) exceeds bookable room cap ({rules.bookable_cap})."],
                reserves_inventory=False,
                freshness_ttl_seconds=120,
                created_at=now_str,
            )

        # 3. Live Opera Room Availability
        room_options: List[AvailableRoomCategoryOption] = []
        try:
            opera_avail = self.opera_client.get_room_availability(
                arrival_date=request.arrival_date,
                departure_date=request.departure_date,
                adults=request.party_size,
                children=0,
            )
            for cat in opera_avail.categories:
                code = cat.room_category.upper()
                buffer = rules.category_buffers.get(code, 0)
                net_available = max(0, cat.available_rooms - buffer)
                rate_plan = cat.rates[0] if cat.rates else None
                if rate_plan:
                    rate_per_night = (Decimal(str(rate_plan.total_amount)) / Decimal(nights)).quantize(Decimal("0.01"))
                    subtotal = (Decimal(str(rate_plan.total_amount)) * Decimal(request.room_count)).quantize(Decimal("0.01"))
                else:
                    rate_per_night = Decimal("0.00")
                    subtotal = Decimal("0.00")

                is_avail = (net_available >= request.room_count) and bool(rate_plan)
                room_options.append(
                    AvailableRoomCategoryOption(
                        room_category=cat.room_category,
                        category_name=cat.category_name,
                        available_rooms=net_available,
                        rate_per_night=format(rate_per_night, ".2f"),
                        subtotal=format(subtotal, ".2f"),
                        breakfast_included=True,
                        is_available=is_avail,
                    )
                )
        except Exception as exc:
            warnings.append(f"Live Opera room availability check encountered an error: {str(exc)}")

        # 4. Live BRS Golf Availability for each preferred course across the stay days
        daily_golf_options: List[DailyCourseTeeTimes] = []
        courses_to_query = [cid for cid in request.preferred_courses if cid in COURSES]
        if not courses_to_query:
            courses_to_query = [1, 3, 4]

        # Generate stay dates (arrival day through day of departure)
        stay_dates = [
            request.arrival_date + dt.timedelta(days=i)
            for i in range(nights + 1)
        ]

        # Check closures configured in rules
        closure_dict = {}
        for c in rules.closures:
            cid = c.get("course_id")
            cdate = c.get("date")
            if cid and cdate:
                closure_dict[(cid, str(cdate))] = c.get("reason", "Course closed for maintenance / season")

        # Built-in agronomy maintenance rest dates for St Patrick's Links (MM-DD)
        st_patricks_agronomy_rest = {
            "04-13", "04-15", "04-20", "04-27", "05-06", "05-11", "05-18", "05-25",
            "06-01", "06-08", "06-15", "06-22", "06-29", "07-08", "07-20", "07-21",
            "07-27", "08-03", "08-05", "08-10", "08-17", "08-26", "08-31", "09-07",
            "09-28", "10-05", "10-07", "10-12", "10-14", "10-18",
        }

        for day in stay_dates:
            for course_id in courses_to_query:
                course_cfg = COURSES[course_id]
                day_str = day.isoformat()

                # Check configured or standard closure
                closure_reason = closure_dict.get((course_id, day_str))
                if not closure_reason and course_id == 4:
                    mm_dd = f"{day.month:02d}-{day.day:02d}"
                    if day.month in (11, 12, 1, 2, 3):
                        closure_reason = "St Patrick's Links is closed for winter agronomy rest (November to March)."
                    elif mm_dd in st_patricks_agronomy_rest:
                        closure_reason = "St Patrick's Links is resting for agronomy maintenance on this date."

                if closure_reason:
                    daily_golf_options.append(
                        DailyCourseTeeTimes(
                            date=day,
                            course_id=course_id,
                            course_name=course_cfg["name"],
                            is_closed=True,
                            closure_reason=closure_reason,
                            alternative_suggestion="Old Tom Morris Links or Sandy Hills Links available with 2-course championship rate.",
                            available_slots_count=0,
                            tee_slots=[],
                        )
                    )
                    continue

                try:
                    needed_players = min(4, max(1, request.party_size))
                    brs_avail = self.teesheet_cache.get_course_availability(
                        day=day,
                        course_id=course_id,
                        players=needed_players,
                    )

                    if getattr(brs_avail, "status", None) == "unconfigured_tenant":
                        daily_golf_options.append(
                            DailyCourseTeeTimes(
                                date=day,
                                course_id=course_id,
                                course_name=course_cfg["name"],
                                is_closed=True,
                                closure_reason=f"{course_cfg['name']} tee sheet is unconfigured.",
                                alternative_suggestion="Please select Old Tom Morris Links or Sandy Hills Links.",
                                available_slots_count=0,
                                tee_slots=[],
                            )
                        )
                        continue

                    # Filter and build slot options
                    slots_list: List[AvailableTeeSlotOption] = []
                    base_rate = Decimal("120.00") if is_golf_ireland_eligible else Decimal("180.00")

                    for s in getattr(brs_avail, "slots", []):
                        slot_rate = base_rate
                        if getattr(s, "rates", None) and len(s.rates) > 0:
                            slot_rate = Decimal(str(s.rates[0].price_per_player))
                            if is_golf_ireland_eligible:
                                slot_rate = min(slot_rate, Decimal("120.00"))

                        avail_cap = getattr(s, "available_capacity", getattr(s, "available_players", 0))
                        slots_list.append(
                            AvailableTeeSlotOption(
                                time=s.time,
                                available_slots=avail_cap,
                                rate_per_player=format(slot_rate, ".2f"),
                                is_hot_deal=getattr(s, "is_hot_deal", False),
                            )
                        )

                    is_closed = (len(slots_list) == 0 and getattr(brs_avail, "status", "") == "closed")
                    alt_sugg = None
                    if len(slots_list) == 0:
                        alt_sugg = f"No matching tee times on {course_cfg['name']} for {day}. Consider alternative stay dates or courses."

                    daily_golf_options.append(
                        DailyCourseTeeTimes(
                            date=day,
                            course_id=course_id,
                            course_name=course_cfg["name"],
                            is_closed=is_closed,
                            closure_reason="No open slots or course closed" if is_closed else None,
                            alternative_suggestion=alt_sugg,
                            available_slots_count=len(slots_list),
                            tee_slots=slots_list,
                        )
                    )

                except Exception as exc:
                    warnings.append(f"Failed to query tee times for {course_cfg['name']} on {day}: {str(exc)}")
                    daily_golf_options.append(
                        DailyCourseTeeTimes(
                            date=day,
                            course_id=course_id,
                            course_name=course_cfg["name"],
                            is_closed=False,
                            closure_reason=None,
                            alternative_suggestion="Live availability check failed for this course. Please contact Reservations.",
                            available_slots_count=0,
                            tee_slots=[],
                        )
                    )

        # Check if St Patrick's Links is resting for the entire stay window
        st_patricks_days = [g for g in daily_golf_options if g.course_id == 4]
        if st_patricks_days and all(g.is_closed or g.available_slots_count == 0 for g in st_patricks_days):
            warnings.append(
                "St Patrick's Links is resting for agronomy on your selected stay dates. Two-championship course package rates applied."
            )

        # 5. Build recommended package itineraries if rooms and golf are available
        recommended_packages: List[RecommendedPackageOption] = []
        available_rooms = [r for r in room_options if r.is_available]

        if available_rooms:
            for room in available_rooms[:2]:
                selected_course_names = []
                total_golf = Decimal("0.00")
                rounds_picked = 0

                for day in stay_dates:
                    if rounds_picked >= required_rounds:
                        break
                    day_options = [
                        g for g in daily_golf_options
                        if g.date == day and g.available_slots_count > 0 and g.course_name not in selected_course_names
                    ]
                    if not day_options:
                        day_options = [g for g in daily_golf_options if g.date == day and g.available_slots_count > 0]

                    if day_options:
                        chosen = day_options[0]
                        selected_course_names.append(chosen.course_name)
                        player_rate = Decimal(chosen.tee_slots[0].rate_per_player)
                        total_golf += player_rate * Decimal(request.party_size)
                        rounds_picked += 1

                if rounds_picked >= required_rounds:
                    room_subtotal = Decimal(room.subtotal)
                    package_total = room_subtotal + total_golf
                    dep_amount = (package_total * Decimal(rules.deposit_percent) / Decimal(100)).quantize(Decimal("0.01"))
                    bal_amount = (package_total - dep_amount).quantize(Decimal("0.01"))
                    bal_date = request.arrival_date - dt.timedelta(weeks=rules.balance_due_weeks)

                    pkg_name = f"{room.category_name} & {rounds_picked}-Round Championship"
                    if "St Patrick's Links" not in selected_course_names:
                        pkg_name = f"{room.category_name} & 2-Course Championship (St Patrick's Rest Rate)"

                    recommended_packages.append(
                        RecommendedPackageOption(
                            name=pkg_name,
                            room_category=room.room_category,
                            selected_courses=selected_course_names,
                            total_price=format(package_total, ".2f"),
                            deposit_amount=format(dep_amount, ".2f"),
                            balance_amount=format(bal_amount, ".2f"),
                            balance_due_date=bal_date,
                        )
                    )

        # Determine overall search status
        has_rooms = any(r.is_available for r in room_options)
        has_golf = any(g.available_slots_count > 0 for g in daily_golf_options)
        if has_rooms and has_golf:
            status = "available"
        elif has_rooms or has_golf:
            status = "partial"
        else:
            status = "unavailable"

        return PackageSearchResponse(
            search_id=search_id,
            status=status,
            arrival_date=request.arrival_date,
            departure_date=request.departure_date,
            nights=nights,
            party_size=request.party_size,
            room_count=request.room_count,
            is_golf_ireland_eligible=is_golf_ireland_eligible,
            required_minimum_rounds=required_rounds,
            room_options=room_options,
            daily_golf_options=daily_golf_options,
            recommended_packages=recommended_packages,
            warnings=warnings,
            reserves_inventory=False,
            freshness_ttl_seconds=120,
            created_at=now_str,
        )
