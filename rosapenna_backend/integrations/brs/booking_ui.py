"""BRS date and time controls used by booking and availability flows."""

import time
from datetime import date
from typing import Optional

from playwright.sync_api import Page, TimeoutError as PWTimeout

from rosapenna_backend.config import settings
from rosapenna_backend.core.diagnostics import temp_output_path
from rosapenna_backend.core.logging import logger

MARKETING_CHECKBOX_IDS = {
    "email": "marketing-email",
    "sms": "marketing-sms",
    "post": "marketing-post",
    "telephone": "marketing-telephone",
}

TERMS_CHECKBOX_ID = "marketing-terms"

ORDINAL_WORDS = {2: "Two", 3: "Three", 4: "Four"}

MONTH_NAMES = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]

CALENDAR_INSPECT_JS = r"""
() => {
  const popup = document.querySelector('.popover-origin') || document.querySelector('.popover-container');
  if (!popup) return { error: 'no popup element found (.popover-origin / .popover-container)' };

  const MONTH_NAMES = ["January","February","March","April","May","June","July",
                        "August","September","October","November","December"];
  const monthRe = new RegExp('(' + MONTH_NAMES.join('|') + ')\\s+\\d{4}');

  const all = Array.from(popup.querySelectorAll('*'));

  let headerEl = null, headerText = null, headerChildCount = Infinity;
  for (const el of all) {
    const t = (el.textContent || '').trim();
    if (monthRe.test(t) && t.length < 40) {
      if (!headerEl || t.length < headerText.length ||
          (t.length === headerText.length && el.children.length < headerChildCount)) {
        headerEl = el;
        headerText = t;
        headerChildCount = el.children.length;
      }
    }
  }
  if (!headerEl) {
    return { error: 'no month/year header text found in popup', htmlSnippet: popup.outerHTML.slice(0, 2000) };
  }
  const hRect = headerEl.getBoundingClientRect();

  let prev = null, next = null;
  const candidates = Array.from(popup.querySelectorAll('button, [role="button"], a, [tabindex]'));
  for (const el of candidates) {
    if (el === headerEl || headerEl.contains(el) || el.contains(headerEl)) continue;
    const r = el.getBoundingClientRect();
    if (r.width === 0 || r.height === 0) continue;
    const label = [el.getAttribute('aria-label'), el.getAttribute('title'), el.textContent]
      .filter(Boolean).join(' ').trim().toLowerCase();
    const point = { x: r.left + r.width / 2, y: r.top + r.height / 2 };
    if (/\b(previous|prev|back)\b|^[<‹«←]$/.test(label)) prev = point;
    if (/\b(next|forward)\b|^[>›»→]$/.test(label)) next = point;
  }

  const headerCenterY = hRect.top + hRect.height / 2;
  for (const el of all) {
    if (el === headerEl || headerEl.contains(el) || el.contains(headerEl)) continue;
    const r = el.getBoundingClientRect();
    if (r.width === 0 || r.height === 0) continue;
    if (r.width > 80 || r.height > 80) continue;
    const centerY = r.top + r.height / 2;
    if (Math.abs(centerY - headerCenterY) > Math.max(r.height, hRect.height) / 2 + 10) continue;
    const cx = r.left + r.width / 2;
    const cy = r.top + r.height / 2;
    if (cx < hRect.left) {
      if (!prev || cx > prev.x) prev = { x: cx, y: cy };
    } else if (cx > hRect.right) {
      if (!next || cx < next.x) next = { x: cx, y: cy };
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
        path = temp_output_path(f"{filename_prefix}_calendar_state_{tag}.json")
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
