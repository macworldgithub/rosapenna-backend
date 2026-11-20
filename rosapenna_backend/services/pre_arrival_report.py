"""Read-only staff report for synthetic pre-arrival bookings."""

import datetime as dt
from typing import Literal

from pydantic import AwareDatetime, Field, ValidationError

from rosapenna_backend.api.schemas.pre_arrival_rooming import Fixture, Identifier, RoomingModel, RoomingSnapshot


class BalanceFixture(RoomingModel):
    due: dt.date
    booked_on: dt.date
    total_minor: int = Field(strict=True, ge=0)
    verified_paid_minor: int = Field(strict=True, ge=0)
    updated_at: AwareDatetime


class CaddyEvent(RoomingModel):
    model_config = {"extra": "ignore"}
    round_id: Identifier
    golfers: list[Identifier] = Field(max_length=4)
    recorded_at: AwareDatetime
    handoff_status: Literal["not_sent", "pending", "accepted", "failed", "unknown"]


def report(database, now, *, status=None, arrival_from=None, arrival_to=None, due_from=None, due_to=None):
    if database.name != "rosapenna_n10_test_sandbox":
        raise ValueError("Staff preview requires the local sandbox database")
    if (arrival_from and arrival_to and arrival_from > arrival_to) or (due_from and due_to and due_from > due_to):
        raise ValueError("The end date must be on or after the start date")

    query = {"synthetic": True, "fixture.confirmed": True, "cleanup_at": {"$gt": now}}
    arrival = {"$gte": (arrival_from or now.date()).isoformat()}
    if arrival_to:
        arrival["$lte"] = arrival_to.isoformat()
    query["fixture.start"] = arrival
    projection = {"fixture": 1, "n12_test_balance": 1, "rooming": 1, "history": {"$slice": -1}, "caddy_history": 1}
    bookings = database.rooming_bookings.find(query, projection).sort([("fixture.start", 1), ("_id", 1)]).limit(501)
    cases = []
    for index, row in enumerate(bookings):
        if index == 500:
            raise ValueError("Narrow the arrival dates to report on 500 bookings or fewer")
        fixture = Fixture.model_validate(row["fixture"])
        if fixture.reference != row["_id"]:
            raise ValueError("Test booking reference mismatch")

        def add(kind, state, owner, action, *, updated=None, due=None, **details):
            if status and status != state:
                return
            if (due_from or due_to) and (due is None or (due_from and due < due_from) or (due_to and due > due_to)):
                return
            cases.append({
                "booking_reference": fixture.reference, "arrival": fixture.start.isoformat(),
                "kind": kind, "status": state, "owner": owner, "next_action": action,
                "due": due.isoformat() if due else None,
                "source_updated_at": updated.isoformat() if updated is not None else None,
                **details,
            })

        try:
            balance = BalanceFixture.model_validate(row.get("n12_test_balance"))
        except ValidationError:
            add("balance", "unknown", "Reservations", "Check the verified balance record")
        else:
            remaining = max(0, balance.total_minor - balance.verified_paid_minor)
            if remaining:
                state = "overdue" if balance.due < now.date() else "unpaid"
                add("balance", state, "Reservations", "Review the outstanding balance",
                    updated=balance.updated_at, due=balance.due, remaining_minor=remaining, currency="EUR")
                if balance.booked_on > balance.due:
                    add("balance", "late_booking", "Reservations", "Apply the approved late-booking rules",
                        updated=balance.updated_at, due=balance.due)

        history = row.get("history", [])
        rooming = row.get("rooming")
        try:
            if rooming is not None:
                RoomingSnapshot.model_validate(rooming)
            if rooming is None and not history:
                add("rooming", "missing", "Reservations", "Follow up for the rooming list")
            elif rooming is None or not history or history[-1].get("recorded") != rooming:
                add("rooming", "unknown", "Reservations", "Verify the saved rooming list")
        except ValidationError:
            add("rooming", "unknown", "Reservations", "Verify the saved rooming list")

        seen = set()
        for raw in reversed(row.get("caddy_history", [])):
            try:
                event = CaddyEvent.model_validate(raw)
            except ValidationError:
                add("caddy", "unknown", "Golf operations", "Verify the caddy request history")
                break
            if event.round_id in seen:
                continue
            seen.add(event.round_id)
            if not event.golfers:
                continue
            state = "pending" if event.handoff_status == "not_sent" else event.handoff_status
            actions = {
                "pending": "Check delivery with golf operations",
                "accepted": "No follow-up needed",
                "failed": "Review the delivery failure before retrying",
                "unknown": "Verify the delivery result before retrying",
            }
            add("caddy", state, "Golf operations", actions[state],
                updated=event.recorded_at, round_id=event.round_id, requested_count=len(event.golfers))
    return {"source": "local_test_database", "generated_at": now.isoformat(), "cases": cases}
