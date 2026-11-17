"""Balance Payment Gateway Integration and Local Simulation for Ticket N09.

Strictly protects live Prommt credentials:
- Zero external HTTP requests (gw.prommt.com / app.prommt.com are never called).
- Runs disabled by default; provides LocalBalanceGateway for test and development simulation.
- Enforces constant-time HMAC-SHA256 signature verification over exact raw payload bytes.
- Produces immutable VerifiedBalanceEvidence only after cryptographic verification.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import json
from typing import Any, Dict, Optional, Protocol, Tuple
import uuid

from rosapenna_backend.api.schemas.balance_payments import (
    BalancePaymentRequestDoc,
    VerifiedBalanceEvidence,
    validate_price_string,
)
from rosapenna_backend.integrations.payments.simulation import (
    sign_simulation_payload,
    verify_simulation_signature,
)


class BalanceGatewayError(Exception):
    """Base exception for balance gateway operations."""


class BalanceGatewayDisabledError(BalanceGatewayError):
    """Raised when an operation is attempted on a disabled gateway."""


class BalanceGatewayTimeoutError(BalanceGatewayError):
    """Raised when payment link creation times out."""


class BalanceGatewaySignatureError(BalanceGatewayError):
    """Raised when webhook or evidence signature verification fails."""


def _utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


class BalanceGatewayProtocol(Protocol):
    """Protocol for balance payment gateway adapters."""

    def create_balance_request(
        self,
        booking_reference: str,
        property_id: str,
        amount: str,
        currency: str,
        payer_email: str,
        payer_name: str,
        booking_revision: int,
        ledger_version: int,
        accepted_snapshot_hash: str,
        command_id: str,
        idempotency_key: str,
        now: Optional[dt.datetime] = None,
    ) -> BalancePaymentRequestDoc:
        ...

    def verify_event(
        self,
        raw_bytes: bytes,
        signature: str,
        secret: Optional[str] = None,
    ) -> VerifiedBalanceEvidence:
        ...


class DisabledBalanceGateway:
    """Default gateway implementation that safely fails closed with zero network calls."""

    def create_balance_request(
        self,
        booking_reference: str,
        property_id: str,
        amount: str,
        currency: str,
        payer_email: str,
        payer_name: str,
        booking_revision: int,
        ledger_version: int,
        accepted_snapshot_hash: str,
        command_id: str,
        idempotency_key: str,
        now: Optional[dt.datetime] = None,
    ) -> BalancePaymentRequestDoc:
        raise BalanceGatewayDisabledError(
            "Balance payment gateway is disabled by policy; no live or simulated requests permitted."
        )

    def verify_event(
        self,
        raw_bytes: bytes,
        signature: str,
        secret: Optional[str] = None,
    ) -> VerifiedBalanceEvidence:
        raise BalanceGatewayDisabledError(
            "Balance payment gateway ingress is disabled; no events can be processed."
        )


class LocalBalanceGateway:
    """Local simulation gateway for Ticket N09 development and offline verification.

    Executes completely offline without external network calls.
    Generates and verifies cryptographically signed simulation evidence.
    """

    def __init__(
        self,
        signing_secret: str,
        environment: str = "test",
        property_id: str = "ROSA",
        test_run_id: Optional[str] = None,
        force_timeout: bool = False,
    ):
        if not signing_secret or len(signing_secret) < 16:
            raise ValueError("LocalBalanceGateway requires a signing_secret of at least 16 characters")
        self.signing_secret = signing_secret
        self.environment = environment.strip().lower()
        self.property_id = property_id.strip().upper()
        self.test_run_id = test_run_id
        self.force_timeout = force_timeout

    def create_balance_request(
        self,
        booking_reference: str,
        property_id: str,
        amount: str,
        currency: str,
        payer_email: str,
        payer_name: str,
        booking_revision: int,
        ledger_version: int,
        accepted_snapshot_hash: str,
        command_id: str,
        idempotency_key: str,
        now: Optional[dt.datetime] = None,
    ) -> BalancePaymentRequestDoc:
        """Create a simulated balance payment request with bound token."""
        if self.force_timeout:
            raise BalanceGatewayTimeoutError("Simulated creation timeout for balance payment request")

        clock = now or _utc_now()
        clean_amount = validate_price_string(amount)
        clean_curr = currency.strip().upper()
        clean_ref = booking_reference.strip().upper()

        payment_id = f"pay_bal_{uuid.uuid4().hex[:12]}"
        sim_token = uuid.uuid4().hex
        hosted_url = f"https://payments.rosapenna.invalid/balance/checkout?token=sim_{sim_token}&pid={payment_id}"
        expires_at = clock + dt.timedelta(days=14)

        return BalancePaymentRequestDoc(
            payment_id=payment_id,
            idempotency_key=idempotency_key,
            command_id=command_id,
            booking_reference=clean_ref,
            property_id=property_id or self.property_id,
            environment=self.environment,
            booking_revision=booking_revision,
            ledger_version=ledger_version,
            accepted_snapshot_hash=accepted_snapshot_hash,
            amount=clean_amount,
            currency=clean_curr,
            purpose="balance",
            payer_email=payer_email,
            payer_name=payer_name,
            provider="simulation",
            provider_account_id=f"acct_sim_{self.property_id.lower()}",
            provider_request_id=f"prm_req_{uuid.uuid4().hex[:10]}",
            hosted_url=hosted_url,
            state="ready",
            reason=None,
            attempts=1,
            created_at=clock,
            expires_at=expires_at,
            updated_at=clock,
            is_test=True,
            test_run_id=self.test_run_id,
        )

    def generate_simulated_event(
        self,
        payment_request: BalancePaymentRequestDoc,
        scenario: str = "success",
        now: Optional[dt.datetime] = None,
        override_amount: Optional[str] = None,
        override_currency: Optional[str] = None,
        custom_transaction_id: Optional[str] = None,
    ) -> Tuple[bytes, str, VerifiedBalanceEvidence]:
        """Generate raw JSON bytes and valid HMAC signature for a simulated payment event."""
        clock = now or _utc_now()
        event_id = f"evt_bal_{uuid.uuid4().hex[:12]}"
        receipt_id = f"rcpt_bal_{uuid.uuid4().hex[:12]}"

        if scenario == "success":
            status = "completed"
            txn_id = custom_transaction_id or f"txn_bal_{uuid.uuid4().hex[:12]}"
        elif scenario == "failure":
            status = "failed"
            txn_id = custom_transaction_id or f"txn_bal_fail_{uuid.uuid4().hex[:8]}"
        elif scenario == "pending":
            status = "pending"
            txn_id = custom_transaction_id or f"txn_bal_pend_{uuid.uuid4().hex[:8]}"
        elif scenario == "expired":
            status = "failed"
            txn_id = custom_transaction_id or f"txn_bal_exp_{uuid.uuid4().hex[:8]}"
        else:
            status = "completed"
            txn_id = custom_transaction_id or f"txn_bal_{uuid.uuid4().hex[:12]}"

        amt = override_amount or payment_request.amount
        curr = override_currency or payment_request.currency

        payload_dict: Dict[str, Any] = {
            "schema_version": "1.0",
            "event_id": event_id,
            "receipt_id": receipt_id,
            "payment_id": payment_request.payment_id,
            "booking_reference": payment_request.booking_reference,
            "property_id": payment_request.property_id,
            "environment": self.environment,
            "purpose": "balance",
            "amount": amt,
            "currency": curr,
            "status": status,
            "transaction_id": txn_id,
            "occurred_at": clock.isoformat(),
            "evidence_source": "simulation",
            "is_test": True,
            "test_run_id": self.test_run_id,
        }

        raw_bytes = json.dumps(payload_dict, sort_keys=True, separators=(",", ":")).encode("utf-8")
        signature = sign_simulation_payload(raw_bytes, self.signing_secret)
        raw_hash = hashlib.sha256(raw_bytes).hexdigest()

        evidence = VerifiedBalanceEvidence(
            receipt_id=receipt_id,
            event_id=event_id,
            transaction_id=txn_id,
            payment_id=payment_request.payment_id,
            booking_reference=payment_request.booking_reference,
            property_id=payment_request.property_id,
            environment=self.environment,
            purpose="balance",
            amount=amt,
            currency=curr,
            status=status,
            occurred_at=clock,
            verified_at=clock,
            evidence_source="simulation",
            raw_event_hash=raw_hash,
            is_test=True,
            test_run_id=self.test_run_id,
        )

        return raw_bytes, signature, evidence

    def verify_event(
        self,
        raw_bytes: bytes,
        signature: str,
        secret: Optional[str] = None,
    ) -> VerifiedBalanceEvidence:
        """Verify HMAC-SHA256 signature in constant time and parse into VerifiedBalanceEvidence."""
        active_secret = secret or self.signing_secret
        if not verify_simulation_signature(raw_bytes, signature, active_secret):
            raise BalanceGatewaySignatureError("Simulation HMAC signature verification failed")

        try:
            data = json.loads(raw_bytes.decode("utf-8"))
        except Exception as exc:
            raise BalanceGatewaySignatureError(f"Malformed event JSON: {exc}") from exc

        # Strict validation of envelope fields
        if data.get("purpose") != "balance":
            raise BalanceGatewaySignatureError(f"Invalid payment purpose: {data.get('purpose')}")
        if data.get("environment") != self.environment:
            raise BalanceGatewaySignatureError(
                f"Environment mismatch: event has {data.get('environment')}, expected {self.environment}"
            )
        if self.test_run_id and data.get("test_run_id") != self.test_run_id:
            raise BalanceGatewaySignatureError(
                f"Test run mismatch: event has {data.get('test_run_id')}, expected {self.test_run_id}"
            )

        raw_hash = hashlib.sha256(raw_bytes).hexdigest()
        occurred_dt = (
            dt.datetime.fromisoformat(data["occurred_at"])
            if isinstance(data.get("occurred_at"), str)
            else data.get("occurred_at") or _utc_now()
        )
        if occurred_dt.tzinfo is None:
            occurred_dt = occurred_dt.replace(tzinfo=dt.timezone.utc)

        return VerifiedBalanceEvidence(
            receipt_id=data.get("receipt_id") or f"rcpt_bal_{uuid.uuid4().hex[:12]}",
            event_id=data["event_id"],
            transaction_id=data["transaction_id"],
            payment_id=data["payment_id"],
            booking_reference=data["booking_reference"],
            property_id=data.get("property_id", self.property_id),
            environment=self.environment,
            purpose="balance",
            amount=validate_price_string(data["amount"]),
            currency=data.get("currency", "EUR").strip().upper(),
            status=data["status"],
            occurred_at=occurred_dt,
            verified_at=_utc_now(),
            evidence_source="simulation",
            raw_event_hash=raw_hash,
            is_test=True,
            test_run_id=self.test_run_id,
        )
