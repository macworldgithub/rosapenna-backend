"""Local Payment Simulator and Evidence Engine for Ticket N06.

Enforces zero external network calls:
- Runs strictly in development and test environments with BOOKING_SIMULATION_ENABLED=True.
- Binds payment attempts to quote version, hash, exact decimal amount, currency, intent, and simulation run ID.
- Generates signed local simulation events with constant-time HMAC verification.
- Prohibits regression of verified payments and prevents cross-intent replay.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import json
import uuid
from decimal import Decimal
from typing import Any, Dict, List, Literal, Optional, Tuple

from pydantic import BaseModel, Field


def _utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _utc_now_str() -> str:
    return _utc_now().isoformat()


def sign_simulation_payload(raw_bytes: bytes, secret: str) -> str:
    """Compute constant-time verifiable HMAC-SHA256 signature over exact raw bytes."""
    return hmac.new(secret.encode("utf-8"), raw_bytes, hashlib.sha256).hexdigest()


def verify_simulation_signature(raw_bytes: bytes, signature: str, secret: str) -> bool:
    """Verify HMAC-SHA256 signature using constant-time comparison."""
    if not signature or not secret:
        return False
    expected = sign_simulation_payload(raw_bytes, secret)
    return hmac.compare_digest(expected.lower(), signature.strip().lower())


class SimulationPaymentRequest(BaseModel):
    payment_id: str
    booking_intent_id: str
    booking_reference: str
    quote_id: str
    quote_version: int
    quote_hash: str
    purpose: str = "deposit"
    amount: str
    currency: str = "EUR"
    environment: str = "development"
    simulation_run_id: Optional[str] = None
    created_at: str
    expires_at: str
    reminder_at: str
    status: Literal["unpaid", "pending", "verified_paid", "failed", "expired", "cancelled"] = "unpaid"
    url: str
    evidence_source: str = "simulation"
    is_test: bool = True
    transaction_id: Optional[str] = None
    paid_at: Optional[str] = None
    failure_reason: Optional[str] = None


class SimulationEventEnvelope(BaseModel):
    schema_version: str = "1.0"
    event_id: str
    event_type: Literal["payment.succeeded", "payment.failed", "payment.cancelled", "payment.expired", "payment.pending"]
    request_id: str
    payment_id: str
    intent_id: str
    purpose: str = "deposit"
    amount: str
    currency: str = "EUR"
    transaction_id: str
    occurred_at: str
    simulation_run_id: Optional[str] = None
    evidence_source: str = "simulation"
    is_test: bool = True
    status: str
    failure_reason: Optional[str] = None


ALLOWED_SIMULATION_SCENARIOS = {
    "success",
    "failure",
    "cancellation",
    "pending",
    "expired",
    "delayed_success",
    "timeout",
}


class PaymentSimulator:
    """In-memory and MongoDB-backed payment simulator for offline development and testing."""

    def __init__(self, database=None, default_secret: Optional[str] = None):
        self.db = database
        import secrets
        self.default_secret = default_secret or secrets.token_urlsafe(32)
        self._mem_requests: Dict[str, dict] = {}
        self._mem_events: Dict[str, dict] = {}
        if self.db is not None:
            self._init_indexes()

    def _init_indexes(self) -> None:
        if self.db is None:
            return
        coll = self.db["simulation_payments"]
        coll.create_index(
            [("environment", 1), ("booking_intent_id", 1), ("purpose", 1), ("quote_version", 1)],
            unique=True,
            name="uniq_sim_intent_quote_attempt",
        )
        coll.create_index([("payment_id", 1)], unique=True, name="uniq_sim_payment_id")
        events_coll = self.db["simulation_payment_events"]
        events_coll.create_index([("event_id", 1)], unique=True, name="uniq_sim_event_id")

    def create_or_get_payment_request(
        self,
        booking_intent_id: str,
        booking_reference: str,
        quote_id: str,
        quote_version: int,
        quote_hash: str,
        amount: str,
        currency: str = "EUR",
        environment: str = "development",
        simulation_run_id: Optional[str] = None,
        lead_guest: Optional[Dict[str, Any]] = None,
    ) -> SimulationPaymentRequest:
        """Create or idempotently retrieve active payment request scoped to intent & quote version."""
        # Normalize exact decimal representation
        amount_decimal = Decimal(str(amount)).quantize(Decimal("0.01"))
        amount_str = f"{amount_decimal:.2f}"

        # 1. Check existing attempt in Mongo or memory
        query = {
            "environment": environment,
            "booking_intent_id": booking_intent_id,
            "purpose": "deposit",
            "quote_version": quote_version,
        }
        existing = None
        if self.db is not None:
            existing = self.db["simulation_payments"].find_one(query)
        else:
            for req in self._mem_requests.values():
                if (
                    req.get("environment") == environment
                    and req.get("booking_intent_id") == booking_intent_id
                    and req.get("purpose") == "deposit"
                    and req.get("quote_version") == quote_version
                ):
                    existing = req
                    break

        if existing:
            return SimulationPaymentRequest.model_validate(existing)

        # 2. Build fresh request
        payment_id = f"pay_sim_{uuid.uuid4().hex[:12]}"
        now = _utc_now()
        created_at_str = now.isoformat()
        expires_at_str = (now + dt.timedelta(days=7)).isoformat()
        reminder_at_str = (now + dt.timedelta(days=5)).isoformat()
        local_url = f"/dev/payments/{payment_id}"

        doc = {
            "payment_id": payment_id,
            "booking_intent_id": booking_intent_id,
            "booking_reference": booking_reference,
            "quote_id": quote_id,
            "quote_version": quote_version,
            "quote_hash": quote_hash,
            "purpose": "deposit",
            "amount": amount_str,
            "currency": currency.upper(),
            "environment": environment,
            "simulation_run_id": simulation_run_id,
            "created_at": created_at_str,
            "expires_at": expires_at_str,
            "reminder_at": reminder_at_str,
            "status": "unpaid",
            "url": local_url,
            "evidence_source": "simulation",
            "is_test": True,
            "lead_guest": lead_guest or {},
        }

        if self.db is not None:
            self.db["simulation_payments"].insert_one(doc)
        else:
            self._mem_requests[payment_id] = doc

        return SimulationPaymentRequest.model_validate(doc)

    def get_payment_request(self, payment_id: str) -> Optional[SimulationPaymentRequest]:
        doc = None
        if self.db is not None:
            doc = self.db["simulation_payments"].find_one({"payment_id": payment_id})
        else:
            doc = self._mem_requests.get(payment_id)
        if not doc:
            return None
        return SimulationPaymentRequest.model_validate(doc)

    def generate_simulated_event(
        self,
        payment_id: str,
        scenario: str,
        secret: str,
        simulation_run_id: Optional[str] = None,
        event_id_override: Optional[str] = None,
    ) -> Tuple[SimulationEventEnvelope, str, bytes]:
        """Derive simulation event from saved payment request and sign raw bytes with secret."""
        if scenario not in ALLOWED_SIMULATION_SCENARIOS:
            raise ValueError(f"Scenario '{scenario}' not allowed. Must be one of {sorted(ALLOWED_SIMULATION_SCENARIOS)}")

        req = self.get_payment_request(payment_id)
        if not req:
            raise LookupError(f"Payment request '{payment_id}' not found.")

        # Determine event type & status from scenario
        if scenario in ("success", "delayed_success"):
            event_type = "payment.succeeded"
            status = "verified_paid"
            failure_reason = None
        elif scenario == "failure":
            event_type = "payment.failed"
            status = "failed"
            failure_reason = "Simulated card decline by test gateway"
        elif scenario == "cancellation":
            event_type = "payment.cancelled"
            status = "cancelled"
            failure_reason = "Customer cancelled simulation checkout"
        elif scenario == "expired":
            event_type = "payment.expired"
            status = "expired"
            failure_reason = "Deposit link expired after 7 days"
        elif scenario == "pending":
            event_type = "payment.pending"
            status = "pending"
            failure_reason = None
        elif scenario == "timeout":
            raise TimeoutError("Simulated upstream payment gateway network timeout")
        else:
            event_type = "payment.failed"
            status = "failed"
            failure_reason = "Unknown scenario"

        event_id = event_id_override or f"evt_sim_{uuid.uuid4().hex[:12]}"
        txn_id = f"txn_sim_{uuid.uuid4().hex[:12]}"

        envelope = SimulationEventEnvelope(
            schema_version="1.0",
            event_id=event_id,
            event_type=event_type,
            request_id=f"req_sim_{payment_id}",
            payment_id=payment_id,
            intent_id=req.booking_intent_id,
            purpose=req.purpose,
            amount=req.amount,
            currency=req.currency,
            transaction_id=txn_id,
            occurred_at=_utc_now_str(),
            simulation_run_id=simulation_run_id or req.simulation_run_id,
            evidence_source="simulation",
            is_test=True,
            status=status,
            failure_reason=failure_reason,
        )

        raw_bytes = json.dumps(envelope.model_dump(mode="json"), sort_keys=True).encode("utf-8")
        signature = sign_simulation_payload(raw_bytes, secret)

        return envelope, signature, raw_bytes

    def process_simulation_event(
        self,
        raw_bytes: bytes,
        signature: str,
        secret: str,
        allowed_run_id: Optional[str] = None,
    ) -> SimulationPaymentRequest:
        """Validate signature over exact raw bytes, correlate with intent/payment, and apply idempotent state transition."""
        # 1. Constant-time raw signature verification
        if not verify_simulation_signature(raw_bytes, signature, secret):
            raise ValueError("Invalid simulation HMAC signature.")

        # 2. Parse envelope
        try:
            data = json.loads(raw_bytes.decode("utf-8"))
            envelope = SimulationEventEnvelope.model_validate(data)
        except Exception as exc:
            raise ValueError(f"Malformed simulation event envelope: {exc}") from exc

        # 3. Check timestamp tolerance (e.g. 300s window)
        try:
            occurred = dt.datetime.fromisoformat(envelope.occurred_at)
            now = _utc_now()
            if abs((now - occurred).total_seconds()) > 300:
                raise ValueError("Simulation event timestamp skewed by more than 300 seconds.")
        except ValueError as exc:
            if "timestamp skewed" in str(exc):
                raise
            # Unparseable timestamp
            raise ValueError("Invalid timestamp in simulation event envelope.") from exc

        # 4. Check run binding if constrained
        if allowed_run_id and envelope.simulation_run_id != allowed_run_id:
            raise ValueError("Simulation event does not match the active run.")

        # 5. Look up matching payment request
        req = self.get_payment_request(envelope.payment_id)
        if not req:
            raise LookupError(f"Payment request '{envelope.payment_id}' does not exist.")

        # 6. Correlation integrity checks
        if allowed_run_id and req.simulation_run_id != allowed_run_id:
            raise ValueError("Simulation payment does not match the active run.")
        if (req.environment not in {"development", "test"}
                or req.evidence_source != "simulation" or req.is_test is not True
                or envelope.evidence_source != "simulation" or envelope.is_test is not True):
            raise ValueError("Invalid simulation payment provenance.")
        if envelope.request_id != f"req_sim_{req.payment_id}":
            raise ValueError("Simulation payment request does not match the event.")
        expected_status = {
            "payment.succeeded": "verified_paid",
            "payment.failed": "failed",
            "payment.cancelled": "cancelled",
            "payment.expired": "expired",
            "payment.pending": "pending",
        }
        if envelope.status != expected_status[envelope.event_type]:
            raise ValueError("Simulation event type and status do not match.")
        if req.booking_intent_id != envelope.intent_id:
            raise ValueError(f"Intent mismatch: payment belongs to '{req.booking_intent_id}', event specifies '{envelope.intent_id}'.")

        if Decimal(req.amount) != Decimal(envelope.amount):
            raise ValueError(f"Amount mismatch: expected '{req.amount}', event specifies '{envelope.amount}'.")

        if req.currency != envelope.currency:
            raise ValueError(f"Currency mismatch: expected '{req.currency}', event specifies '{envelope.currency}'.")

        if req.purpose != envelope.purpose:
            raise ValueError(f"Purpose mismatch: expected '{req.purpose}', event specifies '{envelope.purpose}'.")

        # 7. Check idempotency and payload conflict for repeated event ID
        event_query = {"event_id": envelope.event_id}
        payload_hash = hashlib.sha256(raw_bytes).hexdigest()
        existing_event = None
        if self.db is not None:
            existing_event = self.db["simulation_payment_events"].find_one(event_query)
        else:
            existing_event = self._mem_events.get(envelope.event_id)

        if existing_event:
            if existing_event.get("payload_hash") != payload_hash:
                raise ValueError(f"Event ID '{envelope.event_id}' was already submitted with different payload (conflict).")
            # Identical replay: return current payment state
            return req

        # 8. Conditional state transitions (cannot regress verified_paid)
        if req.status == "verified_paid" and envelope.status != "verified_paid":
            # Out-of-order failure/pending does NOT overwrite already verified_paid
            return req

        updates: Dict[str, Any] = {
            "status": envelope.status,
            "transaction_id": envelope.transaction_id,
            "evidence_source": "simulation",
            "is_test": True,
        }
        if envelope.status == "verified_paid":
            updates["paid_at"] = envelope.occurred_at
            updates["failure_reason"] = None
        else:
            updates["failure_reason"] = envelope.failure_reason

        if self.db is not None:
            self.db["simulation_payments"].update_one(
                {"payment_id": envelope.payment_id},
                {"$set": updates},
            )
            self.db["simulation_payment_events"].insert_one({
                "event_id": envelope.event_id,
                "payload_hash": payload_hash,
                "payment_id": envelope.payment_id,
                "created_at": _utc_now_str(),
            })
        else:
            self._mem_requests[envelope.payment_id].update(updates)
            self._mem_events[envelope.event_id] = {
                "event_id": envelope.event_id,
                "payload_hash": payload_hash,
                "payment_id": envelope.payment_id,
                "created_at": _utc_now_str(),
            }

        return self.get_payment_request(envelope.payment_id)
