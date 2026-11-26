"""Booking-scoped N10 access and atomic saves for synthetic records only."""

import datetime as dt
import hashlib
import secrets
from collections import Counter

from fastapi import HTTPException
from pydantic import ValidationError

from rosapenna_backend.api.schemas.pre_arrival_rooming import Fixture, RoomingSnapshot, Submission


def now():
    return dt.datetime.now(dt.UTC)


def digest(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()


class SandboxRooming:
    def __init__(self, database, clock=now):
        if not database.name.startswith("rosapenna_n10_test_"):
            raise ValueError("N10 requires an isolated test database")
        self.bookings = database["rooming_bookings"]
        self.access = database["rooming_access"]
        self.clock = clock
        self.access.create_index("cleanup_at", expireAfterSeconds=0)
        self.access.create_index("session_digest", unique=True, sparse=True)

    def _booking(self, reference):
        row = self.bookings.find_one({"_id": reference, "synthetic": True})
        if row is None:
            raise HTTPException(404, "Rooming details are unavailable")
        try:
            fixture = Fixture.model_validate(row.get("fixture"))
            if row.get("rooming") is not None:
                RoomingSnapshot.model_validate(row["rooming"])
        except ValidationError:
            raise HTTPException(503, "The saved rooming details could not be verified") from None
        if (fixture.reference != reference or not fixture.confirmed
                or fixture.start <= self.clock().date()):
            raise HTTPException(404, "Rooming details are unavailable")
        return row, fixture

    def issue_invite(self, reference: str):
        _, fixture = self._booking(reference)
        token = secrets.token_urlsafe(32)
        expires = self.clock() + dt.timedelta(hours=1)
        self.access.insert_one({
            "_id": digest(token), "reference": reference, "lead_version": fixture.lead_version,
            "invite_expires": expires, "cleanup_at": expires,
        })
        return fixture.lead_email, token

    def exchange(self, token: str):
        current = self.clock()
        invite = self.access.find_one({"_id": digest(token), "invite_expires": {"$gt": current},
                                       "consumed_at": {"$exists": False}})
        self._check_access(invite)
        session = secrets.token_urlsafe(32)
        expiry = current + dt.timedelta(minutes=20)
        consumed = self.access.find_one_and_update(
            {"_id": invite["_id"], "consumed_at": {"$exists": False}, "invite_expires": {"$gt": self.clock()}},
            {"$set": {"consumed_at": current, "session_digest": digest(session),
                      "session_expires": expiry, "cleanup_at": expiry}},
        )
        if consumed is None:
            raise HTTPException(401, "This rooming link or session is unavailable")
        return {"token": session, "expires_at": expiry}

    def _check_access(self, access):
        if access:
            try:
                row, fixture = self._booking(access["reference"])
                if access["lead_version"] == fixture.lead_version:
                    return row, fixture
            except HTTPException:
                pass
        raise HTTPException(401, "This rooming link or session is unavailable")

    def session_booking(self, session):
        return self._check_access(self.access.find_one({
            "session_digest": digest(session), "session_expires": {"$gt": self.clock()},
        }))

    def logout(self, session):
        self.access.delete_one({"session_digest": digest(session)})
        return {"signed_out": True}

    @staticmethod
    def _result(operation):
        return {"status": "recorded_in_test", "id": operation["id"], "recorded_at": operation["recorded_at"]}

    def form(self, session):
        row, fixture = self.session_booking(session)
        history = row.get("history", [])
        return {
            **fixture.model_dump(mode="json", exclude={"lead_email", "lead_version", "confirmed"}),
            "version": row["version"], "rooming": row.get("rooming"),
            "last_submission": self._result(history[-1]) if history else None,
        }

    def result(self, session, operation_id):
        row, _ = self.session_booking(session)
        for operation in row.get("history", []):
            if operation["id"] == operation_id:
                return self._result(operation)
        raise HTTPException(404, "Rooming submission is unavailable")

    @staticmethod
    def _validate(fixture, submission, previous):
        groups = {group.id: group for group in fixture.groups}
        slots = {slot.id: slot for slot in fixture.slots}
        occupants = {occupant.id: occupant for occupant in submission.occupants}
        if slots.keys() != occupants.keys() or len(occupants) != len(submission.occupants):
            raise HTTPException(422, "Enter the details for each booked guest")
        old_assignments = {person["id"]: person["group_id"] for person in (previous or {}).get("occupants", [])}
        nightly = Counter()
        for occupant in submission.occupants:
            slot = slots[occupant.id]
            group = groups.get(occupant.group_id)
            if not group or not group.covers(occupant) or (slot.start, slot.end) != (occupant.start, occupant.end):
                raise HTTPException(422, "Use the booked dates and an eligible room group for each guest")
            if old_assignments.get(occupant.id, group.id) != group.id:
                raise HTTPException(409, "Ask reservations staff to change a saved room group")
            for night in occupant.nights():
                nightly[group.id, night, slot.category] += 1
                adults = nightly[group.id, night, "adult"]
                children = nightly[group.id, night, "child"]
                if adults > group.adult_limit or children > group.child_limit or adults + children > group.total_limit:
                    raise HTTPException(422, "A room group exceeds its limit for one or more nights")
        requested_nights = set()
        for request in submission.sharing_requests:
            group = groups.get(request.group_id)
            if not group or not group.covers(request):
                raise HTTPException(422, "Use booked room dates for sharing requests")
            for night in request.nights():
                key = (group.id, night)
                if key in requested_nights or not (nightly[*key, "adult"] or nightly[*key, "child"]):
                    raise HTTPException(422, "Sharing requests must cover distinct nights with a guest in the room")
                requested_nights.add(key)

    def submit(self, session, submission: Submission):
        row, fixture = self.session_booking(session)
        payload_hash = hashlib.sha256(submission.model_dump_json().encode("utf-8")).hexdigest()
        history = row.get("history", [])
        for entry in history:
            if entry["request_id"] == submission.request_id:
                if entry["payload_hash"] == payload_hash:
                    return self._result(entry)
                raise HTTPException(409, "This request ID is already in use")
        if row["version"] != submission.version:
            raise HTTPException(409, "Rooming details changed. Reload and review the current list")
        if len(history) >= 20:
            raise HTTPException(409, "Ask reservations staff to make another correction")
        self._validate(fixture, submission, row.get("rooming"))
        snapshot = submission.model_dump(mode="json", include={"occupants", "sharing_requests"})
        recorded_at = self.clock()
        operation = {
            "id": secrets.token_hex(16), "request_id": submission.request_id, "payload_hash": payload_hash,
            "submitted_at": recorded_at, "recorded_at": recorded_at, "submitted": snapshot, "recorded": snapshot,
        }
        saved = self.bookings.update_one(
            {"_id": fixture.reference, "synthetic": True, "version": submission.version, "fixture": row["fixture"]},
            {"$set": {"rooming": snapshot}, "$inc": {"version": 1}, "$push": {"history": operation}},
        )
        fresh, _ = self.session_booking(session)
        for entry in fresh.get("history", []):
            if entry["request_id"] == submission.request_id and entry["payload_hash"] == payload_hash:
                if entry["submitted"] == entry["recorded"] == snapshot and (
                    fresh["version"] > submission.version + 1 or fresh["rooming"] == snapshot
                ):
                    return self._result(entry)
        if saved.matched_count == 0:
            raise HTTPException(409, "Rooming details changed. Reload and review the current list")
        raise HTTPException(503, "The test save could not be verified")
