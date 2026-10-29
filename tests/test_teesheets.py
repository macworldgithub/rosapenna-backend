"""Focused cache/safety regressions. No network, bookings or payments."""
import copy
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from unittest.mock import patch

from fastapi import HTTPException
from mongomock import MongoClient
from pymongo.errors import NetworkTimeout

from rosapenna_backend.api.routes.availability import get_golf_availability
from rosapenna_backend.api.schemas.availability import QuoteSelection
from rosapenna_backend.integrations.brs.tee_sheet import (
    BrsTenantUnconfiguredError,
    BrsUnsupportedQueryError,
    COURSES,
    DUBLIN,
    validate_sheet,
)
from rosapenna_backend.services.tee_sheets import TeesheetCache


def sheet(day, price="200.00", slots=4):
    return {"tee_date": day, "currency_code": "EUR", "messages": [],
            "tee_times": [{"id": 101, "time": "10:00", "holes": "18",
                           "slots": {str(i): {"status": "Available", "name_on_teesheet": None} for i in range(slots)},
                           "green_fees": [{"id": 7, "green_fee1_ball": price}]}]}


class FakeReader:
    calls = []
    fail = False
    price = "200.00"
    slots = 4

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def fetch(self, day, course_id):
        self.calls.append((day, course_id))
        if self.fail:
            raise RuntimeError("Network unavailable")
        return sheet(day, self.price, self.slots)


class CacheTests(unittest.TestCase):
    def setUp(self):
        self.mongo = MongoClient(tz_aware=True)
        self.database = self.mongo["cache_test"]
        self.now = datetime(2026, 10, 14, 8, tzinfo=timezone.utc)
        FakeReader.calls, FakeReader.fail, FakeReader.price, FakeReader.slots = [], False, "200.00", 4
        self.cache = self.new_cache()
        self.pacing = patch.object(self.cache.stop, "wait", return_value=False)
        self.pacing.start()

    def tearDown(self):
        self.pacing.stop()
        self.mongo.close()

    def new_cache(self):
        return TeesheetCache(self.database, days=2, reader_factory=FakeReader, now=lambda: self.now)

    def selection(self, **changes):
        return QuoteSelection(**({"date": "2026-10-14", "course_id": 1,
                                  "tee_time_id": 101, "green_fee_id": 7, "players": 1,
                                  "price": "200.00", "currency": "EUR"} | changes))

    def test_in_memory_cache_fetches_live_and_caches_within_ttl(self):
        result = self.cache.get(date(2026, 10, 14), 1)
        self.assertEqual(len(FakeReader.calls), 1)
        self.assertEqual(result["cache"]["storage"], "in_memory")
        self.assertTrue(result["cache"]["live"])
        self.assertFalse(result["cache"]["stale"])

        # Second call within TTL does not hit FakeReader again
        result2 = self.cache.get(date(2026, 10, 14), 1)
        self.assertEqual(len(FakeReader.calls), 1)
        self.assertEqual(result2["data"]["tee_times"][0]["id"], 101)

        # Advance past 60s TTL triggers fresh fetch
        self.now += timedelta(seconds=65)
        result3 = self.cache.get(date(2026, 10, 14), 1)
        self.assertEqual(len(FakeReader.calls), 2)
        self.assertEqual(result3["cache"]["storage"], "in_memory")

    def test_snapshot_upserts_have_one_record_and_save_to_memory_and_db(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(lambda _: self.cache.save("2026-10-14", 1, sheet("2026-10-14")), range(2)))
        self.assertEqual(self.database.tee_sheets.count_documents({}), 1)
        result = self.cache.get(date(2026, 10, 14), 1)
        self.assertEqual(result["cache"]["storage"], "in_memory")
        self.assertIsInstance(result["cache"]["fetched_at"], str)

    def test_missing_and_invalid_dates_do_not_trigger_network(self):
        for day, course_id in [(date(2026, 10, 13), 1), (date(2026, 10, 16), 1), (date(2026, 10, 14), 99)]:
            with self.assertRaises(ValueError):
                self.cache.get(day, course_id)
        self.assertFalse(FakeReader.calls)

    def test_status_reports_in_memory_metrics(self):
        status = self.cache.status()
        self.assertEqual(status["storage"], "in_memory")
        self.assertEqual(status["ttl_seconds"], 60)
        self.assertEqual(status["timezone"], "Europe/Dublin")
        self.assertIn("window_start", status)
        self.assertIn("window_end", status)

    def test_live_price_change_blocks_and_updates_saved_results(self):
        FakeReader.price = "220.00"
        result = self.cache.revalidate(self.selection())
        self.assertFalse(result["valid"])
        self.assertIn("220.00", result["reason"])
        self.assertEqual(self.cache.get(date(2026, 10, 14), 1)["data"]["tee_times"][0]["green_fees"][0]["green_fee1_ball"], "220.00")

    def test_live_check_blocks_sold_out_missing_fee_and_currency_change(self):
        FakeReader.slots = 0
        self.assertFalse(self.cache.revalidate(self.selection())["valid"])
        FakeReader.slots = 4
        self.assertFalse(self.cache.revalidate(self.selection(green_fee_id=8))["valid"])
        self.assertFalse(self.cache.revalidate(self.selection(currency="GBP"))["valid"])
        self.assertTrue(self.cache.revalidate(self.selection())["valid"])

    def test_invalid_price_and_busy_check_fail_before_network(self):
        with self.assertRaises(ValueError):
            self.cache.revalidate(self.selection(price="NaN"))
        self.cache.live_lock.acquire()
        try:
            with self.assertRaises(BlockingIOError):
                self.cache.revalidate(self.selection())
        finally:
            self.cache.live_lock.release()
        self.assertFalse(FakeReader.calls)

    def test_upstream_date_validation_and_player_name_removal(self):
        data = sheet("2026-10-14")
        data["tee_times"][0]["slots"]["0"]["name_on_teesheet"] = "Private name"
        clean = validate_sheet({"data": copy.deepcopy(data)}, "2026-10-14")
        self.assertIsNone(clean["tee_times"][0]["slots"]["0"]["name_on_teesheet"])
        with self.assertRaises(ValueError):
            validate_sheet({"data": data}, "2026-10-15")

    def test_dublin_schedule_handles_summer_and_winter(self):
        self.assertEqual(datetime(2026, 7, 1, 4, tzinfo=timezone.utc).astimezone(DUBLIN).hour, 5)
        self.assertEqual(datetime(2026, 12, 1, 5, tzinfo=timezone.utc).astimezone(DUBLIN).hour, 5)


class BrsReaderTests(unittest.TestCase):
    def test_brs_reader_fetches_and_validates(self):
        import json
        from unittest.mock import MagicMock
        from rosapenna_backend.integrations.brs.tee_sheet import BrsReader

        reader = BrsReader()
        fake_response = MagicMock()
        fake_response.status = 200
        fake_response.code = 200
        fake_payload = {"data": sheet("2026-10-14")}
        fake_response.read.return_value = json.dumps(fake_payload).encode("utf-8")

        fake_opener = MagicMock()
        fake_opener.open.return_value.__enter__.return_value = fake_response

        with patch("urllib.request.build_opener", return_value=fake_opener):
            with reader as r:
                result = r.fetch("2026-10-14", 1)
        self.assertEqual(result["tee_date"], "2026-10-14")
        self.assertEqual(len(result["tee_times"]), 1)

    def test_brs_reader_raises_on_http_error(self):
        from urllib.error import HTTPError
        from unittest.mock import MagicMock
        from rosapenna_backend.integrations.brs.tee_sheet import BrsReader

        reader = BrsReader()
        fake_opener = MagicMock()
        fake_opener.open.side_effect = HTTPError("url", 404, "Not Found", {}, None)

        with patch("urllib.request.build_opener", return_value=fake_opener):
            with reader as r:
                with self.assertRaises(RuntimeError) as ctx:
                    r.fetch("2026-10-14", 1)
                self.assertIn("404", str(ctx.exception))


class ThreeCourseAvailabilityTests(unittest.TestCase):
    def setUp(self):
        self.mongo = MongoClient(tz_aware=True)
        self.database = self.mongo["three_course_test"]
        self.now = datetime(2026, 10, 14, 8, tzinfo=timezone.utc)
        FakeReader.calls, FakeReader.fail, FakeReader.price, FakeReader.slots = [], False, "200.00", 4
        self.cache = TeesheetCache(self.database, days=30, reader_factory=FakeReader, now=lambda: self.now)

    def tearDown(self):
        self.mongo.close()

    def test_three_course_mappings_and_topology(self):
        """All three championship links must have verified mappings and tenants."""
        self.assertIn(1, COURSES)
        self.assertEqual(COURSES[1]["name"], "Old Tom Morris Links")
        self.assertEqual(COURSES[1]["tenant"], "rosapenna")

        self.assertIn(3, COURSES)
        self.assertEqual(COURSES[3]["name"], "Sandy Hills Links")
        self.assertEqual(COURSES[3]["tenant"], "rosapenna")

        self.assertIn(4, COURSES)
        self.assertEqual(COURSES[4]["name"], "St Patrick's Links")
        self.assertEqual(COURSES[4]["tenant"], "stpatricks")

    def test_three_course_availability_reads(self):
        """Querying Old Tom, Sandy Hills, and St Patrick's returns structured availability."""
        day = date(2026, 10, 14)
        for c_id in (1, 3, 4):
            res = self.cache.get_course_availability(day=day, course_id=c_id)
            self.assertEqual(res.course_id, c_id)
            self.assertEqual(res.course_name, COURSES[c_id]["name"])
            self.assertEqual(res.status, "available")
            self.assertEqual(res.available_slots_count, 1)
            self.assertFalse(res.reserves_inventory)
            self.assertEqual(res.freshness_ttl_seconds, 60)
            self.assertTrue(res.slots[0].inventory_id.startswith(f"BRS-{c_id}-20261014-"))

    def test_group_size_filtering(self):
        """Filter slots by minimum player capacity."""
        day = date(2026, 10, 14)
        # Mock returns 4 slots: 2 players request should match
        res_2 = self.cache.get_course_availability(day=day, course_id=1, players=2)
        self.assertEqual(res_2.available_slots_count, 1)
        self.assertEqual(res_2.slots[0].available_capacity, 4)

        # Re-mock with only 1 slot open
        FakeReader.slots = 1
        self.cache.close()  # Clear memory cache
        res_4 = self.cache.get_course_availability(day=day, course_id=1, players=4)
        self.assertEqual(res_4.available_slots_count, 0)
        self.assertEqual(res_4.status, "no_availability")

    def test_time_window_filtering(self):
        """Slots outside time_from / time_to are filtered out."""
        day = date(2026, 10, 14)
        # Slot is at 10:00
        in_range = self.cache.get_course_availability(day=day, course_id=1, time_from="09:00", time_to="11:00")
        self.assertEqual(in_range.available_slots_count, 1)

        out_of_range = self.cache.get_course_availability(day=day, course_id=1, time_from="11:00", time_to="14:00")
        self.assertEqual(out_of_range.available_slots_count, 0)
        self.assertEqual(out_of_range.status, "no_availability")

    def test_unsupported_query_validation(self):
        """Invalid course, players, or inverted time ranges raise BrsUnsupportedQueryError."""
        day = date(2026, 10, 14)
        with self.assertRaises(BrsUnsupportedQueryError):
            self.cache.get_course_availability(day=day, course_id=99)

        with self.assertRaises(BrsUnsupportedQueryError):
            self.cache.get_course_availability(day=day, course_id=1, players=5)

        with self.assertRaises(BrsUnsupportedQueryError):
            self.cache.get_course_availability(day=day, course_id=1, time_from="15:00", time_to="10:00")

    def test_unconfigured_stpatricks_tenant_handling(self):
        """When St Patrick's tenant is unconfigured, return unconfigured_tenant status without error."""
        class StPatricksUnconfiguredReader(FakeReader):
            def fetch(self, day, course_id):
                if course_id == 4:
                    raise BrsTenantUnconfiguredError("St Patrick's tenant is unconfigured.")
                return super().fetch(day, course_id)

        unconfigured_cache = TeesheetCache(
            self.database,
            days=30,
            reader_factory=StPatricksUnconfiguredReader,
            now=lambda: self.now,
        )
        res = unconfigured_cache.get_course_availability(day=date(2026, 10, 14), course_id=4)
        self.assertEqual(res.status, "unconfigured_tenant")
        self.assertEqual(res.available_slots_count, 0)
        self.assertEqual(res.slots, [])

    def test_route_get_golf_availability(self):
        """GET /golf/availability returns valid HTTP response."""
        res = get_golf_availability(
            course_id=1,
            date=date(2026, 10, 14),
            players=2,
            time_from="08:00",
            time_to="12:00",
            cache=self.cache,
        )
        self.assertEqual(res.course_id, 1)
        self.assertEqual(res.course_name, "Old Tom Morris Links")
        self.assertEqual(res.status, "available")
        self.assertFalse(res.reserves_inventory)

    def test_route_get_golf_availability_unsupported_course(self):
        with self.assertRaises(HTTPException) as ctx:
            get_golf_availability(
                course_id=99,
                date=date(2026, 10, 14),
                cache=self.cache,
            )
        self.assertEqual(ctx.exception.status_code, 400)


if __name__ == "__main__":
    unittest.main()
