"""
Rosapenna (BRS Golf) Tee Time Booking API
------------------------------------------
A small FastAPI service that wraps the Playwright booking automation
so you can trigger a booking by POSTing a JSON body instead of editing
config variables in a script.

It deliberately stops BEFORE payment — the browser window opens on
this machine's screen, the form gets filled in automatically, and you
enter your card details and click "Pay" yourself in that window.

Setup:
    pip install fastapi "uvicorn[standard]" playwright
    playwright install chromium

Run:
    uvicorn app:app --reload --port 8000

Then, with the server running, send a request like:

    POST http://localhost:8000/bookings
    Content-Type: application/json

    {
      "course_name": "Old Tom Morris Links",
      "holes": "18 Holes",
      "desired_time": "07:30",
      "player_count": 1,
      "personal_info": {
        "first_name": "John",
        "last_name": "Doe",
        "country": "Spain",
        "email": "karimjawwad09@gmail.com",
        "telephone": "0871234567",
        "mobile": "0879876543",
        "handicap": "12",
        "club": "Royal Dublin",
        "cdh_number": "1234567",
        "special_requirements": "We would appreciate a buggy for one player if available. Thank you."
      },
      "marketing_consent": {
        "email": true,
        "sms": true,
        "post": true,
        "telephone": true
      },
      "accept_terms": true
    }

The response includes a "session_id". The browser stays open after the
form is filled — when you're done (paid, or want to abandon it), call:

    POST http://localhost:8000/bookings/{session_id}/close

Note: this runs ONE browser per request body, on whatever machine runs
`uvicorn`. It is meant for local/personal use, not as a public-facing
service — there's no auth, and every request opens a real, visible
Chromium window (booking) or a quick headless one (available-times).

To see what's bookable before committing to a time, call:

    POST http://localhost:8000/available-times
    Content-Type: application/json

    {
      "course_name": "Old Tom Morris Links",
      "holes": "18 Holes"
    }

This returns every rate section (Standard Visitor / Irish Resident)
with each available time and its player-count/price options, so the
user can pick a time before you call POST /bookings with it.
"""

import re
import threading
import uuid
from typing import Dict, List, Optional

import uvicorn
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from playwright.sync_api import sync_playwright, Page, TimeoutError as PWTimeout

# ---------------------- CONFIG DEFAULTS ----------------------
DEFAULT_URL = "https://visitors.brsgolf.com/rosapenna#/course/3"
DEBUG = True  # saves debug_<session>_<step>.png/.txt screenshots on failures

ORDINAL_WORDS = {2: "Two", 3: "Three", 4: "Four"}

MARKETING_CHECKBOX_IDS = {
    "email": "marketing-email",
    "sms": "marketing-sms",
    "post": "marketing-post",
    "telephone": "marketing-telephone",
}
TERMS_CHECKBOX_ID = "marketing-terms"

# Safety cap: if nobody calls /close, the browser auto-closes after
# this many seconds so sessions don't pile up forever.
MAX_SESSION_LIFETIME_SECONDS = 30 * 60


# ============================================================
# Request / response models
# ============================================================

class PersonalInfo(BaseModel):
    first_name: str
    last_name: str
    country: str
    email: str
    telephone: str
    mobile: Optional[str] = ""
    handicap: str
    club: str
    cdh_number: Optional[str] = ""
    special_requirements: Optional[str] = ""


class AdditionalPlayer(BaseModel):
    name: str
    handicap: str
    club: str
    cdh_number: Optional[str] = ""


class MarketingConsent(BaseModel):
    email: bool = False
    sms: bool = False
    post: bool = False
    telephone: bool = False


class BookingRequest(BaseModel):
    url: str = DEFAULT_URL
    course_name: str = "Old Tom Morris Links"   # or "Sandy Hills Links"
    holes: str = "18 Holes"                      # or "9 Holes"
    desired_day_of_month: Optional[int] = None    # e.g. 25, or None for default date
    desired_time: str = Field(..., description="Must exactly match a visible time button, e.g. '07:30'")
    player_count: int = 1
    personal_info: PersonalInfo
    additional_players: List[AdditionalPlayer] = []
    marketing_consent: MarketingConsent = MarketingConsent()
    accept_terms: bool = True
    debug: bool = True


class BookingResponse(BaseModel):
    session_id: str
    status: str           # "filled" | "error" | "running"
    message: str
    log: List[str]


class SessionStatusResponse(BaseModel):
    session_id: str
    status: str            # "running" | "filled" | "error" | "closed"
    log: List[str]
    error: Optional[str] = None


class AvailableTimesRequest(BaseModel):
    url: str = DEFAULT_URL
    course_name: str = "Old Tom Morris Links"   # or "Sandy Hills Links"
    holes: str = "18 Holes"                      # or "9 Holes"
    desired_day_of_month: Optional[int] = None    # e.g. 25, or None for default date
    headless: bool = True   # no need to show the browser just to read times
    debug: bool = False


class PlayerOption(BaseModel):
    id: str            # button id, e.g. "package-349-202606190730-1ball"
    label: str         # e.g. "1 Player - €200.00"
    player_count: Optional[int] = None
    price: Optional[str] = None


class TimeSlot(BaseModel):
    time: str                  # e.g. "07:30"
    time_button_id: Optional[str] = None
    player_options: List[PlayerOption] = []


class RateSection(BaseModel):
    rate_name: Optional[str] = None   # e.g. "1. STANDARD VISITOR - Old Tom Morris"
    times: List[TimeSlot] = []


class AvailableTimesResponse(BaseModel):
    course_name: str
    holes: str
    sections: List[RateSection]
    log: List[str]


# ============================================================
# Booking automation (ported from the standalone script)
# ============================================================

class BookingSession:
    def __init__(self, session_id: str, req: BookingRequest):
        self.session_id = session_id
        self.req = req
        self.log: List[str] = []
        self.status = "running"   # running -> filled/error -> closed
        self.error: Optional[str] = None
        self.ready_event = threading.Event()   # set once filling is done (or failed)
        self.close_event = threading.Event()   # set externally to request shutdown
        self._browser = None
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self._thread.start()

    def request_close(self):
        self.close_event.set()

    def log_msg(self, msg: str):
        print(f"[{self.session_id}] {msg}")
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
        """Reliably check a checkbox styled with Bulma's 'is-checkradio'
        class. The real <input> is visually hidden behind its <label>,
        so a plain click/check() aimed at the input can time out with a
        'pointer events intercepted' failure. Strategy: click the
        <label> first (what a real user clicks), then a forced input
        click, then a JS fallback that sets `.checked` directly and
        fires the events the framework's v-model listens for.
        """
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

    def select_date(self, page: Page, day_number: Optional[int]):
        if not day_number:
            return
        try:
            page.locator("[aria-label*='calendar' i], .calendar, input[type='text']").first.click()
            page.wait_for_timeout(500)
            page.get_by_text(str(day_number), exact=True).first.click()
            self.log_msg(f"  ok: selected day {day_number}")
        except Exception as e:
            self.log_msg(f"  ! Date picker automation failed, leaving default date: {e}")

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

        self.select_date(page, req.desired_day_of_month)

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
        self.log_msg("Form filled. Browser left open for manual payment.")

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

                # Keep the browser open (so the person can pay) until
                # /close is called, or the safety timeout elapses.
                self.close_event.wait(timeout=MAX_SESSION_LIFETIME_SECONDS)
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


# ============================================================
# Available-times scraping (read-only, no booking)
# ============================================================

# JS that walks the tee-sheet DOM and pulls out every rate section,
# every time slot, and every player-count/price option for each slot.
# Reading the DOM directly via JS (instead of Playwright locators)
# means this works regardless of whether a slot's player-count
# dropdown is currently expanded/visible on screen — the buttons and
# their ids/text exist in the DOM either way.
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
    """Loads the tee sheet, selects course/date/holes, refreshes tee
    times, and returns every available time slot across both rate
    sections (Standard Visitor / Irish Resident), with each slot's
    player-count + price options. Does not click any time or proceed
    to booking — read-only."""
    log: List[str] = []

    def log_msg(msg: str):
        print(f"[available-times] {msg}")
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

            if req.desired_day_of_month:
                try:
                    page.locator("[aria-label*='calendar' i], .calendar, input[type='text']").first.click()
                    page.wait_for_timeout(500)
                    page.get_by_text(str(req.desired_day_of_month), exact=True).first.click()
                    log_msg(f"  ok: selected day {req.desired_day_of_month}")
                except Exception as e:
                    log_msg(f"  ! Date picker automation failed, leaving default date: {e}")

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
                sections=sections,
                log=log,
            )
        finally:
            browser.close()




app = FastAPI(title="Rosapenna Tee Time Booking API")

SESSIONS: Dict[str, BookingSession] = {}
SESSIONS_LOCK = threading.Lock()


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/bookings", response_model=BookingResponse)
def create_booking(req: BookingRequest):
    session_id = str(uuid.uuid4())
    session = BookingSession(session_id, req)

    with SESSIONS_LOCK:
        SESSIONS[session_id] = session

    session.start()

    # Wait for the form-filling phase to finish (or fail). The browser
    # itself stays open after this regardless, for manual payment.
    finished_in_time = session.ready_event.wait(timeout=90)

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
        message="Form filled successfully. Browser is open — complete payment manually, "
                "then call POST /bookings/{session_id}/close when done.",
        log=session.log,
    )


@app.post("/available-times", response_model=AvailableTimesResponse)
def get_available_times(req: AvailableTimesRequest):
    """Scrape every bookable tee time (with player-count/price options)
    for the given course + holes, without booking anything. Use this
    first to show the user their choices, then pass their chosen
    `time` (and `course_name`/`holes`) into POST /bookings."""
    try:
        return scrape_available_times(req)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to fetch available times: {e}")


@app.get("/bookings/{session_id}", response_model=SessionStatusResponse)
def get_booking_status(session_id: str):
    session = SESSIONS.get(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    return SessionStatusResponse(
        session_id=session_id,
        status=session.status,
        log=session.log,
        error=session.error,
    )


@app.post("/bookings/{session_id}/close")
def close_booking(session_id: str):
    session = SESSIONS.get(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    session.request_close()
    return {"session_id": session_id, "message": "Close requested; browser will shut down shortly."}


if __name__ == "__main__":
    uvicorn.run("app:app", host="0.0.0.0", port=8000, reload=False)