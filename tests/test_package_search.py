"""Unit tests for Ticket N03: Live Stay and Golf Search."""

import datetime as dt
import unittest
from unittest.mock import MagicMock

from mongomock import MongoClient

from rosapenna_backend.api.routes.packages import search_packages
from rosapenna_backend.api.schemas.availability import (
    BrsCourseAvailabilityResponse,
    BrsSlotRate,
    BrsTeeSlot,
)
from rosapenna_backend.api.schemas.packages import (
    PackageRulesConfig,
    PackageSearchRequest,
    PackageSearchResponse,
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


def make_mock_opera_avail(arrival: dt.date, departure: dt.date) -> RoomAvailabilityResponse:
    return RoomAvailabilityResponse(
        hotel_id="ROSA",
        arrival_date=arrival,
        departure_date=departure,
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
                available_rooms=4,
                rates=[
                    RoomRatePlan(
                        rate_plan_code="ROSA_BB_2027",
                        rate_plan_name="Bed & Breakfast",
                        total_amount=700.0,
                        currency="EUR",
                        breakfast_included=True,
                    )
                ],
            ),
        ],
    )


def make_mock_brs_avail(course_id: int, date_val: dt.date, course_name: str) -> BrsCourseAvailabilityResponse:
    rate = BrsSlotRate(
        rate_id=1,
        package_name="Visitor Rate",
        players=2,
        price_per_player="180.00",
        total_price="360.00",
        currency="EUR",
        num_holes="18",
        type="Visitor",
    )
    slot1 = BrsTeeSlot(
        slot_id=101,
        time="09:30",
        holes="18",
        available_capacity=4,
        is_available=True,
        rates=[rate],
        inventory_id="inv-101",
    )
    slot2 = BrsTeeSlot(
        slot_id=102,
        time="10:00",
        holes="18",
        available_capacity=4,
        is_available=True,
        rates=[rate],
        inventory_id="inv-102",
    )
    return BrsCourseAvailabilityResponse(
        course_id=course_id,
        course_name=course_name,
        tenant="rosapenna" if course_id != 4 else "stpatrickslinks",
        date=date_val,
        status="available",
        available_slots_count=2,
        slots=[slot1, slot2],
        retrieved_at=dt.datetime.now(dt.timezone.utc),
        freshness_ttl_seconds=60,
        reserves_inventory=False,
        source="brs_api",
    )


class PackageSearchTests(unittest.TestCase):
    """Test suite for live stay and golf search service (Ticket N03)."""

    def setUp(self):
        self.mongo = MongoClient(tz_aware=True)
        self.db = self.mongo["packages_search_test_db"]
        self.store = BookingIntentStore(self.db.booking_intents)

        self.mock_opera = MagicMock(spec=OperaClient)
        self.mock_opera.get_room_availability.side_effect = lambda arrival_date, departure_date, adults, children: make_mock_opera_avail(arrival_date, departure_date)

        self.mock_cache = MagicMock(spec=TeesheetCache)
        self.mock_cache.get_course_availability.side_effect = lambda day, course_id, players: make_mock_brs_avail(
            course_id, day, "Course " + str(course_id)
        )

        self.service = PackagePricingService(
            opera_client=self.mock_opera,
            teesheet_cache=self.mock_cache,
            booking_store=self.store,
            database=self.db,
        )

    def tearDown(self):
        self.mongo.close()

    def test_search_rejects_single_night_stay(self):
        """Stay under 2 nights returns ineligible with truthful explanation."""
        req = PackageSearchRequest(
            arrival_date=dt.date(2027, 6, 10),
            departure_date=dt.date(2027, 6, 11),
            party_size=2,
            room_count=1,
        )
        res = self.service.search_stay_and_golf(req)
        self.assertEqual(res.status, "ineligible")
        self.assertTrue(any("minimum stay of 2 nights" in w for w in res.warnings))
        self.assertFalse(res.reserves_inventory)

    def test_search_rejects_more_than_five_nights(self):
        """Stay over 5 nights returns ineligible with bespoke review guidance."""
        req = PackageSearchRequest(
            arrival_date=dt.date(2027, 6, 10),
            departure_date=dt.date(2027, 6, 16),
            party_size=2,
            room_count=1,
        )
        res = self.service.search_stay_and_golf(req)
        self.assertEqual(res.status, "ineligible")
        self.assertTrue(any("longer than 5 nights require bespoke staff review" in w for w in res.warnings))

    def test_search_successful_two_night_package(self):
        """Valid 2-night stay returns rooms, 3-course tee times, and package recommendation."""
        req = PackageSearchRequest(
            arrival_date=dt.date(2027, 6, 10),
            departure_date=dt.date(2027, 6, 12),
            party_size=2,
            room_count=1,
            preferred_courses=[1, 3, 4],
        )
        res = self.service.search_stay_and_golf(req)
        self.assertEqual(res.status, "available")
        self.assertEqual(res.nights, 2)
        self.assertEqual(res.required_minimum_rounds, 2)
        self.assertFalse(res.reserves_inventory)

        # Room options verified
        self.assertEqual(len(res.room_options), 2)
        classic = next(r for r in res.room_options if r.room_category == "CLASSIC")
        self.assertEqual(classic.available_rooms, 4)  # 5 raw - 1 buffer = 4
        self.assertTrue(classic.is_available)
        self.assertEqual(classic.rate_per_night, "300.00")

        # 3 courses queried for each day of stay (3 days: arrival, intermediate, departure)
        self.assertEqual(len(res.daily_golf_options), 9)  # 3 days * 3 courses
        courses_represented = {g.course_id for g in res.daily_golf_options}
        self.assertEqual(courses_represented, {1, 3, 4})

        # Recommended packages calculated
        self.assertTrue(len(res.recommended_packages) >= 1)
        rec = res.recommended_packages[0]
        # Room: 600.00, 2 rounds * 2 players * 180.00 = 720.00 -> Total: 1320.00
        self.assertEqual(rec.total_price, "1320.00")
        self.assertEqual(rec.deposit_amount, "330.00")  # 25%
        self.assertEqual(rec.balance_amount, "990.00")  # 75%
        self.assertEqual(rec.balance_due_date, dt.date(2027, 4, 29))  # 42 days prior

    def test_search_four_night_stay_requires_three_rounds(self):
        """Stays of 4-5 nights require at least 3 rounds."""
        req = PackageSearchRequest(
            arrival_date=dt.date(2027, 6, 10),
            departure_date=dt.date(2027, 6, 14),
            party_size=2,
            room_count=1,
        )
        res = self.service.search_stay_and_golf(req)
        self.assertEqual(res.nights, 4)
        self.assertEqual(res.required_minimum_rounds, 3)

    def test_golf_ireland_dual_fact_pricing(self):
        """Dual-fact Golf Ireland eligibility grants member resident rate."""
        # Case 1: Irish residence and Golf Ireland index
        req_gi = PackageSearchRequest(
            arrival_date=dt.date(2027, 6, 10),
            departure_date=dt.date(2027, 6, 12),
            party_size=2,
            room_count=1,
            residence_country="IE",
            has_golf_ireland_index=True,
        )
        res_gi = self.service.search_stay_and_golf(req_gi)
        self.assertTrue(res_gi.is_golf_ireland_eligible)
        slot = res_gi.daily_golf_options[0].tee_slots[0]
        self.assertEqual(slot.rate_per_player, "120.00")

        # Case 2: Irish residence but no Golf Ireland index -> visitor rate
        req_non_gi = PackageSearchRequest(
            arrival_date=dt.date(2027, 6, 10),
            departure_date=dt.date(2027, 6, 12),
            party_size=2,
            room_count=1,
            residence_country="IE",
            has_golf_ireland_index=False,
        )
        res_non_gi = self.service.search_stay_and_golf(req_non_gi)
        self.assertFalse(res_non_gi.is_golf_ireland_eligible)
        slot_non_gi = res_non_gi.daily_golf_options[0].tee_slots[0]
        self.assertEqual(slot_non_gi.rate_per_player, "180.00")

    def test_course_closure_in_rules_returns_safe_alternative(self):
        """Course closure configured in rules shows closure reason and alternatives."""
        # Set closure in rules for St Patrick's Links on 2027-06-10
        rules = self.service.get_rules()
        rules.closures = [{"course_id": 4, "date": "2027-06-10", "reason": "Seasonal links rest"}]
        self.service._cached_rules = rules

        req = PackageSearchRequest(
            arrival_date=dt.date(2027, 6, 10),
            departure_date=dt.date(2027, 6, 12),
            party_size=2,
            room_count=1,
            preferred_courses=[1, 3, 4],
        )
        res = self.service.search_stay_and_golf(req)
        stp_day1 = next(
            g for g in res.daily_golf_options
            if g.course_id == 4 and g.date == dt.date(2027, 6, 10)
        )
        self.assertTrue(stp_day1.is_closed)
        self.assertEqual(stp_day1.closure_reason, "Seasonal links rest")
        self.assertIn("Old Tom Morris", stp_day1.alternative_suggestion)

    def test_unconfigured_tenant_returns_closed_safely(self):
        """Unconfigured BRS tenant does not fake open availability."""
        unconfigured_resp = BrsCourseAvailabilityResponse(
            course_id=4,
            course_name="St Patrick's Links",
            tenant="stpatrickslinks",
            date=dt.date(2027, 6, 10),
            status="unconfigured_tenant",
            available_slots_count=0,
            slots=[],
            retrieved_at=dt.datetime.now(dt.timezone.utc),
            freshness_ttl_seconds=60,
            reserves_inventory=False,
            source="brs_api",
        )
        self.mock_cache.get_course_availability.side_effect = lambda day, course_id, players: (
            unconfigured_resp if course_id == 4 else make_mock_brs_avail(course_id, day, "Course " + str(course_id))
        )
        req = PackageSearchRequest(
            arrival_date=dt.date(2027, 6, 10),
            departure_date=dt.date(2027, 6, 12),
            party_size=2,
            room_count=1,
            preferred_courses=[4],
        )
        res = self.service.search_stay_and_golf(req)
        stp = res.daily_golf_options[0]
        self.assertTrue(stp.is_closed)
        self.assertIn("unconfigured", stp.closure_reason)

    def test_opera_error_fails_gracefully(self):
        """Opera integration failure logs a warning and does not crash search."""
        self.mock_opera.get_room_availability.side_effect = OperaIntegrationError(
            status_code=502, message="Opera Gateway Timeout"
        )
        req = PackageSearchRequest(
            arrival_date=dt.date(2027, 6, 10),
            departure_date=dt.date(2027, 6, 12),
            party_size=2,
            room_count=1,
        )
        res = self.service.search_stay_and_golf(req)
        self.assertTrue(any("Opera room availability check encountered an error" in w for w in res.warnings))
        self.assertEqual(res.room_options, [])
        self.assertIn(res.status, ["partial", "unavailable"])

    def test_st_patricks_agronomy_and_winter_closure(self):
        """St Patrick's Links marks maintenance Mondays/Wednesdays and winter months as closed."""
        # 2027-06-15 is in st_patricks_agronomy_rest ("06-15")
        req = PackageSearchRequest(
            arrival_date=dt.date(2027, 6, 14),
            departure_date=dt.date(2027, 6, 16),
            party_size=2,
            room_count=1,
            preferred_courses=[1, 3, 4],
        )
        res = self.service.search_stay_and_golf(req)
        # Find St Patrick's option on 2027-06-15
        stp_closed_day = next(
            g for g in res.daily_golf_options
            if g.course_id == 4 and g.date == dt.date(2027, 6, 15)
        )
        self.assertTrue(stp_closed_day.is_closed)
        self.assertIn("resting for agronomy", stp_closed_day.closure_reason)

        # Winter stay (e.g. December)
        req_winter = PackageSearchRequest(
            arrival_date=dt.date(2026, 12, 10),
            departure_date=dt.date(2026, 12, 12),
            party_size=2,
            room_count=1,
            preferred_courses=[4],
        )
        res_winter = self.service.search_stay_and_golf(req_winter)
        stp_winter = res_winter.daily_golf_options[0]
        self.assertTrue(stp_winter.is_closed)
        self.assertIn("winter agronomy rest", stp_winter.closure_reason)
        self.assertTrue(any("resting for agronomy" in w for w in res_winter.warnings))

    def test_route_search_packages(self):
        """FastAPI route handler delegates cleanly to pricing service."""
        req = PackageSearchRequest(
            arrival_date=dt.date(2027, 6, 10),
            departure_date=dt.date(2027, 6, 12),
            party_size=2,
            room_count=1,
            preferred_courses=[1, 3],
        )
        res = search_packages(request=req, pricing_service=self.service)
        self.assertEqual(res.status, "available")
        self.assertEqual(res.nights, 2)
        self.assertFalse(res.reserves_inventory)


if __name__ == "__main__":
    unittest.main()
