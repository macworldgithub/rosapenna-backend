"""Unit tests for Ticket F13: Stay & Play package rules, live pricing, and eligibility."""

import datetime as dt
import unittest
import uuid
from decimal import Decimal
from unittest.mock import MagicMock, patch

from mongomock import MongoClient

from rosapenna_backend.api.routes.packages import (
    calculate_package_quote,
    get_package_rules,
    update_package_rules,
)
from rosapenna_backend.api.schemas.booking_intents import (
    BookingIntentCreate,
    LeadGuest,
    PaymentRecordPayload,
    QuoteSelection,
)
from rosapenna_backend.api.schemas.packages import (
    GolfRoundQuoteRequest,
    PackageQuoteRequest,
    PackageRulesConfig,
    RoomSelectionQuoteRequest,
    UpdatePackageRulesRequest,
)
from rosapenna_backend.integrations.opera.client import OperaClient, OperaIntegrationError
from rosapenna_backend.integrations.opera.schemas import (
    RoomAvailabilityResponse,
    RoomCategoryAvailability,
    RoomRatePlan,
)
from rosapenna_backend.services.booking_intents import BookingIntentStore
from rosapenna_backend.services.package_pricing import PackagePricingService
from rosapenna_backend.services.tee_sheets import TeesheetCache


def make_mock_opera_avail() -> RoomAvailabilityResponse:
    return RoomAvailabilityResponse(
        hotel_id="ROSA",
        arrival_date=dt.date(2027, 6, 10),
        departure_date=dt.date(2027, 6, 12),
        adults=2,
        children=0,
        source="opera_cloud",
        categories=[
            RoomCategoryAvailability(
                room_category="CLASSIC",
                category_name="Classic Room",
                available_rooms=5,
                rates=[
                    RoomRatePlan(
                        rate_plan_code="ROSA_BB_2027",
                        rate_plan_name="Bed & Breakfast",
                        total_amount=600.0,
                        currency="EUR",
                        breakfast_included=True,
                    )
                ],
            ),
            RoomCategoryAvailability(
                room_category="DELUXE",
                category_name="Deluxe Room",
                available_rooms=3,
                rates=[
                    RoomRatePlan(
                        rate_plan_code="ROSA_BB_2027",
                        rate_plan_name="Bed & Breakfast",
                        total_amount=660.0,
                        currency="EUR",
                        breakfast_included=True,
                    )
                ],
            ),
        ],
    )


def make_mock_brs_avail(course_id: int, time: str, players: int) -> MagicMock:
    slot = MagicMock()
    slot.time = time
    slot.available_players = 4
    rate = MagicMock()
    rate.price_per_player = "180.00"
    rate.green_fee_id = 1
    slot.rates = [rate]

    avail = MagicMock()
    avail.slots = [slot]
    avail.slots_count = 1
    return avail


class PackagePricingServiceTests(unittest.TestCase):
    """Test suite for PackagePricingService core calculations and rule validations."""

    def setUp(self):
        self.mongo = MongoClient(tz_aware=True)
        self.db = self.mongo["packages_test_db"]
        self.store = BookingIntentStore(self.db.booking_intents)

        self.mock_opera = MagicMock(spec=OperaClient)
        self.mock_opera.get_room_availability.return_value = make_mock_opera_avail()

        self.mock_cache = MagicMock(spec=TeesheetCache)
        self.mock_cache.get_course_availability.return_value = make_mock_brs_avail(1, "09:30", 2)

        self.service = PackagePricingService(
            opera_client=self.mock_opera,
            teesheet_cache=self.mock_cache,
            booking_store=self.store,
            database=self.db,
        )

    def tearDown(self):
        self.mongo.close()

    def test_eligibility_requires_accommodation(self):
        """Stay & Play requires accommodation; golf-only returns ineligible."""
        req = PackageQuoteRequest(
            arrival_date=dt.date(2027, 6, 10),
            departure_date=dt.date(2027, 6, 12),
            rooms=[],  # Missing rooms
            golf_rounds=[
                GolfRoundQuoteRequest(course_id=1, date=dt.date(2027, 6, 10), time="09:30", players=2),
                GolfRoundQuoteRequest(course_id=3, date=dt.date(2027, 6, 11), time="10:00", players=2),
            ],
        )
        res = self.service.calculate_quote(req)
        self.assertEqual(res.status, "ineligible")
        self.assertTrue(any("require accommodation" in e for e in res.errors))

    def test_eligibility_stay_length_boundaries(self):
        """Stays under 2 nights or over 5 nights are rejected."""
        # 1 night
        req_1night = PackageQuoteRequest(
            arrival_date=dt.date(2027, 6, 10),
            departure_date=dt.date(2027, 6, 11),
            rooms=[RoomSelectionQuoteRequest(room_category="CLASSIC", room_quantity=1)],
            golf_rounds=[GolfRoundQuoteRequest(course_id=1, date=dt.date(2027, 6, 10), time="09:30", players=2)],
        )
        res_1 = self.service.calculate_quote(req_1night)
        self.assertEqual(res_1.status, "ineligible")
        self.assertTrue(any("minimum stay of 2 nights" in e for e in res_1.errors))

        # 6 nights
        req_6night = PackageQuoteRequest(
            arrival_date=dt.date(2027, 6, 10),
            departure_date=dt.date(2027, 6, 16),
            rooms=[RoomSelectionQuoteRequest(room_category="CLASSIC", room_quantity=1)],
            golf_rounds=[
                GolfRoundQuoteRequest(course_id=1, date=dt.date(2027, 6, 10), time="09:30", players=2),
                GolfRoundQuoteRequest(course_id=3, date=dt.date(2027, 6, 11), time="09:30", players=2),
                GolfRoundQuoteRequest(course_id=4, date=dt.date(2027, 6, 12), time="09:30", players=2),
            ],
        )
        res_6 = self.service.calculate_quote(req_6night)
        self.assertEqual(res_6.status, "ineligible")
        self.assertTrue(any("longer than 5 nights" in e for e in res_6.errors))

    def test_eligibility_minimum_golf_rounds_rule(self):
        """2-3 nights require 2 rounds; 4-5 nights require 3 rounds."""
        # 2 nights with 1 round -> ineligible
        req = PackageQuoteRequest(
            arrival_date=dt.date(2027, 6, 10),
            departure_date=dt.date(2027, 6, 12),
            rooms=[RoomSelectionQuoteRequest(room_category="CLASSIC", room_quantity=1)],
            golf_rounds=[
                GolfRoundQuoteRequest(course_id=1, date=dt.date(2027, 6, 10), time="09:30", players=2),
            ],
        )
        res = self.service.calculate_quote(req)
        self.assertEqual(res.status, "ineligible")
        self.assertTrue(any("require at least 2 golf rounds" in e for e in res.errors))

        # 4 nights with 2 rounds -> ineligible
        req_4n = PackageQuoteRequest(
            arrival_date=dt.date(2027, 6, 10),
            departure_date=dt.date(2027, 6, 14),
            rooms=[RoomSelectionQuoteRequest(room_category="CLASSIC", room_quantity=1)],
            golf_rounds=[
                GolfRoundQuoteRequest(course_id=1, date=dt.date(2027, 6, 10), time="09:30", players=2),
                GolfRoundQuoteRequest(course_id=3, date=dt.date(2027, 6, 11), time="09:30", players=2),
            ],
        )
        res_4 = self.service.calculate_quote(req_4n)
        self.assertEqual(res_4.status, "ineligible")
        self.assertTrue(any("require at least 3 golf rounds" in e for e in res_4.errors))

    def test_live_package_quote_calculation_success(self):
        """Valid quote combines room and golf rates with 25% deposit and balance due date."""
        req = PackageQuoteRequest(
            arrival_date=dt.date(2027, 6, 10),
            departure_date=dt.date(2027, 6, 12),
            rooms=[RoomSelectionQuoteRequest(room_category="CLASSIC", room_quantity=1)],
            golf_rounds=[
                GolfRoundQuoteRequest(course_id=1, date=dt.date(2027, 6, 10), time="09:30", players=2),
                GolfRoundQuoteRequest(course_id=3, date=dt.date(2027, 6, 11), time="09:30", players=2),
            ],
        )
        res = self.service.calculate_quote(req)

        self.assertEqual(res.status, "available")
        self.assertEqual(res.nights, 2)
        self.assertEqual(res.total_rooms_price, "600.00")
        # 2 rounds * 2 players * €180 = €720
        self.assertEqual(res.total_golf_price, "720.00")
        # €600 + €720 = €1320
        self.assertEqual(res.total_package_price, "1320.00")
        # 25% of €1320 = €330
        self.assertEqual(res.deposit_amount, "330.00")
        # 75% of €1320 = €990
        self.assertEqual(res.balance_amount, "990.00")
        # 6 weeks prior to 2027-06-10 is 2027-04-29
        self.assertEqual(res.balance_due_date, dt.date(2027, 4, 29))
        self.assertFalse(res.reserves_inventory)
        self.assertEqual(res.freshness_ttl_seconds, 120)

    def test_category_buffer_enforcement(self):
        """Room requests exceeding available inventory minus category buffer return unavailable."""
        # DELUXE category has 3 available rooms; buffer is 1 -> net available is 2
        # Requesting 3 rooms must be rejected
        req = PackageQuoteRequest(
            arrival_date=dt.date(2027, 6, 10),
            departure_date=dt.date(2027, 6, 12),
            rooms=[RoomSelectionQuoteRequest(room_category="DELUXE", room_quantity=3)],
            golf_rounds=[
                GolfRoundQuoteRequest(course_id=1, date=dt.date(2027, 6, 10), time="09:30", players=2),
                GolfRoundQuoteRequest(course_id=3, date=dt.date(2027, 6, 11), time="09:30", players=2),
            ],
        )
        res = self.service.calculate_quote(req)
        self.assertEqual(res.status, "unavailable")
        self.assertTrue(any("Insufficient inventory" in e for e in res.errors))

    def test_upstream_opera_error_returns_unavailable(self):
        """If Opera Cloud throws an integration error, pricing returns unavailable without crashing."""
        self.mock_opera.get_room_availability.side_effect = OperaIntegrationError("OHIP gateway timed out")

        req = PackageQuoteRequest(
            arrival_date=dt.date(2027, 6, 10),
            departure_date=dt.date(2027, 6, 12),
            rooms=[RoomSelectionQuoteRequest(room_category="CLASSIC", room_quantity=1)],
            golf_rounds=[
                GolfRoundQuoteRequest(course_id=1, date=dt.date(2027, 6, 10), time="09:30", players=2),
                GolfRoundQuoteRequest(course_id=3, date=dt.date(2027, 6, 11), time="09:30", players=2),
            ],
        )
        res = self.service.calculate_quote(req)
        self.assertEqual(res.status, "unavailable")
        self.assertTrue(any("OHIP gateway timed out" in e for e in res.errors))

    def test_amendment_quote_recalculated_against_f05_verified_ledger(self):
        """Amendment quote reads verified paid amount from F05 ledger and computes delta and balance."""
        # Create an existing booking intent
        key = uuid.uuid4()
        intent_payload = BookingIntentCreate(
            selection=QuoteSelection(
                date=dt.date(2027, 6, 10),
                course_id=1,
                tee_time_id=101,
                green_fee_id=1,
                time="09:30",
                holes="18",
                package_name="Standard Package",
                players=2,
                price="1000.00",
                currency="EUR",
            ),
            lead_guest=LeadGuest(
                first_name="Padraig",
                last_name="Harrington",
                email="padraig@example.com",
                telephone="+353871112233",
            ),
        )
        intent, _ = self.store.create(key, intent_payload)

        # Record verified payment in F05 ledger (e.g. €500 paid deposit)
        payment_payload = PaymentRecordPayload(
            payment_id="PAY-101",
            provider="prommt",
            payment_type="deposit",
            amount="500.00",
            currency="EUR",
            status="completed",
        )
        self.store.record_payment(
            intent_id=intent["booking_intent_id"],
            expected_revision=intent["revision"],
            payment_payload=payment_payload,
        )

        # Request amendment quote (revised package total = €1320)
        req = PackageQuoteRequest(
            arrival_date=dt.date(2027, 6, 10),
            departure_date=dt.date(2027, 6, 12),
            rooms=[RoomSelectionQuoteRequest(room_category="CLASSIC", room_quantity=1)],
            golf_rounds=[
                GolfRoundQuoteRequest(course_id=1, date=dt.date(2027, 6, 10), time="09:30", players=2),
                GolfRoundQuoteRequest(course_id=3, date=dt.date(2027, 6, 11), time="09:30", players=2),
            ],
            existing_booking_intent_id=intent["booking_intent_id"],
        )
        res = self.service.calculate_quote(req)

        self.assertEqual(res.status, "available")
        self.assertIsNotNone(res.amendment_summary)
        summary = res.amendment_summary
        self.assertEqual(summary.current_total, "1000.00")
        self.assertEqual(summary.revised_total, "1320.00")
        self.assertEqual(summary.verified_paid, "500.00")
        # Difference: €1320 - €1000 = €320
        self.assertEqual(summary.amount_difference, "320.00")
        # Balance due: €1320 - €500 = €820
        self.assertEqual(summary.balance_due, "820.00")
        self.assertEqual(summary.credit_amount, "0.00")

    def test_staff_update_rules_with_audit_trail(self):
        """Staff updating package configuration persists new version and records audit trail."""
        update_req = UpdatePackageRulesRequest(
            actor="Reservations Manager",
            reason="Adjust seasonal category buffer for June peak",
            effective_date=dt.date(2027, 6, 1),
            bookable_cap=60,
            category_buffers={"CLASSIC": 2, "DELUXE": 2, "BAYVIEW_SUITE": 1, "PENTHOUSE": 0},
            deposit_percent=30,
            balance_due_weeks=8,
        )
        updated = self.service.update_rules(update_req)

        self.assertEqual(updated.bookable_cap, 60)
        self.assertEqual(updated.deposit_percent, 30)
        self.assertEqual(updated.balance_percent, 70)
        self.assertEqual(updated.balance_due_weeks, 8)
        self.assertEqual(updated.category_buffers["CLASSIC"], 2)

        # Audit record saved in MongoDB
        doc = self.db.package_rules.find_one({"is_active": True})
        self.assertIsNotNone(doc)
        self.assertEqual(doc["actor"], "Reservations Manager")
        self.assertEqual(doc["reason"], "Adjust seasonal category buffer for June peak")


class PackageRouteHandlerTests(unittest.TestCase):
    """Test suite for FastAPI route endpoints in packages.py."""

    def setUp(self):
        self.mongo = MongoClient(tz_aware=True)
        self.db = self.mongo["packages_routes_test_db"]
        self.store = BookingIntentStore(self.db.booking_intents)

        self.mock_opera = MagicMock(spec=OperaClient)
        self.mock_opera.get_room_availability.return_value = make_mock_opera_avail()

        self.mock_cache = MagicMock(spec=TeesheetCache)
        self.mock_cache.get_course_availability.return_value = make_mock_brs_avail(1, "09:30", 2)

        self.service = PackagePricingService(
            opera_client=self.mock_opera,
            teesheet_cache=self.mock_cache,
            booking_store=self.store,
            database=self.db,
        )

    def tearDown(self):
        self.mongo.close()

    def test_route_calculate_package_quote(self):
        req = PackageQuoteRequest(
            arrival_date=dt.date(2027, 6, 10),
            departure_date=dt.date(2027, 6, 12),
            rooms=[RoomSelectionQuoteRequest(room_category="CLASSIC", room_quantity=1)],
            golf_rounds=[
                GolfRoundQuoteRequest(course_id=1, date=dt.date(2027, 6, 10), time="09:30", players=2),
                GolfRoundQuoteRequest(course_id=3, date=dt.date(2027, 6, 11), time="09:30", players=2),
            ],
        )
        res = calculate_package_quote(request=req, pricing_service=self.service)
        self.assertEqual(res.status, "available")
        self.assertEqual(res.total_package_price, "1320.00")

    def test_route_get_and_update_rules(self):
        rules = get_package_rules(pricing_service=self.service)
        self.assertEqual(rules.bookable_cap, 65)

        update_req = UpdatePackageRulesRequest(
            actor="Admin",
            reason="Update cap",
            effective_date=dt.date(2027, 7, 1),
            bookable_cap=62,
        )
        updated = update_package_rules(request=update_req, pricing_service=self.service)
        self.assertEqual(updated.bookable_cap, 62)


if __name__ == "__main__":
    unittest.main()
