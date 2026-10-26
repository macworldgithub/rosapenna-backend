"""Live read-only availability scraper for the Rosapenna BRS page."""

import re
from typing import List, Optional

from playwright.sync_api import Page, TimeoutError as PWTimeout, sync_playwright

from rosapenna_backend.api.schemas.availability import (
    AvailableTimesRequest, AvailableTimesResponse, PlayerOption, RateSection, TimeSlot,
)
from rosapenna_backend.config import settings
from rosapenna_backend.core.logging import logger
from rosapenna_backend.integrations.brs.booking_ui import select_calendar_date, select_time_range
from rosapenna_backend.integrations.brs.navigation import restrict_browser_navigation

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
            restrict_browser_navigation(page)

            log_msg("Loading page...")
            page.goto(req.url, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_selector("text=Choose Course", timeout=20000)

            log_msg("Selecting course...")
            course_locator = page.locator("label:visible").filter(has_text=req.course_name)
            if course_locator.count() == 0:
                course_locator = page.get_by_text(req.course_name, exact=False)
            if not safe_click(page, course_locator, "select course"):
                raise RuntimeError("Could not select the requested course")

            date_selected: Optional[bool] = None
            if req.desired_date:
                log_msg(f"Selecting date {req.desired_date}...")
                date_selected = select_calendar_date(
                    page, req.desired_date, log_msg, safe_click,
                    debug=req.debug and settings.environment.lower() != "production",
                    debug_prefix="debug_available_times",
                )
                if not date_selected:
                    raise RuntimeError("Could not select the requested date")

            if req.earliest_time or req.latest_time:
                log_msg(f"Setting time range {req.earliest_time or 'default'} - {req.latest_time or 'default'}...")
                if not select_time_range(page, req.earliest_time, req.latest_time, log_msg,
                                         debug=req.debug and settings.environment.lower() != "production"):
                    raise RuntimeError("Could not apply the requested time range")

            log_msg("Selecting holes...")
            holes_locator = page.locator("label:visible, button:visible").filter(has_text=req.holes)
            if holes_locator.count() == 0:
                holes_locator = page.get_by_text(req.holes, exact=True)
            if not safe_click(page, holes_locator, "select holes"):
                raise RuntimeError("Could not select the requested holes option")

            log_msg("Refreshing tee times...")
            if not safe_click(page, page.get_by_text("REFRESH TEE TIMES", exact=False), "refresh tee times"):
                raise RuntimeError("Could not refresh tee times")
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
