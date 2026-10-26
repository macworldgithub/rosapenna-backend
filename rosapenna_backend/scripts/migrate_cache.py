"""Import legacy SQLite snapshots into MongoDB without replacing existing records."""

import argparse
import json
import sqlite3
from contextlib import closing
from datetime import date, datetime
from pathlib import Path

from pymongo.errors import PyMongoError

from rosapenna_backend.database import DatabaseUnavailable, connect_database
from rosapenna_backend.integrations.brs.tee_sheet import COURSES, validate_sheet
from rosapenna_backend.services.tee_sheets import TeesheetCache


def timestamp(value):
    result = datetime.fromisoformat(value)
    if result.tzinfo is None:
        raise ValueError("Legacy timestamps must include a timezone")
    return result


def read_legacy(path):
    """Validate everything before writing; never change the original SQLite file."""
    source = Path(path).resolve()
    with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        sheets, runs = [], []
        for row in db.execute("SELECT * FROM sheets"):
            day = date.fromisoformat(row["day"]).isoformat()
            if row["course_id"] not in COURSES:
                raise ValueError("Legacy cache contains an unsupported course")
            data = validate_sheet({"data": json.loads(row["data"])}, day)
            sheets.append({"_id": f"{day}:{row['course_id']}", "day": day,
                           "course_id": row["course_id"], "data": data,
                           "fetched_at": timestamp(row["fetched_at"])})
        for row in db.execute("SELECT * FROM refresh_runs"):
            day = date.fromisoformat(row["day"]).isoformat()
            if row["status"] not in ("running", "complete", "partial", "failed"):
                raise ValueError("Legacy cache contains an unknown refresh status")
            errors = json.loads(row["errors"])
            if not isinstance(errors, list) or not all(isinstance(e, str) for e in errors):
                raise ValueError("Legacy cache contains an invalid error list")
            runs.append({"_id": day, "day": day, "started_at": timestamp(row["started_at"]),
                         "finished_at": timestamp(row["finished_at"]) if row["finished_at"] else None,
                         "status": row["status"], "saved": row["saved"], "errors": errors})
    return sheets, runs


def import_legacy(database, sheets, runs):
    cache = TeesheetCache(database)
    inserted = {"sheets": 0, "runs": 0}
    for doc in sheets:
        result = cache.sheets.update_one(
            {"day": doc["day"], "course_id": doc["course_id"]},
            {"$setOnInsert": doc}, upsert=True,
        )
        inserted["sheets"] += result.upserted_id is not None
    for doc in runs:
        result = cache.runs.update_one({"_id": doc["_id"]}, {"$setOnInsert": doc}, upsert=True)
        inserted["runs"] += result.upserted_id is not None
    return inserted


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", default="state/brs-teesheets.sqlite3")
    parser.add_argument("--apply", action="store_true", help="Write missing records to MongoDB")
    args = parser.parse_args()
    client = None
    try:
        sheets, runs = read_legacy(args.source)
        print(f"Validated {len(sheets)} legacy snapshots and {len(runs)} refresh records.")
        if not args.apply:
            print("Dry run only. Add --apply to import; existing MongoDB records will be preserved.")
            return 0
        client, db = connect_database()
        result = import_legacy(db, sheets, runs)
        print(f"Imported {result['sheets']} snapshots and {result['runs']} refresh records.")
        return 0
    except (DatabaseUnavailable, PyMongoError, sqlite3.Error, ValueError, TypeError, KeyError):
        print("Migration failed. Check the legacy cache, backend MongoDB configuration and network access.")
        return 1
    finally:
        if client is not None:
            client.close()


if __name__ == "__main__":
    raise SystemExit(main())
