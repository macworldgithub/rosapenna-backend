"""Unit and integration tests for Ticket M12 — Per-Round Caddy Request Updates."""

import datetime as dt
import unittest
from fastapi import HTTPException, Response
from mongomock import MongoClient

from rosapenna_backend.api.dependencies import require_guest_booking_scope
from rosapenna_backend.api.routes.mmb_caddy import get_caddy_roster, update_round_caddy
from rosapenna_backend.api.schemas.brs_booking import CaddyRequestPayload
from rosapenna_backend.api.schemas.mmb_auth import GuestSessionPrincipal
from rosapenna_backend.api.schemas.mmb_caddy import (
    CADDY_FEE_PER_BAG,
    CADDY_NOTICE,
    CaddyBagPreference,
    RoundCaddyUpdateRequest,
)
from rosapenna_backend.integrations.brs.booking_client import BrsBookingClient
from rosapenna_backend.services.booking_linkage import BookingLinkageService
from rosapenna_backend.services.mmb_caddy import (
    MmbCaddyConflictError,
    MmbCaddyError,
    MmbCaddyNotFoundError,
    MmbCaddyService,
)


class TestMmbCaddyM12(unittest.TestCase):
    """Test suite covering M12 per-round caddy request updates."""

    def setUp(self):
        self.mongo = MongoClient(tz_aware=True)
        self.db = self.mongo["test_mmb_m12_db"]
        self.brs_client = BrsBookingClient()
        self.linkage_service = BookingLinkageService(database=self.db)
        self.service = MmbCaddyService(
            database=self.db,
            linkage_service=self.linkage_service,
            brs_client=self.brs_client,
        )

        # Seed booking linkage with a confirmed 2-player round
        self.db.mmb_booking_linkages.insert_one({
            "booking_reference": "RSP-20270810-M1201",
            "source_channel": "online",
            "status": "linked",
            "lead_guest": {
                "first_name": "Fionn",
                "last_name": "MacCool",
                "email": "fionn@example.com",
                "telephone": "+353 74 915 5000",
            },
            "opera_stay": {
                "reservation_id": "OPERA-M1201",
                "confirmation_number": "CONF-M1201",
                "room_category": "DELUXE",
                "room_quantity": 1,
                "arrival_date": "2027-08-10",
                "departure_date": "2027-08-13",
                "total_amount": "1200.00",
                "occupant_names": ["Fionn MacCool"],
                "assigned_room_number": None,
            },
            "brs_rounds": [
                {
                    "round_id": "RND-SANDY-001",
                    "course_id": 1,
                    "course_name": "Sandy Hills Links",
                    "date": "2027-08-11",
                    "time": "09:30",
                    "holes": "18",
                    "players": 2,
                    "price": "180.00",
                    "currency": "EUR",
                    "status": "Confirmed",
                    "brs_booking_id": "BRS-1201",
                    "caddy_request_status": "not_requested",
                    "caddy_request": {
                        "caddies_requested": 0,
                        "caddy_category": None,
                        "notes": None,
                    },
                }
            ],
            "payments": [],
            "audit_trail": [],
            "revision": 1,
            "created_at": dt.datetime(2026, 11, 1, 10, 0, tzinfo=dt.timezone.utc),
            "updated_at": dt.datetime(2026, 11, 1, 10, 0, tzinfo=dt.timezone.utc),
        })

    def test_get_caddy_roster_success(self):
        """Authoritative get returns confirmed rounds and caddy information."""
        roster = self.service.get_caddy_roster("RSP-20270810-M1201")
        self.assertEqual(roster.booking_reference, "RSP-20270810-M1201")
        self.assertEqual(roster.booking_revision, 1)
        self.assertEqual(len(roster.rounds), 1)

        rnd = roster.rounds[0]
        self.assertEqual(rnd.round_id, "RND-SANDY-001")
        self.assertEqual(rnd.players, 2)
        self.assertEqual(rnd.caddies_requested, 0)
        self.assertEqual(rnd.online_charge, "0.00")
        self.assertEqual(rnd.estimated_day_fee, "0.00")
        self.assertIn("Golf Pavilion", rnd.notice)

    def test_get_caddy_roster_not_found(self):
        """Unknown booking raises MmbCaddyNotFoundError."""
        with self.assertRaises(MmbCaddyNotFoundError):
            self.service.get_caddy_roster("UNKNOWN-REF")

    def test_update_caddy_request_success(self):
        """Valid caddy update within player capacity calculates day fee with 0 online charge."""
        req = RoundCaddyUpdateRequest(
            booking_reference="RSP-20270810-M1201",
            round_id="RND-SANDY-001",
            caddies_requested=2,
            caddy_category="caddie",
            bag_preferences=[
                CaddyBagPreference(bag_index=1, caddy_category="caddie", notes="Single bag"),
                CaddyBagPreference(bag_index=2, caddy_category="caddie", notes="Single bag"),
            ],
            general_notes="Left handed player on bag 2",
            expected_revision=1,
        )

        res = self.service.update_round_caddy(
            booking_reference="RSP-20270810-M1201",
            round_id="RND-SANDY-001",
            request=req,
            idempotency_key="idemp_key_test_12345",
            actor="guest_fionn@example.com",
        )

        self.assertEqual(res.booking_reference, "RSP-20270810-M1201")
        self.assertEqual(res.round_id, "RND-SANDY-001")
        self.assertEqual(res.caddies_requested, 2)
        self.assertEqual(res.caddy_category, "caddie")
        self.assertEqual(res.caddy_request_status, "requested")
        self.assertEqual(res.online_charge, "0.00")
        self.assertEqual(res.estimated_day_fee, "180.00")  # 2 * 90.00
        self.assertEqual(res.booking_revision, 2)
        self.assertTrue(res.verified_via_readback)
        self.assertFalse(res.replayed)

        # Verify DB linkage is updated and audit trail recorded
        linkage = self.db.mmb_booking_linkages.find_one({"booking_reference": "RSP-20270810-M1201"})
        self.assertEqual(linkage["revision"], 2)
        round_doc = linkage["brs_rounds"][0]
        self.assertEqual(round_doc["caddy_request_status"], "requested")
        self.assertEqual(round_doc["caddy_request"]["caddies_requested"], 2)
        self.assertEqual(round_doc["caddy_request"]["caddy_category"], "caddie")
        self.assertEqual(round_doc["caddy_request"]["payment_terms"], "payable_on_the_day")
        self.assertEqual(len(linkage["audit_trail"]), 1)
        self.assertEqual(linkage["audit_trail"][0]["action"], "round_caddy_updated")

    def test_caddy_request_capacity_validation(self):
        """Requesting more caddies than players in round raises 400 error."""
        # Round has 2 players, requesting 3 caddies must fail
        req = RoundCaddyUpdateRequest(
            booking_reference="RSP-20270810-M1201",
            round_id="RND-SANDY-001",
            caddies_requested=3,
            caddy_category="caddie",
            expected_revision=1,
        )

        with self.assertRaises(MmbCaddyError) as ctx:
            self.service.update_round_caddy(
                booking_reference="RSP-20270810-M1201",
                round_id="RND-SANDY-001",
                request=req,
                idempotency_key="idemp_key_cap_test_123",
                actor="guest_fionn@example.com",
            )
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("Cannot request 3 caddies for a 2-player round", str(ctx.exception))

    def test_caddy_request_removal(self):
        """Setting caddies_requested=0 clears the caddy request."""
        # First set 1 caddy
        req1 = RoundCaddyUpdateRequest(
            booking_reference="RSP-20270810-M1201",
            round_id="RND-SANDY-001",
            caddies_requested=1,
            caddy_category="forecaddie",
            expected_revision=1,
        )
        res1 = self.service.update_round_caddy(
            booking_reference="RSP-20270810-M1201",
            round_id="RND-SANDY-001",
            request=req1,
            idempotency_key="idemp_key_set_1_12345",
            actor="guest_fionn@example.com",
        )
        self.assertEqual(res1.booking_revision, 2)
        self.assertEqual(res1.caddies_requested, 1)

        # Now remove caddies
        req0 = RoundCaddyUpdateRequest(
            booking_reference="RSP-20270810-M1201",
            round_id="RND-SANDY-001",
            caddies_requested=0,
            caddy_category=None,
            expected_revision=2,
        )
        res0 = self.service.update_round_caddy(
            booking_reference="RSP-20270810-M1201",
            round_id="RND-SANDY-001",
            request=req0,
            idempotency_key="idemp_key_clear_12345",
            actor="guest_fionn@example.com",
        )
        self.assertEqual(res0.caddies_requested, 0)
        self.assertEqual(res0.caddy_request_status, "not_requested")
        self.assertEqual(res0.estimated_day_fee, "0.00")
        self.assertEqual(res0.booking_revision, 3)

    def test_optimistic_concurrency_conflict(self):
        """Outdated expected_revision raises 409 conflict."""
        req = RoundCaddyUpdateRequest(
            booking_reference="RSP-20270810-M1201",
            round_id="RND-SANDY-001",
            caddies_requested=1,
            caddy_category="caddie",
            expected_revision=99,  # Current is 1
        )
        with self.assertRaises(MmbCaddyConflictError) as ctx:
            self.service.update_round_caddy(
                booking_reference="RSP-20270810-M1201",
                round_id="RND-SANDY-001",
                request=req,
                idempotency_key="idemp_key_conflict_123",
                actor="guest_fionn@example.com",
            )
        self.assertEqual(ctx.exception.status_code, 409)

    def test_idempotency_replay_and_conflict(self):
        """Same idempotency key with identical payload replays, differing payload raises 409."""
        req = RoundCaddyUpdateRequest(
            booking_reference="RSP-20270810-M1201",
            round_id="RND-SANDY-001",
            caddies_requested=1,
            caddy_category="caddie",
            expected_revision=1,
        )

        # First request
        res1 = self.service.update_round_caddy(
            booking_reference="RSP-20270810-M1201",
            round_id="RND-SANDY-001",
            request=req,
            idempotency_key="idemp_key_replay_test123",
            actor="guest_fionn@example.com",
        )
        self.assertFalse(res1.replayed)

        # Replay identical
        res2 = self.service.update_round_caddy(
            booking_reference="RSP-20270810-M1201",
            round_id="RND-SANDY-001",
            request=req,
            idempotency_key="idemp_key_replay_test123",
            actor="guest_fionn@example.com",
        )
        self.assertTrue(res2.replayed)
        self.assertEqual(res2.operation_id, res1.operation_id)

        # Replay differing payload
        diff_req = RoundCaddyUpdateRequest(
            booking_reference="RSP-20270810-M1201",
            round_id="RND-SANDY-001",
            caddies_requested=2,  # Changed from 1 to 2
            caddy_category="caddie",
            expected_revision=1,
        )
        with self.assertRaises(MmbCaddyConflictError) as ctx:
            self.service.update_round_caddy(
                booking_reference="RSP-20270810-M1201",
                round_id="RND-SANDY-001",
                request=diff_req,
                idempotency_key="idemp_key_replay_test123",
                actor="guest_fionn@example.com",
            )
        self.assertEqual(ctx.exception.status_code, 409)

    def test_idor_booking_scoping(self):
        """Session token cannot access or update caddies on a different booking reference."""
        session = GuestSessionPrincipal(
            session_token="test_token_xyz_12345",
            booking_reference="RSP-20270810-M1201",
            lead_guest_email="fionn@example.com",
            expires_at=dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=1),
        )

        with self.assertRaises(HTTPException) as ctx:
            require_guest_booking_scope(
                booking_reference="RSP-DIFFERENT-BOOKING",
                session=session,
            )
        self.assertEqual(ctx.exception.status_code, 403)


if __name__ == "__main__":
    unittest.main()
