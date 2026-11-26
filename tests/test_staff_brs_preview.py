"""Verify the staff preview without contacting BRS or a payment provider."""

import asyncio
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

from fastapi import FastAPI

from rosapenna_backend.api.routes import staff_operations
from rosapenna_backend.api.security import rate_limit, verify_api_key
from rosapenna_backend.api import staff_security
from rosapenna_backend.integrations.brs.staff_booking_ui import preview_staff_tee_time


class StaffBrsPreviewTests(TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.storage = Path(directory.name) / "state.json"
        self.storage.write_text("{}", encoding="utf-8")
        self.settings = Mock(
            payment_mode="disabled", simulation_enabled=False,
            staff_brs_preview_storage_state=self.storage,
        )
        self.audit = Mock()
        auth = patch.object(staff_security, "settings", Mock(
            environment="production", staff_api_key="staff-preview-fixture",
        ))
        auth.start()
        self.addCleanup(auth.stop)

        app = FastAPI()
        app.include_router(staff_operations.router)
        app.dependency_overrides.update({
            rate_limit: lambda: None,
            verify_api_key: lambda: None,
            staff_operations.get_settings: lambda: self.settings,
            staff_operations.get_staff_audit_log: lambda: self.audit,
        })
        self.app = app
        self.payload = {"course_id": 1, "day": "2027-01-12", "tee_time": "10:00"}
        self.headers = {"X-Staff-Token": "staff-preview-fixture"}

    def post_preview(self, *, payload, headers=None):
        events = []

        async def receive():
            return {"type": "http.request", "body": json.dumps(payload).encode(), "more_body": False}

        async def send(event):
            events.append(event)

        path = "/staff/brs-preview"
        request_headers = {"content-type": "application/json", **(headers or {})}
        asyncio.run(self.app({
            "type": "http", "asgi": {"version": "3.0"}, "method": "POST",
            "scheme": "http", "http_version": "1.1", "path": path,
            "raw_path": path.encode(), "root_path": "", "query_string": b"",
            "headers": [(key.lower().encode(), value.encode()) for key, value in request_headers.items()],
        }, receive, send))
        return SimpleNamespace(status_code=events[0]["status"], body=json.loads(events[1]["body"]))

    @patch.object(staff_operations, "preview_staff_tee_time")
    def test_denies_unauthorized_and_invalid_requests(self, preview):
        response = self.post_preview(payload=self.payload)
        self.assertEqual(response.status_code, 401)
        for invalid in ({"course_id": 2}, {"tee_time": "25:00"}, {"url": "https://example.invalid"}):
            with self.subTest(invalid=invalid):
                response = self.post_preview(
                    headers=self.headers, payload=self.payload | invalid,
                )
                self.assertEqual(response.status_code, 422)
        preview.assert_not_called()
        self.audit.record.assert_not_called()

    @patch("rosapenna_backend.integrations.brs.staff_booking_ui.sync_playwright")
    @patch("rosapenna_backend.integrations.brs.staff_booking_ui.inspect_rosapenna_staff_tee_time", return_value=False)
    def test_preview_uses_the_provider_site_and_course_id(self, inspect, playwright):
        from datetime import date, time

        browser = playwright.return_value.__enter__.return_value.chromium.launch.return_value.__enter__.return_value
        page = browser.new_context.return_value.new_page.return_value
        for course, location in (
            (1, "rosapenna/day.php?course_id1=1"),
            (3, "rosapenna/day.php?course_id1=3"),
            (4, "stpatrickslinks/day.php?course_id1=1"),
        ):
            with self.subTest(course=course):
                self.assertFalse(preview_staff_tee_time(self.storage, course_id=course, day=date(2027, 1, 12), at=time(10)))
                self.assertEqual(page.goto.call_args.args[0], f"https://brsgolf.com/{location}&Day_Page=DayDetail&d_date=2027-01-12")

    @patch.object(staff_operations, "preview_staff_tee_time")
    def test_preview_requires_payments_off_and_session(self, preview):
        self.settings.payment_mode = "prommt_test"
        self.assertEqual(self.post_preview(
            headers=self.headers, payload=self.payload,
        ).status_code, 503)
        self.settings.payment_mode = "disabled"
        self.storage.unlink()
        self.assertEqual(self.post_preview(
            headers=self.headers, payload=self.payload,
        ).status_code, 503)
        preview.assert_not_called()

    @patch.object(staff_operations, "preview_staff_tee_time", return_value=True)
    def test_records_check_and_reports_no_booking(self, preview):
        response = self.post_preview(headers=self.headers, payload=self.payload)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.body, {
            "payment": "simulated", "brs_row_checked": True,
            "add_enabled": True, "booking_saved": False,
        })
        preview.assert_called_once()
        self.assertEqual([call.args[-1] for call in self.audit.record.call_args_list],
                         ["simulation_requested", "add_enabled"])

    @patch("rosapenna_backend.integrations.brs.staff_booking_ui.sync_playwright")
    @patch("rosapenna_backend.integrations.brs.staff_booking_ui.inspect_rosapenna_staff_tee_time", return_value=False)
    def test_browser_allows_only_read_requests_and_refuses_redirects(self, inspect, playwright):
        from datetime import date, time

        browser = playwright.return_value.__enter__.return_value.chromium.launch.return_value.__enter__.return_value
        context = browser.new_context.return_value
        self.assertFalse(preview_staff_tee_time(self.storage, course_id=1, day=date(2027, 1, 12), at=time(10)))
        inspect.assert_called_once()
        context.route_web_socket.assert_called_once()
        handler = context.route.call_args.args[1]
        sheet = context.new_page.return_value.goto.call_args.args[0]
        for method, url, kind, status, allowed in (
            ("GET", sheet, "document", 200, True),
            ("POST", sheet, "document", 200, False),
            ("GET", sheet, "document", 302, True),
            ("GET", "https://brsgolf.com/rosapenna/add.php", "xhr", 200, False),
            ("GET", "https://example.invalid/editor.js", "script", 200, False),
        ):
            with self.subTest(method=method, url=url, status=status):
                route = Mock()
                route.request.method = method
                route.request.url = url
                route.request.resource_type = kind
                route.fetch.return_value.status = status
                handler(route)
                if allowed:
                    route.fetch.assert_called_once_with(max_redirects=0, max_retries=0, timeout=8000)
                else:
                    route.fetch.assert_not_called()
                self.assertEqual(route.fulfill.called, allowed and status == 200)
                self.assertEqual(route.abort.called, not route.fulfill.called)


    @patch("rosapenna_backend.integrations.brs.staff_booking_ui.sync_playwright")
    @patch("rosapenna_backend.integrations.brs.staff_booking_ui._find_staff_row")
    @patch("rosapenna_backend.integrations.brs.staff_booking_ui.inspect_rosapenna_staff_tee_time", return_value=True)
    def test_preview_selects_row_and_only_reads_add_state(self, inspect, find, playwright):
        from datetime import date, time

        browser = playwright.return_value.__enter__.return_value.chromium.launch.return_value.__enter__.return_value
        page = browser.new_context.return_value.new_page.return_value
        add = page.locator.return_value.get_by_role.return_value
        add.is_enabled.return_value = True
        self.assertTrue(preview_staff_tee_time(self.storage, course_id=1, day=date(2027, 1, 12), at=time(10)))
        inspect.assert_called_once()
        find.return_value.locator.return_value.check.assert_called_once_with(timeout=4000)
        add.is_enabled.assert_called_once_with(timeout=4000)
        add.click.assert_not_called()
