"""N10 contract tests. Optional real MongoDB is restricted to literal loopback."""

import asyncio
import datetime as dt
import json
import os
import secrets
import unittest
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from types import SimpleNamespace

import mongomock

from rosapenna_backend.api.routes.pre_arrival_rooming import create_rooming_sandbox
from rosapenna_backend.database import MongoSettings, connect_database
from rosapenna_backend.services.pre_arrival_rooming import SandboxRooming
from scripts.run_n10_sandbox import create_fixture


class AsgiClient:
    def __init__(self, app):
        self.app = app

    def send(self, method, path, headers, **kwargs):
        headers = dict(headers)
        body = kwargs.get("content", "").encode()
        if "json" in kwargs:
            body = json.dumps(kwargs["json"]).encode()
            headers["Content-Type"] = "application/json"

        async def call():
            response = []

            async def receive():
                return {"type": "http.request", "body": body, "more_body": False}

            async def send(message):
                response.append(message)

            await self.app({
                "type": "http", "asgi": {"version": "3.0", "spec_version": "2.4"},
                "http_version": "1.1", "method": method, "scheme": "http",
                "path": path, "query_string": b"",
                "headers": [(key.lower().encode(), value.encode()) for key, value in headers.items()],
                "client": ("127.0.0.1", 12345), "server": ("127.0.0.1", 4042),
            }, receive, send)
            start, *chunks = response
            return SimpleNamespace(
                status_code=start["status"],
                headers={key.decode(): value.decode() for key, value in start["headers"]},
                json=lambda: json.loads(b"".join(chunk.get("body", b"") for chunk in chunks)),
            )

        return asyncio.run(call())

    def get(self, path, **kwargs):
        return self.send("GET", path, **kwargs)

    def put(self, path, **kwargs):
        return self.send("PUT", path, **kwargs)

    def post(self, path, **kwargs):
        return self.send("POST", path, **kwargs)

    def delete(self, path, **kwargs):
        return self.send("DELETE", path, **kwargs)


class RoomingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        name = f"rosapenna_n10_test_suite_{secrets.token_hex(6)}"
        if os.environ.get("N10_TEST_LOCAL_MONGO") == "true":
            cls.client, cls.database = connect_database(MongoSettings(
                _env_file=None, uri="mongodb://127.0.0.1:27017", database=name,
            ))
        else:
            cls.client = mongomock.MongoClient(tz_aware=True)
            cls.database = cls.client[name]

    @classmethod
    def tearDownClass(cls):
        cls.client.close()

    def setUp(self):
        self.current = dt.datetime.now(dt.UTC)
        self.service = SandboxRooming(self.database, lambda: self.current)
        self.key = {"X-API-Key": secrets.token_urlsafe(32)}
        self.http = AsgiClient(create_rooming_sandbox(self.service, self.key["X-API-Key"]))
        self.invite = create_fixture(self.service)
        self.path = "/pre-arrival/rooming"

    def exchange(self, invite=None):
        return self.http.post(self.path + "/session", headers=self.key, json={"invite_token": invite or self.invite})

    def sign_in(self, invite=None):
        result = self.exchange(invite)
        self.assertEqual(result.status_code, 200)
        return {**self.key, "X-Pre-Arrival-Session": result.json()["token"]}

    def form(self, headers):
        response = self.http.get(self.path, headers=headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["cache-control"], "no-store")
        return response.json()

    def submission(self, form):
        return {
            "version": form["version"], "request_id": secrets.token_hex(16), "reviewed": True,
            "occupants": [
                {"id": slot["id"], "start": slot["start"], "end": slot["end"],
                 "group_id": "group-a" if index < 2 else "group-b", "first_name": "Érin", "last_name": "Test"}
                for index, slot in enumerate(form["slots"])
            ],
            "sharing_requests": [{"group_id": "group-a", "start": form["start"], "end": form["end"],
                                  "notes": "<b>Separate beds please</b>"}],
        }

    def test_single_exchange_and_logout(self):
        barrier = Barrier(2)

        def exchange():
            barrier.wait()
            return self.exchange()

        with ThreadPoolExecutor(max_workers=2) as workers:
            first, second = workers.submit(exchange), workers.submit(exchange)
            responses = [first.result(), second.result()]
        self.assertEqual(sorted(response.status_code for response in responses), [200, 401])
        active = next(response.json()["token"] for response in responses if response.status_code == 200)
        headers = {**self.key, "X-Pre-Arrival-Session": active}
        self.assertEqual(self.http.delete(self.path + "/session", headers=headers).status_code, 200)
        self.assertEqual(self.http.get(self.path, headers=headers).status_code, 401)

    def test_expiry_revoked_lead_and_missing_key(self):
        headers = self.sign_in()
        form = self.form(headers)
        self.assertNotIn("lead_email", form)
        self.assertNotIn("lead_version", form)
        self.assertEqual(self.http.get(self.path, headers={
            "X-Pre-Arrival-Session": headers["X-Pre-Arrival-Session"],
        }).status_code, 401)
        other_session = self.sign_in(create_fixture(self.service))
        self.service.bookings.update_one({"_id": form["reference"]}, {"$set": {"fixture.lead_version": "changed"}})
        self.assertEqual(self.http.get(self.path, headers=headers).status_code, 401)
        self.current += dt.timedelta(minutes=21)
        self.assertEqual(self.http.get(self.path, headers=other_session).status_code, 401)
        invite = create_fixture(self.service)
        self.current += dt.timedelta(hours=1)
        self.assertEqual(self.exchange(invite).status_code, 401)

    def test_validation_never_saves_an_invalid_form(self):
        headers = self.sign_in()
        form = self.form(headers)
        for invalid in ("reviewed", "physical_room", "overflow", "dates", "duplicate", "control", "sharing"):
            with self.subTest(invalid=invalid):
                body = self.submission(form)
                if invalid == "reviewed":
                    body["reviewed"] = 1
                elif invalid == "physical_room":
                    body["occupants"][0]["room_number"] = "101"
                elif invalid == "overflow":
                    body["occupants"][2]["group_id"] = "group-a"
                elif invalid == "dates":
                    body["occupants"][0]["end"] = form["start"]
                elif invalid == "duplicate":
                    body["occupants"][1]["id"] = body["occupants"][0]["id"]
                elif invalid == "control":
                    body["occupants"][0]["first_name"] = "A\u0000b"
                else:
                    body["sharing_requests"] *= 2
                self.assertEqual(self.http.put(self.path, headers=headers, json=body).status_code, 422)
                self.assertIsNone(self.form(headers)["rooming"])

    def test_durable_replay_readback_scope_and_staff_field(self):
        headers = self.sign_in()
        form = self.form(headers)
        self.service.bookings.update_one({"_id": form["reference"]}, {"$set": {"staff_note": "Keep this note"}})
        body = self.submission(form)
        result = self.http.put(self.path, headers=headers, json=body)
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()["status"], "recorded_in_test")
        self.assertEqual(self.http.put(self.path, headers=headers, json=body).json(), result.json())
        other_headers = self.sign_in(create_fixture(self.service))
        path = self.path + "/submissions/" + result.json()["id"]
        self.assertEqual(self.http.get(path, headers=other_headers).status_code, 404)
        restarted = SandboxRooming(self.database, lambda: self.current)
        self.assertEqual(restarted.result(headers["X-Pre-Arrival-Session"], result.json()["id"])["id"], result.json()["id"])
        record = restarted.bookings.find_one({"_id": form["reference"]})
        self.assertEqual(record["staff_note"], "Keep this note")
        self.assertEqual(len(record["history"]), 1)
        entry = record["history"][0]
        self.assertEqual(entry["submitted"], entry["recorded"])
        self.assertEqual(entry["recorded"], self.form(headers)["rooming"])
        body["occupants"][0]["first_name"] = "Changed"
        self.assertEqual(self.http.put(self.path, headers=headers, json=body).status_code, 409)
        body["request_id"] = secrets.token_hex(16)
        self.assertEqual(self.http.put(self.path, headers=headers, json=body).status_code, 409)
        body["version"] = record["version"]
        body["occupants"][0]["group_id"] = "group-b"
        self.assertEqual(self.http.put(self.path, headers=headers, json=body).status_code, 409)

    def test_concurrent_submissions_allow_one_update(self):
        headers = self.sign_in()
        form = self.form(headers)
        barrier = Barrier(2)

        def write():
            body = self.submission(form)
            barrier.wait()
            return self.http.put(self.path, headers=headers, json=body).status_code

        with ThreadPoolExecutor(max_workers=2) as workers:
            first, second = workers.submit(write), workers.submit(write)
            self.assertEqual(sorted([first.result(), second.result()]), [200, 409])
        self.assertEqual(len(self.service.bookings.find_one({"_id": form["reference"]})["history"]), 1)

    def test_protocol_limits(self):
        response = self.http.post(self.path + "/session", headers=self.key, content='{"invite_token":"redacted"}')
        self.assertEqual(response.status_code, 415)
        oversized = self.http.post(self.path + "/session", headers=self.key, json={"invite_token": "x" * 33000})
        self.assertEqual(oversized.status_code, 413)
        self.assertEqual(oversized.headers["cache-control"], "no-store")
