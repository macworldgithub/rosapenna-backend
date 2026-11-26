"""Run staff booking operations against a browser fixture with no external requests."""

from html import escape
import os
from datetime import date, time
from pathlib import Path
from urllib.parse import parse_qs
import unittest
from unittest.mock import patch

from playwright.sync_api import sync_playwright

from rosapenna_backend.integrations.brs.booking_client import BrsConflictError, BrsReadbackMismatchError
from rosapenna_backend.integrations.brs.staff_booking_ui import (
    add_to_empty_staff_row,
    inspect_rosapenna_staff_tee_time,
    modify_staff_row,
)


class StaffBookingTests(unittest.TestCase):
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
