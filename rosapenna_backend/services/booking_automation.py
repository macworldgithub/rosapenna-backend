"""Playwright workflow that fills the Rosapenna booking form."""

import threading
import time
from typing import List, Optional

from playwright.sync_api import Page, TimeoutError as PWTimeout, sync_playwright

from rosapenna_backend.api.schemas.bookings import AdditionalPlayer, BookingRequest, CardDetails, MarketingConsent
from rosapenna_backend.config import settings
from rosapenna_backend.core.diagnostics import temp_output_path
from rosapenna_backend.core.logging import logger
from rosapenna_backend.integrations.brs.booking_ui import (
    MARKETING_CHECKBOX_IDS, ORDINAL_WORDS, TERMS_CHECKBOX_ID, _ordinal, select_calendar_date, select_time_range,
)
from rosapenna_backend.integrations.brs.navigation import restrict_browser_navigation

class BookingSession:
    def __init__(self, session_id: str, req: BookingRequest, semaphore: threading.Semaphore):
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
        self._semaphore = semaphore
        self._sensitive_data_entered = False
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> bool:
        if not self._semaphore.acquire(blocking=False):
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
        # Screenshots and text dumps can capture guest or payment details.
        # Keep diagnostics to the pre-form part of the flow only.
        if (
            not self.req.debug
            or settings.environment.lower() == "production"
            or self._sensitive_data_entered
        ):
            return
        try:
            screenshot_path = temp_output_path(f"debug_{self.session_id}_{step_name}.png")
            page.screenshot(path=str(screenshot_path), full_page=True)
            texts = page.locator("button, a, [role='button'], label, input").all_inner_texts()
            with open(temp_output_path(f"debug_{self.session_id}_{step_name}.txt"), "w", encoding="utf-8") as f:
                f.write("\n".join(t.strip() for t in texts if t.strip()))
        except Exception as exc:
            self.log_msg(f"  [debug] snapshot failed ({type(exc).__name__})")

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
        except Exception as exc:
            self.log_msg(f"  ! Could not fill '{label_text}' ({type(exc).__name__})")

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
            except Exception as exc:
                self.log_msg(
                    f"  ! Could not fill player {player_number} field #{i + 1} ({type(exc).__name__})"
                )

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
        except Exception as exc:
            self.log_msg(f"  ! Stripe card element never became visible ({type(exc).__name__})")
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
        except Exception as exc:
            self.log_msg(f"  ! Locator click failed ({type(exc).__name__}), trying bounding-box click...")

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
            except Exception as exc:
                self.log_msg(f"  ! Bounding-box click also failed ({type(exc).__name__})")

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

        except Exception as exc:
            self.log_msg(f"  ! Error while typing card details ({type(exc).__name__})")
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
        except Exception as exc:
            self.log_msg(f"  ! Could not click Pay button ({type(exc).__name__})")
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
        course_locator = page.locator("label:visible").filter(has_text=req.course_name)
        if course_locator.count() == 0:
            course_locator = page.get_by_text(req.course_name, exact=False)
        if not self.safe_click(page, course_locator, "select course"):
            raise RuntimeError("Could not select the requested course")

        if req.desired_date:
            self.log_msg(f"Selecting date {req.desired_date}...")
            if not select_calendar_date(
                page, req.desired_date, self.log_msg, self.safe_click,
                debug=req.debug and settings.environment.lower() != "production",
                debug_prefix=f"debug_{self.session_id}",
            ):
                raise RuntimeError("Could not select the requested date")

        if req.earliest_time or req.latest_time:
            self.log_msg(
                f"Setting time range {req.earliest_time or 'default'} - {req.latest_time or 'default'}..."
            )
            if not select_time_range(page, req.earliest_time, req.latest_time, self.log_msg,
                                     debug=req.debug and settings.environment.lower() != "production"):
                raise RuntimeError("Could not apply the requested time range")

        self.log_msg("Selecting holes...")
        holes_locator = page.locator("label:visible, button:visible").filter(has_text=req.holes)
        if holes_locator.count() == 0:
            holes_locator = page.get_by_text(req.holes, exact=True)
        if not self.safe_click(page, holes_locator, "select holes"):
            raise RuntimeError("Could not select the requested holes option")

        self.log_msg("Refreshing tee times...")
        if not self.safe_click(page, page.get_by_text("REFRESH TEE TIMES", exact=False), "refresh tee times"):
            raise RuntimeError("Could not refresh tee times")
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

        # From this point onward the page contains guest details, and may later
        # contain card details, so no debug screenshot or page text dump is safe.
        self._sensitive_data_entered = True
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
                except Exception as exc:
                    self.log_msg(f"  ! Could not set country automatically ({type(exc).__name__})")

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
                    "Check the .temp folder for stripe_* debug screenshots."
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
                restrict_browser_navigation(page)
                try:
                    self._do_booking_flow(page)
                    self.status = "filled"
                except Exception as e:
                    self.status = "error"
                    self.error = "Booking automation failed. Check the service logs using the session ID."
                    logger.error("Booking session %s failed (%s)", self.session_id, type(e).__name__)
                    self.log.append("ERROR: Booking automation failed.")
                finally:
                    self.ready_event.set()

                self.close_event.wait(timeout=settings.session_ttl_seconds)
                try:
                    self._browser.close()
                except Exception:
                    pass
        except Exception as e:
            self.status = "error"
            self.error = "Booking automation failed. Check the service logs using the session ID."
            logger.error("Booking session %s failed (%s)", self.session_id, type(e).__name__)
            self.log.append("ERROR: Booking automation failed.")
            self.ready_event.set()
        finally:
            self.status = "closed" if self.status == "filled" else self.status
            if self.status == "running":
                self.status = "error"
            if self._acquired_slot:
                self._semaphore.release()
                self._acquired_slot = False
