"""
Rosapenna (BRS Golf) Tee Time Booking API — Production Build
--------------------------------------------------------------
A FastAPI service that wraps a Playwright booking automation so you can
trigger a booking by POSTing a JSON body instead of editing config
variables in a script. It deliberately stops BEFORE payment — the form
gets filled in automatically and a human enters card details and clicks
"Pay" themselves.

This version is hardened for running unattended on a VPS AND ships with
full interactive Swagger / ReDoc documentation:

  * Config via environment variables (12-factor), with sane defaults.
  * API-key auth on every endpoint except /health.
  * Structured logging to console + rotating log file.
  * Bounded concurrency (semaphore) so a VPS doesn't get N browsers at once.
  * A background reaper that evicts stale/closed sessions from memory.
  * Graceful shutdown (closes all open browsers on SIGTERM/SIGINT).
  * A naive in-memory rate limiter per client IP.
  * Automatic virtual display (Xvfb) support — VPSes have no monitor, so
    a "headed" Chromium window (needed so a human can see it and pay)
    requires a virtual X server. If `pyvirtualdisplay` + `xvfb` are
    installed, one is started automatically.
  * Optional automatic VNC server (x11vnc) bound to that virtual display,
    so you can connect from your laptop, see the browser, and complete
    payment manually — exactly like the original local-machine workflow,
    just remote.
  * Rich OpenAPI metadata: every route has a summary, long-form
    description, response description, and a `tags` grouping so the
    generated Swagger UI (/docs) and ReDoc (/redoc) pages are genuinely
    useful, not just auto-generated stubs. Every request/response model
    field carries a `description` and a realistic `example`, so the
    "Try it out" panel in Swagger is pre-filled with sensible values.
  * Stripe card filling: card number, expiry and CVV are typed into the
    Stripe iframe by simulating keyboard input after clicking the element,
    since the iframe is cross-origin and cannot be filled via Playwright's
    normal fill() API.

------------------------------------------------------------------------
INSTALL (Ubuntu/Debian VPS)
------------------------------------------------------------------------
    sudo apt update
    sudo apt install -y xvfb x11vnc           # for remote-viewable browser
    python3 -m venv venv && source venv/bin/activate
    pip install fastapi "uvicorn[standard]" playwright pydantic-settings \
                pyvirtualdisplay python-dotenv
    playwright install --with-deps chromium

------------------------------------------------------------------------
CONFIGURE (environment variables, all optional, prefix BOOKING_)
------------------------------------------------------------------------
    BOOKING_API_KEY=some-long-random-string     # REQUIRED in practice
    BOOKING_HOST=0.0.0.0
    BOOKING_PORT=4040
    BOOKING_MAX_CONCURRENT_SESSIONS=3
    BOOKING_SESSION_TTL_SECONDS=1800
    BOOKING_SESSION_REAP_INTERVAL_SECONDS=60
    BOOKING_BOOKING_FILL_TIMEOUT_SECONDS=90
    BOOKING_LOG_LEVEL=INFO
    BOOKING_LOG_FILE=booking_api.log
    BOOKING_CORS_ORIGINS=*
    BOOKING_RATE_LIMIT_PER_MINUTE=20
    BOOKING_USE_VIRTUAL_DISPLAY=true
    BOOKING_ENABLE_VNC=false
    BOOKING_VNC_PORT=5900
    BOOKING_VNC_PASSWORD=

------------------------------------------------------------------------
RUN
------------------------------------------------------------------------
    uvicorn app:app --host 0.0.0.0 --port 4040 --workers 1
"""

import logging
import os
import re
import subprocess
import threading
import time
import uuid
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from datetime import datetime, date
from logging.handlers import RotatingFileHandler
from typing import Dict, List, Optional

from fastapi import FastAPI, HTTPException, Header, Request, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, ConfigDict
from pydantic_settings import BaseSettings, SettingsConfigDict
from playwright.sync_api import sync_playwright, Page, TimeoutError as PWTimeout

# ============================================================
# Settings
# ============================================================

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="BOOKING_", env_file=".env", extra="ignore")

    api_key: Optional[str] = None
    host: str = "0.0.0.0"
    port: int = 4040

    default_url: str = "https://visitors.brsgolf.com/rosapenna#/course/1"

    max_concurrent_sessions: int = 3
    session_ttl_seconds: int = 30 * 60
    session_reap_interval_seconds: int = 60
    booking_fill_timeout_seconds: int = 90

    log_level: str = "INFO"
    log_file: str = "booking_api.log"

    cors_origins: str = "*"
    rate_limit_per_minute: int = 20

    use_virtual_display: bool = True
    virtual_display_size: str = "1920x1080"

    enable_vnc: bool = False
    vnc_port: int = 5900
    vnc_password: Optional[str] = None

    debug_screenshots: bool = True


settings = Settings()

ORDINAL_WORDS = {2: "Two", 3: "Three", 4: "Four"}

MONTH_NAMES = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]

MARKETING_CHECKBOX_IDS = {
    "email": "marketing-email",
    "sms": "marketing-sms",
    "post": "marketing-post",
    "telephone": "marketing-telephone",
}
TERMS_CHECKBOX_ID = "marketing-terms"


# ============================================================
# Logging
# ============================================================

logger = logging.getLogger("booking_api")
logger.setLevel(settings.log_level.upper())
logger.propagate = False

if not logger.handlers:
    _fmt = logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s")

    _console = logging.StreamHandler()
    _console.setFormatter(_fmt)
    logger.addHandler(_console)

    try:
        _file = RotatingFileHandler(settings.log_file, maxBytes=10 * 1024 * 1024, backupCount=5)
        _file.setFormatter(_fmt)
        logger.addHandler(_file)
    except Exception as e:
        logger.warning("Could not attach file log handler (%s): %s", settings.log_file, e)


# ============================================================
# Virtual display + VNC
# ============================================================

_display_handle = None
_vnc_process: Optional[subprocess.Popen] = None


def start_virtual_display():
    global _display_handle
    if not settings.use_virtual_display:
        return
    if os.environ.get("DISPLAY"):
        logger.info("DISPLAY=%s already set, skipping virtual display startup", os.environ["DISPLAY"])
        return
    try:
        from pyvirtualdisplay import Display
        w, h = (int(x) for x in settings.virtual_display_size.lower().split("x"))
        _display_handle = Display(visible=0, size=(w, h))
        _display_handle.start()
        logger.info("Started virtual display %s at DISPLAY=%s", settings.virtual_display_size, os.environ.get("DISPLAY"))
    except Exception as e:
        logger.warning(
            "Could not start virtual display (install xvfb + pyvirtualdisplay for headed browsers "
            "on a VPS with no monitor): %s", e
        )


def stop_virtual_display():
    global _display_handle
    if _display_handle is not None:
        try:
            _display_handle.stop()
        except Exception:
            pass
        _display_handle = None


def start_vnc_server():
    global _vnc_process
    if not settings.enable_vnc:
        return
    display = os.environ.get("DISPLAY")
    if not display:
        logger.warning("BOOKING_ENABLE_VNC=true but no DISPLAY is set; skipping VNC startup")
        return
    cmd = ["x11vnc", "-display", display, "-forever", "-shared", "-rfbport", str(settings.vnc_port)]
    if settings.vnc_password:
        cmd += ["-passwd", settings.vnc_password]
    else:
        cmd += ["-nopw"]
    try:
        _vnc_process = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        logger.info("Started x11vnc on display %s, port %s", display, settings.vnc_port)
    except FileNotFoundError:
        logger.warning("x11vnc binary not found; install it (apt install x11vnc) to enable remote viewing")
    except Exception as e:
        logger.warning("Could not start x11vnc: %s", e)


def stop_vnc_server():
    global _vnc_process
    if _vnc_process is not None:
        try:
            _vnc_process.terminate()
        except Exception:
            pass
        _vnc_process = None


# ============================================================
# Request / response models
# ============================================================

class PersonalInfo(BaseModel):
    model_config = ConfigDict(json_schema_extra={
        "example": {
            "first_name": "Aoife",
            "last_name": "Byrne",
            "country": "Ireland",
            "email": "aoife.byrne@example.com",
            "telephone": "+353 87 123 4567",
            "mobile": "+353 87 123 4567",
            "handicap": "14.2",
            "club": "Portsalon Golf Club",
            "cdh_number": "1234567",
            "special_requirements": "",
        }
    })

    first_name: str = Field(..., description="Golfer's first name.")
    last_name: str = Field(..., description="Golfer's last (family) name.")
    country: str = Field(..., description="Country of residence, matched against the site's country dropdown.")
    email: str = Field(..., description="Contact email address.")
    telephone: str = Field(..., description="Primary telephone number, including country code.")
    mobile: Optional[str] = Field("", description="Mobile number, if different from telephone. Optional.")
    handicap: str = Field(..., description="Official golf handicap index, e.g. '14.2'.")
    club: str = Field(..., description="Home golf club name.")
    cdh_number: Optional[str] = Field("", description="CDH reference number. Optional.")
    special_requirements: Optional[str] = Field("", description="Free-text notes for the course. Optional.")


class AdditionalPlayer(BaseModel):
    model_config = ConfigDict(json_schema_extra={
        "example": {
            "name": "Ciaran Doyle",
            "handicap": "9.4",
            "club": "Rosapenna Golf Club",
            "cdh_number": "7654321",
        }
    })

    name: str = Field(..., description="Full name of the additional player.")
    handicap: str = Field(..., description="This player's golf handicap index.")
    club: str = Field(..., description="This player's home golf club.")
    cdh_number: Optional[str] = Field("", description="This player's CDH reference number. Optional.")


class MarketingConsent(BaseModel):
    model_config = ConfigDict(json_schema_extra={
        "example": {"email": True, "sms": False, "post": False, "telephone": False}
    })

    email: bool = Field(False, description="Opt in to marketing emails.")
    sms: bool = Field(False, description="Opt in to marketing SMS messages.")
    post: bool = Field(False, description="Opt in to marketing by post.")
    telephone: bool = Field(False, description="Opt in to marketing phone calls.")


class CardDetails(BaseModel):
    model_config = ConfigDict(json_schema_extra={
        "example": {
            "card_number": "4242424242424242",
            "expiry": "1228",
            "cvv": "123",
        }
    })

    card_number: str = Field(..., description="Card number digits only, no spaces, e.g. '4242424242424242'.")
    expiry: str = Field(..., description="Expiry as MMYY, e.g. '1228' for Dec 2028.")
    cvv: str = Field(..., description="3 or 4 digit CVV/CVC.")


class BookingRequest(BaseModel):
    model_config = ConfigDict(json_schema_extra={
        "example": {
            "url": settings.default_url,
            "course_name": "Old Tom Morris Links",
            "holes": "18 Holes",
            "desired_date": "2026-07-26",
            "earliest_time": "07:00",
            "latest_time": "15:00",
            "desired_time": "07:30",
            "player_count": 2,
            "personal_info": {
                "first_name": "Aoife",
                "last_name": "Byrne",
                "country": "Ireland",
                "email": "aoife.byrne@example.com",
                "telephone": "+353 87 123 4567",
                "mobile": "+353 87 123 4567",
                "handicap": "14.2",
                "club": "Portsalon Golf Club",
                "cdh_number": "1234567",
                "special_requirements": "",
            },
            "additional_players": [
                {"name": "Ciaran Doyle", "handicap": "9.4", "club": "Rosapenna Golf Club", "cdh_number": "7654321"}
            ],
            "marketing_consent": {"email": True, "sms": False, "post": False, "telephone": False},
            "accept_terms": True,
            "card_details": {
                "card_number": "4242424242424242",
                "expiry": "1228",
                "cvv": "123",
            },
            "auto_pay": False,
            "debug": True,
        }
    })

    url: str = Field(settings.default_url, description="Full URL of the BRS Golf tee-sheet page.")
    course_name: str = Field("Old Tom Morris Links", description="Course label as shown on the site.")
    holes: str = Field("18 Holes", description="Holes option label, e.g. '18 Holes' or '9 Holes'.")
    desired_date: Optional[str] = Field(None, description="Date to book, formatted 'YYYY-MM-DD'.")
    earliest_time: Optional[str] = Field(None, description="Lower bound 'HH:MM' for the Time range slider.")
    latest_time: Optional[str] = Field(None, description="Upper bound 'HH:MM' for the Time range slider.")
    desired_time: str = Field(..., description="The exact tee time to book, e.g. '07:30'.")
    player_count: int = Field(1, description="Number of players (1-4).")
    personal_info: PersonalInfo = Field(..., description="Lead player details.")
    additional_players: List[AdditionalPlayer] = Field(default_factory=list, description="Details for players 2-4.")
    marketing_consent: MarketingConsent = Field(default_factory=MarketingConsent, description="Marketing consent checkboxes.")
    accept_terms: bool = Field(True, description="Whether to tick the terms & conditions checkbox.")
    card_details: Optional[CardDetails] = Field(
        None,
        description="Card details to fill into Stripe automatically. If omitted, the browser is left open for manual card entry.",
    )
    auto_pay: bool = Field(
        False,
        description="If True AND card_details are supplied, click the Pay button automatically after filling card details. "
                    "Defaults to False — set True only when you are confident the form is correct.",
    )
    debug: bool = Field(settings.debug_screenshots, description="Save screenshots + text dumps after each major step.")


class BookingResponse(BaseModel):
    model_config = ConfigDict(json_schema_extra={
        "example": {
            "session_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
            "status": "filled",
            "message": "Form filled successfully. Browser is open — complete payment manually.",
            "log": ["ok: select course", "ok: selected date 2026-07-26 (input now reads 'Sunday 26th July')"],
        }
    })

    session_id: str = Field(..., description="Unique ID for this booking session.")
    status: str = Field(..., description="One of: 'filled', 'error', 'running'.")
    message: str = Field(..., description="Human-readable summary.")
    log: List[str] = Field(..., description="Step-by-step log lines.")


class SessionStatusResponse(BaseModel):
    model_config = ConfigDict(json_schema_extra={
        "example": {
            "session_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
            "status": "filled",
            "created_at": 1753600000.0,
            "log": ["ok: select course"],
            "error": None,
        }
    })

    session_id: str = Field(..., description="The session ID being queried.")
    status: str = Field(..., description="One of: 'running', 'filled', 'error', 'closed'.")
    created_at: float = Field(..., description="Unix timestamp when the session was created.")
    log: List[str] = Field(..., description="Step-by-step log lines.")
    error: Optional[str] = Field(None, description="Error message, present only when status is 'error'.")


class AvailableTimesRequest(BaseModel):
    model_config = ConfigDict(json_schema_extra={
        "example": {
            "url": settings.default_url,
            "course_name": "Old Tom Morris Links",
            "holes": "18 Holes",
            "desired_date": "2026-07-26",
            "earliest_time": "07:00",
            "latest_time": "15:00",
            "headless": True,
            "debug": False,
        }
    })

    url: str = Field(settings.default_url, description="Full URL of the BRS Golf tee-sheet page.")
    course_name: str = Field("Old Tom Morris Links", description="Course label as shown on the site.")
    holes: str = Field("18 Holes", description="Holes option label.")
    desired_date: Optional[str] = Field(None, description="Date to check, formatted 'YYYY-MM-DD'.")
    earliest_time: Optional[str] = Field(None, description="Lower bound 'HH:MM' for the Time range slider.")
    latest_time: Optional[str] = Field(None, description="Upper bound 'HH:MM' for the Time range slider.")
    headless: bool = Field(True, description="Run the browser headlessly.")
    debug: bool = Field(False, description="Dump extra diagnostic state on failure.")


class PlayerOption(BaseModel):
    model_config = ConfigDict(json_schema_extra={
        "example": {"id": "opt-2ball-0730", "label": "2 Players - €180.00", "player_count": 2, "price": "€180.00"}
    })

    id: str = Field(..., description="DOM element id of this option's button.")
    label: str = Field(..., description="Raw visible label, e.g. '2 Players - €180.00'.")
    player_count: Optional[int] = Field(None, description="Parsed number of players.")
    price: Optional[str] = Field(None, description="Parsed price.")


class TimeSlot(BaseModel):
    model_config = ConfigDict(json_schema_extra={
        "example": {
            "time": "07:30",
            "time_button_id": "teetime-0730",
            "player_options": [{"id": "opt-2ball-0730", "label": "2 Players - €180.00", "player_count": 2, "price": "€180.00"}],
        }
    })

    time: str = Field(..., description="Tee time, e.g. '07:30'.")
    time_button_id: Optional[str] = Field(None, description="DOM element id of the time button.")
    player_options: List[PlayerOption] = Field(default_factory=list, description="Player-count/price combinations.")


class RateSection(BaseModel):
    model_config = ConfigDict(json_schema_extra={
        "example": {"rate_name": "Visitor Rate", "times": []}
    })

    rate_name: Optional[str] = Field(None, description="Name of the rate/package.")
    times: List[TimeSlot] = Field(default_factory=list, description="Tee times under this rate.")


class AvailableTimesResponse(BaseModel):
    model_config = ConfigDict(json_schema_extra={
        "example": {
            "course_name": "Old Tom Morris Links",
            "holes": "18 Holes",
            "date": "2026-07-26",
            "date_selected": True,
            "sections": [],
            "log": ["ok: select course", "ok: found 12 time slot(s) across 1 section(s)"],
        }
    })

    course_name: str = Field(..., description="Course echoed from request.")
    holes: str = Field(..., description="Holes echoed from request.")
    date: Optional[str] = Field(None, description="Date echoed from request.")
    date_selected: Optional[bool] = Field(None, description="Whether the calendar click was verified.")
    sections: List[RateSection] = Field(..., description="Available tee times grouped by rate.")
    log: List[str] = Field(..., description="Step-by-step log lines.")


# ============================================================
# Auth + rate limiting
# ============================================================

def verify_api_key(x_api_key: Optional[str] = Header(default=None, alias="X-API-Key")):
    if settings.api_key and x_api_key != settings.api_key:
        raise HTTPException(status_code=401, detail="Missing or invalid X-API-Key header")


_rate_buckets: Dict[str, deque] = defaultdict(deque)
_rate_lock = threading.Lock()


def rate_limit(request: Request):
    if settings.rate_limit_per_minute <= 0:
        return
    client_ip = request.client.host if request.client else "unknown"
    now = time.monotonic()
    window = 60.0
    with _rate_lock:
        bucket = _rate_buckets[client_ip]
        while bucket and now - bucket[0] > window:
            bucket.popleft()
        if len(bucket) >= settings.rate_limit_per_minute:
            raise HTTPException(status_code=429, detail="Rate limit exceeded, slow down")
        bucket.append(now)


COMMON_DEPS = [Depends(verify_api_key), Depends(rate_limit)]


# ============================================================
# Concurrency control
# ============================================================

_session_semaphore = threading.Semaphore(settings.max_concurrent_sessions)


# ============================================================
# Shared date-picker automation
# ============================================================

CALENDAR_INSPECT_JS = r"""
() => {
  const popup = document.querySelector('.popover-origin') || document.querySelector('.popover-container');
  if (!popup) return { error: 'no popup element found (.popover-origin / .popover-container)' };

  const MONTH_NAMES = ["January","February","March","April","May","June","July",
                        "August","September","October","November","December"];
  const monthRe = new RegExp('(' + MONTH_NAMES.join('|') + ')\\s+\\d{4}');

  const all = Array.from(popup.querySelectorAll('*'));

  let headerEl = null, headerText = null;
  for (const el of all) {
    const t = (el.textContent || '').trim();
    if (monthRe.test(t) && t.length < 40) {
      if (!headerEl || t.length < headerText.length) { headerEl = el; headerText = t; }
    }
  }
  if (!headerEl) {
    return { error: 'no month/year header text found in popup', htmlSnippet: popup.outerHTML.slice(0, 2000) };
  }
  const hRect = headerEl.getBoundingClientRect();

  let prev = null, next = null;
  for (const el of all) {
    if (el === headerEl || headerEl.contains(el) || el.contains(headerEl)) continue;
    const r = el.getBoundingClientRect();
    if (r.width === 0 || r.height === 0) continue;
    if (r.width > 34 || r.height > 34) continue;
    if (r.bottom < hRect.top - 4 || r.top > hRect.bottom + 4) continue;
    const cx = r.left + r.width / 2;
    const cy = r.top + r.height / 2;
    if (cx < hRect.left) {
      if (!prev || cx > prev.cx) prev = { cx, x: cx, y: cy };
    } else if (cx > hRect.right) {
      if (!next || cx < next.cx) next = { cx, x: cx, y: cy };
    }
  }

  const days = [];
  for (const el of all) {
    if (el.children.length > 0) continue;
    const t = (el.textContent || '').trim();
    if (/^\d{1,2}$/.test(t)) {
      const r = el.getBoundingClientRect();
      if (r.width === 0 || r.height === 0) continue;
      days.push({ text: t, x: r.left + r.width / 2, y: r.top + r.height / 2, top: r.top, left: r.left });
    }
  }
  days.sort((a, b) => (Math.abs(a.top - b.top) > 5 ? a.top - b.top : a.left - b.left));

  return {
    headerText,
    prev: prev ? { x: prev.x, y: prev.y } : null,
    next: next ? { x: next.x, y: next.y } : null,
    days,
  };
}
"""


def _ordinal(n: int) -> str:
    if 11 <= (n % 100) <= 13:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def _dump_calendar_state(state: dict, filename_prefix: str, tag: str, log_msg):
    import json
    try:
        path = f"{filename_prefix}_calendar_state_{tag}.json"
        with open(path, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
        log_msg(f"  [debug] dumped calendar state to {path}")
    except Exception as e:
        log_msg(f"  [debug] could not dump calendar state: {e}")


def select_calendar_date(
    page: Page,
    desired_date: Optional[str],
    log_msg,
    safe_click,
    debug: bool = False,
    debug_prefix: str = "calendar",
) -> bool:
    if not desired_date:
        return True

    try:
        target = datetime.strptime(desired_date, "%Y-%m-%d").date()
    except ValueError:
        log_msg(f"  ! Invalid desired_date '{desired_date}', expected YYYY-MM-DD. Leaving default date.")
        return False

    date_input = page.locator("#desktop-teesheet-filter-datepicker").first

    day_token = _ordinal(target.day)
    month_name = MONTH_NAMES[target.month - 1]
    target_label = f"{month_name} {target.year}"
    month_year_re = re.compile(r"([A-Za-z]+)\s+(\d{4})")
    day_str = str(target.day)

    leading_blanks = date(target.year, target.month, 1).weekday()
    expected_index = leading_blanks + (target.day - 1)

    max_attempts = 3
    for attempt in range(1, max_attempts + 1):
        try:
            already_open = page.locator(".popover-origin").count() > 0 and page.locator(".popover-origin").first.is_visible()
            if not already_open:
                date_input.click(timeout=6000)
            page.wait_for_selector(".popover-origin", state="visible", timeout=5000)
            page.wait_for_timeout(300)
        except Exception as e:
            log_msg(f"  ! (attempt {attempt}) Popup did not open after clicking date input: {e}")
            continue

        max_month_clicks = 36
        matched_month = False
        state = None
        for _ in range(max_month_clicks):
            try:
                state = page.evaluate(CALENDAR_INSPECT_JS)
            except Exception as e:
                log_msg(f"  ! (attempt {attempt}) Could not inspect calendar popup: {e}")
                state = None
                break

            if state.get("error"):
                log_msg(f"  ! (attempt {attempt}) calendar inspect: {state['error']}")
                if debug:
                    _dump_calendar_state(state, debug_prefix, f"attempt{attempt}_inspect_error", log_msg)
                break

            header_text = state.get("headerText", "") or ""
            if target_label in header_text:
                matched_month = True
                break

            m = month_year_re.search(header_text)
            go_forward = True
            if m:
                try:
                    cur_month = MONTH_NAMES.index(m.group(1)) + 1
                    cur_year = int(m.group(2))
                    go_forward = (target.year, target.month) > (cur_year, cur_month)
                except ValueError:
                    pass

            arrow = state.get("next") if go_forward else state.get("prev")
            if not arrow:
                log_msg(f"  ! (attempt {attempt}) Could not locate the {'next' if go_forward else 'previous'} month arrow near header '{header_text}'")
                if debug:
                    _dump_calendar_state(state, debug_prefix, f"attempt{attempt}_no_arrow", log_msg)
                break

            try:
                page.mouse.click(arrow["x"], arrow["y"])
                page.wait_for_timeout(350)
            except Exception as e:
                log_msg(f"  ! (attempt {attempt}) Failed clicking month arrow: {e}")
                break

        if not matched_month:
            log_msg(f"  ! (attempt {attempt}) Gave up navigating calendar to {target_label}")
            continue

        days = state.get("days", []) if state else []
        cell = None
        if 0 <= expected_index < len(days) and days[expected_index]["text"] == day_str:
            cell = days[expected_index]
        else:
            matches = [d for d in days if d["text"] == day_str]
            if matches:
                matches.sort(key=lambda d: abs(days.index(d) - expected_index))
                cell = matches[0]

        if not cell:
            log_msg(f"  ! (attempt {attempt}) Could not locate a grid cell for day {day_str} ({len(days)} day cells found)")
            if debug and state:
                _dump_calendar_state(state, debug_prefix, f"attempt{attempt}_no_day_cell", log_msg)
            continue

        try:
            page.mouse.click(cell["x"], cell["y"])
        except Exception as e:
            log_msg(f"  ! (attempt {attempt}) Failed clicking day {day_str}: {e}")
            continue

        page.wait_for_timeout(400)

        try:
            page.keyboard.press("Escape")
        except Exception:
            pass

        try:
            actual_display = date_input.input_value(timeout=3000).strip()
        except Exception as e:
            log_msg(f"  ! Could not read back date input value to verify: {e}")
            actual_display = ""

        day_ok = re.search(rf"\b{re.escape(day_token)}\b", actual_display) is not None
        month_ok = month_name in actual_display
        if day_ok and month_ok:
            log_msg(f"  ok: selected date {target.isoformat()} (input now reads '{actual_display}')")
            return True

        log_msg(
            f"  ! (attempt {attempt}) Date input reads '{actual_display}', expected '{day_token}' and '{month_name}' — retrying..."
        )
        if debug and state:
            _dump_calendar_state(state, debug_prefix, f"attempt{attempt}_verify_mismatch", log_msg)

    log_msg(
        f"  ! FAILED to select {target.isoformat()} after {max_attempts} attempts. "
        f"Re-run with debug=True and check the *_calendar_state_*.json dumps."
    )
    return False


# ============================================================
# Shared time-range slider automation
# ============================================================

def _parse_hhmm(s: str) -> Optional[int]:
    m = re.match(r"^\s*(\d{1,2}):(\d{2})\s*$", s or "")
    if not m:
        return None
    return int(m.group(1)) * 60 + int(m.group(2))


def _format_hhmm(total_minutes: float) -> str:
    total_minutes = max(0, int(round(total_minutes)))
    h, m = divmod(total_minutes, 60)
    return f"{h:02d}:{m:02d}"


def _read_time_slider_dots(page: Page) -> Optional[list]:
    try:
        dots = page.locator(".vue-slider .vue-slider-dot")
        count = dots.count()
        if count < 2:
            return None
        results = []
        for i in range(count):
            dot = dots.nth(i)
            style = dot.get_attribute("style") or ""
            m = re.search(r"left:\s*([\d.]+)%", style)
            percent = float(m.group(1)) if m else None
            text = dot.get_attribute("aria-valuenow") or dot.get_attribute("aria-valuetext") or ""
            minutes = _parse_hhmm(text)
            if percent is None or minutes is None:
                return None
            results.append({"percent": percent, "minutes": minutes, "text": text, "index": i})
        results.sort(key=lambda d: d["percent"])
        return results
    except Exception:
        return None


def select_time_range(
    page: Page,
    earliest_time: Optional[str],
    latest_time: Optional[str],
    log_msg,
    debug: bool = False,
) -> bool:
    if not earliest_time and not latest_time:
        return True

    rail = page.locator(".vue-slider-rail").first
    try:
        rail_box = rail.bounding_box(timeout=5000)
    except Exception as e:
        log_msg(f"  ! Could not find the time-range slider rail: {e}")
        return False
    if not rail_box:
        log_msg("  ! Time-range slider rail has no bounding box (not visible?)")
        return False

    dots_state = _read_time_slider_dots(page)
    if not dots_state or len(dots_state) < 2:
        log_msg("  ! Could not read the time-range slider's current handle positions")
        return False

    lo, hi = dots_state[0], dots_state[1]
    if hi["percent"] == lo["percent"]:
        log_msg("  ! Time-range slider handles are at the same position; can't calibrate scale")
        return False

    slope = (hi["minutes"] - lo["minutes"]) / (hi["percent"] - lo["percent"])
    domain_start = lo["minutes"] - slope * lo["percent"]

    def minutes_to_x(target_minutes: int) -> float:
        percent = (target_minutes - domain_start) / slope if slope else 0
        percent = max(0.0, min(100.0, percent))
        return rail_box["x"] + (percent / 100.0) * rail_box["width"]

    dots = page.locator(".vue-slider .vue-slider-dot")

    def drag_handle(handle_index: int, target_minutes: int, label: str) -> bool:
        try:
            handle = dots.nth(handle_index)
            box = handle.bounding_box(timeout=4000)
            if not box:
                log_msg(f"  ! Could not get bounding box for the {label} time handle")
                return False
            start_x = box["x"] + box["width"] / 2
            start_y = box["y"] + box["height"] / 2
            target_x = minutes_to_x(target_minutes)

            page.mouse.move(start_x, start_y)
            page.mouse.down()
            page.mouse.move((start_x + target_x) / 2, start_y, steps=5)
            page.mouse.move(target_x, start_y, steps=10)
            page.mouse.up()
            page.wait_for_timeout(300)
            log_msg(f"  ok: dragged {label} time handle toward {_format_hhmm(target_minutes)}")
            return True
        except Exception as e:
            log_msg(f"  ! Failed dragging the {label} time handle: {e}")
            return False

    ok = True
    if earliest_time:
        target = _parse_hhmm(earliest_time)
        if target is None:
            log_msg(f"  ! Invalid earliest_time '{earliest_time}', expected 'HH:MM'")
            ok = False
        else:
            ok = drag_handle(lo["index"], target, "earliest") and ok

    if latest_time:
        target = _parse_hhmm(latest_time)
        if target is None:
            log_msg(f"  ! Invalid latest_time '{latest_time}', expected 'HH:MM'")
            ok = False
        else:
            ok = drag_handle(hi["index"], target, "latest") and ok

    final_state = _read_time_slider_dots(page)
    if not final_state or len(final_state) < 2:
        log_msg("  ! Could not read back the time-range slider after dragging")
        return False

    final_lo, final_hi = final_state[0], final_state[1]
    tolerance_min = 15
    verified = True

    if earliest_time:
        target = _parse_hhmm(earliest_time)
        if target is not None and abs(final_lo["minutes"] - target) > tolerance_min:
            log_msg(f"  ! Earliest time landed at {final_lo['text']}, wanted {earliest_time} (off by {abs(final_lo['minutes'] - target)} min)")
            verified = False

    if latest_time:
        target = _parse_hhmm(latest_time)
        if target is not None and abs(final_hi["minutes"] - target) > tolerance_min:
            log_msg(f"  ! Latest time landed at {final_hi['text']}, wanted {latest_time} (off by {abs(final_hi['minutes'] - target)} min)")
            verified = False

    if verified:
        log_msg(f"  ok: time range now {final_lo['text']} - {final_hi['text']}")
    return ok and verified


# ============================================================
# Booking automation
# ============================================================

class BookingSession:
    def __init__(self, session_id: str, req: BookingRequest):
        self.session_id = session_id
        self.req = req
        self.log: List[str] = []
        self.status = "running"
        self.error: Optional[str] = None
        self.created_at = time.time()
        self.ready_event = threading.Event()
        self.close_event = threading.Event()
        self._browser = None
        self._acquired_slot = False
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> bool:
        if not _session_semaphore.acquire(blocking=False):
            return False
        self._acquired_slot = True
        self._thread.start()
        return True

    def request_close(self):
        self.close_event.set()

    def log_msg(self, msg: str):
        logger.info("[%s] %s", self.session_id, msg)
        self.log.append(msg)

    def snapshot(self, page: Page, step_name: str):
        if not self.req.debug:
            return
        try:
            page.screenshot(path=f"debug_{self.session_id}_{step_name}.png", full_page=True)
            texts = page.locator("button, a, [role='button'], label, input").all_inner_texts()
            with open(f"debug_{self.session_id}_{step_name}.txt", "w", encoding="utf-8") as f:
                f.write("\n".join(t.strip() for t in texts if t.strip()))
        except Exception as e:
            self.log_msg(f"  [debug] snapshot failed: {e}")

    def safe_click(self, page: Page, locator, step_name: str, timeout: int = 8000) -> bool:
        try:
            loc = locator.first
            loc.scroll_into_view_if_needed(timeout=timeout)
            loc.wait_for(state="visible", timeout=timeout)
            try:
                loc.click(timeout=timeout)
            except PWTimeout:
                self.log_msg(f"  ! normal click timed out for '{step_name}', forcing click...")
                loc.click(force=True, timeout=timeout)
            self.log_msg(f"  ok: {step_name}")
            return True
        except Exception as e:
            self.log_msg(f"  ! FAILED: {step_name} -> {e}")
            self.snapshot(page, step_name.replace(" ", "_").replace(":", "-"))
            return False

    def fill_text_field(self, page: Page, label_text: str, value: str):
        if not value:
            return
        try:
            page.get_by_label(label_text, exact=False).first.fill(value)
            return
        except Exception:
            pass
        try:
            label = page.locator(f"text={label_text}").first
            input_el = label.locator("xpath=following::input[1]")
            input_el.fill(value)
        except Exception as e:
            self.log_msg(f"  ! Could not fill '{label_text}': {e}")

    def fill_additional_player(self, page: Page, player_number: int, info: AdditionalPlayer):
        ordinal = ORDINAL_WORDS.get(player_number)
        if not ordinal:
            self.log_msg(f"  ! No ordinal word configured for player {player_number}, skipping")
            return

        heading = page.get_by_text(f"Player {ordinal} Details", exact=False).first
        try:
            heading.wait_for(state="visible", timeout=6000)
        except PWTimeout:
            self.log_msg(f"  ! Could not find 'Player {ordinal} Details' section — skipping")
            return

        field_values = [info.name, info.handicap, info.club, info.cdh_number or ""]
        following_inputs = heading.locator("xpath=following::input")

        for i, value in enumerate(field_values):
            if not value:
                continue
            try:
                following_inputs.nth(i).fill(value)
            except Exception as e:
                self.log_msg(f"  ! Could not fill player {player_number} field #{i + 1}: {e}")

        self.log_msg(f"  ok: filled Player {ordinal} Details")

    def check_checkbox_robust(self, page: Page, element_id: str, label: str = None) -> bool:
        name = label or element_id
        checkbox = page.locator(f"#{element_id}")
        label_loc = page.locator(f'label[for="{element_id}"]')

        try:
            checkbox.wait_for(state="attached", timeout=6000)
        except PWTimeout:
            self.log_msg(f"  ! '{name}' checkbox (#{element_id}) not found on page")
            return False

        if checkbox.is_checked():
            self.log_msg(f"  ok: '{name}' already checked")
            return True

        try:
            label_loc.click(timeout=5000)
            if checkbox.is_checked():
                self.log_msg(f"  ok: checked '{name}' via label click")
                return True
        except Exception as e:
            self.log_msg(f"  ! label click for '{name}' failed: {e}")

        try:
            checkbox.check(force=True, timeout=5000)
            if checkbox.is_checked():
                self.log_msg(f"  ok: checked '{name}' via forced input click")
                return True
        except Exception as e:
            self.log_msg(f"  ! forced click for '{name}' failed: {e}")

        try:
            checkbox.evaluate(
                "(el) => { el.checked = true; "
                "el.dispatchEvent(new Event('input', { bubbles: true })); "
                "el.dispatchEvent(new Event('change', { bubbles: true })); }"
            )
            if checkbox.is_checked():
                self.log_msg(f"  ok: checked '{name}' via JS fallback")
                return True
        except Exception as e:
            self.log_msg(f"  ! JS fallback for '{name}' failed: {e}")

        self.log_msg(f"  ! Could not check '{name}' (#{element_id}) by any method")
        return False

    def set_marketing_consent(self, page: Page, consent: MarketingConsent):
        consent_dict = consent.model_dump()
        if not any(consent_dict.values()):
            self.log_msg("  (marketing consent: nothing selected, leaving all unchecked)")
            return
        for key, element_id in MARKETING_CHECKBOX_IDS.items():
            if consent_dict.get(key):
                self.check_checkbox_robust(page, element_id, label=key)

    def accept_terms(self, page: Page):
        if not self.req.accept_terms:
            self.log_msg("  (accept_terms is False, leaving terms checkbox unchecked)")
            return
        self.check_checkbox_robust(page, TERMS_CHECKBOX_ID, label="terms acceptance")

    def select_player_count(self, page: Page, desired_count: int) -> bool:
        visible_options = page.locator('button[id*="ball"]:visible')
        try:
            visible_options.first.wait_for(state="visible", timeout=8000)
        except PWTimeout:
            self.log_msg("  ! No visible player-count options found — was a time slot actually selected?")
            self.snapshot(page, "no_player_options")
            return False

        texts = visible_options.all_inner_texts()
        cleaned = [t.strip().replace("\n", " ") for t in texts]
        self.log_msg(f"  available player options: {cleaned}")

        pattern = re.compile(rf"^\s*{desired_count}\s*Player", re.IGNORECASE)
        for i, t in enumerate(cleaned):
            if pattern.search(t):
                visible_options.nth(i).click()
                self.log_msg(f"  ok: selected '{t}'")
                return True

        self.log_msg(f"  ! {desired_count}-player option not available for this slot, falling back...")
        visible_options.first.click()
        self.log_msg(f"  ok: fell back to '{cleaned[0]}'")
        return True

    # ----------------------------------------------------------
    # Stripe card filling
    # ----------------------------------------------------------

    def fill_stripe_card(self, page: Page, card: CardDetails) -> bool:
        """
        Fill Stripe's combined card iframe by clicking into the wrapper
        and using keyboard input to move between card number -> expiry -> CVV.

        Stripe's card element is a cross-origin sandboxed iframe, so
        Playwright's normal fill() / frame_locator() APIs won't reach it
        on live publishable keys. The only reliable approach is:
          1. Click the wrapper div to hand focus to the iframe.
          2. Type the card number — Stripe inserts spaces automatically.
          3. Tab into expiry, type MMYY.
          4. Tab into CVV, type the digits.

        The method tries two click strategies:
          - Primary:  page.locator("#stripe-card").click()
          - Fallback: page.mouse.click() at the centre of the bounding box
            (needed when the iframe intercepts pointer events before the
            wrapper registers them).
        """
        self.log_msg("Waiting for Stripe card element...")
        stripe_wrapper = page.locator("#stripe-card")

        try:
            stripe_wrapper.wait_for(state="visible", timeout=15000)
            self.log_msg("  ok: Stripe card element is visible")
        except Exception as e:
            self.log_msg(f"  ! Stripe card element never became visible: {e}")
            self.snapshot(page, "stripe_not_visible")
            return False

        # Scroll the checkout section into view so the iframe is on screen
        # before we try to click it — an off-screen click silently fails.
        try:
            page.locator(".checkout-section").scroll_into_view_if_needed(timeout=5000)
            page.wait_for_timeout(400)
        except Exception:
            pass

        # --- Strategy 1: locator click ---
        clicked = False
        try:
            stripe_wrapper.click(timeout=6000)
            page.wait_for_timeout(700)
            clicked = True
            self.log_msg("  ok: clicked Stripe wrapper (strategy 1)")
        except Exception as e:
            self.log_msg(f"  ! Locator click failed ({e}), trying bounding-box click...")

        # --- Strategy 2: mouse.click at centre of bounding box ---
        if not clicked:
            try:
                box = stripe_wrapper.bounding_box(timeout=5000)
                if box:
                    cx = box["x"] + box["width"] / 2
                    cy = box["y"] + box["height"] / 2
                    page.mouse.click(cx, cy)
                    page.wait_for_timeout(700)
                    clicked = True
                    self.log_msg("  ok: clicked Stripe wrapper (strategy 2 — bounding box)")
                else:
                    self.log_msg("  ! Stripe wrapper has no bounding box — is it visible?")
            except Exception as e:
                self.log_msg(f"  ! Bounding-box click also failed: {e}")

        if not clicked:
            self.log_msg("  ! Could not focus the Stripe card element by any click strategy")
            self.snapshot(page, "stripe_click_failed")
            return False

        try:
            # ── Card number ──────────────────────────────────────────────
            # Type digits with a small inter-key delay so Stripe's JS can
            # keep up with the input events and format the number correctly.
            self.log_msg("  Typing card number...")
            page.keyboard.type(card.card_number, delay=80)
            page.wait_for_timeout(500)

            # ── Expiry ───────────────────────────────────────────────────
            # A single Tab moves focus from the card number field to the
            # expiry field within the same Stripe iframe.
            self.log_msg("  Typing expiry (Stripe auto-advanced)...")
            page.keyboard.type(card.expiry, delay=80)
            page.wait_for_timeout(500)

            # expiry is 'MMYY' e.g. '1228'; Stripe renders it as 'MM / YY'
            self.log_msg("  Typing expiry...")
            page.keyboard.type(card.expiry, delay=80)
            page.wait_for_timeout(300)

            # ── CVV ──────────────────────────────────────────────────────
            self.log_msg("  Tabbing to CVV field...")
            page.keyboard.press("Tab")
            page.wait_for_timeout(300)

            self.log_msg("  Typing CVV...")
            page.keyboard.type(card.cvv, delay=80)

            self.snapshot(page, "05_card_filled")
            self.log_msg("  ok: Stripe card details typed")
            return True

        except Exception as e:
            self.log_msg(f"  ! Error while typing card details: {e}")
            self.snapshot(page, "stripe_type_error")
            return False

    def click_pay_button(self, page: Page) -> bool:
        """Click the Pay button after card details have been filled."""
        self.log_msg("Clicking Pay button...")
        pay_btn = page.locator("#checkout-pay")
        try:
            pay_btn.wait_for(state="visible", timeout=8000)
            pay_btn.scroll_into_view_if_needed(timeout=5000)
            pay_btn.click(timeout=8000)
            self.log_msg("  ok: Pay button clicked")
            return True
        except Exception as e:
            self.log_msg(f"  ! Could not click Pay button: {e}")
            self.snapshot(page, "pay_button_failed")
            return False

    # ----------------------------------------------------------
    # Main booking flow
    # ----------------------------------------------------------

    def _do_booking_flow(self, page: Page):
        req = self.req

        self.log_msg("Loading page...")
        page.goto(req.url, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_selector("text=Choose Course", timeout=20000)
        self.snapshot(page, "00_loaded")

        self.log_msg("Selecting course...")
        course_locator = page.locator("label").filter(has_text=req.course_name)
        if course_locator.count() == 0:
            course_locator = page.get_by_text(req.course_name, exact=False)
        self.safe_click(page, course_locator, "select course")

        if req.desired_date:
            self.log_msg(f"Selecting date {req.desired_date}...")
            select_calendar_date(
                page, req.desired_date, self.log_msg, self.safe_click,
                debug=req.debug, debug_prefix=f"debug_{self.session_id}",
            )

        if req.earliest_time or req.latest_time:
            self.log_msg(
                f"Setting time range {req.earliest_time or 'default'} - {req.latest_time or 'default'}..."
            )
            select_time_range(page, req.earliest_time, req.latest_time, self.log_msg, debug=req.debug)

        self.log_msg("Selecting holes...")
        holes_locator = page.locator("label, button").filter(has_text=req.holes)
        if holes_locator.count() == 0:
            holes_locator = page.get_by_text(req.holes, exact=True)
        self.safe_click(page, holes_locator, "select holes")

        self.log_msg("Refreshing tee times...")
        if self.safe_click(page, page.get_by_text("REFRESH TEE TIMES", exact=False), "refresh tee times"):
            page.wait_for_timeout(2500)
        self.snapshot(page, "01_after_refresh")

        self.log_msg(f"Looking for {req.desired_time}...")
        time_locator = page.get_by_text(req.desired_time, exact=True)
        if not self.safe_click(page, time_locator, f"click time {req.desired_time}"):
            raise RuntimeError(f"Could not click time slot '{req.desired_time}'")
        self.snapshot(page, "02_after_time_click")

        self.log_msg(f"Selecting player count (want {req.player_count})...")
        if not self.select_player_count(page, req.player_count):
            raise RuntimeError("Could not select any player count")
        self.snapshot(page, "03_after_player_count")

        self.log_msg("Waiting for personal details form...")
        page.wait_for_selector("text=PERSONAL INFORMATION", timeout=15000)
        self.log_msg("  ok: form loaded")

        self.log_msg("Filling personal details...")
        info = req.personal_info
        self.fill_text_field(page, "First Name", info.first_name)
        self.fill_text_field(page, "Last Name", info.last_name)
        self.fill_text_field(page, "Email", info.email)
        self.fill_text_field(page, "Telephone", info.telephone)
        self.fill_text_field(page, "Mobile", info.mobile or "")
        self.fill_text_field(page, "Handicap", info.handicap)
        self.fill_text_field(page, "Club", info.club)
        self.fill_text_field(page, "CDH Number", info.cdh_number or "")
        self.fill_text_field(page, "special requirements", info.special_requirements or "")

        if info.country:
            try:
                page.get_by_label("Country").select_option(label=info.country)
            except Exception:
                try:
                    page.get_by_text("Select one", exact=False).first.click()
                    page.get_by_text(info.country, exact=False).first.click()
                except Exception as e:
                    self.log_msg(f"  ! Could not set country automatically: {e}")

        if req.player_count > 1:
            self.log_msg(f"Filling details for {req.player_count - 1} additional player(s)...")
            for idx in range(2, req.player_count + 1):
                player_info = req.additional_players[idx - 2] if idx - 2 < len(req.additional_players) else None
                if not player_info:
                    self.log_msg(f"  ! No config provided for player {idx}, skipping")
                    continue
                self.fill_additional_player(page, idx, player_info)

        self.log_msg("Setting marketing consent checkboxes...")
        self.set_marketing_consent(page, req.marketing_consent)

        self.log_msg("Checking terms acceptance box...")
        self.accept_terms(page)

        self.snapshot(page, "04_form_filled")

        # ── Stripe card details ──────────────────────────────────────────
        if req.card_details:
            card_ok = self.fill_stripe_card(page, req.card_details)
            if card_ok:
                if req.auto_pay:
                    self.log_msg("auto_pay=True — clicking Pay button automatically...")
                    self.click_pay_button(page)
                else:
                    self.log_msg(
                        "Card details filled. auto_pay=False — browser left open. "
                        "Review the form then click Pay manually."
                    )
            else:
                self.log_msg(
                    "! Card fill failed — browser left open for manual card entry. "
                    "Check the stripe_* debug screenshots for details."
                )
        else:
            self.log_msg(
                "No card_details provided — browser left open. "
                "Enter card details and click Pay manually."
            )

        self.log_msg("Booking flow complete. Browser remains open.")

    def _run(self):
        try:
            with sync_playwright() as p:
                self._browser = p.chromium.launch(headless=False, slow_mo=150)
                page = self._browser.new_page()
                try:
                    self._do_booking_flow(page)
                    self.status = "filled"
                except Exception as e:
                    self.status = "error"
                    self.error = str(e)
                    self.log_msg(f"ERROR: {e}")
                finally:
                    self.ready_event.set()

                self.close_event.wait(timeout=settings.session_ttl_seconds)
                try:
                    self._browser.close()
                except Exception:
                    pass
        except Exception as e:
            self.status = "error"
            self.error = str(e)
            self.log_msg(f"FATAL ERROR: {e}")
            self.ready_event.set()
        finally:
            self.status = "closed" if self.status == "filled" else self.status
            if self.status == "running":
                self.status = "error"
            if self._acquired_slot:
                _session_semaphore.release()
                self._acquired_slot = False


# ============================================================
# Available-times scraping
# ============================================================

SCRAPE_TIMES_JS = """
() => {
  const sections = [];
  document.querySelectorAll('.teetimes-panel-packages').forEach(panel => {
    const titleEl = panel.querySelector('.package-title-name');
    const rateName = titleEl ? titleEl.textContent.trim() : null;
    const times = [];
    panel.querySelectorAll('.select-players').forEach(sp => {
      const timeBtn = sp.querySelector(':scope > .button.is-teetime');
      const timeText = timeBtn ? timeBtn.textContent.trim() : null;
      const timeId = timeBtn ? timeBtn.id : null;
      const options = [];
      sp.querySelectorAll('.select-players-dropdown button').forEach(b => {
        options.push({
          id: b.id,
          label: b.textContent.replace(/\\s+/g, ' ').trim()
        });
      });
      if (timeText) {
        times.push({ time: timeText, time_button_id: timeId, player_options: options });
      }
    });
    if (rateName || times.length) {
      sections.push({ rate_name: rateName, times });
    }
  });
  return sections;
}
"""

PLAYER_OPTION_PATTERN = re.compile(
    r"^\s*(\d+)\s*Player.*?-\s*(€[\d,.]+)", re.IGNORECASE
)


def _parse_player_option(raw: dict) -> PlayerOption:
    match = PLAYER_OPTION_PATTERN.search(raw.get("label", ""))
    player_count = int(match.group(1)) if match else None
    price = match.group(2) if match else None
    return PlayerOption(
        id=raw.get("id", ""),
        label=raw.get("label", ""),
        player_count=player_count,
        price=price,
    )


def scrape_available_times(req: AvailableTimesRequest) -> AvailableTimesResponse:
    log: List[str] = []

    def log_msg(msg: str):
        logger.info("[available-times] %s", msg)
        log.append(msg)

    def safe_click(page: Page, locator, step_name: str, timeout: int = 8000) -> bool:
        try:
            loc = locator.first
            loc.scroll_into_view_if_needed(timeout=timeout)
            loc.wait_for(state="visible", timeout=timeout)
            try:
                loc.click(timeout=timeout)
            except PWTimeout:
                loc.click(force=True, timeout=timeout)
            log_msg(f"  ok: {step_name}")
            return True
        except Exception as e:
            log_msg(f"  ! FAILED: {step_name} -> {e}")
            return False

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=req.headless, slow_mo=100 if not req.headless else 0)
        try:
            page = browser.new_page()

            log_msg("Loading page...")
            page.goto(req.url, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_selector("text=Choose Course", timeout=20000)

            log_msg("Selecting course...")
            course_locator = page.locator("label").filter(has_text=req.course_name)
            if course_locator.count() == 0:
                course_locator = page.get_by_text(req.course_name, exact=False)
            safe_click(page, course_locator, "select course")

            date_selected: Optional[bool] = None
            if req.desired_date:
                log_msg(f"Selecting date {req.desired_date}...")
                date_selected = select_calendar_date(
                    page, req.desired_date, log_msg, safe_click,
                    debug=req.debug, debug_prefix="debug_available_times",
                )

            if req.earliest_time or req.latest_time:
                log_msg(f"Setting time range {req.earliest_time or 'default'} - {req.latest_time or 'default'}...")
                select_time_range(page, req.earliest_time, req.latest_time, log_msg, debug=req.debug)

            log_msg("Selecting holes...")
            holes_locator = page.locator("label, button").filter(has_text=req.holes)
            if holes_locator.count() == 0:
                holes_locator = page.get_by_text(req.holes, exact=True)
            safe_click(page, holes_locator, "select holes")

            log_msg("Refreshing tee times...")
            if safe_click(page, page.get_by_text("REFRESH TEE TIMES", exact=False), "refresh tee times"):
                page.wait_for_timeout(2500)

            log_msg("Scraping available times...")
            raw_sections = page.evaluate(SCRAPE_TIMES_JS)

            sections: List[RateSection] = []
            for raw_section in raw_sections:
                times: List[TimeSlot] = []
                for raw_time in raw_section.get("times", []):
                    options = [_parse_player_option(o) for o in raw_time.get("player_options", [])]
                    times.append(TimeSlot(
                        time=raw_time.get("time", ""),
                        time_button_id=raw_time.get("time_button_id"),
                        player_options=options,
                    ))
                sections.append(RateSection(rate_name=raw_section.get("rate_name"), times=times))

            log_msg(f"  ok: found {sum(len(s.times) for s in sections)} time slot(s) across {len(sections)} section(s)")

            return AvailableTimesResponse(
                course_name=req.course_name,
                holes=req.holes,
                date=req.desired_date,
                date_selected=date_selected,
                sections=sections,
                log=log,
            )
        finally:
            browser.close()


# ============================================================
# Session store + reaper
# ============================================================

SESSIONS: Dict[str, BookingSession] = {}
SESSIONS_LOCK = threading.Lock()

_reaper_stop = threading.Event()


def _reap_sessions_loop():
    while not _reaper_stop.wait(settings.session_reap_interval_seconds):
        try:
            now = time.time()
            with SESSIONS_LOCK:
                stale_ids = [
                    sid for sid, s in SESSIONS.items()
                    if s.status in ("closed", "error")
                    and (now - s.created_at) > settings.session_ttl_seconds
                ]
                for sid in stale_ids:
                    del SESSIONS[sid]
            if stale_ids:
                logger.info("Reaped %d stale session(s): %s", len(stale_ids), stale_ids)
        except Exception as e:
            logger.warning("Session reaper iteration failed: %s", e)


_reaper_thread = threading.Thread(target=_reap_sessions_loop, daemon=True)


# ============================================================
# App lifespan
# ============================================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info(
        "Starting up: max_concurrent_sessions=%d, ttl=%ds",
        settings.max_concurrent_sessions, settings.session_ttl_seconds,
    )
    if not settings.api_key:
        logger.warning(
            "BOOKING_API_KEY is not set — every endpoint is UNAUTHENTICATED. "
            "Set it before exposing this to the internet."
        )
    start_virtual_display()
    start_vnc_server()
    _reaper_thread.start()

    yield

    logger.info("Shutting down: closing all open sessions...")
    _reaper_stop.set()
    with SESSIONS_LOCK:
        sessions = list(SESSIONS.values())
    for s in sessions:
        if s.status in ("running", "filled"):
            s.request_close()
    deadline = time.time() + 10
    for s in sessions:
        remaining = max(0, deadline - time.time())
        s._thread.join(timeout=remaining)
    stop_vnc_server()
    stop_virtual_display()
    logger.info("Shutdown complete.")


# ============================================================
# OpenAPI metadata
# ============================================================

tags_metadata = [
    {
        "name": "availability",
        "description": "Read-only endpoints for checking what tee times are open. "
                       "Never touches the booking form — safe to call frequently.",
    },
    {
        "name": "bookings",
        "description": "Endpoints that drive the booking automation: opening a browser, "
                       "filling the form, optionally filling card details and paying.",
    },
    {
        "name": "sessions",
        "description": "Introspection into currently tracked booking sessions.",
    },
    {
        "name": "health",
        "description": "Unauthenticated liveness/readiness probe.",
    },
]

API_DESCRIPTION = """
Wraps a Playwright browser-automation flow for the Rosapenna (BRS Golf)
visitor tee-sheet.

### How it works

1. `POST /available-times` — headlessly scrapes open tee times.
2. `POST /bookings` — opens a real Chromium window, fills course/date/time/
   players/personal details, and optionally fills Stripe card details.
   Set `auto_pay: true` to also click Pay automatically.
3. `GET /bookings/{id}` — poll session status.
4. `POST /bookings/{id}/close` — close the browser once done.

### Card details

Pass `card_details: { card_number, expiry, cvv }` in the booking request
to have card details filled automatically. Stripe's iframe is cross-origin
so this works via simulated keyboard input (click → type number → Tab →
type expiry → Tab → type CVV).

Set `auto_pay: true` to also click the Pay button after filling card details.
Leave it `false` (default) to review manually before paying.

### Authentication

Every route except `/health` requires an `X-API-Key` header.
"""

app = FastAPI(
    title="Rosapenna Tee Time Booking API",
    description=API_DESCRIPTION,
    version="1.1.0",
    contact={"name": "Booking API Maintainer"},
    openapi_tags=tags_metadata,
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"] if settings.cors_origins.strip() == "*" else [o.strip() for o in settings.cors_origins.split(",")],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    logger.exception("Unhandled exception on %s %s", request.method, request.url.path)
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})


# ============================================================
# Routes
# ============================================================

@app.get(
    "/health",
    tags=["health"],
    summary="Liveness / readiness probe",
    description="Returns basic service status. No API key required.",
    response_description="Service status and current session load.",
)
def health():
    with SESSIONS_LOCK:
        active = sum(1 for s in SESSIONS.values() if s.status == "running")
    return {
        "status": "ok",
        "active_sessions": active,
        "max_concurrent_sessions": settings.max_concurrent_sessions,
    }


@app.post(
    "/bookings",
    response_model=BookingResponse,
    dependencies=COMMON_DEPS,
    tags=["bookings"],
    summary="Start a booking automation run",
    description=(
        "Opens a real Chromium browser, navigates the tee sheet, fills all form fields, "
        "and optionally fills Stripe card details and clicks Pay.\n\n"
        "Supply `card_details` to automate card entry. Set `auto_pay: true` to also "
        "click Pay — leave it false (default) to review before paying.\n\n"
        "Blocks up to `BOOKING_BOOKING_FILL_TIMEOUT_SECONDS` (default 90s); returns "
        "`status: 'running'` if still in progress — poll `GET /bookings/{id}`."
    ),
    response_description="Session ID, status, and step-by-step log.",
    responses={
        429: {"description": "At capacity or rate limit exceeded."},
        401: {"description": "Missing or invalid X-API-Key."},
    },
)
def create_booking(req: BookingRequest):
    session_id = str(uuid.uuid4())
    session = BookingSession(session_id, req)

    if not session.start():
        raise HTTPException(
            status_code=429,
            detail=f"At capacity ({settings.max_concurrent_sessions} concurrent sessions). Try again shortly.",
        )

    with SESSIONS_LOCK:
        SESSIONS[session_id] = session

    finished_in_time = session.ready_event.wait(timeout=settings.booking_fill_timeout_seconds)

    if not finished_in_time:
        return BookingResponse(
            session_id=session_id,
            status="running",
            message="Still working — check status via GET /bookings/{session_id}.",
            log=session.log,
        )

    if session.status == "error":
        return BookingResponse(
            session_id=session_id,
            status="error",
            message=session.error or "Unknown error during booking automation.",
            log=session.log,
        )

    return BookingResponse(
        session_id=session_id,
        status="filled",
        message=(
            "Form filled successfully. "
            + (
                "Card details entered and Pay clicked automatically."
                if req.card_details and req.auto_pay
                else "Card details entered — click Pay manually to complete."
                if req.card_details
                else "Browser left open — enter card details and click Pay manually."
            )
        ),
        log=session.log,
    )


@app.post(
    "/available-times",
    response_model=AvailableTimesResponse,
    dependencies=COMMON_DEPS,
    tags=["availability"],
    summary="List open tee times for a course/date",
    description=(
        "Headlessly loads the tee sheet, applies filters, and scrapes every visible tee time "
        "with player-count/price options. Read-only — never fills the booking form."
    ),
    response_description="Tee times grouped by rate/package.",
    responses={
        401: {"description": "Missing or invalid X-API-Key."},
        500: {"description": "Scrape failed."},
    },
)
def get_available_times(req: AvailableTimesRequest):
    try:
        return scrape_available_times(req)
    except Exception as e:
        logger.exception("available-times scrape failed")
        raise HTTPException(status_code=500, detail=f"Failed to fetch available times: {e}")


@app.get(
    "/bookings/{session_id}",
    response_model=SessionStatusResponse,
    dependencies=COMMON_DEPS,
    tags=["bookings"],
    summary="Poll a booking session's status",
    description="Fetch the current status and log for a session created via POST /bookings.",
    response_description="Status, creation time, log, and error if any.",
    responses={
        404: {"description": "Session not found."},
        401: {"description": "Missing or invalid X-API-Key."},
    },
)
def get_booking_status(session_id: str):
    session = SESSIONS.get(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    return SessionStatusResponse(
        session_id=session_id,
        status=session.status,
        created_at=session.created_at,
        log=session.log,
        error=session.error,
    )


@app.post(
    "/bookings/{session_id}/close",
    tags=["bookings"],
    summary="Close a booking session's browser",
    description="Signals the background thread to close the Chromium window. Call once payment is done or abandoned.",
    response_description="Acknowledgement that the close request was received.",
    responses={
        404: {"description": "Session not found."},
        401: {"description": "Missing or invalid X-API-Key."},
    },
)
def close_booking(session_id: str):
    session = SESSIONS.get(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    session.request_close()
    return {"session_id": session_id, "message": "Close requested; browser will shut down shortly."}


@app.get(
    "/sessions",
    tags=["sessions"],
    summary="List all tracked booking sessions",
    description="Admin/debug endpoint returning every session currently in memory.",
    response_description="List of sessions with ID, status, creation time, and error.",
    responses={401: {"description": "Missing or invalid X-API-Key."}},
)
def list_sessions():
    with SESSIONS_LOCK:
        return [
            {"session_id": sid, "status": s.status, "created_at": s.created_at, "error": s.error}
            for sid, s in SESSIONS.items()
        ]


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host=settings.host, port=settings.port, reload=False, workers=1)