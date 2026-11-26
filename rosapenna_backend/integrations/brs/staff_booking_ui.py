"""Playwright booking operations for an already identified BRS staff tee-time row."""

from collections.abc import Callable, Sequence
from datetime import date, time
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

from playwright.sync_api import Error as PlaywrightError, Locator, Page, Response, Route, expect, sync_playwright

from rosapenna_backend.integrations.brs.booking_client import (
    BrsConflictError,
    BrsReadbackMismatchError,
)


def _find_staff_row(page: Page, *, course_id: int, day: date, at: time) -> Locator:
    url = urlsplit(page.url)
    date_label = f"{day:%a} {day.day:02d} {day:%b %Y} Tee Booking System"
    if (
        course_id not in {1, 3}
        or (url.scheme, url.netloc, url.path) != ("https", "brsgolf.com", "/rosapenna/day.php")
        or parse_qs(url.query).get("course_id1") != [str(course_id)]
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
    if course_id not in {1, 3}:
        raise ValueError("Unsupported staff tee sheet.")
    query = urlencode({"course_id1": course_id, "Day_Page": "DayDetail", "d_date": day.isoformat()})
    sheet_url = f"https://brsgolf.com/rosapenna/day.php?{query}"

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
