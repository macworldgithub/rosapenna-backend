"""Booking-intent idempotency and revision checks; synthetic data only."""

import asyncio
import os
import unittest
import uuid
from unittest.mock import patch

from mongomock import MongoClient
from fastapi.exceptions import RequestValidationError
from pydantic import ValidationError
from starlette.requests import Request

from rosapenna_backend.api.schemas.booking_intents import BookingIntentAction, BookingIntentCreate
from rosapenna_backend.core.errors import request_validation_exception_handler
from rosapenna_backend.services.booking_intents import BookingIntentStore, IntentConflict


def draft(**changes):
    data = {
        "selection": {
            "date": "2026-10-23", "course_id": 1, "tee_time_id": 101,
            "green_fee_id": 7, "time": "10:00", "holes": "18",
            "package_name": "Standard Visitor", "players": 2,
            "price": "200", "currency": "EUR",
        },
        "lead_guest": {
            "first_name": "Synthetic", "last_name": "Guest",
            "email": "synthetic@example.test", "telephone": "+441632960000",
        },
    }
    data.update(changes)
    return BookingIntentCreate.model_validate(data)


class BookingIntentTests(unittest.TestCase):
    def setUp(self):
        self.mongo = MongoClient(tz_aware=True)
        self.collection = self.mongo["booking_intent_test"].booking_intents
        self.store = BookingIntentStore(self.collection)
        self.key = uuid.uuid4()

    def tearDown(self):
        self.mongo.close()

    def action(self, name, revision, **evidence):
        return BookingIntentAction(
            action_id=uuid.uuid4(), action=name, expected_revision=revision, **evidence,
        )

    def test_create_is_idempotent_and_conflicting_reuse_is_rejected(self):
        first, created = self.store.create(self.key, draft())
        replay, replay_created = self.store.create(self.key, draft())
        self.assertTrue(created)
        self.assertFalse(replay_created)
        self.assertEqual(first["booking_intent_id"], replay["booking_intent_id"])
        self.assertEqual(self.collection.count_documents({}), 1)
        self.assertEqual(first["stage"], "draft")
        self.assertEqual(first["payment_state"], "not_started")
        self.assertEqual(first["reservation_state"], "not_confirmed")
        self.assertEqual(first["source"], "website_checkout")
        self.assertEqual(first["quote"]["selected"]["source"], "website_submitted_quote")
        self.assertEqual(first["selection"]["holes"], "18")
        self.assertEqual(first["lead_guest"]["email"], "synthetic@example.test")
        self.assertEqual(first["links"], {"opera_reservation": None, "brs_rounds": [], "payments": []})
        self.assertNotIn("idempotency_key_hash", first)
        self.assertNotIn("create_fingerprint", first)
        with self.assertRaises(IntentConflict):
            self.store.create(self.key, draft(lead_guest={
                "first_name": "Changed", "last_name": "Guest",
                "email": "synthetic@example.test", "telephone": "+441632960000",
            }))
        self.assertEqual(self.collection.count_documents({}), 1)

    def test_quote_and_action_history_are_versioned_without_confirmation_claims(self):
        intent, _ = self.store.create(self.key, draft())
        updated, added = self.store.append_action(intent["booking_intent_id"], self.action(
            "live_revalidation_passed", 1,
            quote={"price": "200.00", "currency": "EUR"},
        ))
        self.assertTrue(added)
        self.assertEqual(updated["revision"], 2)
        self.assertEqual(updated["stage"], "quote_revalidated")
        self.assertEqual(updated["quote"]["version"], 2)
        self.assertEqual(updated["quote"]["live_revalidated"]["source"], "live_teesheet_revalidation")
        replay, added_again = self.store.append_action(
            intent["booking_intent_id"],
            BookingIntentAction.model_validate({
                "action_id": updated["actions"][-1]["action_id"],
                "action": "live_revalidation_passed", "expected_revision": 1,
                "quote": {"price": "200.00", "currency": "EUR"},
            }),
        )
        self.assertFalse(added_again)
        self.assertEqual(replay["revision"], 2)
        self.assertEqual(len(replay["actions"]), 2)
        self.assertEqual(replay["payment_state"], "not_started")
        self.assertEqual(replay["reservation_state"], "not_confirmed")

    def test_stale_revision_and_invalid_transition_do_not_change_record(self):
        intent, _ = self.store.create(self.key, draft())
        self.store.append_action(intent["booking_intent_id"], self.action(
            "live_revalidation_passed", 1,
            quote={"price": "200.00", "currency": "EUR"},
        ))
        with self.assertRaises(IntentConflict):
            self.store.append_action(intent["booking_intent_id"], self.action(
                "checkout_preparation_started", 1,
            ))
        with self.assertRaises(IntentConflict):
            self.store.append_action(intent["booking_intent_id"], self.action(
                "checkout_preparation_response_received", 2, status_code=200,
            ))
        current = self.store.get(intent["booking_intent_id"])
        self.assertEqual(current["revision"], 2)
        self.assertEqual(current["stage"], "quote_revalidated")
        self.assertEqual(len(current["actions"]), 2)

    def test_retry_after_recorded_429_requires_a_new_quote_check(self):
        intent, _ = self.store.create(self.key, draft())
        self.store.append_action(intent["booking_intent_id"], self.action(
            "live_revalidation_passed", 1, quote={"price": "200.00", "currency": "EUR"},
        ))
        self.store.append_action(intent["booking_intent_id"], self.action("checkout_preparation_started", 2))
        failed, _ = self.store.append_action(intent["booking_intent_id"], self.action(
            "checkout_preparation_failed", 3, status_code=429, detail_code="upstream_response_error",
        ))
        self.assertEqual(failed["stage"], "checkout_preparation_failed")
        checked, _ = self.store.append_action(intent["booking_intent_id"], self.action(
            "live_revalidation_passed", 4, quote={"price": "200.00", "currency": "EUR"},
        ))
        self.assertEqual(checked["stage"], "quote_revalidated")
        started, _ = self.store.append_action(intent["booking_intent_id"], self.action("checkout_preparation_started", 5))
        self.assertEqual(started["stage"], "checkout_preparation_pending")
        self.assertEqual(started["payment_state"], "not_started")
        self.assertEqual(started["reservation_state"], "not_confirmed")

    def test_only_minimum_lead_contact_fields_are_accepted(self):
        payload = draft().model_dump(mode="python")
        payload["lead_guest"]["card_number"] = "4111111111111111"
        with self.assertRaises(ValidationError):
            BookingIntentCreate.model_validate(payload)
        payload = draft().model_dump(mode="python")
        payload["marketing"] = {"email": True}
        with self.assertRaises(ValidationError):
            BookingIntentCreate.model_validate(payload)

    def test_linked_booking_records_and_reference_lookups(self):
        intent, _ = self.store.create(self.key, draft())
        self.assertIn("booking_reference", intent)
        booking_ref = intent["booking_reference"]
        self.assertTrue(booking_ref.startswith("RSP-"))

        # Link Opera hotel reservation
        updated, added = self.store.append_action(intent["booking_intent_id"], BookingIntentAction.model_validate({
            "action_id": str(uuid.uuid4()),
            "action": "link_opera_reservation",
            "expected_revision": 1,
            "opera_payload": {
                "reservation_id": "OPERA-RES-9988",
                "confirmation_number": "CONF-9988",
                "hotel_id": "ROSA",
                "room_category": "DELUXE",
                "arrival_date": "2027-05-14",
                "departure_date": "2027-05-17",
                "total_amount": "990.00",
                "currency": "EUR",
                "status": "Reserved",
            },
        }))
        self.assertTrue(added)
        self.assertEqual(updated["revision"], 2)
        self.assertEqual(updated["reservation_state"], "reserved")
        self.assertEqual(updated["links"]["opera_reservation"]["reservation_id"], "OPERA-RES-9988")

        # Link BRS golf round
        updated2, added2 = self.store.append_action(intent["booking_intent_id"], BookingIntentAction.model_validate({
            "action_id": str(uuid.uuid4()),
            "action": "link_brs_round",
            "expected_revision": 2,
            "brs_payload": {
                "round_id": "round_1",
                "course_id": 1,
                "course_name": "Old Tom Morris Links",
                "date": "2027-05-15",
                "time": "10:30",
                "holes": "18",
                "players": 2,
                "brs_booking_id": "BRS-BK-1001",
                "price": "320.00",
                "currency": "EUR",
                "status": "Reserved",
            },
        }))
        self.assertTrue(added2)
        self.assertEqual(len(updated2["links"]["brs_rounds"]), 1)

        # Record verified payment
        updated3, added3 = self.store.append_action(intent["booking_intent_id"], BookingIntentAction.model_validate({
            "action_id": str(uuid.uuid4()),
            "action": "record_payment",
            "expected_revision": 3,
            "payment_payload": {
                "payment_id": "PAY-BOIPA-5566",
                "provider": "boipa",
                "payment_type": "deposit",
                "amount": "327.50",
                "currency": "EUR",
                "status": "completed",
                "provider_reference": "TXN_BOIPA_REF_123",
            },
        }))
        self.assertTrue(added3)
        self.assertEqual(updated3["payment_state"], "deposit_paid")
        self.assertEqual(updated3["stage"], "confirmed")
        self.assertEqual(updated3["reservation_state"], "confirmed")

        # Lookup by booking reference
        by_ref = self.store.get_by_reference(booking_ref)
        self.assertEqual(by_ref["booking_intent_id"], intent["booking_intent_id"])

        # Lookup by Opera reservation ID
        by_opera = self.store.get_by_reference("OPERA-RES-9988")
        self.assertEqual(by_opera["booking_intent_id"], intent["booking_intent_id"])

        # Lookup by Opera confirmation number
        by_conf = self.store.get_by_reference("CONF-9988")
        self.assertEqual(by_conf["booking_intent_id"], intent["booking_intent_id"])

        # Recovery transition preserves all linked evidence
        updated4, _ = self.store.append_action(intent["booking_intent_id"], BookingIntentAction.model_validate({
            "action_id": str(uuid.uuid4()),
            "action": "mark_recovery_required",
            "expected_revision": 4,
        }))
        self.assertEqual(updated4["stage"], "recovery_required")
        self.assertEqual(updated4["links"]["opera_reservation"]["reservation_id"], "OPERA-RES-9988")
        self.assertEqual(len(updated4["links"]["brs_rounds"]), 1)
        self.assertEqual(len(updated4["links"]["payments"]), 1)

    def test_database_has_a_unique_idempotency_index(self):
        indexes = self.collection.index_information()
        self.assertTrue(indexes["uniq_booking_intent_idempotency"]["unique"])

    def test_api_validation_response_does_not_echo_submitted_values(self):
        payload = {
            "selection": {
                "date": "2026-10-23", "course_id": 99, "tee_time_id": 101,
                "green_fee_id": 7, "time": "10:00", "players": 2,
                "price": "200.00", "currency": "EUR",
            },
            "lead_guest": {
                "first_name": "Synthetic", "last_name": "Guest",
                "email": "synthetic@example.test", "telephone": "+441632960000",
            },
        }
        with self.assertRaises(ValidationError) as raised:
            BookingIntentCreate.model_validate(payload)
        validation_error = RequestValidationError(raised.exception.errors(), body=payload)
        request = Request({"type": "http", "method": "POST", "path": "/booking-intents", "headers": [], "query_string": b""})
        response = asyncio.run(request_validation_exception_handler(request, validation_error))
        self.assertEqual(response.status_code, 422)
        self.assertNotIn(b"99", response.body)
        self.assertNotIn(b"synthetic@example.test", response.body)


if __name__ == "__main__":
    unittest.main()
