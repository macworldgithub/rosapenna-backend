"""N11 request capture in the isolated pre-arrival sandbox."""

import datetime as dt
import secrets

from fastapi import HTTPException
from pydantic import Field, ValidationError, field_validator

from rosapenna_backend.api.schemas.pre_arrival_rooming import Identifier, RoomingModel, plain_text
from rosapenna_backend.services.pre_arrival_rooming import SandboxRooming


class TestRound(RoomingModel):
    id: Identifier
    course: str = Field(min_length=1, max_length=80)
    date: dt.date
    golfers: list[Identifier] = Field(min_length=1, max_length=4)


class CaddySubmission(RoomingModel):
    version: int = Field(strict=True, ge=0)
    request_id: Identifier
    golfers: list[Identifier] = Field(max_length=4)
    notes: str = Field(default="", max_length=200)

    _safe_notes = field_validator("notes")(plain_text)


class SandboxCaddy:
    def __init__(self, rooming: SandboxRooming):
        self.rooming = rooming

    def _rounds(self, row, fixture):
        try:
            rounds = [TestRound.model_validate(item) for item in row.get("caddy_rounds", [])]
        except ValidationError:
            raise HTTPException(503, "Test round details could not be verified") from None
        eligible = {slot.id: slot for slot in fixture.slots}
        if len({item.id for item in rounds}) != len(rounds) or any(
            len(set(item.golfers)) != len(item.golfers)
            or not fixture.start <= item.date <= fixture.end
            or any(golfer not in eligible or not eligible[golfer].start <= item.date <= eligible[golfer].end
                   for golfer in item.golfers)
            for item in rounds
        ):
            raise HTTPException(503, "Test round details could not be verified")
        return rounds

    @staticmethod
    def _result(event):
        return {
            "id": event["id"], "version": event["version"], "recorded_at": event["recorded_at"],
            "status": "requested" if event["golfers"] else "withdrawn",
            "golfers": event["golfers"], "requested_count": len(event["golfers"]), "notes": event["notes"],
            "brs_status": "not_sent", "handoff_status": "not_sent",
        }

    def read(self, session):
        row, fixture = self.rooming.session_booking(session)
        history = row.get("caddy_history", [])
        return {
            "booking_reference": fixture.reference,
            "version": row.get("caddy_version", 0),
            "notice": "Caddies are subject to availability and payable on the day.",
            "rounds": [
                {**item.model_dump(mode="json"), "request": next(
                    (self._result(event) for event in reversed(history) if event["round_id"] == item.id), None,
                )}
                for item in self._rounds(row, fixture)
            ],
        }

    def submit(self, session, round_id, submission: CaddySubmission):
        row, fixture = self.rooming.session_booking(session)
        linked = next((item for item in self._rounds(row, fixture) if item.id == round_id), None)
        if linked is None:
            raise HTTPException(404, "Test round is unavailable")
        golfers = sorted(submission.golfers)
        if len(set(golfers)) != len(golfers) or not set(golfers) <= set(linked.golfers):
            raise HTTPException(422, "Select each eligible golfer at most once")
        if not golfers and submission.notes:
            raise HTTPException(422, "Remove the notes when withdrawing a request")
        payload = {"round_id": linked.id, "expected_version": submission.version,
                   "golfers": golfers, "notes": submission.notes}

        def replay(history):
            for event in history:
                if event["request_id"] == submission.request_id:
                    if any(event[key] != value for key, value in payload.items()):
                        raise HTTPException(409, "This retry ID belongs to a different request")
                    return self._result(event)
            return None

        history = row.get("caddy_history", [])
        previous = replay(history)
        if previous is not None:
            return previous
        if row.get("caddy_version", 0) != submission.version or len(history) >= 100:
            raise HTTPException(409, "Refresh the request or contact staff")
        event = {
            **payload, "request_id": submission.request_id, "id": secrets.token_hex(16),
            "recorded_at": self.rooming.clock().isoformat(), "version": submission.version + 1,
            "course": linked.course, "date": linked.date.isoformat(),
            "handoff_status": "not_sent", "brs_status": "not_sent",
        }
        result = self.rooming.bookings.update_one(
            {"_id": fixture.reference, "synthetic": True, "fixture": row["fixture"],
             "caddy_rounds": row["caddy_rounds"],
             "caddy_version": submission.version if "caddy_version" in row else {"$exists": False}},
            {"$inc": {"caddy_version": 1}, "$push": {"caddy_history": event}},
        )
        if result.matched_count:
            return self._result(event)
        current, _ = self.rooming.session_booking(session)
        previous = replay(current.get("caddy_history", []))
        if previous is not None:
            return previous
        raise HTTPException(409, "The request changed. Refresh before saving")
