import secrets
import unittest
from concurrent.futures import ThreadPoolExecutor

import mongomock

from rosapenna_backend.api.routes.pre_arrival_rooming import create_rooming_sandbox
from rosapenna_backend.services.pre_arrival_rooming import SandboxRooming
from scripts.run_n10_sandbox import create_fixture
from tests.test_pre_arrival_rooming import AsgiClient


class CaddyTests(unittest.TestCase):
    def setUp(self):
        self.rooming = SandboxRooming(mongomock.MongoClient(tz_aware=True).rosapenna_n10_test_caddy)
        key = secrets.token_urlsafe(32)
        self.key = {"X-API-Key": key}
        self.http = AsgiClient(create_rooming_sandbox(self.rooming, key))
        self.path = "/pre-arrival/caddy"
        self.headers = self.sign_in()
        roster = self.http.get(self.path, headers=self.headers).json()
        self.reference = roster["booking_reference"]
        self.round = roster["rounds"][0]
        self.round_path = self.path + "/" + self.round["id"]
        self.payload = {"version": 0, "request_id": "first", "golfers": self.round["golfers"], "notes": "Sample"}

    def sign_in(self):
        reply = self.http.post("/pre-arrival/rooming/session", headers=self.key,
                               json={"invite_token": create_fixture(self.rooming)})
        self.assertEqual(reply.status_code, 200)
        return {**self.key, "X-Pre-Arrival-Session": reply.json()["token"]}

    def submit(self, **changes):
        return self.http.put(self.round_path, headers=self.headers, json={**self.payload, **changes})

    def test_scoped_round_and_input_validation(self):
        self.assertEqual(self.http.get(self.path, headers=self.key).status_code, 401)
        second = self.sign_in()
        self.assertEqual(self.http.put(self.round_path, headers=second, json=self.payload).status_code, 404)
        for changes in ({"golfers": ["guest-c"]}, {"golfers": ["guest-a", "guest-a"]},
                        {"course": "Injected"}, {"requested_count": 3}, {"online_charge": 5}):
            with self.subTest(changes=changes):
                self.assertEqual(self.submit(**changes).status_code, 422)
        self.assertEqual(self.http.get(self.path, headers=self.headers).json()["version"], 0)

    def test_replay_edit_withdraw_and_staff_history(self):
        self.rooming.bookings.update_one({"_id": self.reference}, {"$set": {"staff_note": "Preserve", "ledger": []}})
        first = self.submit()
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.json()["requested_count"], 2)
        self.assertEqual(first.json()["status"], "requested")
        self.assertEqual(first.json()["handoff_status"], "not_sent")
        self.assertEqual(first.json()["brs_status"], "not_sent")
        self.assertEqual(self.submit(golfers=list(reversed(self.payload["golfers"]))).json(), first.json())
        self.assertEqual(self.submit(notes="Different").status_code, 409)
        self.assertEqual(self.submit(request_id="stale").status_code, 409)
        self.assertEqual(self.submit(version=1, request_id="withdraw", golfers=[], notes="").json()["status"], "withdrawn")
        self.assertEqual(self.http.get(self.path, headers=self.headers).json()["rounds"][0]["request"]["status"], "withdrawn")
        row = self.rooming.bookings.find_one({"_id": self.reference})
        self.assertEqual(row["ledger"], [])
        self.assertEqual(row["version"], 1)
        self.assertEqual(row["staff_note"], "Preserve")
        self.assertEqual([event["version"] for event in row["caddy_history"]], [1, 2])
        self.assertEqual(row["caddy_history"][0]["date"], self.round["date"])
        self.assertEqual(row["caddy_history"][0]["course"], self.round["course"])

    def test_simultaneous_identical_requests_save_once(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            first, second = list(pool.map(lambda _: self.submit(), range(2)))
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.json(), second.json())
        row = self.rooming.bookings.find_one({"_id": self.reference})
        self.assertEqual(len(row["caddy_history"]), 1)
