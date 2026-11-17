"""Run the audited N09 synthetic balance payment fixture demonstration.

Demonstrates deterministic balance calculation, request creation, verified evidence ingestion,
and reminder suppression in an isolated loopback test environment without external network calls.
"""

from __future__ import annotations

import argparse
import datetime as dt
from decimal import Decimal
import os
from pathlib import Path
import secrets
import sys
import tempfile

from mongomock import MongoClient

_ALLOWED_HOSTS = {"", "localhost", "127.0.0.1", "::1"}


def _guard(event: str, args: tuple) -> None:
    host: object = None
    if event == "socket.connect":
        address = args[1] if len(args) > 1 else None
        host = address[0] if isinstance(address, tuple) and address else address
    elif event == "socket.sendto":
        address = args[-1] if args else None
        host = address[0] if isinstance(address, tuple) and address else address
    elif event in {"socket.getaddrinfo", "socket.gethostbyname", "socket.gethostbyname_ex"}:
        host = args[0] if args else None
        if host is None:
            return
    elif event == "http.client.connect":
        host = args[1] if len(args) > 1 else None
    elif event in {"subprocess.Popen", "os.system", "os.exec", "os.spawn"}:
        raise PermissionError("N09 fixture policy blocked process launch")
    else:
        return

    if host is not None and str(host).strip().lower() not in _ALLOWED_HOSTS:
        raise PermissionError(f"N09 fixture policy blocked network access to '{host}'")


def run_fixture(scenario: str = "balance-success") -> int:
    backend_root = Path(__file__).resolve().parents[1]
    if str(backend_root) not in sys.path:
        sys.path.insert(0, str(backend_root))

    # Enforce loopback guard
    sys.addaudithook(_guard)

    from rosapenna_backend.api.schemas.booking_linkage import (
        BrsRoundDetails,
        LeadGuestContact,
        OperaStayDetails,
        PaymentLedgerEntry,
        StaffLinkBookingRequest,
    )
    from rosapenna_backend.api.schemas.pre_arrival import PreArrivalPolicyDoc
    from rosapenna_backend.integrations.payments.balance_gateway import LocalBalanceGateway
    from rosapenna_backend.services.balance_payments import BalancePaymentService
    from rosapenna_backend.services.booking_linkage import BookingLinkageService
    from rosapenna_backend.services.email_delivery import DisabledConfirmationTransport
    from rosapenna_backend.services.pre_arrival import PreArrivalService

    mongo = MongoClient(tz_aware=True)
    db = mongo["n09_synthetic_fixture_db"]
    linkage_service = BookingLinkageService(database=db)

    test_run_id = f"fix-n09-{secrets.token_hex(4)}"
    signing_secret = secrets.token_hex(32)

    gateway = LocalBalanceGateway(
        signing_secret=signing_secret,
        environment="test",
        property_id="ROSA",
        test_run_id=test_run_id,
    )

    transport = DisabledConfirmationTransport()
    pre_arrival_service = PreArrivalService(
        database=db,
        transport=transport,
        environment="test",
        property_id="ROSA",
        test_run_id=test_run_id,
    )

    balance_service = BalancePaymentService(
        database=db,
        linkage_service=linkage_service,
        gateway=gateway,
        pre_arrival_service=pre_arrival_service,
        environment="test",
        property_id="ROSA",
        test_run_id=test_run_id,
    )
    pre_arrival_service.balance_service = balance_service

    # Fixture baseline
    clock = dt.datetime(2027, 5, 1, 8, 0, 0, tzinfo=dt.timezone.utc)
    arrival_date = dt.date(2027, 7, 1)

    print("=" * 70)
    print("N09 SYNTHETIC FIXTURE DEMONSTRATION")
    print(f"  Scenario    : {scenario}")
    print(f"  Test Run ID : {test_run_id}")
    print(f"  Clock       : {clock.isoformat()}")
    print("=" * 70)

    # 1. Seed policy (42 days due, 56 days first request)
    policy = PreArrivalPolicyDoc(
        policy_id="POL-ROSA-FIXTURE",
        version=1,
        environment="test",
        approval_status="approved",
        approved_by="finance_director",
        approved_at=clock,
        source="Approved Fixture Baseline",
        timezone="Europe/Dublin",
        local_send_time="09:00",
        balance_request_lead_days=56,
        balance_due_lead_days=42,
        rooming_caddy_lead_days=42,
    )
    db["pre_arrival_policies"].insert_one(policy.model_dump(mode="json"))

    # 2. Seed confirmed booking: EUR 1600 total (1000 stay + 600 golf), EUR 400 deposit
    booking_ref = "RSP-20270701-FIX01"
    seed_req = StaffLinkBookingRequest(
        booking_reference=booking_ref,
        source_channel="online",
        lead_guest=LeadGuestContact(
            first_name="Jane",
            last_name="Golfer",
            email="guest@booking.invalid",
            telephone="+353871234567",
        ),
        manual_opera_stay=OperaStayDetails(
            reservation_id="RES-OPERA-FIX01",
            confirmation_number="CONF-OPERA-FIX01",
            hotel_id="ROSA",
            room_category="DELUXE",
            room_quantity=1,
            arrival_date=arrival_date,
            departure_date=dt.date(2027, 7, 5),
            total_amount="1000.00",
            currency="EUR",
            occupant_names=["Jane Golfer"],
        ),
        brs_rounds=[
            BrsRoundDetails(
                round_id="RND-BRS-FIX01",
                course_id=1,
                course_name="Old Tom Morris Links",
                date=dt.date(2027, 7, 2),
                time="10:00",
                holes="18",
                players=2,
                brs_booking_id="BRS-FIX-BOOKING-01",
                price="600.00",
                currency="EUR",
            )
        ],
        payments=[
            PaymentLedgerEntry(
                payment_id="PAY-DEP-FIX01",
                provider="simulation",
                payment_type="deposit",
                amount="400.00",
                currency="EUR",
                status="completed",
                provider_reference="TXN-DEP-FIX01",
                verified_at=clock,
            )
        ],
        reason="N09 synthetic baseline fixture setup",
    )
    linkage_service.link_booking(seed_req, actor="fixture_setup")

    # Schedule N08 reminders
    pre_arrival_service.reconcile_booking(booking_ref, now=clock)

    # 3. Read initial summary
    summary_before = balance_service.get_balance_summary(booking_ref, now=clock)
    print("\n[Step 1: Baseline Summary]")
    print(f"  Booking Reference : {summary_before.booking_reference}")
    print(f"  Accepted Total    : EUR {summary_before.accepted_total}")
    print(f"  Deposit Paid      : EUR {summary_before.verified_deposit}")
    print(f"  Remaining Balance : EUR {summary_before.balance_due}")
    print(f"  Balance Due Date  : {summary_before.balance_due_date} (Arrival minus 42 days)")
    print(f"  First Request Date: {summary_before.first_request_date} (Arrival minus 56 days)")
    print(f"  Collection Status : {summary_before.collection_status}")

    assert summary_before.balance_due == "1200.00", "Baseline balance due must be 1200.00"

    # 4. Create bound balance payment request
    cmd_id = f"cmd_fix_{secrets.token_hex(4)}"
    req = balance_service.ensure_balance_request(
        booking_reference=booking_ref,
        expected_booking_revision=1,
        expected_ledger_version=1,
        command_id=cmd_id,
        actor="fixture_operator",
        now=clock,
    )
    print("\n[Step 2: Bound Request Created]")
    print(f"  Payment ID        : {req.payment_id}")
    print(f"  Amount Bound      : EUR {req.amount}")
    print(f"  Hosted URL        : {req.hosted_url}")
    print(f"  Request State     : {req.state}")

    # 5. Ingest verified payment evidence
    _, _, evidence = gateway.generate_simulated_event(
        payment_request=req,
        scenario="success",
        now=clock,
    )
    receipt, linkage_after = balance_service.ingest_verified_evidence(
        evidence=evidence,
        actor="fixture_webhook",
        now=clock,
    )

    print("\n[Step 3: Verified Evidence Ingested]")
    print(f"  Receipt ID        : {receipt.receipt_id}")
    print(f"  Transaction ID    : {receipt.transaction_id}")
    print(f"  Receipt State     : {receipt.state}")
    print(f"  New Ledger Version: {linkage_after.ledger_version}")
    print(f"  New Balance Due   : EUR {linkage_after.financials['balance_due']}")
    print(f"  Total Paid        : EUR {linkage_after.financials['verified_paid_total']}")
    print(f"  Payment Status    : {linkage_after.financials['payment_status']}")

    assert linkage_after.financials["balance_due"] == "0.00", "Remaining balance must be 0.00"
    assert linkage_after.ledger_version == 2, "Ledger version must increment to 2"

    # 6. Verify N08 reminder suppression
    n08_jobs = pre_arrival_service.get_jobs_for_booking(booking_ref)
    bal_job = next(j for j in n08_jobs if j.reminder_type == "balance_due_first")
    rc_job = next(j for j in n08_jobs if j.reminder_type == "rooming_and_caddy")

    print("\n[Step 4: N08 Pre-Arrival Truthful Suppression]")
    print(f"  Balance Job Status: {bal_job.status} (Reason: {bal_job.reason_code})")
    print(f"  Rooming Job Status: {rc_job.status} (Reason: {rc_job.reason_code})")

    assert bal_job.status == "suppressed", "Balance reminder must be suppressed after full payment"
    assert bal_job.reason_code == "BALANCE_ALREADY_PAID"
    assert rc_job.status == "scheduled", "Rooming/caddy preference reminder must remain scheduled"

    # 7. Duplicate event idempotency test
    print("\n[Step 5: Duplicate Event Idempotency Check]")
    receipt_dup, linkage_dup = balance_service.ingest_verified_evidence(
        evidence=evidence,
        actor="fixture_webhook_replay",
        now=clock,
    )
    assert receipt_dup.receipt_id == receipt.receipt_id
    assert linkage_dup.financials["balance_due"] == "0.00"
    assert linkage_dup.ledger_version == 2
    print("  Idempotent replay verified: zero duplicate charge or ledger corruption.")

    print("\n" + "=" * 70)
    print("N09 SYNTHETIC FIXTURE DEMONSTRATION: SUCCESSFUL")
    print("=" * 70)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="N09 synthetic balance payment fixture runner")
    parser.add_argument("--scenario", default="balance-success", choices=["balance-success"], help="Scenario to run")
    parser.add_argument("--offline", action="store_true", default=True, help="Force offline mode")
    args = parser.parse_args()
    return run_fixture(scenario=args.scenario)


if __name__ == "__main__":
    raise SystemExit(main())
