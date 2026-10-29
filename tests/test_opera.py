"""Unit tests for Oracle Opera Cloud / OHIP client and API endpoints."""

import datetime as dt
import unittest
import uuid
from unittest.mock import MagicMock, patch
import urllib.error

from fastapi import HTTPException
from mongomock import MongoClient
from pydantic import SecretStr, ValidationError

from rosapenna_backend.api.routes.rooms import (
    amend_room_reservation,
    create_room_reservation,
    get_room_availability,
    get_room_reservation,
)
from rosapenna_backend.api.schemas.booking_intents import BookingIntentCreate
from rosapenna_backend.integrations.opera.client import (
    OperaAuthError,
    OperaClient,
    OperaConflictError,
    OperaIntegrationError,
    OperaNotFoundError,
    OperaPermissionError,
    OperaRateLimitError,
    OperaReadbackMismatchError,
    OperaSettings,
    OperaUnavailableError,
)
from rosapenna_backend.integrations.opera.schemas import (
    CreateReservationRequest,
    RoomOccupant,
    RoomReservationDetail,
    UpdateReservationRequest,
)
from rosapenna_backend.services.booking_intents import BookingIntentStore


def make_mock_client():
    settings = OperaSettings(
        hostname="",
        gateway_url="",
        client_id="",
        client_secret=SecretStr(""),
        app_key=SecretStr(""),
        enterprise_id="",
        hotel_id="ROSAPENNA",
        token=SecretStr(""),
        enable_mock_fallback=True,
        allow_live_writes=False,
    )
    return OperaClient(settings)


def make_draft_intent(store: BookingIntentStore) -> dict:
    key = uuid.uuid4()
    req = BookingIntentCreate.model_validate({
        "selection": {
            "date": "2027-06-10",
            "course_id": 1,
            "tee_time_id": 101,
            "green_fee_id": 7,
            "time": "10:00",
            "holes": "18",
            "package_name": "Standard Visitor",
            "players": 2,
            "price": "200",
            "currency": "EUR",
        },
        "lead_guest": {
            "first_name": "Test",
            "last_name": "Golfer",
            "email": "test@example.com",
            "telephone": "+353871234567",
        },
    })
    intent, _ = store.create(key, req)
    return intent


class OperaClientUnitTests(unittest.TestCase):
    def setUp(self):
        self.settings = OperaSettings(
            hostname="https://ocim-gateway-eu1.oraclehospitality.com",
            client_id="test-client-id",
            client_secret=SecretStr("test-secret"),
            app_key=SecretStr("test-app-key"),
            enterprise_id="test-enterprise",
            hotel_id="ROSAPENNA",
            token=SecretStr(""),
            enable_mock_fallback=True,
            allow_live_writes=False,
        )
        self.client = OperaClient(self.settings)

    def test_configuration_detection(self):
        self.assertTrue(self.client.is_configured)

        empty_client = make_mock_client()
        self.assertFalse(empty_client.is_configured)

    def test_availability_date_validation(self):
        arrival = dt.date(2027, 6, 10)
        departure = dt.date(2027, 6, 10)
        with self.assertRaises(OperaIntegrationError) as ctx:
            self.client.get_room_availability(arrival, departure)
        self.assertEqual(ctx.exception.status_code, 400)

    def test_fixture_availability_returns_expected_room_types(self):
        empty_client = make_mock_client()
        arrival = dt.date(2027, 6, 1)
        departure = dt.date(2027, 6, 4)

        result = empty_client.get_room_availability(arrival, departure, adults=2)
        self.assertEqual(result.source, "fixture_fallback")
        self.assertEqual(result.hotel_id, "ROSAPENNA")
        self.assertEqual(len(result.categories), 4)

        cat_codes = [c.room_category for c in result.categories]
        self.assertIn("CLASSIC", cat_codes)
        self.assertIn("DELUXE", cat_codes)
        self.assertIn("BAYVIEW", cat_codes)
        self.assertIn("PENTHOUSE", cat_codes)

        classic = next(c for c in result.categories if c.room_category == "CLASSIC")
        self.assertTrue(classic.rates[0].breakfast_included)
        self.assertGreater(classic.rates[0].total_amount, 0)

    def test_fixture_reservation_lookup(self):
        empty_client = make_mock_client()
        res = empty_client.get_reservation("RES12345")
        self.assertEqual(res.reservation_id, "RES12345")
        self.assertEqual(res.source, "fixture_fallback")
        self.assertIsNone(res.room_number)
        self.assertGreaterEqual(len(res.occupants), 1)

    @patch("urllib.request.urlopen")
    def test_oauth_token_exchange(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.read.return_value = b'{"access_token": "mock-token-xyz", "expires_in": 3600}'
        mock_resp.__enter__.return_value = mock_resp
        mock_urlopen.return_value = mock_resp

        token = self.client._get_oauth_token()
        self.assertEqual(token, "mock-token-xyz")
        token2 = self.client._get_oauth_token()
        self.assertEqual(token2, "mock-token-xyz")
        mock_urlopen.assert_called_once()

    @patch("urllib.request.urlopen")
    def test_oauth_auth_error_handling(self, mock_urlopen):
        mock_urlopen.side_effect = urllib.error.HTTPError("url", 401, "Unauthorized", {}, None)
        with self.assertRaises(OperaAuthError):
            self.client._get_oauth_token()

    @patch("urllib.request.urlopen")
    def test_oauth_rate_limit_handling(self, mock_urlopen):
        mock_urlopen.side_effect = urllib.error.HTTPError("url", 429, "Too Many Requests", {}, None)
        with self.assertRaises(OperaRateLimitError):
            self.client._get_oauth_token()

    # --- Ticket F08 Write Tests ---

    def test_create_reservation_schema_blocks_room_number(self):
        """Verify that client writes strictly forbid physical room number selection."""
        with self.assertRaises(ValidationError):
            CreateReservationRequest.model_validate({
                "booking_intent_id": "test-intent-id",
                "arrival_date": "2027-06-10",
                "departure_date": "2027-06-13",
                "room_category": "DELUXE",
                "occupants": [{"first_name": "Test", "last_name": "User", "is_primary": True}],
                "room_number": "101",  # Forbidden by schema
            })

    def test_create_reservation_schema_date_order(self):
        """Departure date must be after arrival date."""
        with self.assertRaises(ValidationError):
            CreateReservationRequest.model_validate({
                "booking_intent_id": "test-intent-id",
                "arrival_date": "2027-06-15",
                "departure_date": "2027-06-14",
                "room_category": "DELUXE",
                "occupants": [{"first_name": "Test", "last_name": "User", "is_primary": True}],
            })

    def test_create_reservation_sandbox_execution(self):
        """Verify verified sandbox creates reservation with readback verification."""
        req = CreateReservationRequest(
            booking_intent_id="intent-test-1",
            arrival_date=dt.date(2027, 6, 10),
            departure_date=dt.date(2027, 6, 13),
            room_category="DELUXE",
            occupants=[RoomOccupant(first_name="Alice", last_name="Smith", is_primary=True)],
            guest_phone="+353871234567",
            guest_email="alice@example.com",
        )
        result = self.client.create_reservation(req)
        self.assertTrue(result.reservation_id.startswith("OPERA-"))
        self.assertEqual(result.status, "Confirmed")
        self.assertEqual(result.room_category, "DELUXE")
        self.assertEqual(result.source, "fixture_sandbox")
        self.assertTrue(result.verified_via_readback)
        self.assertGreater(result.total_amount, 0)

        # Subsequent lookup reads back the created sandbox reservation
        readback = self.client.get_reservation(result.reservation_id)
        self.assertEqual(readback.reservation_id, result.reservation_id)
        self.assertIsNone(readback.room_number)
        self.assertEqual(readback.arrival_date, dt.date(2027, 6, 10))

    def test_amend_reservation_sandbox_execution(self):
        """Verify amendment updates requested fields and preserves state."""
        # 1. Create initial reservation
        create_req = CreateReservationRequest(
            booking_intent_id="intent-test-2",
            arrival_date=dt.date(2027, 6, 10),
            departure_date=dt.date(2027, 6, 12),
            room_category="CLASSIC",
            occupants=[RoomOccupant(first_name="Bob", last_name="Jones", is_primary=True)],
        )
        created = self.client.create_reservation(create_req)

        # 2. Amend departure date and category
        amend_req = UpdateReservationRequest(
            reservation_id=created.reservation_id,
            departure_date=dt.date(2027, 6, 14),
            room_category="BAYVIEW",
        )
        amended = self.client.amend_reservation(amend_req)
        self.assertEqual(amended.reservation_id, created.reservation_id)
        self.assertEqual(amended.room_category, "BAYVIEW")
        self.assertEqual(amended.departure_date, dt.date(2027, 6, 14))

        # 3. Readback confirms preserved occupants and updated fields
        readback = self.client.get_reservation(created.reservation_id)
        self.assertEqual(readback.room_category, "BAYVIEW")
        self.assertEqual(readback.occupants[0].last_name, "Jones")

    def test_amend_reservation_conflict_on_cancelled_status(self):
        """Cannot amend a reservation that is cancelled."""
        create_req = CreateReservationRequest(
            booking_intent_id="intent-test-3",
            arrival_date=dt.date(2027, 6, 10),
            departure_date=dt.date(2027, 6, 12),
            room_category="CLASSIC",
            occupants=[RoomOccupant(first_name="Bob", last_name="Jones", is_primary=True)],
        )
        created = self.client.create_reservation(create_req)

        # Force status to Cancelled in sandbox
        self.client._sandbox_reservations[created.reservation_id].status = "Cancelled"

        amend_req = UpdateReservationRequest(
            reservation_id=created.reservation_id,
            room_category="DELUXE",
        )
        with self.assertRaises(OperaConflictError):
            self.client.amend_reservation(amend_req)

    def test_readback_mismatch_detection(self):
        """Verify that readback verification raises an error if Opera returns mismatched values."""
        req = CreateReservationRequest(
            booking_intent_id="intent-test-4",
            arrival_date=dt.date(2027, 6, 10),
            departure_date=dt.date(2027, 6, 13),
            room_category="DELUXE",
            occupants=[RoomOccupant(first_name="Charlie", last_name="Brown", is_primary=True)],
        )

        mismatched_readback = RoomReservationDetail(
            reservation_id="OPERA-MISMATCH",
            confirmation_number="CONF-MISMATCH",
            hotel_id="ROSAPENNA",
            status="Confirmed",
            arrival_date=dt.date(2027, 6, 11),  # Mismatched date!
            departure_date=dt.date(2027, 6, 13),
            room_category="DELUXE",
            total_amount=600.0,
            currency="EUR",
            source="opera_cloud",
        )

        with self.assertRaises(OperaReadbackMismatchError):
            self.client._verify_readback(req, mismatched_readback)


class OperaRouteHandlerTests(unittest.TestCase):
    def setUp(self):
        self.client = make_mock_client()
        self.mongo = MongoClient(tz_aware=True)
        self.collection = self.mongo["opera_test_db"].booking_intents
        self.store = BookingIntentStore(self.collection)

    def tearDown(self):
        self.mongo.close()

    def test_route_availability_success(self):
        res = get_room_availability(
            arrival_date=dt.date(2027, 5, 10),
            departure_date=dt.date(2027, 5, 13),
            adults=2,
            children=0,
            client=self.client,
        )
        self.assertEqual(res.hotel_id, "ROSAPENNA")
        self.assertEqual(len(res.categories), 4)

    def test_route_availability_invalid_dates(self):
        with self.assertRaises(HTTPException) as ctx:
            get_room_availability(
                arrival_date=dt.date(2027, 5, 13),
                departure_date=dt.date(2027, 5, 10),
                client=self.client,
            )
        self.assertEqual(ctx.exception.status_code, 400)

    def test_route_reservation_lookup(self):
        res = get_room_reservation(
            reservation_id="OPERA-RES-99",
            client=self.client,
        )
        self.assertEqual(res.reservation_id, "OPERA-RES-99")
        self.assertIsNone(res.room_number)

    def test_route_create_room_reservation_links_intent(self):
        """POST /rooms/reservations creates reservation and links to MongoDB intent."""
        intent = make_draft_intent(self.store)
        req = CreateReservationRequest(
            booking_intent_id=intent["booking_intent_id"],
            arrival_date=dt.date(2027, 6, 10),
            departure_date=dt.date(2027, 6, 13),
            room_category="DELUXE",
            occupants=[RoomOccupant(first_name="Test", last_name="Golfer", is_primary=True)],
        )

        res = create_room_reservation(
            request=req,
            client=self.client,
            booking_store=self.store,
        )
        self.assertTrue(res.reservation_id.startswith("OPERA-"))
        self.assertEqual(res.status, "Confirmed")
        self.assertFalse(res.idempotency_replayed)

        # Verify MongoDB booking intent was updated with linked Opera reservation
        updated_intent = self.store.get(intent["booking_intent_id"])
        self.assertEqual(updated_intent["reservation_state"], "reserved")
        linked_opera = updated_intent["links"]["opera_reservation"]
        self.assertEqual(linked_opera["reservation_id"], res.reservation_id)
        self.assertEqual(linked_opera["room_category"], "DELUXE")

    def test_route_create_room_reservation_idempotency_replay(self):
        """Repeating the request returns the existing reservation without duplicate creation."""
        intent = make_draft_intent(self.store)
        req = CreateReservationRequest(
            booking_intent_id=intent["booking_intent_id"],
            arrival_date=dt.date(2027, 6, 10),
            departure_date=dt.date(2027, 6, 13),
            room_category="DELUXE",
            occupants=[RoomOccupant(first_name="Test", last_name="Golfer", is_primary=True)],
        )

        first_res = create_room_reservation(req, self.client, self.store)
        second_res = create_room_reservation(req, self.client, self.store)

        self.assertEqual(first_res.reservation_id, second_res.reservation_id)
        self.assertFalse(first_res.idempotency_replayed)
        self.assertTrue(second_res.idempotency_replayed)

    def test_route_create_readback_failure_flags_recovery(self):
        """If Opera readback fails, booking intent stage transitions to recovery_required."""
        intent = make_draft_intent(self.store)
        req = CreateReservationRequest(
            booking_intent_id=intent["booking_intent_id"],
            arrival_date=dt.date(2027, 6, 10),
            departure_date=dt.date(2027, 6, 13),
            room_category="DELUXE",
            occupants=[RoomOccupant(first_name="Test", last_name="Golfer", is_primary=True)],
        )

        with patch.object(self.client, "create_reservation", side_effect=OperaReadbackMismatchError("Mismatch in dates")):
            with self.assertRaises(HTTPException) as ctx:
                create_room_reservation(req, self.client, self.store)
            self.assertEqual(ctx.exception.status_code, 502)

        # Verify intent in MongoDB is marked recovery_required
        updated = self.store.get(intent["booking_intent_id"])
        self.assertEqual(updated["stage"], "recovery_required")

    def test_route_amend_room_reservation(self):
        """PUT /rooms/reservations/{id} updates Opera reservation and updates linked intent."""
        intent = make_draft_intent(self.store)
        create_req = CreateReservationRequest(
            booking_intent_id=intent["booking_intent_id"],
            arrival_date=dt.date(2027, 6, 10),
            departure_date=dt.date(2027, 6, 12),
            room_category="CLASSIC",
            occupants=[RoomOccupant(first_name="Test", last_name="Golfer", is_primary=True)],
        )
        created = create_room_reservation(create_req, self.client, self.store)

        # Amend reservation
        update_req = UpdateReservationRequest(
            reservation_id=created.reservation_id,
            departure_date=dt.date(2027, 6, 15),
            room_category="PENTHOUSE",
        )
        amended = amend_room_reservation(created.reservation_id, update_req, self.client, self.store)
        self.assertEqual(amended.room_category, "PENTHOUSE")
        self.assertEqual(amended.departure_date, dt.date(2027, 6, 15))

        # Check intent updated
        updated_intent = self.store.get(intent["booking_intent_id"])
        linked_opera = updated_intent["links"]["opera_reservation"]
        self.assertEqual(linked_opera["room_category"], "PENTHOUSE")


if __name__ == "__main__":
    unittest.main()
