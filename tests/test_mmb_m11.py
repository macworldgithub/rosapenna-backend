"""Unit and integration tests for Ticket M11 — Guest Phone Updates."""

import datetime as dt
import unittest
from unittest.mock import MagicMock
from fastapi import HTTPException, Response
from mongomock import MongoClient

from rosapenna_backend.api.dependencies import require_guest_booking_scope, require_mmb_enabled
from rosapenna_backend.api.routes.mmb_contact import get_guest_phone, update_guest_phone
from rosapenna_backend.api.schemas.booking_linkage import (
    BookingLinkageResponse,
    LeadGuestContact,
    OperaStayDetails,
)
from rosapenna_backend.api.schemas.mmb_auth import GuestSessionPrincipal
from rosapenna_backend.api.schemas.mmb_contact import (
    GuestPhoneUpdateRequest,
    mask_phone_number,
    normalize_phone_number,
)
from rosapenna_backend.integrations.opera.client import (
    OperaClient,
    OperaReadbackMismatchError,
)
from rosapenna_backend.integrations.opera.schemas import RoomReservationDetail, RoomOccupant
from rosapenna_backend.services.booking_linkage import BookingLinkageService
from rosapenna_backend.services.mmb_contact import (
    MmbContactConflictError,
    MmbContactError,
    MmbContactIntegrationError,
    MmbContactNotFoundError,
    MmbContactService,
)
from rosapenna_backend.services.mmb_gate import MmbGateService


class TestMmbPhoneM11(unittest.TestCase):
    """Test suite covering all M11 phone update specifications and security rules."""

    def setUp(self):
        self.mongo = MongoClient(tz_aware=True)
        self.db = self.mongo["test_mmb_m11_db"]
        self.opera_client = OperaClient()
        self.linkage_service = BookingLinkageService(
            database=self.db,
            opera_client=self.opera_client,
        )
        self.service = MmbContactService(
            database=self.db,
            linkage_service=self.linkage_service,
            opera_client=self.opera_client,
        )

        # Pre-seed booking linkage in MongoDB
        self.db.mmb_booking_linkages.insert_one({
            "booking_reference": "RSP-20270810-M1101",
            "source_channel": "online",
            "status": "linked",
            "lead_guest": {
                "first_name": "Arthur",
                "last_name": "Pendelton",
                "email": "arthur@example.com",
                "telephone": "+353 74 915 5000",
            },
            "opera_stay": {
                "reservation_id": "OPERA-M1101",
                "confirmation_number": "CONF-M1101",
                "room_category": "DELUXE",
                "room_quantity": 1,
                "arrival_date": "2027-08-10",
                "departure_date": "2027-08-13",
                "total_amount": "1200.00",
                "occupant_names": ["Arthur Pendelton"],
                "assigned_room_number": None,
            },
            "brs_rounds": [],
            "payments": [],
            "audit_trail": [],
            "revision": 1,
            "created_at": dt.datetime(2026, 11, 1, 10, 0, tzinfo=dt.timezone.utc),
            "updated_at": dt.datetime(2026, 11, 1, 10, 0, tzinfo=dt.timezone.utc),
        })

        # Pre-seed sandbox reservation in Opera client
        self.opera_client._sandbox_reservations["OPERA-M1101"] = RoomReservationDetail(
            reservation_id="OPERA-M1101",
            confirmation_number="CONF-M1101",
            hotel_id="ROSAPENNA",
            status="Confirmed",
            arrival_date=dt.date(2027, 8, 10),
            departure_date=dt.date(2027, 8, 13),
            room_category="DELUXE",
            room_quantity=1,
            guest_phone="+353 74 915 5000",
            occupants=[RoomOccupant(first_name="Arthur", last_name="Pendelton", is_primary=True)],
            total_amount=1200.00,
            currency="EUR",
            source="fixture_sandbox",
        )
        self.opera_client._sandbox_phones["OPERA-M1101"] = "+353 74 915 5000"

    def test_phone_normalization_and_validation(self):
        """Test normalization and format validation of phone numbers."""
        # Valid international format
        self.assertEqual(normalize_phone_number("+353 74 915 5000"), "+353 74 915 5000")
        self.assertEqual(normalize_phone_number("+44  20  7946  0991"), "+44 20 7946 0991")
        self.assertEqual(normalize_phone_number("074-915-5000"), "074 915 5000")

        # Invalid formats
        with self.assertRaises(ValueError):
            normalize_phone_number("abc-defg")
        with self.assertRaises(ValueError):
            normalize_phone_number("123")  # Too few digits (< 6)
        with self.assertRaises(ValueError):
            normalize_phone_number("1" * 25)  # Too many digits (> 16)
        with self.assertRaises(ValueError):
            normalize_phone_number("")

    def test_phone_masking(self):
        """Test phone number masking for routine logs and privacy."""
        self.assertEqual(mask_phone_number("+353 74 915 5000"), "+...5000")
        self.assertEqual(mask_phone_number("0871234567"), "+...4567")
        self.assertEqual(mask_phone_number("123"), "***123")
        self.assertEqual(mask_phone_number(None), "")

    def test_get_lead_phone(self):
        """Test fetching lead phone and contact version."""
        res = self.service.get_lead_phone("RSP-20270810-M1101")
        self.assertEqual(res.booking_reference, "RSP-20270810-M1101")
        self.assertEqual(res.lead_guest_name, "Arthur Pendelton")
        self.assertEqual(res.phone_number, "+353 74 915 5000")
        self.assertEqual(res.masked_phone, "+...5000")
        self.assertEqual(res.booking_revision, 1)
        self.assertTrue(len(res.contact_version) > 0)

    def test_successful_phone_update_with_readback(self):
        """Test valid update applies narrow Opera write, readback, and linkage revision bump."""
        req = GuestPhoneUpdateRequest(
            phone_number="+353 87 111 2222",
            expected_revision=1,
        )
        res = self.service.update_lead_phone(
            booking_reference="RSP-20270810-M1101",
            request=req,
            idempotency_key="idemp_key_001_success",
            actor="guest_auth_user",
        )
        self.assertEqual(res.status, "confirmed")
        self.assertEqual(res.phone_number, "+353 87 111 2222")
        self.assertEqual(res.masked_phone, "+...2222")
        self.assertEqual(res.booking_revision, 2)
        self.assertTrue(res.verified_via_readback)
        self.assertFalse(res.replayed)

        # Verify Opera client has updated phone
        opera_phone = self.opera_client.get_reservation_phone("OPERA-M1101")
        self.assertEqual(opera_phone.phone_number, "+353 87 111 2222")

        # Verify Linkage in DB has revision 2 and audit entry
        linkage = self.linkage_service.get_linkage("RSP-20270810-M1101")
        self.assertEqual(linkage.revision, 2)
        self.assertEqual(linkage.lead_guest.telephone, "+353 87 111 2222")
        self.assertEqual(len(linkage.audit_trail), 1)
        self.assertEqual(linkage.audit_trail[0].action, "guest_phone_updated")

    def test_idempotent_replay(self):
        """Test repeated requests with identical key return cached response."""
        req = GuestPhoneUpdateRequest(
            phone_number="+353 87 222 3333",
            expected_revision=1,
        )
        res1 = self.service.update_lead_phone(
            booking_reference="RSP-20270810-M1101",
            request=req,
            idempotency_key="idemp_replay_test_key",
            actor="guest_auth_user",
        )
        self.assertFalse(res1.replayed)

        # Repeat identical call
        res2 = self.service.update_lead_phone(
            booking_reference="RSP-20270810-M1101",
            request=req,
            idempotency_key="idemp_replay_test_key",
            actor="guest_auth_user",
        )
        self.assertTrue(res2.replayed)
        self.assertEqual(res2.operation_id, res1.operation_id)
        self.assertEqual(res2.phone_number, res1.phone_number)

    def test_idempotent_key_conflict_different_payload(self):
        """Test same idempotency key with different phone number raises conflict."""
        req1 = GuestPhoneUpdateRequest(
            phone_number="+353 87 333 4444",
            expected_revision=1,
        )
        self.service.update_lead_phone(
            booking_reference="RSP-20270810-M1101",
            request=req1,
            idempotency_key="idemp_conflict_key_999",
            actor="guest_auth_user",
        )

        req2 = GuestPhoneUpdateRequest(
            phone_number="+353 87 999 0000",
            expected_revision=2,
        )
        with self.assertRaises(MmbContactConflictError):
            self.service.update_lead_phone(
                booking_reference="RSP-20270810-M1101",
                request=req2,
                idempotency_key="idemp_conflict_key_999",
                actor="guest_auth_user",
            )

    def test_optimistic_concurrency_revision_conflict(self):
        """Test outdated expected_revision raises conflict."""
        req = GuestPhoneUpdateRequest(
            phone_number="+353 87 444 5555",
            expected_revision=99,  # Current is 1
        )
        with self.assertRaises(MmbContactConflictError):
            self.service.update_lead_phone(
                booking_reference="RSP-20270810-M1101",
                request=req,
                idempotency_key="idemp_revision_mismatch",
                actor="guest_auth_user",
            )

    def test_contact_version_conflict(self):
        """Test outdated contact_version raises conflict."""
        req = GuestPhoneUpdateRequest(
            phone_number="+353 87 555 6666",
            expected_revision=1,
            contact_version="stale_version_token_xyz",
        )
        with self.assertRaises(MmbContactConflictError):
            self.service.update_lead_phone(
                booking_reference="RSP-20270810-M1101",
                request=req,
                idempotency_key="idemp_ver_mismatch",
                actor="guest_auth_user",
            )

    def test_zero_mutation_returns_confirmed_without_vendor_write(self):
        """Test submitting already active phone number is a no-op."""
        req = GuestPhoneUpdateRequest(
            phone_number="+353 74 915 5000",  # Matches current seed
            expected_revision=1,
        )
        res = self.service.update_lead_phone(
            booking_reference="RSP-20270810-M1101",
            request=req,
            idempotency_key="idemp_zero_mutation_key",
            actor="guest_auth_user",
        )
        self.assertEqual(res.status, "confirmed")
        self.assertEqual(res.booking_revision, 1)  # Revision unchanged
        self.assertIn("already matches", res.message)

    def test_lead_guest_only_forbids_extra_fields(self):
        """Test payload forbids extra fields like email or lead name mutation."""
        with self.assertRaises(ValueError):
            GuestPhoneUpdateRequest(
                phone_number="+353 87 111 2222",
                expected_revision=1,
                email="attacker@evil.com",  # type: ignore
            )

    def test_opera_readback_failure_raises_integration_error(self):
        """Test Opera readback mismatch raises integration error."""
        # Mock opera client to raise OperaReadbackMismatchError
        mock_opera = MagicMock(spec=OperaClient)
        mock_opera.update_reservation_phone.side_effect = OperaReadbackMismatchError("Readback failed")

        svc = MmbContactService(
            database=self.db,
            linkage_service=self.linkage_service,
            opera_client=mock_opera,
        )
        req = GuestPhoneUpdateRequest(
            phone_number="+353 87 666 7777",
            expected_revision=1,
        )
        with self.assertRaises(MmbContactIntegrationError):
            svc.update_lead_phone(
                booking_reference="RSP-20270810-M1101",
                request=req,
                idempotency_key="idemp_opera_fail_key",
                actor="guest_auth_user",
            )

    def test_route_handlers_and_security_controls(self):
        """Test FastAPI route handlers, headers, and IDOR prevention."""
        session = GuestSessionPrincipal(
            session_token="guest_session_valid",
            booking_reference="RSP-20270810-M1101",
            lead_guest_email="arthur@example.com",
            expires_at=dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=2),
        )

        response = Response()
        read_res = get_guest_phone(
            booking_reference="RSP-20270810-M1101",
            response=response,
            session=session,
            service=self.service,
        )
        self.assertEqual(read_res.booking_reference, "RSP-20270810-M1101")
        self.assertEqual(response.headers.get("Cache-Control"), "no-store")

        # Test IDOR check
        with self.assertRaises(HTTPException) as cm_idor:
            require_guest_booking_scope(
                booking_reference="RSP-DIFFERENT-BOOKING",
                session=session,
            )
        self.assertEqual(cm_idor.exception.status_code, 403)

        # Test missing Idempotency-Key
        req = GuestPhoneUpdateRequest(
            phone_number="+353 87 777 8888",
            expected_revision=1,
        )
        with self.assertRaises(HTTPException) as cm_key:
            update_guest_phone(
                booking_reference="RSP-20270810-M1101",
                request=req,
                response=response,
                idempotency_key=None,
                session=session,
                service=self.service,
            )
        self.assertEqual(cm_key.exception.status_code, 400)


if __name__ == "__main__":
    unittest.main()
