"""Database configuration and legacy-import regressions. No external connections."""

import json
import asyncio
import os
import sqlite3
import tempfile
import unittest
from types import SimpleNamespace
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

from mongomock import MongoClient
from pymongo.errors import ServerSelectionTimeoutError
from starlette.requests import Request

from rosapenna_backend.database import DatabaseUnavailable, MongoSettings, connect_database
from rosapenna_backend.scripts.migrate_cache import import_legacy, read_legacy
from tests.test_teesheets import sheet


class DatabaseTests(unittest.TestCase):
    def test_missing_uri_fails_without_localhost_fallback(self):
        with self.assertRaisesRegex(DatabaseUnavailable, "MONGODB_URI"):
            connect_database(MongoSettings(uri="", _env_file=None))

    def test_connection_failure_hides_credentials_and_closes_client(self):
        client = MagicMock()
        client.get_database.return_value.command.side_effect = ServerSelectionTimeoutError("private-password")
        settings = MongoSettings(uri="mongodb://user:private-password@localhost/", database="rosapenna", _env_file=None)
        self.assertNotIn("private-password", repr(settings))
        with patch("rosapenna_backend.database.MongoClient", return_value=client):
            with self.assertRaises(DatabaseUnavailable) as raised:
                connect_database(settings)
        self.assertNotIn("private-password", str(raised.exception))
        client.close.assert_called_once()

    def test_database_override_precedes_uri_default(self):
        client = MagicMock()
        with patch("rosapenna_backend.database.MongoClient", return_value=client):
            connected, db = connect_database(MongoSettings(
                uri="mongodb://localhost/uri_database", database="chosen_database", _env_file=None,
            ))
        self.assertIs(connected, client)
        self.assertIs(db, client.get_database.return_value)
        self.assertEqual(client.get_database.call_args.args[0], "chosen_database")
        client.get_default_database.assert_not_called()

    def test_uri_database_or_rosapenna_fallback_is_used(self):
        client = MagicMock()
        client.get_default_database.return_value.name = "uri_database"
        with patch("rosapenna_backend.database.MongoClient", return_value=client):
            connect_database(MongoSettings(uri="mongodb://localhost/uri_database", database=None, _env_file=None))
        client.get_default_database.assert_called_once_with(default="rosapenna")
        self.assertEqual(client.get_database.call_args.args[0], "uri_database")

    def test_migration_is_read_only_idempotent_and_does_not_overwrite_newer_data(self):
        day = "2026-10-14"
        data = sheet(day)
        data["tee_times"][0]["slots"]["0"]["name_on_teesheet"] = "Private name"
        fetched = "2026-10-14T08:00:00+00:00"
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "legacy.sqlite3"
            with closing(sqlite3.connect(path)) as sql, sql:
                sql.execute("CREATE TABLE sheets(day TEXT, course_id INTEGER, data TEXT, fetched_at TEXT)")
                sql.execute("CREATE TABLE refresh_runs(day TEXT, started_at TEXT, finished_at TEXT, status TEXT, saved INTEGER, errors TEXT)")
                sql.execute("INSERT INTO sheets VALUES (?, ?, ?, ?)", (day, 1, json.dumps(data), fetched))
                sql.execute("INSERT INTO refresh_runs VALUES (?, ?, ?, ?, ?, ?)", (day, fetched, fetched, "complete", 1, "[]"))
            original = path.read_bytes()
            sheets, runs = read_legacy(path)
            self.assertEqual(path.read_bytes(), original)
        db = MongoClient(tz_aware=True).migration_test
        self.assertEqual(import_legacy(db, sheets, runs), {"sheets": 1, "runs": 1})
        saved = db.tee_sheets.find_one({"day": day})
        self.assertIsNone(saved["data"]["tee_times"][0]["slots"]["0"]["name_on_teesheet"])
        self.assertEqual(saved["fetched_at"], datetime(2026, 10, 14, 8, tzinfo=timezone.utc))
        db.tee_sheets.update_one({"day": day}, {"$set": {"data.currency_code": "GBP"}})
        self.assertEqual(import_legacy(db, sheets, runs), {"sheets": 0, "runs": 0})
        self.assertEqual(db.tee_sheets.find_one({"day": day})["data"]["currency_code"], "GBP")


class ApiDatabaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with patch.dict(os.environ, {"BOOKING_LOG_FILE": os.devnull}):
            from rosapenna_backend.api.routes import availability, health
            from rosapenna_backend.core import errors
        cls.api = SimpleNamespace(availability=availability, health=health, errors=errors)

    def test_readiness_returns_503_when_mongodb_fails_without_diagnostics(self):
        db = MagicMock()
        db.command.side_effect = ServerSelectionTimeoutError("private-uri")
        request = Request({
            "type": "http", "method": "GET", "path": "/health/ready", "headers": [],
            "query_string": b"", "app": SimpleNamespace(state=SimpleNamespace(mongo_database=db)),
        })
        response = self.api.health.readiness(request)
        self.assertEqual(response.status_code, 503)
        self.assertNotIn(b"private-uri", response.body)
        self.assertEqual(json.loads(response.body)["status"], "unavailable")

    def test_database_failure_is_503_and_not_mislabeled_as_brs_failure(self):
        failure = ServerSelectionTimeoutError("private-uri")
        cache = MagicMock()
        cache.revalidate.side_effect = failure
        with self.assertRaises(ServerSelectionTimeoutError):
            self.api.availability.revalidate_teesheet(None, cache)
        with self.assertLogs("booking_api", level="WARNING"):
            response = asyncio.run(self.api.errors.database_exception_handler(None, failure))
        self.assertEqual(response.status_code, 503)
        self.assertNotIn(b"private-uri", response.body)


if __name__ == "__main__":
    unittest.main()
