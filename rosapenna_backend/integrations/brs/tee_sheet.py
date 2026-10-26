"""BRS tee-sheet reader and upstream-data validation."""

from datetime import date
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from playwright.sync_api import sync_playwright
from pydantic import BaseModel, Field

from rosapenna_backend.core.logging import logger

DUBLIN = ZoneInfo("Europe/Dublin")

BRS_URL = "https://visitors.brsgolf.com/rosapenna#/course/1"

COURSES = {
    1: {"id": 1, "facilities_id": 5797, "name": "Old Tom Morris Links", "playing_time": "", "colour": ""},
    3: {"id": 3, "facilities_id": 5896, "name": "Sandy Hills Links", "playing_time": "", "colour": "3"},
}

def amount(value):
    try:
        result = Decimal(str(value))
        if not result.is_finite() or result < 0 or result != result.quantize(Decimal("0.01")):
            raise ValueError("Invalid BRS price")
    except (InvalidOperation, ValueError):
        raise ValueError("Invalid BRS price")
    return result

def validate_sheet(payload, requested_date):
    """Reject bad/incorrect-date upstream data before replacing a good snapshot."""
    data = payload.get("data")
    if (not isinstance(data, dict) or data.get("tee_date") != requested_date
            or not isinstance(data.get("tee_times"), list)
            or not isinstance(data.get("currency_code"), str)
            or len(data["currency_code"]) != 3 or not data["currency_code"].isalpha()
            or not data["currency_code"].isupper()):
        raise ValueError("BRS returned an invalid tee sheet or a different date")
    for tee in data["tee_times"]:
        if not isinstance(tee.get("id"), int) or not isinstance(tee.get("slots"), dict) or not isinstance(tee.get("green_fees"), list):
            raise ValueError("BRS returned an invalid tee time")
        # Booked-player names are unnecessary for public availability snapshots.
        for slot in tee["slots"].values():
            slot["name_on_teesheet"] = None
        for fee in tee["green_fees"]:
            if not isinstance(fee.get("id"), int):
                raise ValueError("BRS returned an invalid green fee")
            for players in range(1, 5):
                price = fee.get(f"green_fee{players}_ball")
                if price is not None:
                    amount(price)
    return data

class BrsReader:
    """Reuse one public browser session for the daily batch; never book or pay."""

    def __enter__(self):
        self.playwright = sync_playwright().start()
        self.browser = None
        try:
            self.browser = self.playwright.chromium.launch(headless=True)
            self.page = self.browser.new_page()
            self.page.goto(BRS_URL, wait_until="domcontentloaded", timeout=30000)
            self.page.wait_for_selector("text=Choose Course", timeout=30000)
            return self
        except Exception:
            self.__exit__(None, None, None)
            raise

    def fetch(self, day, course_id):
        result = self.page.evaluate("""async ({day, courseId}) => {
          const response = await fetch(`/api/casualBooking/teesheet?date=${day}&course_id=${courseId}`, {
            headers: {'X-Club-ID': 'rosapenna', 'X-Requested-With': 'XMLHttpRequest', 'X-Booking-Source': 'ui'},
            cache: 'no-store', signal: AbortSignal.timeout(30000)
          });
          if (!response.ok) throw new Error(`BRS returned HTTP ${response.status}`);
          return await response.json();
        }""", {"day": day, "courseId": course_id})
        return validate_sheet(result, day)

    def __exit__(self, *_):
        try:
            if self.browser:
                self.browser.close()
        finally:
            self.playwright.stop()
