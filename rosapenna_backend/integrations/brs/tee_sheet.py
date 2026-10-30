"""BRS tee-sheet reader and upstream-data validation."""

import json
import urllib.parse
import urllib.request
from datetime import date
from decimal import Decimal, InvalidOperation
from urllib.error import HTTPError, URLError
from zoneinfo import ZoneInfo

from rosapenna_backend.core.logging import logger

DUBLIN = ZoneInfo("Europe/Dublin")

BRS_URL = "https://visitors.brsgolf.com/rosapenna#/course/1"
BRS_API_BASE = "https://visitors.brsgolf.com/api/casualBooking/teesheet"

class BrsIntegrationError(RuntimeError):
    """Base exception for BRS Golf integration errors."""

    def __init__(self, message: str, status_code: int = 502, detail_code: str = "brs_error"):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.detail_code = detail_code


class BrsUnsupportedQueryError(BrsIntegrationError, ValueError):
    def __init__(self, message: str = "Unsupported course, date, or player query."):
        super().__init__(message, status_code=400, detail_code="unsupported_query")


class BrsTenantUnconfiguredError(BrsIntegrationError):
    def __init__(self, message: str = "BRS course tenant is not configured."):
        super().__init__(message, status_code=503, detail_code="unconfigured_tenant")


class BrsAuthError(BrsIntegrationError):
    def __init__(self, message: str = "BRS authentication failed."):
        super().__init__(message, status_code=502, detail_code="brs_auth_error")


class BrsUpstreamError(BrsIntegrationError):
    def __init__(self, message: str = "BRS service is temporarily unavailable."):
        super().__init__(message, status_code=502, detail_code="brs_upstream_error")


class BrsMalformedResponseError(BrsIntegrationError):
    def __init__(self, message: str = "BRS returned an invalid or unparseable tee-sheet response."):
        super().__init__(message, status_code=502, detail_code="brs_malformed_response")


class BrsStaleDataError(BrsIntegrationError):
    def __init__(self, message: str = "Tee sheet snapshot is stale and live refresh failed."):
        super().__init__(message, status_code=502, detail_code="stale_snapshot")


COURSES = {
    1: {
        "id": 1,
        "facilities_id": 5797,
        "name": "Old Tom Morris Links",
        "slug": "old-tom-morris",
        "tenant": "rosapenna",
        "external_course_id": 1,
        "playing_time": "4h 15m",
        "colour": "",
    },
    3: {
        "id": 3,
        "facilities_id": 5896,
        "name": "Sandy Hills Links",
        "slug": "sandy-hills",
        "tenant": "rosapenna",
        "external_course_id": 3,
        "playing_time": "4h 30m",
        "colour": "3",
    },
    4: {
        "id": 4,
        "facilities_id": 5897,
        "name": "St Patrick's Links",
        "slug": "st-patricks",
        "tenant": "stpatrickslinks",
        "external_course_id": 1,
        "playing_time": "4h 30m",
        "colour": "4",
    },
}

DEFAULT_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://visitors.brsgolf.com/rosapenna",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "X-Club-ID": "rosapenna",
    "X-Booking-Source": "ui",
    "X-Requested-With": "XMLHttpRequest",
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
    """Direct HTTP reader for BRS tee sheets; never book or pay."""

    def __init__(self, timeout=30, headers=None):
        self.timeout = timeout
        self.headers = headers or DEFAULT_HEADERS
        self.opener = None

    def __enter__(self):
        self.opener = urllib.request.build_opener()
        return self

    def fetch(self, day, course_id):
        if course_id not in COURSES:
            raise BrsUnsupportedQueryError(f"Course ID {course_id} is not supported.")

        course_meta = COURSES[course_id]
        tenant = course_meta.get("tenant", "rosapenna")
        external_course_id = course_meta.get("external_course_id", course_id)

        headers = dict(self.headers)
        if tenant != "rosapenna":
            headers["X-Club-ID"] = tenant
            headers["Referer"] = f"https://visitors.brsgolf.com/{tenant}"

        params = urllib.parse.urlencode({"date": str(day), "course_id": external_course_id})
        url = f"{BRS_API_BASE}?{params}"
        req = urllib.request.Request(url, headers=headers, method="GET")
        try:
            opener = self.opener or urllib.request.build_opener()
            with opener.open(req, timeout=self.timeout) as response:
                status_code = getattr(response, "status", response.code)
                if status_code != 200:
                    raise BrsUpstreamError(f"BRS returned HTTP {status_code}")
                raw_bytes = response.read()
                payload = json.loads(raw_bytes.decode("utf-8"))
        except HTTPError as e:
            if e.code == 404 and tenant != "rosapenna":
                raise BrsTenantUnconfiguredError(f"BRS tenant '{tenant}' for {course_meta['name']} is unconfigured.") from e
            if e.code in (401, 403):
                raise BrsAuthError(f"BRS authentication failed with HTTP {e.code}") from e
            raise BrsUpstreamError(f"BRS returned HTTP {e.code}") from e
        except URLError as e:
            raise BrsUpstreamError(f"Failed to connect to BRS: {e.reason}") from e
        except json.JSONDecodeError as e:
            raise BrsMalformedResponseError("BRS returned invalid JSON") from e

        try:
            return validate_sheet(payload, str(day))
        except ValueError as e:
            raise BrsMalformedResponseError(str(e)) from e

    def __exit__(self, *_):
        self.opener = None
