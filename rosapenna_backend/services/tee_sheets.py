"""MongoDB-backed tee-sheet cache and daily refresh scheduler."""

import threading
from datetime import date, datetime, time, timedelta, timezone

from pymongo.errors import DuplicateKeyError, PyMongoError

from rosapenna_backend.api.schemas.availability import QuoteSelection
from rosapenna_backend.core.logging import logger
from rosapenna_backend.integrations.brs.tee_sheet import BrsReader, COURSES, DUBLIN, amount

class TeesheetCache:
    def __init__(self, database, days=30, refresh_hour=5, reader_factory=BrsReader, now=None):
        self.sheets = database["tee_sheets"]
        self.runs = database["tee_sheet_refresh_runs"]
        self.days = days
        self.refresh_hour = refresh_hour
        self.reader_factory = reader_factory
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.stop = threading.Event()
        self.thread = None
        self.live_lock = threading.Lock()
        self.sheets.create_index([("day", 1), ("course_id", 1)], unique=True,
                                 name="day_course_unique")

    def window(self):
        start = self.now().astimezone(DUBLIN).date()
        return start, start + timedelta(days=self.days - 1)

    def check_selection(self, day, course_id):
        start, end = self.window()
        if course_id not in COURSES:
            raise ValueError("Unsupported course")
        if not start <= day <= end:
            raise ValueError(f"Choose a date between {start} and {end}")

    def save(self, day, course_id, data):
        key = {"day": str(day), "course_id": course_id}
        # A deterministic _id also makes simultaneous first writes converge.
        values = {**key, "data": data, "fetched_at": self.now()}
        try:
            self.sheets.update_one(key, {"$set": values, "$setOnInsert": {
                "_id": f"{day}:{course_id}",
            }}, upsert=True)
        except DuplicateKeyError:
            # Another request inserted this course/day while the upsert was running.
            self.sheets.update_one(key, {"$set": values})

    def status(self):
        last = self.runs.find_one({}, {"_id": 0}, sort=[("day", -1)])
        if last:
            for field in ("started_at", "finished_at"):
                if last.get(field) is not None:
                    last[field] = last[field].isoformat()
        start, end = self.window()
        return {"window_start": str(start), "window_end": str(end),
                "refresh_hour": self.refresh_hour, "timezone": "Europe/Dublin",
                "storage": "mongodb", "last_run": last}

    def get(self, day, course_id):
        self.check_selection(day, course_id)
        row = self.sheets.find_one({"day": str(day), "course_id": course_id})
        if row is None:
            return None
        local_now = self.now().astimezone(DUBLIN)
        due = datetime.combine(local_now.date(), time(self.refresh_hour), DUBLIN)
        if local_now < due:
            due -= timedelta(days=1)
        fetched = row["fetched_at"]
        return {"data": row["data"], "cache": {
            "fetched_at": fetched.isoformat(),
            "stale": fetched < due and fetched.astimezone(DUBLIN).date() != local_now.date(),
            **self.status(),
        }}

    def refresh(self, retry_failed=False):
        """Persist the daily claim before network access, including across restarts."""
        today, end = self.window()
        run = {"day": str(today), "started_at": self.now(), "finished_at": None,
               "status": "running", "saved": 0, "errors": []}
        try:
            self.runs.insert_one({"_id": str(today), **run})
            claimed = True
        except DuplicateKeyError:
            claimed = False
            if retry_failed:
                claimed = self.runs.update_one(
                    {"_id": str(today), "status": {"$in": ["failed", "partial"]}},
                    {"$set": run},
                ).modified_count == 1
        if not claimed:
            return self.status()
        saved, errors = 0, []
        try:
            with self.reader_factory() as reader:
                for offset in range(self.days):
                    for course_id in COURSES:
                        if self.stop.is_set():
                            raise RuntimeError("Refresh interrupted by shutdown")
                        day = str(today + timedelta(days=offset))
                        try:
                            data = reader.fetch(day, course_id)
                            self.save(day, course_id, data)
                            saved += 1
                        except Exception:
                            logger.exception("Daily BRS refresh failed: %s course %s", day, course_id)
                            errors.append(f"Could not update {day}, course {course_id}")
                        # Sequential requests, paced to avoid a burst against BRS.
                        self.stop.wait(0.25)
        except Exception:
            logger.exception("Daily BRS refresh interrupted")
            errors.append("Daily BRS connection failed or refresh was interrupted")
        finally:
            self.runs.update_one({"_id": str(today)}, {"$set": {
                "finished_at": self.now(),
                "status": "partial" if errors and saved else "failed" if errors else "complete",
                "saved": saved, "errors": errors,
            }})
            self.sheets.delete_many({"$or": [{"day": {"$lt": str(today)}}, {"day": {"$gt": str(end)}}]})
            self.runs.delete_many({"_id": {"$lt": str(today - timedelta(days=90))}})
        return self.status()

    def revalidate(self, selection):
        self.check_selection(selection.date, selection.course_id)
        amount(selection.price)
        # Only one extra live-check browser per process; browsing never takes this lock.
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
            return {"valid": False, "reason": f"The price has changed to {data['currency_code']} {current}. Please return to tee times and select the updated price."}
        return {"valid": True, "price": current, "currency": data["currency_code"],
                "teeTime": tee, "greenFee": fee, "course": COURSES[selection.course_id],
                "checked_at": self.now().isoformat()}

    def start(self):
        self.stop.clear()

        def run():
            handled_day = None
            while not self.stop.is_set():
                try:
                    local_now = self.now().astimezone(DUBLIN)
                    if local_now.hour >= self.refresh_hour and handled_day != local_now.date():
                        self.refresh()
                        handled_day = local_now.date()
                except PyMongoError as exc:
                    logger.warning("Daily BRS scheduler MongoDB operation failed (%s); retrying in 60 seconds",
                                   type(exc).__name__)
                except Exception:
                    logger.exception("Daily BRS scheduler failed")
                self.stop.wait(60)

        self.thread = threading.Thread(target=run, name="brs-daily-refresh", daemon=True)
        self.thread.start()

    def close(self):
        self.stop.set()
        if self.thread:
            self.thread.join(timeout=95)
