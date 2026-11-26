"""Run staff booking operations against a browser fixture with no external requests."""

from html import escape
import os
from datetime import date, time
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from urllib.parse import parse_qs
import unittest
from unittest.mock import patch

from playwright.sync_api import BrowserContext, Page, sync_playwright
from pydantic import SecretStr

from rosapenna_backend.integrations.brs.booking_client import BrsConflictError, BrsReadbackMismatchError
from rosapenna_backend.integrations.brs.staff_booking_ui import (
    _sign_in_to_staff,
    add_to_empty_staff_row,
    book_staff_tee_time,
    inspect_rosapenna_staff_tee_time,
    modify_staff_row,
)


class StaffBookingTests(unittest.TestCase):
    def test_staff_sign_in_submits_once_in_the_server_browser(self):
        login = "https://brsgolf.com/rosapenna/login"
        real_route = Page.route
        submits = []

        def register(page, match, handler):
            def serve(route):
                def respond():
                    self.assertEqual(route.request.url, login + ("_check" if route.request.method == "POST" else ""))
                    if route.request.method == "POST":
                        submits.append(parse_qs(route.request.post_data))
                        route.fulfill(
                            status=200, body="Signed in",
                            headers={"Set-Cookie": "staff_fixture=1; Path=/; Secure; HttpOnly"},
                        )
                    else:
                        route.fulfill(status=200, content_type="text/html", body=(
                            '<form method="post" action="/rosapenna/login_check">'
                            '<input name="_username" type="text"><input name="_password" type="password">'
                            '<button type="submit">Sign in</button></form>'
                        ))
                handler(SimpleNamespace(request=route.request, abort=route.abort, continue_=respond))
            real_route(page, match, serve)

        with sync_playwright() as playwright:
            with playwright.chromium.launch() as browser:
                context = browser.new_context()
                with patch.object(Page, "route", register):
                    _sign_in_to_staff(context, SecretStr("fixture-user"), SecretStr("fixture-value"))
                self.assertEqual(context.cookies(login)[0]["name"], "staff_fixture")
        self.assertEqual(len(submits), 1)

    def test_checkout_script_saves_the_exact_staff_details_once(self):
        continue_registration = BrowserContext.route
        saves = []
        day_url = "https://brsgolf.com/rosapenna/day.php?course_id1=1&Day_Page=DayDetail&d_date=2027-01-12"
        detail_url = "https://brsgolf.com/rosapenna/stt.php?id=660140"
        save_url = detail_url.split("?", 1)[0]

        def register(context, match, handler):
            def serve(route):
                def response():
                    if route.request.url == day_url:
                        name = "Test Lead" if saves else ""
                        cells = ["18", name, "", "", "", "", "", "", "10"]
                        row = "".join(f"<td>{cell}</td>" for cell in cells)
                        body = (
                            "<title>Tue 12 Jan 2027 Tee Booking System</title><form id=outputForm><table>"
                            '<tr class="tee-time-row"><td><a href="stt.php?id=660140">10:00</a>'
                            '<input type="checkbox" name="T_ids[]" value="660140"></td>'
                            f"{row}</tr></table></form>"
                        )
                    elif route.request.url == detail_url:
                        saved = saves[-1] if saves else {}
                        player_fields = "".join(
                            f'<input name="Player{i}" value="{escape(saved.get(f"Player{i}", ""))}">'
                            f'<input type="hidden" name="Player{i}ContactId" value="">'
                            for i in range(1, 5)
                        )
                        selected = "selected" if saves else ""
                        body = (
                            "<h1>Booking Details for Tue 12 Jan 2027, 10:00</h1>"
                            '<p>Old Tom Morris Links</p><form method="post" action="/rosapenna/stt.php">'
                            f'<p>Booking Ref: {169293 if saves else 0}</p>'
                            '<input name="unique_id" value="660140"><input name="course_id" value="1">'
                            '<input name="tee_time" value="10:00">'
                            f'<input id="ReservationName" name="ReservationName" value="{escape(saved.get("ReservationName", ""))}">'
                            '<select id="ReservationType" name="ReservationType"><option></option>'
                            f'<option {selected}>Member</option></select>{player_fields}'
                            '<input type="submit" name="NDB_Apply" value="Apply"></form>'
                        )
                    elif route.request.url == save_url and route.request.method == "POST":
                        saves.append({key: value[0] for key, value in parse_qs(route.request.post_data).items()})
                        body = "Saved"
                    else:
                        raise AssertionError("Unexpected BRS request")
                    route.fulfill(status=200, content_type="text/html", body=body)

                handler(SimpleNamespace(request=route.request, continue_=response, abort=route.abort))

            continue_registration(context, match, serve)

        with TemporaryDirectory() as directory:
            storage_state = Path(directory) / "state.json"
            storage_state.write_text('{"cookies": [], "origins": []}', encoding="utf-8")
            args = dict(course_id=1, day=date(2027, 1, 12), at=time(10), players=["Test Lead", "Test Guest"])
            with patch.object(BrowserContext, "route", register):
                self.assertEqual(book_staff_tee_time(storage_state, **args), "169293")
                with self.assertRaises(BrsConflictError):
                    book_staff_tee_time(storage_state, **args)
        self.assertEqual(len(saves), 1)
        self.assertEqual(saves[0]["ReservationName"], "Test Lead")
        self.assertEqual(saves[0]["Player2"], "Test Guest")

    @patch.dict(os.environ, {
        "BRS_ALLOW_LIVE_WRITES": "false",
        "OPERA_ALLOW_LIVE_WRITES": "false",
        "EMAIL_ALLOW_LIVE_SEND": "false",
        "PROMMT_ALLOW_LIVE_CALLS": "false",
    })
    def test_verified_test_deposit_drives_staff_sheet_save(self):
        from tests import test_n06_commit

        journey = test_n06_commit.TestN06CommitJourney(
            "test_successful_commit_after_simulated_deposit"
        )
        journey.setUp()
        self.addCleanup(journey.doCleanups)

        first_round = test_n06_commit.make_mock_available_quote().itemized_golf[0]

        def run_booking(add_booking):
            original_create = journey.brs_client.create_round

            def create_round(*, request, booking_reference, lead_guest):
                if (request.course_id, request.date, request.time) == (
                    first_round.course_id, first_round.date, first_round.time,
                ):
                    payment = journey.db.package_payments.find_one(
                        {"booking_reference": booking_reference}
                    )
                    self.assertTrue(payment["is_test"])
                    self.assertEqual(payment["status"], "verified_paid")
                    guest = payment["additional_guests"][0]
                    add_booking(
                        f"{lead_guest['first_name']} {lead_guest['last_name']}",
                        f"{guest['first_name']} {guest['last_name']}",
                    )
                return original_create(
                    request=request, booking_reference=booking_reference, lead_guest=lead_guest
                )

            with patch.object(journey.brs_client, "create_round", side_effect=create_round):
                journey.test_successful_commit_after_simulated_deposit()

        self.run_add_fixture(
            persist=True,
            run_booking=run_booking,
            row_label=f"{first_round.course_name} {first_round.date} {first_round.time}",
        )

    def test_add_confirms_after_reload_and_refuses_to_overwrite(self):
        self.run_add_fixture(persist=True, verify_lifecycle=True)

    def test_save_without_persistence_is_uncertain_and_is_not_retried(self):
        self.run_add_fixture(persist=False)

    def test_lost_save_response_uses_readback_without_submitting_again(self):
        self.run_add_fixture(persist=True, lose_response=True)

    def run_add_fixture(
        self, *, persist, lose_response=False, verify_lifecycle=False, run_booking=None,
        row_label="Sandy Hills 2027-06-15 09:40",
    ):
        url = "https://brs-staff-fixture.invalid/"
        saved = {}
        submissions = []

        def serve(route):
            if route.request.url != url:
                route.abort()
                return
            if route.request.method == "POST":
                submissions.append(parse_qs(route.request.post_data, keep_blank_values=True))
                if persist:
                    saved.update({key: values[0] for key, values in submissions[-1].items()})
                if lose_response:
                    route.abort()
                else:
                    route.fulfill(status=200, body="Saved")
                return
            route.fulfill(content_type="text/html", body=f"""
                <section aria-label="{escape(row_label)}">
                    <span id="saved-name">{escape(saved.get('name', ''))}</span>
                    <span id="saved-player">{escape(saved.get('player', ''))}</span>
                    <form method="post">
                        <input id="name" name="name" value="{escape(saved.get('name', ''))}" {'required' if not saved.get('name') else ''}>
                        <select id="type" name="type" required>
                            <option value="">Choose</option><option {'selected' if saved.get('type') else ''}>Member</option>
                        </select>
                        <input id="player" name="player" value="{escape(saved.get('player', ''))}" {'required' if not saved.get('name') else ''}>
                        <button {'disabled' if not saved.get('name') else ''}>{'Modify' if saved.get('name') else 'Add'}</button>
                    </form>
                </section>
                <aside>08:40 Existing group</aside>
                <script>
                    const form = document.querySelector("form");
                    form.addEventListener("input", () => {{
                        form.querySelector("button").disabled = !form.checkValidity();
                    }});
                </script>
            """)

        with sync_playwright() as playwright:
            bundled = Path(playwright.chromium.executable_path)
            edge = Path("C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe")
            executable = bundled if bundled.is_file() else edge
            if not executable.is_file():
                self.skipTest("Install Playwright Chromium to run the browser fixture.")
            with playwright.chromium.launch(executable_path=str(executable)) as browser:
                page = browser.new_page()
                page.route("**/*", serve)
                page.goto(url)
                row = page.get_by_role("region", name=row_label, exact=True)
                add = row.get_by_role("button", name="Add", exact=True)
                self.assertTrue(add.is_disabled())

                def add_booking(name="TEST Alex", player="TEST Jordan"):
                    add_to_empty_staff_row(
                        page,
                        row=row,
                        saved_name=row.locator("#saved-name"),
                        saved_players=[row.locator("#saved-player")],
                        reservation_name=row.locator("#name"),
                        reservation_type=row.locator("#type"),
                        player_inputs=[row.locator("#player")],
                        add_button=add,
                        is_save_response=lambda response: response.url == url and response.request.method == "POST",
                        name=name,
                        type_label="Member",
                        players=[player],
                    )

                if run_booking is not None:
                    run_booking(add_booking)
                    self.assertEqual(submissions, [{
                        "name": ["Jane Golfer"], "type": ["Member"], "player": ["Test Companion"]
                    }])
                    return

                if persist:
                    add_booking()
                    with self.assertRaises(BrsConflictError):
                        add_booking()
                else:
                    with self.assertRaises(BrsReadbackMismatchError):
                        add_booking()
                self.assertEqual(submissions, [{"name": ["TEST Alex"], "type": ["Member"], "player": ["TEST Jordan"]}])

                if verify_lifecycle:
                    def modify_booking(players, *, current_name="TEST Alex", current_player="TEST Jordan"):
                        modify_staff_row(
                            page,
                            row=row,
                            saved_name=row.locator("#saved-name"),
                            saved_players=[row.locator("#saved-player")],
                            reservation_name=row.locator("#name"),
                            player_inputs=[row.locator("#player")],
                            modify_button=row.get_by_role("button", name="Modify", exact=True),
                            is_save_response=lambda response: response.url == url and response.request.method == "POST",
                            current_name=current_name,
                            current_players=[current_player],
                            new_players=players,
                        )

                    with self.assertRaises(BrsConflictError):
                        modify_booking(None, current_name="Other booking")
                    self.assertEqual(len(submissions), 1)
                    modify_booking(["TEST Morgan"])
                    self.assertEqual(saved["player"], "TEST Morgan")
                    modify_booking(None, current_player="TEST Morgan")
                    self.assertEqual(saved, {"name": "", "type": "Member", "player": ""})
                    self.assertEqual(len(submissions), 3)
                    self.assertEqual(row.count(), 1)
                    self.assertTrue(page.get_by_text("08:40 Existing group", exact=True).is_visible())

                if persist:
                    staff_url = "https://brsgolf.com/rosapenna/day.php?course_id1=3"
                    page.route(staff_url, lambda route: route.fulfill(content_type="text/html", body="""
                        <title>Tue 15 Jun 2027 Tee Booking System</title>
                        <form id="outputForm"><table><tr class="tee-time-row">
                        <td><input type="checkbox" name="T_ids[]"><a>09:40</a></td>
                        <td>18</td><td></td><td>Member</td>
                        <td></td><td></td><td></td><td></td><td></td><td></td>
                        </tr></table></form>
                    """))
                    page.goto(staff_url)
                    lookup = dict(course_id=3, day=date(2027, 6, 15), at=time(9, 40))
                    self.assertTrue(inspect_rosapenna_staff_tee_time(page, **lookup))
                    page.locator("tr.tee-time-row td").nth(4).evaluate("(cell) => cell.textContent = 'TEST existing player'")
                    self.assertFalse(inspect_rosapenna_staff_tee_time(page, **lookup))
                    with self.assertRaises(BrsConflictError):
                        inspect_rosapenna_staff_tee_time(page, **(lookup | {"course_id": 1}))
