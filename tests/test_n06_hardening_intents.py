"""N06 hardening: commit quote binding and reaccept consent; synthetic data only."""

import unittest
import uuid
from unittest.mock import MagicMock, patch

from fastapi import HTTPException
from mongomock import MongoClient
from pydantic import ValidationError

from rosapenna_backend.api.routes.booking_intents import commit_booking_intent, reaccept_offer
from rosapenna_backend.api.schemas.booking_intents import BookingIntentCommitRequest, BookingIntentReacceptRequest
from rosapenna_backend.services.booking_intents import BookingIntentStore
from rosapenna_backend.services.package_pricing import compute_quote_hash
from tests.test_booking_intents import draft
from tests.test_n06_commit import make_mock_available_quote

TERMS = "terms-v1-20261014"


class N06HardeningIntentTests(unittest.TestCase):
    def setUp(self):
        self.mongo = MongoClient(tz_aware=True)
        self.collection = self.mongo["n06_hardening_intents"].booking_intents
        self.store = BookingIntentStore(self.collection)
        self.quote = make_mock_available_quote()
        self.hash = compute_quote_hash(self.quote)
        self.pricing = MagicMock()
        self.pricing.get_quote.return_value = self.quote
        self.pricing.is_quote_expired.return_value = False
        self.pricing.payments_collection = None
        self.pricing._payments_cache = {}
        self.engine = MagicMock()
        self.intent_id, _ = self.store.create(uuid.uuid4(), draft())
        self.intent_id = self.intent_id["booking_intent_id"]

    def tearDown(self):
        self.mongo.close()

    def reaccept(self, revision=1, quote_hash=None, quote_id=None):
        request = BookingIntentReacceptRequest(
            expected_revision=revision,
            quote_id=quote_id or self.quote.quote_id,
            quote_hash=quote_hash or self.hash,
            terms_version=TERMS,
            accepted_terms=True,
        )
        return reaccept_offer(request, self.intent_id, self.store, self.pricing)

    def commit(self, quote_hash=None, quote_id=None, revision=2):
        request = BookingIntentCommitRequest(
            expected_revision=revision,
            quote_id=quote_id or self.quote.quote_id,
            quote_hash=quote_hash or self.hash,
        )
        return commit_booking_intent(request, self.intent_id, self.store, self.pricing, self.engine)

    def stored(self):
        return self.collection.find_one({"_id": self.intent_id})

    def test_a_commit_without_quote_hash_is_rejected(self):
        with self.assertRaises(ValidationError):
            BookingIntentCommitRequest(expected_revision=2, quote_id=self.quote.quote_id)

    def test_b_commit_with_wrong_hash_or_other_quote_is_rejected_without_change(self):
        self.reaccept()
        before = self.stored()
        with self.assertRaises(HTTPException) as wrong_hash:
            self.commit(quote_hash="0" * 64)
        self.assertEqual(wrong_hash.exception.status_code, 409)
        with self.assertRaises(HTTPException) as other_quote:
            self.commit(quote_id="QTE-OTHER")
        self.assertEqual(other_quote.exception.status_code, 409)
        self.assertEqual(self.stored(), before)
        self.engine.execute_operation.assert_not_called()

    def test_c_commit_with_right_hash_still_proceeds(self):
        self.reaccept()
        self.collection.update_one(
            {"_id": self.intent_id},
            {"$set": {"links.payments": [{"payment_id": "pay_1", "status": "verified_paid"}]}},
        )
        self.engine.execute_operation.return_value = MagicMock(
            status="confirmed", model_dump=MagicMock(return_value={"status": "confirmed"}),
        )
        response = self.commit()
        self.assertEqual(response, {"status": "confirmed"})
        self.engine.execute_operation.assert_called_once()

    def test_d_reaccept_with_stale_revision_returns_409_and_stores_no_consent(self):
        before = self.stored()
        with self.assertRaises(HTTPException) as raised:
            self.reaccept(revision=7)
        self.assertEqual(raised.exception.status_code, 409)
        after = self.stored()
        self.assertEqual(after, before)
        self.assertNotIn("consents", after)

    def test_e_reaccept_with_changed_quote_hash_is_rejected(self):
        before = self.stored()
        with self.assertRaises(HTTPException) as raised:
            self.reaccept(quote_hash="f" * 64)
        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(self.stored(), before)
        self.assertNotIn("consents", self.stored())

    def test_f_good_reaccept_stores_one_consent_and_one_history_entry(self):
        before_actions = len(self.stored()["actions"])
        result = self.reaccept()
        document = self.stored()
        self.assertEqual(document["revision"], 2)
        self.assertEqual(len(document["consents"]), 1)
        consent = document["consents"][0]
        self.assertEqual(set(consent), {"accepted_at", "expected_revision", "quote_id", "quote_hash", "terms_version"})
        self.assertEqual(consent["expected_revision"], 1)
        self.assertEqual(consent["quote_id"], self.quote.quote_id)
        self.assertEqual(consent["quote_hash"], self.hash)
        self.assertEqual(consent["terms_version"], self.quote.terms_version)
        self.assertIsNotNone(consent["accepted_at"].tzinfo)
        for personal in ("Synthetic", "Guest", "synthetic@example.test", "+441632960000"):
            self.assertNotIn(personal, str(consent))
        self.assertEqual(len(document["actions"]), before_actions + 1)
        self.assertEqual(document["actions"][-1]["action"], "offer_reaccepted")
        self.assertEqual(result["revision"], 2)

    def test_g_concurrent_double_reaccept_changes_record_once(self):
        stale_view = self.store.get(self.intent_id)
        with patch.object(self.store, "get", return_value=stale_view):
            self.reaccept()
            with self.assertRaises(HTTPException) as second:
                self.reaccept()
        self.assertEqual(second.exception.status_code, 409)
        document = self.stored()
        self.assertEqual(document["revision"], 2)
        self.assertEqual(len(document["consents"]), 1)
        self.assertEqual(sum(1 for a in document["actions"] if a["action"] == "offer_reaccepted"), 1)


if __name__ == "__main__":
    unittest.main()
