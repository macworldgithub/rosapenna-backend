"""Focused cache/safety regressions. No network, bookings or payments."""
import copy
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from unittest.mock import patch

from mongomock import MongoClient
from pymongo.errors import NetworkTimeout

from rosapenna_backend.api.schemas.availability import QuoteSelection
from rosapenna_backend.integrations.brs.tee_sheet import DUBLIN, validate_sheet
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

    def test_daily_claim_survives_restart_and_browsing_does_not_fetch(self):
        self.cache.refresh()
        self.assertEqual(len(FakeReader.calls), 4)
        restarted = self.new_cache()
        restarted.refresh()
        result = restarted.get(date(2026, 10, 14), 1)
        self.assertFalse(result["cache"]["stale"])
        self.assertEqual(len(FakeReader.calls), 4)

    def test_concurrent_daily_calls_claim_only_one_batch(self):
        restarted = self.new_cache()
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(lambda cache: cache.refresh(), [self.cache, restarted]))
        self.assertEqual(len(FakeReader.calls), 4)

    def test_snapshot_upserts_have_one_record_and_no_internal_ids_in_api(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(lambda _: self.cache.save("2026-10-14", 1, sheet("2026-10-14")), range(2)))
        self.assertEqual(self.database.tee_sheets.count_documents({}), 1)
        self.cache.refresh()
        result = self.cache.get(date(2026, 10, 14), 1)
        self.assertNotIn("_id", result["cache"]["last_run"])
        self.assertEqual(result["cache"]["storage"], "mongodb")
        self.assertIsInstance(result["cache"]["fetched_at"], str)

    def test_failed_refresh_keeps_last_good_values_and_marks_stale(self):
        self.cache.refresh()
        self.now += timedelta(days=1)
        FakeReader.fail = True
        with self.assertLogs("booking_api", level="ERROR"):
            self.cache.refresh()
        result = self.cache.get(date(2026, 10, 15), 1)
        self.assertTrue(result["cache"]["stale"])
        self.assertEqual(result["data"]["tee_times"][0]["green_fees"][0]["green_fee1_ball"], "200.00")
        self.assertEqual(self.cache.status()["last_run"]["status"], "failed")

    def test_missing_and_invalid_dates_do_not_trigger_network(self):
        self.assertIsNone(self.cache.get(date(2026, 10, 14), 1))
        for day, course_id in [(date(2026, 10, 13), 1), (date(2026, 10, 16), 1), (date(2026, 10, 14), 99)]:
            with self.assertRaises(ValueError):
                self.cache.get(day, course_id)
        self.assertFalse(FakeReader.calls)

    def test_failed_day_is_not_retried_automatically_but_operator_can_retry(self):
        FakeReader.fail = True
        with self.assertLogs("booking_api", level="ERROR"):
            self.cache.refresh()
        FakeReader.fail = False
        self.cache.refresh()
        self.assertEqual(len(FakeReader.calls), 4)
        self.cache.refresh(retry_failed=True)
        self.assertEqual(self.cache.status()["last_run"]["status"], "complete")
        self.assertEqual(len(FakeReader.calls), 8)
        self.cache.refresh(retry_failed=True)
        self.assertEqual(len(FakeReader.calls), 8)

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

    def test_scheduler_handles_each_dublin_day_once(self):
        with patch("rosapenna_backend.services.tee_sheets.threading.Thread") as thread, patch.object(self.cache, "refresh") as refresh:
            self.cache.start()
            with patch.object(self.cache.stop, "is_set", side_effect=[False, False, False, True]):
                self.cache.now = unittest.mock.Mock(side_effect=[self.now, self.now, self.now + timedelta(days=1)])
                thread.call_args.kwargs["target"]()
        self.assertEqual(refresh.call_count, 2)
        self.assertFalse(FakeReader.calls)

    def test_scheduler_retries_database_failure_without_exposing_driver_details(self):
        with patch("rosapenna_backend.services.tee_sheets.threading.Thread") as thread, patch.object(self.cache, "refresh") as refresh:
            refresh.side_effect = [NetworkTimeout("private-connection-details"), None]
            self.cache.start()
            with patch.object(self.cache.stop, "is_set", side_effect=[False, False, False, True]):
                with self.assertLogs("booking_api", level="WARNING") as logs:
                    thread.call_args.kwargs["target"]()
        self.assertEqual(refresh.call_count, 2)
        self.assertIn("NetworkTimeout", logs.output[0])
        self.assertNotIn("private-connection-details", " ".join(logs.output))
        self.assertFalse(FakeReader.calls)

    def test_operator_refresh_before_scheduled_hour_stays_fresh_that_day(self):
        self.now = datetime(2026, 10, 14, 2, tzinfo=timezone.utc)
        self.cache.refresh()
        self.now += timedelta(hours=6)
        self.assertFalse(self.cache.get(date(2026, 10, 14), 1)["cache"]["stale"])


if __name__ == "__main__":
    unittest.main()
