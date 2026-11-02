"""Unit and integration tests for Ticket M02 — Online and Offline Booking Linkage."""

import datetime as dt
import unittest
from unittest.mock import MagicMock
from fastapi import HTTPException
from mongomock import MongoClient

from rosapenna_backend.api.dependencies import require_mmb_enabled
from rosapenna_backend.api.routes.booking_linkage import (
    guest_lookup_booking,
    staff_correct_linkage,
    staff_get_linkage,
    staff_link_booking,
    staff_list_linkages,
)
from rosapenna_backend.api.schemas.booking_linkage import (
    BrsRoundDetails,
    GuestBookingLookupRequest,
    LeadGuestContact,
    OperaStayDetails,
    PaymentLedgerEntry,
    StaffCorrectLinkageRequest,
    StaffLinkBookingRequest,
)
from rosapenna_backend.api.staff_security import StaffPrincipal
from rosapenna_backend.config import Settings
from rosapenna_backend.integrations.opera.client import OperaClient
from rosapenna_backend.services.booking_linkage import (
    BookingLinkageService,
    LinkageNotFoundError,
)
from rosapenna_backend.services.mmb_gate import MmbGateService


class TestBookingLinkageService(unittest.TestCase):
    """Test the domain linkage service logic, reconciliation, and audit recording."""

    def setUp(self):
        self.mongo = MongoClient(tz_aware=True)
        self.db = self.mongo["test_linkage_db"]
        self.opera_client = OperaClient()
        self.service = BookingLinkageService(
            database=self.db,
            opera_client=self.opera_client,
        )

    def tearDown(self):
        self.mongo.close()

    def test_link_booking_complete_online(self):
        """A complete stay-and-play booking links cleanly with status 'linked'."""
        req = StaffLinkBookingRequest(
            booking_reference="RSP-20270514-TEST01",
            source_channel="online",
            lead_guest=LeadGuestContact(
                first_name="Jane",
                last_name="Golfer",
                email="jane.golfer@example.com",
                telephone="+353871234567",
            ),
            manual_opera_stay=OperaStayDetails(
                reservation_id="RES-001",
                confirmation_number="CONF-001",
                hotel_id="ROSA",
                room_category="DELUXE",
                room_quantity=1,
                arrival_date=dt.date(2027, 5, 14),
                departure_date=dt.date(2027, 5, 17),
                total_amount="990.00",
                currency="EUR",
                occupant_names=["Jane Golfer", "Guest Golfer"],
            ),
            brs_rounds=[
                BrsRoundDetails(
                    round_id="RND-001",
                    course_id=1,
                    course_name="Old Tom Morris Links",
                    date=dt.date(2027, 5, 15),
                    time="09:00",
                    holes="18",
                    players=2,
                    brs_booking_id="BRS-101",
                    price="360.00",
                    currency="EUR",
                )
            ],
            payments=[
                PaymentLedgerEntry(
                    payment_id="PAY-001",
                    provider="prommt",
                    payment_type="deposit",
                    amount="337.50",
                    currency="EUR",
                    status="completed",
                    provider_reference="TXN-P001",
                )
            ],
            reason="Initial online booking linkage with verified components",
        )

        res = self.service.link_booking(request=req, actor="staff_tester")
        self.assertEqual(res.booking_reference, "RSP-20270514-TEST01")
        self.assertEqual(res.status, "linked")
        self.assertEqual(len(res.discrepancies), 0)
        self.assertEqual(res.financials["total_amount"], "1350.00")
        self.assertEqual(res.financials["deposit_paid"], "337.50")
        self.assertEqual(res.financials["balance_due"], "1012.50")
        self.assertEqual(len(res.audit_trail), 1)
        self.assertEqual(res.audit_trail[0].actor, "staff_tester")

    def test_link_booking_missing_components(self):
        """Missing payment and golf rounds flags status as 'missing_components'."""
        req = StaffLinkBookingRequest(
            booking_reference="RSP-20270514-TEST02",
            source_channel="phone",
            lead_guest=LeadGuestContact(
                first_name="Jane",
                last_name="Golfer",
                email="jane.golfer@example.com",
                telephone="+353871234567",
            ),
            manual_opera_stay=OperaStayDetails(
                reservation_id="RES-002",
                confirmation_number="CONF-002",
                hotel_id="ROSA",
                room_category="CLASSIC",
                room_quantity=1,
                arrival_date=dt.date(2027, 5, 14),
                departure_date=dt.date(2027, 5, 17),
                total_amount="660.00",
                currency="EUR",
                occupant_names=["Jane Golfer"],
            ),
            brs_rounds=[],
            payments=[],
            reason="Phone inquiry room booked; golf and deposit pending",
        )

        res = self.service.link_booking(request=req, actor="agent_tom")
        self.assertEqual(res.status, "missing_components")
        self.assertTrue(any("Missing golf rounds" in d for d in res.discrepancies))
        self.assertTrue(any("No verified completed payment" in d for d in res.discrepancies))

    def test_link_booking_conflicting_dates(self):
        """Golf round outside hotel stay dates flags status as 'conflicting'."""
        req = StaffLinkBookingRequest(
            booking_reference="RSP-20270514-TEST03",
            source_channel="email",
            lead_guest=LeadGuestContact(
                first_name="Jane",
                last_name="Golfer",
                email="jane.golfer@example.com",
                telephone="+353871234567",
            ),
            manual_opera_stay=OperaStayDetails(
                reservation_id="RES-003",
                confirmation_number="CONF-003",
                hotel_id="ROSA",
                room_category="CLASSIC",
                room_quantity=1,
                arrival_date=dt.date(2027, 5, 14),
                departure_date=dt.date(2027, 5, 16),
                total_amount="440.00",
                currency="EUR",
                occupant_names=["Jane Golfer"],
            ),
            brs_rounds=[
                BrsRoundDetails(
                    round_id="RND-OUTSIDE",
                    course_id=2,
                    course_name="Sandy Hills Links",
                    date=dt.date(2027, 5, 20),  # 4 days after departure!
                    time="10:00",
                    holes="18",
                    players=2,
                    price="380.00",
                    currency="EUR",
                )
            ],
            payments=[
                PaymentLedgerEntry(
                    payment_id="PAY-003",
                    provider="prommt",
                    payment_type="deposit",
                    amount="200.00",
                    currency="EUR",
                    status="completed",
                )
            ],
            reason="Manual email booking with round date after stay",
        )

        res = self.service.link_booking(request=req, actor="agent_tom")
        self.assertEqual(res.status, "conflicting")
        self.assertTrue(any("falls outside stay window" in d for d in res.discrepancies))

    def test_link_booking_conflicting_surname(self):
        """Lead guest surname not matching Opera occupant names flags 'conflicting'."""
        req = StaffLinkBookingRequest(
            booking_reference="RSP-20270514-TEST04",
            source_channel="phone",
            lead_guest=LeadGuestContact(
                first_name="Alice",
                last_name="Smith",  # Does not match Golfer
                email="alice@example.com",
                telephone="+353871234567",
            ),
            manual_opera_stay=OperaStayDetails(
                reservation_id="RES-004",
                confirmation_number="CONF-004",
                hotel_id="ROSA",
                room_category="CLASSIC",
                room_quantity=1,
                arrival_date=dt.date(2027, 5, 14),
                departure_date=dt.date(2027, 5, 16),
                total_amount="440.00",
                currency="EUR",
                occupant_names=["Bob Jones", "Carol Wilson"],
            ),
            brs_rounds=[
                BrsRoundDetails(
                    round_id="RND-004",
                    course_id=1,
                    course_name="Old Tom Morris Links",
                    date=dt.date(2027, 5, 15),
                    time="10:00",
                    holes="18",
                    players=2,
                    price="360.00",
                    currency="EUR",
                )
            ],
            payments=[
                PaymentLedgerEntry(
                    payment_id="PAY-004",
                    provider="prommt",
                    payment_type="deposit",
                    amount="200.00",
                    currency="EUR",
                    status="completed",
                )
            ],
            reason="Lead guest surname mismatch check",
        )

        res = self.service.link_booking(request=req, actor="agent_tom")
        self.assertEqual(res.status, "conflicting")
        self.assertTrue(any("do not match lead guest surname" in d for d in res.discrepancies))

    def test_link_booking_duplicate_reference_detection(self):
        """Attempting to link an Opera reservation already linked to another booking flags 'duplicate_references'."""
        # 1. First booking
        req1 = StaffLinkBookingRequest(
            booking_reference="RSP-20270514-DUP01",
            source_channel="online",
            lead_guest=LeadGuestContact(
                first_name="Jane",
                last_name="Golfer",
                email="jane@example.com",
                telephone="+353871234567",
            ),
            manual_opera_stay=OperaStayDetails(
                reservation_id="RES-SHARED-001",
                confirmation_number="CONF-SHARED-001",
                hotel_id="ROSA",
                room_category="DELUXE",
                room_quantity=1,
                arrival_date=dt.date(2027, 5, 14),
                departure_date=dt.date(2027, 5, 17),
                total_amount="990.00",
                currency="EUR",
                occupant_names=["Jane Golfer"],
            ),
            brs_rounds=[],
            payments=[],
            reason="First reservation",
        )
        self.service.link_booking(req1, "staff_tester")

        # 2. Second booking with same Opera reservation ID
        req2 = StaffLinkBookingRequest(
            booking_reference="RSP-20270514-DUP02",
            source_channel="phone",
            lead_guest=LeadGuestContact(
                first_name="Jane",
                last_name="Golfer",
                email="jane@example.com",
                telephone="+353871234567",
            ),
            manual_opera_stay=OperaStayDetails(
                reservation_id="RES-SHARED-001",
                confirmation_number="CONF-SHARED-001",
                hotel_id="ROSA",
                room_category="DELUXE",
                room_quantity=1,
                arrival_date=dt.date(2027, 5, 14),
                departure_date=dt.date(2027, 5, 17),
                total_amount="990.00",
                currency="EUR",
                occupant_names=["Jane Golfer"],
            ),
            brs_rounds=[],
            payments=[],
            reason="Second reservation attempting to reuse same Opera ID",
        )
        res2 = self.service.link_booking(req2, "staff_tester")
        self.assertEqual(res2.status, "duplicate_references")
        self.assertTrue(any("already linked to booking RSP-20270514-DUP01" in d for d in res2.discrepancies))

    def test_staff_correct_linkage_with_audit_trail(self):
        """Staff-driven correction updates components and records an immutable audit entry."""
        # 1. Initial incomplete booking
        req = StaffLinkBookingRequest(
            booking_reference="RSP-20270514-CORR01",
            source_channel="phone",
            lead_guest=LeadGuestContact(
                first_name="Jane",
                last_name="Golfer",
                email="jane@example.com",
                telephone="+353871234567",
            ),
            manual_opera_stay=OperaStayDetails(
                reservation_id="RES-CORR",
                confirmation_number="CONF-CORR",
                hotel_id="ROSA",
                room_category="DELUXE",
                room_quantity=1,
                arrival_date=dt.date(2027, 5, 14),
                departure_date=dt.date(2027, 5, 17),
                total_amount="990.00",
                currency="EUR",
                occupant_names=["Jane Golfer"],
            ),
            brs_rounds=[],
            payments=[],
            reason="Initial booking missing rounds and payment",
        )
        self.service.link_booking(req, "agent_1")

        # 2. Staff correction: add rounds and deposit payment
        corr_req = StaffCorrectLinkageRequest(
            reason="Guest phoned to confirm Old Tom round and Prommt deposit payment receipt",
            brs_rounds=[
                BrsRoundDetails(
                    round_id="RND-CORR-1",
                    course_id=1,
                    course_name="Old Tom Morris Links",
                    date=dt.date(2027, 5, 15),
                    time="09:30",
                    holes="18",
                    players=2,
                    price="360.00",
                    currency="EUR",
                )
            ],
            payments=[
                PaymentLedgerEntry(
                    payment_id="PAY-CORR-1",
                    provider="prommt",
                    payment_type="deposit",
                    amount="337.50",
                    currency="EUR",
                    status="completed",
                    provider_reference="PRM-999888",
                )
            ],
        )

        res = self.service.correct_linkage(
            booking_reference="RSP-20270514-CORR01",
            request=corr_req,
            actor="manager_alice",
        )
        self.assertEqual(res.status, "linked")
        self.assertEqual(len(res.discrepancies), 0)
        self.assertEqual(res.financials["total_amount"], "1350.00")
        self.assertEqual(res.financials["deposit_paid"], "337.50")
        self.assertEqual(len(res.audit_trail), 2)
        self.assertEqual(res.audit_trail[1].actor, "manager_alice")
        self.assertEqual(res.audit_trail[1].action, "linkage_corrected")
        self.assertIn("Guest phoned", res.audit_trail[1].reason)

    def test_idempotent_linking(self):
        """Calling link_booking with the same booking reference returns existing record unchanged."""
        req = StaffLinkBookingRequest(
            booking_reference="RSP-20270514-IDEM01",
            source_channel="online",
            lead_guest=LeadGuestContact(
                first_name="Jane",
                last_name="Golfer",
                email="jane@example.com",
                telephone="+353871234567",
            ),
            manual_opera_stay=OperaStayDetails(
                reservation_id="RES-IDEM",
                confirmation_number="CONF-IDEM",
                hotel_id="ROSA",
                room_category="DELUXE",
                room_quantity=1,
                arrival_date=dt.date(2027, 5, 14),
                departure_date=dt.date(2027, 5, 17),
                total_amount="990.00",
                currency="EUR",
                occupant_names=["Jane Golfer"],
            ),
            brs_rounds=[],
            payments=[],
            reason="Idempotency test creation",
        )
        first = self.service.link_booking(req, "agent_1")
        second = self.service.link_booking(req, "agent_1")

        self.assertEqual(first.booking_reference, second.booking_reference)
        self.assertEqual(first.created_at.date(), second.created_at.date())
        self.assertEqual(len(second.audit_trail), 1)

    def test_guest_lookup_privacy_and_security(self):
        """Guest lookup exposes safe fields and strictly suppresses internal vendor IDs."""
        req = StaffLinkBookingRequest(
            booking_reference="RSP-20270514-PRIV01",
            source_channel="online",
            lead_guest=LeadGuestContact(
                first_name="Jane",
                last_name="Golfer",
                email="jane@example.com",
                telephone="+353871234567",
            ),
            manual_opera_stay=OperaStayDetails(
                reservation_id="OPERA-SECRET-RES-999",
                confirmation_number="CONF-SECRET-999",
                hotel_id="ROSA",
                room_category="Deluxe Room",
                room_quantity=1,
                arrival_date=dt.date(2027, 5, 14),
                departure_date=dt.date(2027, 5, 17),
                total_amount="990.00",
                currency="EUR",
                occupant_names=["Jane Golfer"],
            ),
            brs_rounds=[
                BrsRoundDetails(
                    round_id="BRS-SECRET-ROUND-123",
                    course_id=1,
                    course_name="Old Tom Morris Links",
                    date=dt.date(2027, 5, 15),
                    time="09:00",
                    holes="18",
                    players=2,
                    brs_booking_id="BRS-SECRET-BOOKING-456",
                    price="360.00",
                    currency="EUR",
                )
            ],
            payments=[
                PaymentLedgerEntry(
                    payment_id="PAY-SECRET-TXN-789",
                    provider="prommt",
                    payment_type="deposit",
                    amount="337.50",
                    currency="EUR",
                    status="completed",
                    provider_reference="PROMMT-PAY-REF-INTERNAL",
                )
            ],
            reason="Privacy test",
        )
        self.service.link_booking(req, "agent_1")

        # Guest lookup
        summary = self.service.guest_lookup(
            booking_reference="RSP-20270514-PRIV01",
            last_name="Golfer",
        )
        self.assertEqual(summary.booking_reference, "RSP-20270514-PRIV01")
        self.assertEqual(summary.lead_guest_name, "Jane Golfer")
        self.assertIsNotNone(summary.stay)
        self.assertEqual(summary.stay.nights, 3)
        self.assertEqual(summary.stay.room_category, "Deluxe Room")
        self.assertEqual(len(summary.golf_rounds), 1)
        self.assertEqual(summary.golf_rounds[0].course_name, "Old Tom Morris Links")
        self.assertEqual(summary.financials.deposit_paid, "337.50")
        self.assertEqual(summary.financials.balance_due, "1012.50")

        # Assert no vendor identifiers exist in the serialized summary
        dumped = summary.model_dump(mode="json")
        dumped_str = str(dumped)
        self.assertNotIn("OPERA-SECRET-RES-999", dumped_str)
        self.assertNotIn("CONF-SECRET-999", dumped_str)
        self.assertNotIn("BRS-SECRET-ROUND-123", dumped_str)
        self.assertNotIn("BRS-SECRET-BOOKING-456", dumped_str)
        self.assertNotIn("PAY-SECRET-TXN-789", dumped_str)
        self.assertNotIn("PROMMT-PAY-REF-INTERNAL", dumped_str)
        self.assertNotIn("_id", dumped_str)

    def test_guest_lookup_mismatched_surname_or_missing(self):
        """Guest lookup fails with LinkageNotFoundError on wrong surname or missing reference."""
        req = StaffLinkBookingRequest(
            booking_reference="RSP-20270514-FAIL01",
            source_channel="online",
            lead_guest=LeadGuestContact(
                first_name="Jane",
                last_name="Golfer",
                email="jane@example.com",
                telephone="+353871234567",
            ),
            manual_opera_stay=None,
            brs_rounds=[],
            payments=[],
            reason="Lookup fail test",
        )
        self.service.link_booking(req, "agent_1")

        # Wrong surname
        with self.assertRaises(LinkageNotFoundError):
            self.service.guest_lookup("RSP-20270514-FAIL01", "WrongSurname")

        # Missing reference
        with self.assertRaises(LinkageNotFoundError):
            self.service.guest_lookup("RSP-NONEXISTENT", "Golfer")


class TestBookingLinkageRoutes(unittest.TestCase):
    """Test API route endpoints and MMB gate enforcement."""

    def setUp(self):
        self.mongo = MongoClient(tz_aware=True)
        self.db = self.mongo["test_linkage_routes_db"]
        self.settings = Settings(
            environment="production",
            api_key="M@cWorld123!!",
            staff_api_key="M@cWorld123!!",
            security_bypass_in_dev=False,
        )
        self.opera_client = OperaClient()
        self.linkage_service = BookingLinkageService(
            database=self.db,
            opera_client=self.opera_client,
        )
        self.mmb_gate_service = MmbGateService(
            database=self.db,
            settings=self.settings,
        )

        self.principal = StaffPrincipal(
            issuer="rosapenna_staff_auth",
            subject="staff_user_1",
            roles=frozenset(["reservations_manager", "site_admin"]),
        )

    def tearDown(self):
        self.mongo.close()

    def test_staff_routes_create_and_correct(self):
        """Staff endpoints create and correct booking linkages cleanly."""
        create_req = StaffLinkBookingRequest(
            booking_reference="RSP-20270514-ROUTE01",
            source_channel="phone",
            lead_guest=LeadGuestContact(
                first_name="Sarah",
                last_name="O'Connor",
                email="sarah@example.com",
                telephone="+353871234567",
            ),
            manual_opera_stay=OperaStayDetails(
                reservation_id="RES-ROUTE-1",
                confirmation_number="CONF-ROUTE-1",
                hotel_id="ROSA",
                room_category="CLASSIC",
                room_quantity=1,
                arrival_date=dt.date(2027, 5, 14),
                departure_date=dt.date(2027, 5, 17),
                total_amount="660.00",
                currency="EUR",
                occupant_names=["Sarah O'Connor"],
            ),
            brs_rounds=[],
            payments=[],
            reason="Phone reservation intake",
        )
        created = staff_link_booking(create_req, self.principal, self.linkage_service)
        self.assertEqual(created.booking_reference, "RSP-20270514-ROUTE01")
        self.assertEqual(created.status, "missing_components")

        # Correct via staff route
        correct_req = StaffCorrectLinkageRequest(
            reason="Customer completed balance and golf schedule via phone call",
            payments=[
                PaymentLedgerEntry(
                    payment_id="PAY-R-1",
                    provider="prommt",
                    payment_type="full",
                    amount="660.00",
                    currency="EUR",
                    status="completed",
                )
            ],
        )
        corrected = staff_correct_linkage("RSP-20270514-ROUTE01", correct_req, self.principal, self.linkage_service)
        self.assertEqual(corrected.financials["balance_due"], "0.00")

        # List linkages
        listed = staff_list_linkages(None, None, 50, self.principal, self.linkage_service)
        self.assertGreaterEqual(len(listed), 1)

        # Get linkage by reference
        fetched = staff_get_linkage("RSP-20270514-ROUTE01", self.principal, self.linkage_service)
        self.assertEqual(fetched.booking_reference, "RSP-20270514-ROUTE01")

    def test_guest_lookup_gated_by_mmb_release_gate(self):
        """When MMB release gate is OFF (default), require_mmb_enabled blocks guest lookup with HTTP 503."""
        # 1. Pre-seed booking
        seed_req = StaffLinkBookingRequest(
            booking_reference="RSP-20270514-GATED01",
            source_channel="online",
            lead_guest=LeadGuestContact(
                first_name="Pat",
                last_name="Gallagher",
                email="pat@example.com",
                telephone="+353871234567",
            ),
            manual_opera_stay=None,
            brs_rounds=[],
            payments=[],
            reason="Seed for gate test",
        )
        staff_link_booking(seed_req, self.principal, self.linkage_service)

        # 2. Gate is OFF: verify dependency raises 503
        mock_request = MagicMock()
        mock_request.app.state.mmb_gate_service = self.mmb_gate_service
        with self.assertRaises(HTTPException) as cm:
            require_mmb_enabled(mock_request)
        self.assertEqual(cm.exception.status_code, 503)

        # 3. Enable gate
        self.db["mmb_release_gate"].update_one(
            {"_id": "mmb_release_gate"},
            {"$set": {"enabled": True, "updated_at": dt.datetime.now(dt.timezone.utc).isoformat()}},
            upsert=True,
        )

        # 4. Now dependency passes without exception
        require_mmb_enabled(mock_request)

        # 5. Guest lookup succeeds
        lookup_req = GuestBookingLookupRequest(
            booking_reference="RSP-20270514-GATED01",
            last_name="Gallagher",
        )
        res = guest_lookup_booking(lookup_req, self.linkage_service)
        self.assertEqual(res.booking_reference, "RSP-20270514-GATED01")
        self.assertEqual(res.lead_guest_name, "Pat Gallagher")


if __name__ == "__main__":
    unittest.main()
