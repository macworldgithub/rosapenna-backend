"""Unit tests for Ticket F10: Linked BRS booking writes, amendments, and releases."""

import datetime as dt
import unittest
import uuid
from decimal import Decimal
from unittest.mock import MagicMock, patch
from types import SimpleNamespace

from fastapi import HTTPException
from mongomock import MongoClient

from rosapenna_backend.api.routes.golf_rounds import (
    amend_golf_round,
    create_golf_round,
    require_local_test_rounds,
    release_golf_round,
)
from rosapenna_backend.api.schemas.booking_intents import (
    BookingIntentCreate,
    BrsRoundPayload,
    LeadGuest,
    QuoteSelection,
)
from rosapenna_backend.api.schemas.brs_booking import (
    CaddyRequestPayload,
    CreateBrsRoundRequest,
    ReleaseBrsRoundRequest,
    UpdateBrsRoundRequest,
)
from rosapenna_backend.integrations.brs.booking_client import (
    BrsAuthExpiredError,
    BrsBookingClient,
    BrsConflictError,
    BrsRateLimitError,
    BrsReadbackMismatchError,
    BrsSettings,
)
from rosapenna_backend.integrations.brs.tee_sheet import COURSES
from rosapenna_backend.services.booking_intents import BookingIntentStore, IntentNotFound
from rosapenna_backend.services.tee_sheets import TeesheetCache


def make_test_booking_intent(store: BookingIntentStore) -> dict:
    """Helper to create a fresh booking intent in the test store."""
    payload = BookingIntentCreate(
        selection=QuoteSelection(
            date=dt.date(2026, 7, 15),
            course_id=1,
            tee_time_id=101,
            green_fee_id=201,
            time="09:30",
            holes="18",
            package_name="Links Championship",
            players=2,
            price="360.00",
            currency="EUR",
        ),
        lead_guest=LeadGuest(
            first_name="Rory",
            last_name="McIlroy",
            email="rory@example.com",
            telephone="+353871234567",
        ),
    )
    key = uuid.uuid4()
    intent, _ = store.create(key, payload)
    return intent


class BrsBookingClientTests(unittest.TestCase):
    """Test suite for BrsBookingClient write and readback logic."""

    def setUp(self):
        self.settings = BrsSettings(allow_live_writes=False)
        self.client = BrsBookingClient(self.settings)

    def test_create_round_sandbox_default(self):
        """Default settings route write to verified sandbox with readback verified."""
        req = CreateBrsRoundRequest(
            booking_intent_id="intent-1",
            idempotency_key="key-1",
            course_id=1,
            date=dt.date(2026, 7, 15),
            time="09:30",
            players=2,
            price="360.00",
            currency="EUR",
        )
        res = self.client.create_round(req, booking_reference="RSP-20260715-000001")

        self.assertTrue(res.success)
        self.assertEqual(res.course_id, 1)
        self.assertEqual(res.course_name, "Old Tom Morris Links")
        self.assertEqual(res.time, "09:30")
        self.assertEqual(res.players, 2)
        self.assertEqual(res.price, "360.00")
        self.assertEqual(res.status, "Reserved")
        self.assertTrue(res.verified_via_readback)
        self.assertEqual(res.source, "verified_sandbox")

    def test_direct_golf_changes_require_explicit_local_simulation(self):
        enabled = dict(environment="test", simulation_enabled=True,
                       payment_mode="simulation", simulation_run_id="test-run")
        require_local_test_rounds(SimpleNamespace(**enabled))
        for change in ({"environment": "production"}, {"simulation_run_id": None},
                       {"simulation_enabled": False}, {"payment_mode": "prommt_test"}):
            with self.subTest(change=change), self.assertRaises(HTTPException) as error:
                require_local_test_rounds(SimpleNamespace(**(enabled | change)))
            self.assertEqual(error.exception.status_code, 404)

    def test_create_round_without_saved_record_fails_readback(self):
        req = CreateBrsRoundRequest(
            booking_intent_id="intent-1", idempotency_key="key-1",
            course_id=1, date=dt.date(2026, 7, 15), time="09:30",
            players=2, price="360.00",
        )
        with patch.object(self.client, "_save_sandbox_round"):
            with self.assertRaises(BrsReadbackMismatchError):
                self.client.create_round(req, booking_reference="RSP-20260715-000001")

    def test_create_round_st_patricks_course4(self):
        """Course 4 (St Patrick's Links) writes under tenant stpatricks in sandbox."""
        req = CreateBrsRoundRequest(
            booking_intent_id="intent-stpat",
            idempotency_key="key-stpat",
            course_id=4,
            date=dt.date(2026, 7, 16),
            time="10:00",
            players=4,
            price="880.00",
            currency="EUR",
        )
        res = self.client.create_round(req, booking_reference="RSP-20260716-000002")

        self.assertEqual(res.course_id, 4)
        self.assertEqual(res.course_name, "St Patrick's Links")
        self.assertEqual(res.players, 4)
        self.assertEqual(res.price, "880.00")
        self.assertTrue(res.verified_via_readback)

    def test_caddy_request_payable_on_the_day_zero_charge(self):
        """Caddy request is strictly payable on the day with 0.00 online charge."""
        caddy = CaddyRequestPayload(
            caddies_requested=2,
            caddy_category="caddie",
            notes="Please assign an experienced links caddie",
        )
        self.assertEqual(caddy.payment_terms, "payable_on_the_day")
        self.assertEqual(caddy.online_charge, "0.00")
        self.assertEqual(caddy.status, "requested")

        req = CreateBrsRoundRequest(
            booking_intent_id="intent-caddy",
            idempotency_key="key-caddy",
            course_id=3,
            date=dt.date(2026, 7, 17),
            time="11:00",
            players=2,
            price="400.00",
            caddy_request=caddy,
        )
        res = self.client.create_round(req, booking_reference="RSP-20260717-000003")

        self.assertIsNotNone(res.caddy_request)
        self.assertEqual(res.caddy_request.caddies_requested, 2)
        self.assertEqual(res.caddy_request.payment_terms, "payable_on_the_day")
        self.assertEqual(res.caddy_request.online_charge, "0.00")
        self.assertEqual(res.caddy_request.notes, "Please assign an experienced links caddie")

    def test_caddy_request_validation(self):
        """When caddies_requested is 0, status is not_requested and category is None."""
        caddy_zero = CaddyRequestPayload(caddies_requested=0)
        self.assertEqual(caddy_zero.status, "not_requested")
        self.assertIsNone(caddy_zero.caddy_category)

        # When caddies_requested > 0, caddy_category must be provided
        with self.assertRaises(ValueError):
            CaddyRequestPayload(caddies_requested=2, caddy_category=None)

    def test_readback_mismatch_raises_error(self):
        """If readback attributes differ, BrsReadbackMismatchError is raised."""
        expected = {"course_id": 1, "date": dt.date(2026, 7, 15), "time": "09:30", "players": 2}
        actual_mismatched = {"course_id": 1, "date": dt.date(2026, 7, 15), "time": "10:00", "players": 2}

        with self.assertRaises(BrsReadbackMismatchError):
            self.client._verify_readback(expected, actual_mismatched)

    def test_amend_round_conflict_when_cancelled(self):
        """Cannot amend a round that has status 'Released' or 'Cancelled'."""
        existing = {
            "round_id": "RND-1",
            "course_id": 1,
            "date": "2026-07-15",
            "time": "09:30",
            "players": 2,
            "price": "360.00",
            "status": "Released",
        }
        update_req = UpdateBrsRoundRequest(
            booking_intent_id="intent-1",
            idempotency_key="key-amend",
            round_id="RND-1",
            new_time="10:30",
        )
        with self.assertRaises(BrsConflictError):
            self.client.amend_round(update_req, existing, booking_reference="RSP-1")

    def test_release_round_sandbox(self):
        """Releasing a round marks status as Released."""
        res = self.client.release_round("RND-RELEASE-1", booking_reference="RSP-1")
        self.assertTrue(res.success)
        self.assertEqual(res.status, "Released")

    def test_existing_round_cannot_be_changed_through_another_booking(self):
        created = self.client.create_round(CreateBrsRoundRequest(
            booking_intent_id="intent-owner",
            idempotency_key="key-owner",
            course_id=1,
            date=dt.date(2026, 7, 15),
            time="09:30",
            players=2,
            price="360.00",
        ), booking_reference="RSP-OWNER")
        existing = created.model_dump()
        update = UpdateBrsRoundRequest(
            booking_intent_id="intent-other",
            idempotency_key="key-other",
            round_id=created.round_id,
            new_time="10:30",
        )

        with self.assertRaises(BrsConflictError):
            self.client.amend_round(update, existing, booking_reference="RSP-OTHER")
        with self.assertRaises(BrsConflictError):
            self.client.update_round_caddy_request(
                created.round_id, existing, CaddyRequestPayload(caddies_requested=1, caddy_category="caddie"), "RSP-OTHER",
            )
        with self.assertRaises(BrsConflictError):
            self.client.release_round(created.round_id, booking_reference="RSP-OTHER")

        saved = self.client._sandbox_rounds[created.round_id]
        self.assertEqual(saved, created)
        self.assertEqual(self.client.release_round(created.round_id, "RSP-OWNER").status, "Released")


class BrsRouteHandlerTests(unittest.TestCase):
    """Test suite for FastAPI route endpoints in golf_rounds.py."""

    def setUp(self):
        self.mongo = MongoClient(tz_aware=True)
        self.collection = self.mongo["test_brs_db"].booking_intents
        self.store = BookingIntentStore(self.collection)

        self.client = BrsBookingClient(BrsSettings(allow_live_writes=False))
        self.cache = MagicMock(spec=TeesheetCache)
        self.cache.get.return_value = {
            "data": {"tee_times": [{
                "time": "09:30",
                "slots": {str(i): {"status": "Available"} for i in range(4)},
            }]}
        }

    def tearDown(self):
        self.mongo.close()

    def test_route_create_round_success_and_links_to_intent(self):
        """POST /golf/rounds writes round and atomically links it into BookingIntentStore."""
        intent = make_test_booking_intent(self.store)

        req = CreateBrsRoundRequest(
            booking_intent_id=intent["booking_intent_id"],
            idempotency_key=str(uuid.uuid4()),
            course_id=1,
            date=dt.date(2026, 7, 15),
            time="09:30",
            players=2,
            price="360.00",
            caddy_request=CaddyRequestPayload(caddies_requested=1, caddy_category="caddie"),
        )
        res = create_golf_round(
            request=req,
            client=self.client,
            booking_store=self.store,
            teesheet_cache=self.cache,
        )

        self.assertTrue(res.success)
        self.assertEqual(res.course_id, 1)
        self.assertEqual(res.status, "Reserved")
        self.assertFalse(res.idempotency_replayed)

        # Inspect intent document in MongoDB
        refreshed = self.store.get(intent["booking_intent_id"])
        rounds = refreshed["links"]["brs_rounds"]
        self.assertEqual(len(rounds), 1)
        self.assertEqual(rounds[0]["round_id"], res.round_id)
        self.assertEqual(rounds[0]["caddy_request"]["caddies_requested"], 1)
        self.assertEqual(rounds[0]["caddy_request"]["payment_terms"], "payable_on_the_day")

    def test_route_create_round_idempotent_replay(self):
        """Repeating the same round creation replays the existing record without duplicates."""
        intent = make_test_booking_intent(self.store)

        req = CreateBrsRoundRequest(
            booking_intent_id=intent["booking_intent_id"],
            idempotency_key=str(uuid.uuid4()),
            course_id=3,
            date=dt.date(2026, 7, 18),
            time="09:30",
            players=2,
            price="380.00",
        )
        res1 = create_golf_round(req, self.client, self.store, self.cache)
        self.assertFalse(res1.idempotency_replayed)

        # Second call with same parameters
        res2 = create_golf_round(req, self.client, self.store, self.cache)
        self.assertTrue(res2.idempotency_replayed)
        self.assertEqual(res1.round_id, res2.round_id)

        # Verify only one round exists in MongoDB
        refreshed = self.store.get(intent["booking_intent_id"])
        self.assertEqual(len(refreshed["links"]["brs_rounds"]), 1)

    def test_route_create_round_conflict_when_slot_sold_out(self):
        """Reject a partially booked row even when there is room for the request."""
        intent = make_test_booking_intent(self.store)

        self.cache.get.return_value["data"]["tee_times"][0]["slots"]["0"]["status"] = "Booked"

        req = CreateBrsRoundRequest(
            booking_intent_id=intent["booking_intent_id"],
            idempotency_key=str(uuid.uuid4()),
            course_id=1,
            date=dt.date(2026, 7, 15),
            time="09:30",
            players=2,
            price="360.00",
        )
        with self.assertRaises(HTTPException) as ctx:
            create_golf_round(req, self.client, self.store, self.cache)
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertIn("occupied", ctx.exception.detail)
        self.assertFalse(self.client._sandbox_rounds)

    def test_route_create_round_rejects_stale_sheet_for_another_booking(self):
        first_intent = make_test_booking_intent(self.store)
        second_intent = make_test_booking_intent(self.store)
        request = CreateBrsRoundRequest(
            booking_intent_id=first_intent["booking_intent_id"],
            idempotency_key=str(uuid.uuid4()),
            course_id=1,
            date=dt.date(2026, 7, 15),
            time="09:30",
            players=2,
            price="360.00",
        )
        first = create_golf_round(request, self.client, self.store, self.cache)
        second = request.model_copy(update={
            "booking_intent_id": second_intent["booking_intent_id"],
            "idempotency_key": str(uuid.uuid4()),
        })
        with self.assertRaises(HTTPException) as ctx:
            create_golf_round(second, self.client, self.store, self.cache)
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertEqual(list(self.client._sandbox_rounds.values()), [first])

    def test_route_readback_mismatch_marks_recovery_required(self):
        """When client throws BrsReadbackMismatchError, booking intent transitions to recovery_required."""
        intent = make_test_booking_intent(self.store)

        mock_failing_client = MagicMock(spec=BrsBookingClient)
        mock_failing_client.create_round.side_effect = BrsReadbackMismatchError("Mismatch detected")

        req = CreateBrsRoundRequest(
            booking_intent_id=intent["booking_intent_id"],
            idempotency_key=str(uuid.uuid4()),
            course_id=1,
            date=dt.date(2026, 7, 15),
            time="09:30",
            players=2,
            price="360.00",
        )
        with self.assertRaises(HTTPException) as ctx:
            create_golf_round(req, mock_failing_client, self.store, self.cache)

        self.assertEqual(ctx.exception.status_code, 502)

        # Verify booking intent was transitioned to recovery_required
        refreshed = self.store.get(intent["booking_intent_id"])
        self.assertEqual(refreshed["stage"], "recovery_required")

    def test_route_amend_and_release_round(self):
        """PUT /golf/rounds/{round_id} amends round, and DELETE releases it."""
        intent = make_test_booking_intent(self.store)

        # 1. Create initial round
        create_req = CreateBrsRoundRequest(
            booking_intent_id=intent["booking_intent_id"],
            idempotency_key=str(uuid.uuid4()),
            course_id=1,
            date=dt.date(2026, 7, 15),
            time="09:30",
            players=2,
            price="360.00",
        )
        created = create_golf_round(create_req, self.client, self.store, self.cache)

        # 2. Amend round
        amend_req = UpdateBrsRoundRequest(
            booking_intent_id=intent["booking_intent_id"],
            idempotency_key=str(uuid.uuid4()),
            round_id=created.round_id,
            new_players=3,
            new_price="540.00",
            caddy_request=CaddyRequestPayload(caddies_requested=2, caddy_category="forecaddie"),
        )
        amended = amend_golf_round(
            round_id=created.round_id,
            request=amend_req,
            client=self.client,
            booking_store=self.store,
            teesheet_cache=self.cache,
        )
        self.assertEqual(amended.status, "Amended")
        self.assertEqual(amended.players, 3)
        self.assertEqual(amended.price, "540.00")
        self.assertEqual(amended.caddy_request.caddy_category, "forecaddie")

        # 3. Release round (F14 coordination)
        rel_req = ReleaseBrsRoundRequest(
            booking_intent_id=intent["booking_intent_id"],
            idempotency_key=str(uuid.uuid4()),
            round_id=created.round_id,
        )
        released = release_golf_round(
            round_id=created.round_id,
            request=rel_req,
            client=self.client,
            booking_store=self.store,
        )
        self.assertEqual(released.status, "Released")

        # Verify final state in MongoDB
        refreshed = self.store.get(intent["booking_intent_id"])
        target_round = next(r for r in refreshed["links"]["brs_rounds"] if r["round_id"] == created.round_id)
        self.assertEqual(target_round["status"], "Released")


if __name__ == "__main__":
    unittest.main()
