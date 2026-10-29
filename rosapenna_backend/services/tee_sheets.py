"""Real-time in-memory tee-sheet cache with short TTL."""

from decimal import Decimal
import threading
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from rosapenna_backend.api.schemas.availability import (
    BrsCourseAvailabilityResponse,
    BrsSlotRate,
    BrsTeeSlot,
    QuoteSelection,
)
from rosapenna_backend.core.logging import logger
from rosapenna_backend.integrations.brs.tee_sheet import (
    BrsIntegrationError,
    BrsMalformedResponseError,
    BrsReader,
    BrsTenantUnconfiguredError,
    BrsUnsupportedQueryError,
    COURSES,
    DUBLIN,
    amount,
)


class TeesheetCache:
    """Real-time in-memory cache for BRS tee sheets with a 60-second TTL."""

    def __init__(
        self,
        database=None,
        days=30,
        refresh_hour=5,
        reader_factory=BrsReader,
        now=None,
        ttl_seconds=60,
    ):
        self.days = days
        self.refresh_hour = refresh_hour
        self.reader_factory = reader_factory
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.ttl_seconds = ttl_seconds
        self.stop = threading.Event()
        self.live_lock = threading.Lock()
        self._cache_lock = threading.Lock()
        self._memory_cache = {}  # key: (day_str, course_id) -> {"data": data, "fetched_at": datetime}

        if database is not None:
            self.sheets = database["tee_sheets"]
            self.runs = database["tee_sheet_refresh_runs"]
            self.sheets.create_index(
                [("day", 1), ("course_id", 1)], unique=True, name="day_course_unique"
            )
        else:
            self.sheets = None
            self.runs = None

    def window(self):
        start = self.now().astimezone(DUBLIN).date()
        return start, start + timedelta(days=self.days - 1)

    def check_selection(self, day, course_id):
        start, end = self.window()
        if course_id not in COURSES:
            raise BrsUnsupportedQueryError(f"Unsupported course ID {course_id}. Supported: {list(COURSES.keys())}")
        if not start <= day <= end:
            raise BrsUnsupportedQueryError(f"Choose a date between {start} and {end}")

    def save(self, day, course_id, data):
        """Save a sheet into the in-memory cache and database if available."""
        key = (str(day), course_id)
        now_dt = self.now()
        with self._cache_lock:
            self._memory_cache[key] = {"data": data, "fetched_at": now_dt}
        if self.sheets is not None:
            db_key = {"day": str(day), "course_id": course_id}
            values = {**db_key, "data": data, "fetched_at": now_dt}
            try:
                self.sheets.update_one(
                    db_key,
                    {"$set": values, "$setOnInsert": {"_id": f"{day}:{course_id}"}},
                    upsert=True,
                )
            except DuplicateKeyError:
                self.sheets.update_one(db_key, {"$set": values})

    def status(self):
        start, end = self.window()
        return {
            "window_start": str(start),
            "window_end": str(end),
            "storage": "in_memory",
            "ttl_seconds": self.ttl_seconds,
            "timezone": "Europe/Dublin",
            "cached_entries": len(self._memory_cache),
            "last_run": None,
        }

    def get(self, day, course_id):
        """Return cached sheet if fresh within ttl_seconds; otherwise fetch live from BRS."""
        self.check_selection(day, course_id)
        key = (str(day), course_id)
        now_dt = self.now()

        with self._cache_lock:
            entry = self._memory_cache.get(key)
            if entry and (now_dt - entry["fetched_at"]).total_seconds() < self.ttl_seconds:
                cached_data = entry["data"]
                fetched_at = entry["fetched_at"]
                start, end = self.window()
                return {
                    "data": cached_data,
                    "cache": {
                        "fetched_at": fetched_at.isoformat(),
                        "stale": False,
                        "live": True,
                        "storage": "in_memory",
                        "ttl_seconds": self.ttl_seconds,
                        "window_start": str(start),
                        "window_end": str(end),
                        "timezone": "Europe/Dublin",
                        "last_run": None,
                    },
                }

        # Not in cache or expired: fetch live from BRS
        with self.reader_factory() as reader:
            data = reader.fetch(str(day), course_id)

        fetched_at = self.now()
        with self._cache_lock:
            self._memory_cache[key] = {"data": data, "fetched_at": fetched_at}

        start, end = self.window()
        return {
            "data": data,
            "cache": {
                "fetched_at": fetched_at.isoformat(),
                "stale": False,
                "live": True,
                "storage": "in_memory",
                "ttl_seconds": self.ttl_seconds,
                "window_start": str(start),
                "window_end": str(end),
                "timezone": "Europe/Dublin",
                "last_run": None,
            },
        }

    def revalidate(self, selection):
        self.check_selection(selection.date, selection.course_id)
        amount(selection.price)
        if not self.live_lock.acquire(blocking=False):
            raise BlockingIOError("A booking check is in progress. Please try again shortly.")
        try:
            with self.reader_factory() as reader:
                data = reader.fetch(str(selection.date), selection.course_id)
            self.save(str(selection.date), selection.course_id, data)
        finally:
            self.live_lock.release()

        tee = next((t for t in data["tee_times"] if t["id"] == selection.tee_time_id), None)
        if not tee or sum(s["status"] == "Available" for s in tee["slots"].values()) < selection.players:
            return {"valid": False, "reason": "This tee time is no longer available. Please choose another time."}
        fee = next((f for f in tee["green_fees"] if f["id"] == selection.green_fee_id), None)
        current = fee.get(f"green_fee{selection.players}_ball") if fee else None
        if current is None:
            return {"valid": False, "reason": "This rate is no longer available. Please select a rate again."}
        if data["currency_code"] != selection.currency or amount(current) != amount(selection.price):
            return {
                "valid": False,
                "reason": (
                    f"The price has changed to {data['currency_code']} {current}. "
                    "Please return to tee times and select the updated price."
                ),
            }
        return {
            "valid": True,
            "price": current,
            "currency": data["currency_code"],
            "teeTime": tee,
            "greenFee": fee,
            "course": COURSES[selection.course_id],
            "checked_at": self.now().isoformat(),
        }

    def refresh(self, retry_failed=False):
        """No-op for in-memory real-time cache; returns current status."""
        return self.status()

    def start(self):
        """No background polling needed for real-time in-memory cache."""
        pass

    def close(self):
        """Clear the in-memory cache upon shutdown."""
        self.stop.set()
        with self._cache_lock:
            self._memory_cache.clear()

    def get_course_availability(
        self,
        day: date,
        course_id: int,
        players: Optional[int] = None,
        time_from: Optional[str] = None,
        time_to: Optional[str] = None,
    ) -> BrsCourseAvailabilityResponse:
        """Query and filter tee-sheet slots and rates across 3 courses with explicit status."""
        self.check_selection(day, course_id)
        course_meta = COURSES[course_id]

        if players is not None and not (1 <= players <= 4):
            raise BrsUnsupportedQueryError("Player count must be between 1 and 4.")

        if time_from and time_to and time_to <= time_from:
            raise BrsUnsupportedQueryError("time_to must be strictly after time_from.")

        try:
            sheet_result = self.get(day, course_id)
            raw_data = sheet_result["data"]
            source = "brs_api" if sheet_result["cache"].get("live") else "cached_snapshot"
            retrieved_at = datetime.fromisoformat(sheet_result["cache"]["fetched_at"])
        except BrsTenantUnconfiguredError:
            # Explicitly return unconfigured tenant status without treating it as open slots
            return BrsCourseAvailabilityResponse(
                course_id=course_id,
                course_name=course_meta["name"],
                tenant=course_meta.get("tenant", "stpatricks"),
                date=day,
                status="unconfigured_tenant",
                available_slots_count=0,
                slots=[],
                retrieved_at=self.now(),
                freshness_ttl_seconds=self.ttl_seconds,
                reserves_inventory=False,
                source="brs_api",
            )

        slots: list[BrsTeeSlot] = []
        currency = raw_data.get("currency_code", "EUR")

        for tee in raw_data.get("tee_times", []):
            time_str = tee.get("time", "")
            if not time_str:
                continue

            if time_from and time_str < time_from:
                continue
            if time_to and time_str > time_to:
                continue

            slot_dict = tee.get("slots", {})
            available_capacity = sum(1 for s in slot_dict.values() if s.get("status") == "Available")

            if players is not None and available_capacity < players:
                continue

            rates: list[BrsSlotRate] = []
            for gf in tee.get("green_fees", []):
                p_count = players if players is not None else 1
                price_val = gf.get(f"green_fee{p_count}_ball") or gf.get("green_fee1_ball")
                if price_val is not None:
                    try:
                        norm_price = f"{Decimal(str(price_val)):.2f}"
                        unit_price = f"{(Decimal(str(price_val)) / p_count):.2f}"
                    except Exception:
                        norm_price = str(price_val)
                        unit_price = str(price_val)

                    rates.append(BrsSlotRate(
                        rate_id=gf.get("id", 0),
                        package_name=gf.get("package_name") or gf.get("type") or "Standard Visitor",
                        players=p_count,
                        price_per_player=unit_price,
                        total_price=norm_price,
                        currency=currency,
                        num_holes=str(gf.get("num_holes", "18")),
                        type=gf.get("type", "Visitor"),
                    ))

            inventory_id = f"BRS-{course_id}-{day.strftime('%Y%m%d')}-{time_str.replace(':', '')}-{tee.get('id', 0)}"

            slots.append(BrsTeeSlot(
                slot_id=tee.get("id", 0),
                time=time_str,
                holes=str(tee.get("holes", "18")),
                available_capacity=available_capacity,
                is_available=available_capacity > 0,
                rates=rates,
                inventory_id=inventory_id,
            ))

        status = "available" if slots else "no_availability"

        return BrsCourseAvailabilityResponse(
            course_id=course_id,
            course_name=course_meta["name"],
            tenant=course_meta.get("tenant", "rosapenna"),
            date=day,
            status=status,
            available_slots_count=len(slots),
            slots=slots,
            retrieved_at=retrieved_at,
            freshness_ttl_seconds=self.ttl_seconds,
            reserves_inventory=False,
            source=source,
        )
