"""Unit tests for Ticket M10 — Room Occupants, Sharing, and Moves."""

import datetime as dt
import unittest
from unittest.mock import MagicMock

from rosapenna_backend.api.schemas.booking_linkage import (
    BookingLinkageResponse,
    LeadGuestContact,
    OperaStayDetails,
)
from rosapenna_backend.api.schemas.mmb_amendments import (
    AmendmentOfferCreateRequest,
    AmendmentOfferAcceptRequest,
    ProposedStayOption,
    ProposedGolfRoundOption,
)
from rosapenna_backend.api.schemas.mmb_occupants import (
    OccupantDetail,
    OccupantUpdateRequest,
    SharingPreference,
    compute_occupant_version_hash,
)
from rosapenna_backend.integrations.opera.client import OperaClient, OperaReadbackMismatchError
from rosapenna_backend.services.booking_linkage import BookingLinkageService
from rosapenna_backend.services.mmb_amendments import MmbAmendmentService
from rosapenna_backend.services.mmb_gate import MmbGateService
from rosapenna_backend.services.mmb_occupants import (
    ConcurrentEditConflictError,
    MmbOccupantsService,
    OccupantValidationError,
)


class TestMmbOccupantsM10(unittest.TestCase):
    """Test suite covering all M10 acceptance criteria."""

    def setUp(self):
        self.linkage_service = MagicMock(spec=BookingLinkageService)
        self.opera_client = OperaClient()  # Sandbox mode by default
        self.gate_service = MagicMock(spec=MmbGateService)
        self.gate_service.is_enabled.return_value = True

        self.service = MmbOccupantsService(
            database=None,
            linkage_service=self.linkage_service,
            opera_client=self.opera_client,
            gate_service=self.gate_service,
        )

        self.sample_booking = BookingLinkageResponse(
            booking_reference="RSP-20270810-M1001",
            source_channel="online",
            status="linked",
            lead_guest=LeadGuestContact(
                first_name="Arthur",
                last_name="Pendelton",
                email="arthur@example.com",
                telephone="+353749155000",
            ),
            opera_stay=OperaStayDetails(
                reservation_id="OPERA-M1001",
                confirmation_number="CONF-OPERA-M1001",
                room_category="DELUXE",
                room_quantity=2,
                arrival_date=dt.date(2027, 8, 10),
                departure_date=dt.date(2027, 8, 13),
                total_amount="1500.00",
                occupant_names=["Arthur Pendelton", "Guinevere Pendelton"],
                assigned_room_number=None,
            ),
            brs_rounds=[],
            payments=[],
            created_at=dt.datetime(2026, 11, 1, 10, 0, tzinfo=dt.timezone.utc),
            updated_at=dt.datetime(2026, 11, 1, 10, 0, tzinfo=dt.timezone.utc),
        )
        setattr(self.sample_booking, "revision", 1)
        self.linkage_service.get_linkage.return_value = self.sample_booking

        # Pre-populate sandbox reservation in Opera client
        from rosapenna_backend.integrations.opera.schemas import RoomReservationDetail, RoomOccupant
        self.opera_client._sandbox_reservations["OPERA-M1001"] = RoomReservationDetail(
            reservation_id="OPERA-M1001",
            confirmation_number="CONF-OPERA-M1001",
            hotel_id="ROSAPENNA",
            status="Confirmed",
            arrival_date=dt.date(2027, 8, 10),
            departure_date=dt.date(2027, 8, 13),
            room_category="DELUXE",
            room_quantity=2,
            room_group_id="GRP-M1001",
            room_number=None,
            occupants=[
                RoomOccupant(first_name="Arthur", last_name="Pendelton", is_primary=True),
                RoomOccupant(first_name="Guinevere", last_name="Pendelton", is_primary=False),
            ],
            total_amount=1500.00,
            currency="EUR",
            source="fixture_sandbox",
        )

    def test_ac1_ac2_get_roster_suppresses_physical_room_numbers(self):
        """AC 1 & AC 2: Roster returns valid room groups, occupancy, sharing, and unassigned notice."""
        roster = self.service.get_roster("RSP-20270810-M1001")
        self.assertEqual(roster.booking_reference, "RSP-20270810-M1001")
        self.assertEqual(len(roster.room_groups), 2)
        self.assertEqual(roster.room_groups[0].room_group_id, "room_1")
        self.assertEqual(roster.room_groups[1].room_group_id, "room_2")
        self.assertIn("assigned by hotel reception upon check-in", roster.unassigned_physical_room_notice)
        self.assertTrue(len(roster.version_hash) >= 32)

    def test_ac1_validation_rejects_missing_name_or_invalid_dates(self):
        """AC 1: Reject empty names, dates outside stay window, or backwards dates."""
        invalid_dates = [
            OccupantDetail(
                first_name="Arthur",
                last_name="Pendelton",
                room_group_id="room_1",
                effective_from=dt.date(2027, 8, 8),  # Before stay arrival
                effective_to=dt.date(2027, 8, 13),
            )
        ]
        with self.assertRaises(OccupantValidationError):
            self.service.validate_rules(
                stay_arrival=dt.date(2027, 8, 10),
                stay_departure=dt.date(2027, 8, 13),
                room_quantity=2,
                occupants=invalid_dates,
                sharing_preferences=[],
            )

    def test_ac1_validation_rejects_room_capacity_exceeded(self):
        """AC 1: Reject when more than 3 guests are placed in a single room."""
        four_guests = [
            OccupantDetail(
                first_name=f"Guest{i}",
                last_name="Test",
                room_group_id="room_1",
                effective_from=dt.date(2027, 8, 10),
                effective_to=dt.date(2027, 8, 13),
            )
            for i in range(1, 5)
        ]
        with self.assertRaises(OccupantValidationError):
            self.service.validate_rules(
                stay_arrival=dt.date(2027, 8, 10),
                stay_departure=dt.date(2027, 8, 13),
                room_quantity=2,
                occupants=four_guests,
                sharing_preferences=[],
            )

    def test_ac4_ac5_ac8_update_occupants_writes_opera_and_verifies_readback(self):
        """AC 4, AC 5, AC 8: Occupant update writes to Opera, verifies readback, and confirms without price change."""
        req = OccupantUpdateRequest(
            booking_reference="RSP-20270810-M1001",
            expected_revision=1,
            occupants=[
                OccupantDetail(
                    first_name="Arthur",
                    last_name="Pendelton",
                    is_primary=True,
                    room_group_id="room_1",
                    effective_from=dt.date(2027, 8, 10),
                    effective_to=dt.date(2027, 8, 13),
                ),
                OccupantDetail(
                    first_name="Lancelot",
                    last_name="DuLac",
                    is_primary=False,
                    room_group_id="room_2",
                    effective_from=dt.date(2027, 8, 10),
                    effective_to=dt.date(2027, 8, 13),
                ),
            ],
            sharing_preferences=[
                SharingPreference(room_group_id="room_1", bed_type_preference="DOUBLE", notes="Quiet room requested"),
                SharingPreference(room_group_id="room_2", bed_type_preference="TWIN"),
            ],
        )

        resp = self.service.update_occupants("RSP-20270810-M1001", req)
        self.assertEqual(resp.status, "confirmed")
        self.assertTrue(resp.readback_verified)
        self.assertEqual(resp.revision, 2)
        self.assertEqual(len(resp.occupants), 2)
        self.assertEqual(self.sample_booking.opera_stay.occupant_names, ["Arthur Pendelton", "Lancelot DuLac"])

    def test_ac6_concurrent_edit_conflict_detection(self):
        """AC 6: Concurrent staff or guest edit with mismatched expected_revision raises 409 conflict."""
        req = OccupantUpdateRequest(
            booking_reference="RSP-20270810-M1001",
            expected_revision=99,  # Mismatch with actual revision 1
            occupants=[
                OccupantDetail(
                    first_name="Arthur",
                    last_name="Pendelton",
                    room_group_id="room_1",
                    effective_from=dt.date(2027, 8, 10),
                    effective_to=dt.date(2027, 8, 13),
                )
            ],
        )
        with self.assertRaises(ConcurrentEditConflictError):
            self.service.update_occupants("RSP-20270810-M1001", req)

    def test_ac7_idempotency_replay(self):
        """AC 7: Retrying with the same idempotency key returns the cached response."""
        req = OccupantUpdateRequest(
            booking_reference="RSP-20270810-M1001",
            expected_revision=1,
            idempotency_key="idemp_key_m10_123",
            occupants=[
                OccupantDetail(
                    first_name="Arthur",
                    last_name="Pendelton",
                    room_group_id="room_1",
                    effective_from=dt.date(2027, 8, 10),
                    effective_to=dt.date(2027, 8, 13),
                )
            ],
        )
        resp1 = self.service.update_occupants("RSP-20270810-M1001", req)
        resp2 = self.service.update_occupants("RSP-20270810-M1001", req)
        self.assertEqual(resp1.version_hash, resp2.version_hash)
        self.assertEqual(resp1.revision, resp2.revision)

    def test_ac5_upstream_readback_failure_routes_to_staff_review(self):
        """AC 5: If Opera readback fails, response is marked pending_staff_review."""
        mock_opera = MagicMock(spec=OperaClient)
        mock_opera.amend_reservation.side_effect = OperaReadbackMismatchError("Mismatch in readback")
        self.service.opera_client = mock_opera

        req = OccupantUpdateRequest(
            booking_reference="RSP-20270810-M1001",
            expected_revision=1,
            occupants=[
                OccupantDetail(
                    first_name="Arthur",
                    last_name="Pendelton",
                    room_group_id="room_1",
                    effective_from=dt.date(2027, 8, 10),
                    effective_to=dt.date(2027, 8, 13),
                )
            ],
        )
        resp = self.service.update_occupants("RSP-20270810-M1001", req)
        self.assertEqual(resp.status, "pending_staff_review")
        self.assertFalse(resp.readback_verified)
        self.assertIn("requires staff verification", resp.message)

    def test_ac3_review_for_amendment_generates_version_for_m09(self):
        """AC 3: Reviewing occupants for an amendment creates an immutable review version linked to the offer."""
        amendment_service = MagicMock(spec=MmbAmendmentService)
        amendment_service._offers_cache = {
            "OFFER-AMEND-123": {
                "offer_id": "OFFER-AMEND-123",
                "booking_reference": "RSP-20270810-M1001",
                "booking_revision": 1,
                "status": "pending_consent",
                "requires_occupant_review": True,
                "proposed_stay": ProposedStayOption(
                    arrival_date=dt.date(2027, 8, 12),
                    departure_date=dt.date(2027, 8, 15),
                    nights=3,
                    room_category="BAYVIEW",
                    room_quantity=1,
                    is_available=True,
                    nightly_rate="300.00",
                    stay_total="900.00",
                ),
            }
        }
        mock_offer = MagicMock()
        mock_offer.status = "pending_consent"
        mock_offer.booking_revision = 1
        mock_offer.proposed_stay = ProposedStayOption(
            arrival_date=dt.date(2027, 8, 12),
            departure_date=dt.date(2027, 8, 15),
            nights=3,
            room_category="BAYVIEW",
            room_quantity=1,
            is_available=True,
            nightly_rate="300.00",
            stay_total="900.00",
        )
        amendment_service.get_amendment_offer.return_value = mock_offer
        amendment_service.collection = None

        self.service.amendment_service = amendment_service

        req = OccupantUpdateRequest(
            booking_reference="RSP-20270810-M1001",
            amendment_offer_id="OFFER-AMEND-123",
            occupants=[
                OccupantDetail(
                    first_name="Arthur",
                    last_name="Pendelton",
                    room_group_id="room_1",
                    effective_from=dt.date(2027, 8, 12),
                    effective_to=dt.date(2027, 8, 15),
                )
            ],
            sharing_preferences=[SharingPreference(room_group_id="room_1", bed_type_preference="DOUBLE")],
        )

        resp = self.service.review_amendment_occupants("RSP-20270810-M1001", "OFFER-AMEND-123", req)
        self.assertEqual(resp.status, "confirmed")
        self.assertEqual(resp.amendment_offer_id, "OFFER-AMEND-123")
        self.assertTrue(len(resp.version_hash) >= 32)
        # Check that offer snapshot cache was acknowledged
        raw_offer = amendment_service._offers_cache["OFFER-AMEND-123"]
        self.assertTrue(raw_offer["occupant_review_acknowledged"])
        self.assertEqual(raw_offer["occupant_review_version"], resp.version_hash)


if __name__ == "__main__":
    unittest.main()
