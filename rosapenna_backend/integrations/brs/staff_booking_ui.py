"""Playwright booking operations for an already identified BRS staff tee-time row."""

from collections.abc import Callable, Sequence
from datetime import date, time
import re
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

from pydantic import SecretStr
from playwright.sync_api import Error as PlaywrightError, BrowserContext, Locator, Page, Response, Route, expect, sync_playwright

from rosapenna_backend.integrations.brs.booking_client import (
    BrsAuthExpiredError,
    BrsConflictError,
    BrsReadbackMismatchError,
)
from rosapenna_backend.integrations.brs.tee_sheet import COURSES


def _staff_sheet_course(course_id: int) -> tuple[str, str]:
    course = COURSES.get(course_id)
    if course is None:
        raise ValueError("Unsupported staff tee sheet.")
    return f"/{course['tenant']}/day.php", str(course["external_course_id"])


def _find_staff_row(page: Page, *, course_id: int, day: date, at: time) -> Locator:
    path, external_course = _staff_sheet_course(course_id)
    url = urlsplit(page.url)
    date_label = f"{day:%a} {day.day:02d} {day:%b %Y} Tee Booking System"
    if (
        (url.scheme, url.netloc, url.path) != ("https", "brsgolf.com", path)
        or parse_qs(url.query).get("course_id1") != [external_course]
        or page.title() != date_label
    ):
        raise BrsConflictError("The staff tee sheet does not match the selected course and date.")

    sheet = page.locator("form#outputForm")
    row = sheet.locator("tr.tee-time-row").filter(
        has=page.locator("td:first-child a").get_by_text(f"{at:%H:%M}", exact=True)
    )
    if sheet.count() != 1 or row.count() != 1:
        raise BrsConflictError("The exact tee time could not be identified on the staff sheet.")

    return row


def inspect_rosapenna_staff_tee_time(page: Page, *, course_id: int, day: date, at: time) -> bool:
    """Return whether the exact staff row looks empty. This function performs no write."""
    row = _find_staff_row(page, course_id=course_id, day=day, at=at)
    cells = row.locator(":scope > td")
    selector = cells.nth(0).locator('input[type="checkbox"][name="T_ids[]"]')
    if cells.count() != 10 or selector.count() != 1:
        raise BrsConflictError("The staff tee time row has an unexpected layout.")
    if row.locator(":scope > td:first-child.booked").count() or not selector.is_enabled():
        return False
    return not any(cells.nth(index).inner_text().strip() for index in (2, 4, 5, 6, 7, 8))


def preview_staff_tee_time(
    storage_state: Path, *, course_id: int, day: date, at: time,
) -> bool:
    """Open one staff sheet and check Add without sending booking data."""
    path, external_course = _staff_sheet_course(course_id)
    query = urlencode({"course_id1": external_course, "Day_Page": "DayDetail", "d_date": day.isoformat()})
    sheet_url = f"https://brsgolf.com{path}?{query}"

    def read_only(route: Route) -> None:
        request = route.request
        target = urlsplit(request.url)
        is_asset = (
            (target.scheme, target.netloc) == ("https", "brsgolf.com")
            and (request.resource_type, Path(target.path).suffix.lower())
            in {("script", ".js"), ("stylesheet", ".css")}
        )
        if request.method != "GET" or not (request.url == sheet_url or is_asset):
            route.abort()
            return
        # Do not follow a login redirect or let a GET redirect outside the allowlist.
        response = route.fetch(max_redirects=0, max_retries=0, timeout=8000)
        if response.status != 200:
            route.abort()
            return
        route.fulfill(response=response)

    with sync_playwright() as playwright:
        with playwright.chromium.launch(timeout=10000) as browser:
            context = browser.new_context(storage_state=storage_state, service_workers="block")
            context.route("**/*", read_only)
            context.route_web_socket("**/*", lambda socket: socket.close())
            page = context.new_page()
            page.goto(sheet_url, timeout=12000, wait_until="domcontentloaded")
            if not inspect_rosapenna_staff_tee_time(page, course_id=course_id, day=day, at=at):
                return False
            row = _find_staff_row(page, course_id=course_id, day=day, at=at)
            row.locator('td:first-child input[name="T_ids[]"]').check(timeout=4000)
            add = page.locator("form#outputForm").get_by_role("button", name="Add", exact=True)
            return add.is_enabled(timeout=4000)


def book_staff_tee_time(
    storage_state: Path | None, *, course_id: int, day: date, at: time, players: Sequence[str],
    credentials: tuple[SecretStr, SecretStr] | None = None,
) -> str:
    """Save one empty tee time and return its BRS booking reference after a fresh read."""
    if not 1 <= len(players) <= 4 or any(
        not name.strip() or len(name) > 201 or any(ord(char) < 32 for char in name)
        for name in players
    ):
        raise BrsConflictError("The player list cannot be entered on the staff sheet.")

    path, external_course = _staff_sheet_course(course_id)
    details_path = path.removesuffix("day.php") + "stt.php"
    origin = "https://brsgolf.com"
    query = urlencode({"course_id1": external_course, "Day_Page": "DayDetail", "d_date": day.isoformat()})
    sheet_url = f"{origin}{path}?{query}"
    save_url = origin + details_path
    details_url = None
    save_armed = False
    sent = False

    def restrict_requests(route: Route) -> None:
        nonlocal sent
        request = route.request
        target = urlsplit(request.url)
        if (target.scheme, target.netloc) != ("https", "brsgolf.com"):
            route.abort()
            return
        if (
            save_armed and not sent and request.method == "POST"
            and request.url == save_url and request.is_navigation_request()
        ):
            sent = True
            route.continue_()
            return
        if request.method == "GET" and (
            request.url in {sheet_url, details_url}
            or (sent and request.is_navigation_request() and target.path == details_path)
            or request.resource_type in {"script", "stylesheet"}
        ):
            route.continue_()
            return
        route.abort()

    try:
        with sync_playwright() as playwright:
            with playwright.chromium.launch(timeout=10000) as browser:
                context = browser.new_context(storage_state=storage_state, service_workers="block")
                if storage_state is None and credentials is None:
                    raise BrsAuthExpiredError("BRS staff credentials are unavailable.")
                if credentials is not None:
                    _sign_in_to_staff(context, *credentials)
                context.route("**/*", restrict_requests)
                context.route_web_socket("**/*", lambda socket: socket.close())
                page = context.new_page()
                page.set_default_timeout(5000)
                page.goto(sheet_url, timeout=12000, wait_until="domcontentloaded")
                if not inspect_rosapenna_staff_tee_time(page, course_id=course_id, day=day, at=at):
                    raise BrsConflictError("The selected staff tee time is occupied.")

                row = _find_staff_row(page, course_id=course_id, day=day, at=at)
                slot = row.locator('td:first-child input[name="T_ids[]"]').get_attribute("value")
                href = row.locator("td:first-child a").get_attribute("href")
                if not slot or not re.fullmatch(r"[0-9]+", slot) or href != f"stt.php?id={slot}":
                    raise BrsConflictError("BRS did not identify the selected tee time.")

                details_url = f"{save_url}?id={slot}"
                page.goto(details_url, timeout=12000, wait_until="domcontentloaded")
                form = _staff_booking_form(page, details_url, course_id, day, at, slot)
                reference = _staff_booking_reference(form)
                if reference != "0" or form.locator("#ReservationName").input_value().strip():
                    raise BrsConflictError("BRS already has a booking for this tee time.")

                form.locator("#ReservationName").fill(players[0])
                form.locator("#ReservationType").select_option(label="Member")
                _show_staff_players(form)
                for index, name in enumerate(players, 1):
                    player = form.locator(f'[name="Player{index}"]')
                    if player.input_value().strip() or form.locator(f'[name="Player{index}ContactId"]').input_value().strip() not in {"", "0"}:
                        raise BrsConflictError("A BRS player field already contains a player.")
                    player.fill(name)
                for index in range(1, 5):
                    linked = form.locator(f'[name="Player{index}ContactId"]').input_value().strip()
                    value = form.locator(f'[name="Player{index}"]').input_value().strip()
                    expected = players[index - 1] if index <= len(players) else ""
                    if linked not in {"", "0"} or value != expected:
                        raise BrsConflictError("BRS loaded an unexpected player or contact.")
                save_armed = True
                with page.expect_response(
                    lambda response: response.request.method == "POST" and response.url == save_url
                ) as submission:
                    form.locator('input[type="submit"][name="NDB_Apply"][value="Apply"]').click()
                if submission.value.status >= 400:
                    raise BrsReadbackMismatchError("BRS rejected the booking save. Check the slot before retrying.")

                # A fresh GET verifies the save without resubmitting the form.
                page.goto(details_url, timeout=12000, wait_until="domcontentloaded")
                form = _staff_booking_form(page, details_url, course_id, day, at, slot)
                reference = _staff_booking_reference(form)
                _show_staff_players(form)
                if (
                    not sent or reference == "0"
                    or form.locator("#ReservationName").input_value().strip() != players[0]
                    or form.locator("#ReservationType option:checked").inner_text().strip() != "Member"
                    or any(
                        form.locator(f'[name="Player{index}"]').input_value().strip() != name
                        for index, name in enumerate(players, 1)
                    )
                ):
                    raise BrsReadbackMismatchError("BRS did not confirm the saved booking. Check the slot before retrying.")
                return reference
    except PlaywrightError:
        raise BrsReadbackMismatchError("BRS could not verify this booking. Check the slot before retrying.") from None


def _sign_in_to_staff(context: BrowserContext, username: SecretStr, password: SecretStr) -> None:
    login_url = "https://brsgolf.com/rosapenna/login"
    action = None
    submitted = False

    def restrict_login(route: Route) -> None:
        nonlocal submitted
        request = route.request
        url = urlsplit(request.url)
        allowed_site = (url.scheme, url.netloc) == ("https", "brsgolf.com")
        if allowed_site and request.method == "GET":
            route.continue_()
        elif allowed_site and request.method == "POST" and request.url == action and not submitted:
            submitted = True
            route.continue_()
        else:
            route.abort()

    page = context.new_page()
    page.route("**/*", restrict_login)
    try:
        page.set_default_timeout(5000)
        page.goto(login_url, timeout=12000, wait_until="domcontentloaded")
        form = page.locator('form[method="post" i]').filter(has=page.locator('input[type="password"]'))
        if form.count() != 1:
            raise BrsAuthExpiredError("BRS staff sign-in form is unavailable.")
        target = form.evaluate("(form) => form.action")
        if target != "https://brsgolf.com/rosapenna/login_check":
            raise BrsAuthExpiredError("BRS staff sign-in form has an unexpected destination.")
        user_field = form.locator('input[name="_username"][type="text"]:visible')
        password_field = form.locator('input[name="_password"][type="password"]:visible')
        submit = form.locator('button[type="submit"]:visible, input[type="submit"]:visible, button:not([type]):visible')
        if user_field.count() != 1 or password_field.count() != 1 or submit.count() != 1:
            raise BrsAuthExpiredError("BRS staff sign-in fields could not be identified.")
        user_field.fill(username.get_secret_value())
        password_field.fill(password.get_secret_value())
        action = target
        with page.expect_response(lambda response: response.request.method == "POST" and response.url == action) as result:
            submit.click()
        if not submitted or result.value.status >= 400:
            raise BrsAuthExpiredError("BRS rejected the staff sign-in.")
    except PlaywrightError:
        raise BrsAuthExpiredError("BRS staff sign-in could not be completed.") from None
    finally:
        page.close()


def _staff_booking_form(page: Page, url: str, course_id: int, day: date, at: time, slot: str) -> Locator:
    title = f"Booking Details for {day:%a %d %b %Y}, {at:%H:%M}"
    form = page.locator('form[method="post" i]').filter(has=page.locator('input[name="NDB_Apply"]'))
    if (
        page.url != url
        or page.get_by_text(title, exact=True).count() != 1
        or page.get_by_text(COURSES[course_id]["name"], exact=True).count() < 1
        or form.count() != 1
        or form.evaluate("(form) => form.action") != url.split("?", 1)[0]
        or form.locator('[name="unique_id"]').input_value() != slot
        or form.locator('[name="course_id"]').input_value() != _staff_sheet_course(course_id)[1]
        or form.locator('[name="tee_time"]').input_value() != f"{at:%H:%M}"
    ):
        raise BrsConflictError("The BRS editor does not match the selected tee time.")
    return form


def _staff_booking_reference(form: Locator) -> str:
    matches = re.findall(r"Booking Ref:\s*([0-9]+)", form.inner_text())
    if len(matches) != 1:
        raise BrsReadbackMismatchError("BRS did not show one booking reference.")
    return matches[0]


def _show_staff_players(form: Locator) -> None:
    if not form.locator('[name="Player1"]').is_visible():
        form.get_by_text("Players", exact=True).click()


def add_to_empty_staff_row(
    page: Page,
    *,
    row: Locator,
    saved_name: Locator,
    saved_players: Sequence[Locator],
    reservation_name: Locator,
    reservation_type: Locator,
    player_inputs: Sequence[Locator],
    add_button: Locator,
    is_save_response: Callable[[Response], bool],
    name: str,
    type_label: str,
    players: Sequence[str],
) -> None:
    """Save once and verify after reload. The caller must bind the exact row and editor.

    The caller owns availability, payment, approved environment and booking identity checks.
    """
    if not 1 <= len(players) <= len(player_inputs) == len(saved_players) <= 4:
        raise ValueError("Match all player cells and provide one to four players.")
    if not name.strip() or not type_label.strip() or any(not player.strip() for player in players):
        raise ValueError("Reservation name, type and player names are required.")

    cells = [saved_name, *saved_players]
    if row.count() != 1 or any(cell.count() != 1 or cell.inner_text().strip() for cell in cells):
        raise BrsConflictError("The tee time is occupied or could not be verified as empty.")

    fields = [reservation_name, reservation_type, *player_inputs, add_button]
    if any(field.count() != 1 for field in fields):
        raise BrsConflictError("The booking editor could not be identified.")
    if any(field.input_value().strip() for field in [reservation_name, *player_inputs]):
        raise BrsConflictError("The booking editor already contains reservation details.")

    reservation_name.fill(name.strip())
    reservation_type.select_option(label=type_label)
    for field, player in zip(player_inputs, players):
        field.fill(player.strip())
    expected = [name.strip(), *(player.strip() for player in players)]
    expected.extend([""] * (len(cells) - len(expected)))
    _save_staff_row(page, row, cells, expected, add_button, is_save_response)


def modify_staff_row(
    page: Page,
    *,
    row: Locator,
    saved_name: Locator,
    saved_players: Sequence[Locator],
    reservation_name: Locator,
    player_inputs: Sequence[Locator],
    modify_button: Locator,
    is_save_response: Callable[[Response], bool],
    current_name: str,
    current_players: Sequence[str],
    new_players: Sequence[str] | None,
) -> None:
    """Change player names, or clear an exact, fully owned booking if new_players is None.

    The caller must verify the booking reference and scope the row and editor before calling.
    Include every player slot, using an empty string for each unused slot.
    """
    if not 1 <= len(current_players) == len(saved_players) == len(player_inputs) <= 4:
        raise ValueError("Match all player slots before modifying the booking.")
    if not current_name.strip() or not any(player.strip() for player in current_players):
        raise ValueError("The existing booking must have a name and at least one player.")
    current = [current_name.strip(), *(player.strip() for player in current_players)]
    cells = [saved_name, *saved_players]
    fields = [reservation_name, *player_inputs]
    if (
        row.count() != 1
        or modify_button.count() != 1
        or any(cell.count() != 1 or cell.inner_text().strip() != value for cell, value in zip(cells, current))
        or any(field.count() != 1 or field.input_value().strip() != value for field, value in zip(fields, current))
    ):
        raise BrsConflictError("The saved row or editor does not match the booking to modify.")

    if new_players is None:
        expected = [""] * len(fields)
        for field, value in zip(fields, current):
            if value:
                field.press("ControlOrMeta+a")
                field.press("Backspace")
                expect(field).to_have_value("")
    else:
        if len(new_players) != len(current_players) or any(
            bool(before) != bool(after.strip()) for before, after in zip(current[1:], new_players)
        ):
            raise ValueError("A name change must preserve all occupied and empty player slots.")
        expected = [current[0], *(player.strip() for player in new_players)]
        for field, before, after in zip(player_inputs, current[1:], expected[1:]):
            if before != after:
                field.fill(after)

    _save_staff_row(page, row, cells, expected, modify_button, is_save_response)


def _save_staff_row(
    page: Page,
    row: Locator,
    cells: Sequence[Locator],
    expected: Sequence[str],
    button: Locator,
    is_save_response: Callable[[Response], bool],
) -> None:
    expect(button).to_be_enabled(timeout=5000)

    # Reopen the original sheet with a GET so readback cannot resubmit the form.
    sheet_url = page.url
    try:
        with page.expect_response(is_save_response, timeout=10000) as submission:
            button.click(timeout=5000)
        response = submission.value
    except PlaywrightError:
        response = None

    if response is not None and response.status >= 400:
        raise BrsReadbackMismatchError("BRS did not accept the save. Check BRS before retrying.")

    try:
        try:
            page.goto(sheet_url, timeout=10000, wait_until="domcontentloaded")
        except PlaywrightError:
            # The form's navigation can overlap the first read. Retry only the GET.
            page.goto(sheet_url, timeout=10000, wait_until="domcontentloaded")
        expect(row).to_have_count(1, timeout=5000)
        for cell, value in zip(cells, expected):
            expect(cell).to_have_text(value, timeout=5000)
    except (PlaywrightError, AssertionError):
        raise BrsReadbackMismatchError("The save outcome could not be verified. Check BRS before retrying.") from None
