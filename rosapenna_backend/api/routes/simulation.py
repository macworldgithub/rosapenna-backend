"""Protected API routes for local payment simulation (Ticket N06).

Enforces zero external calls, strict development/test environment guard,
and constant-time raw HMAC signature verification.
"""

from __future__ import annotations

from typing import Any, Dict, Literal, Optional
from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field

from rosapenna_backend.api.dependencies import (
    get_booking_operation_engine,
    get_package_pricing_service,
    get_payment_simulator,
    get_settings,
)
from rosapenna_backend.api.schemas.booking_operations import ExecuteBookingOperationRequest
from rosapenna_backend.api.security import COMMON_DEPS
from rosapenna_backend.config import Settings
from rosapenna_backend.core.logging import logger
from rosapenna_backend.integrations.payments.simulation import (
    ALLOWED_SIMULATION_SCENARIOS,
    PaymentSimulator,
)
from rosapenna_backend.services.booking_operations import BookingOperationEngine
from rosapenna_backend.services.package_pricing import PackagePricingService

router = APIRouter(prefix="/dev/payments", tags=["simulation"], dependencies=COMMON_DEPS)


class SimulatePaymentRequestBody(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    scenario: Literal[
        "success",
        "failure",
        "cancellation",
        "pending",
        "expired",
        "delayed_success",
        "timeout",
    ] = Field(..., description="Allowlisted simulation scenario.")
    command_id: Optional[str] = Field(default=None, max_length=100)


def _check_simulation_policy(settings: Settings) -> None:
    if settings.environment not in ("development", "test"):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Simulation routes are strictly disabled in staging and production.",
        )
    if not settings.simulation_enabled or settings.payment_mode != "simulation":
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Simulation mode is not enabled on this server.",
        )
    if not settings.simulation_run_id:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Simulation run is not configured.",
        )


@router.post(
    "/{payment_id}/simulate",
    summary="Simulate a deposit payment outcome in safe development/test mode",
    description="Generates a signed local event, applies constant-time verification, and coordinates safe booking commit.",
)
async def simulate_payment(
    payment_id: str,
    body: SimulatePaymentRequestBody,
    settings: Settings = Depends(get_settings),
    simulator: PaymentSimulator = Depends(get_payment_simulator),
    pricing_service: PackagePricingService = Depends(get_package_pricing_service),
    operation_engine: BookingOperationEngine = Depends(get_booking_operation_engine),
) -> Dict[str, Any]:
    _check_simulation_policy(settings)

    req = simulator.get_payment_request(payment_id)
    if not req:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Simulation payment was not found.")

    payment_doc = pricing_service.get_payment(payment_id)
    if (req.simulation_run_id != settings.simulation_run_id
            or req.environment != settings.environment
            or not payment_doc
            or payment_doc.get("evidence_source") != "simulation"
            or payment_doc.get("is_test") is not True
            or payment_doc.get("simulation_run_id") != settings.simulation_run_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Simulation payment was not found.")

    secret = settings.simulation_signing_secret or simulator.default_secret

    try:
        envelope, signature, raw_bytes = simulator.generate_simulated_event(
            payment_id=payment_id,
            scenario=body.scenario,
            secret=secret,
            simulation_run_id=settings.simulation_run_id,
        )
    except TimeoutError as exc:
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail=str(exc),
        ) from None
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from None

    try:
        processed_req = simulator.process_simulation_event(
            raw_bytes=raw_bytes,
            signature=signature,
            secret=secret,
            allowed_run_id=settings.simulation_run_id,
        )
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from None
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from None

    # Sync into pricing service payment doc
    if payment_doc:
        payment_doc["status"] = processed_req.status
        payment_doc["evidence_source"] = "simulation"
        payment_doc["is_test"] = True
        payment_doc["simulation_run_id"] = settings.simulation_run_id
        if processed_req.status == "verified_paid":
            payment_doc["transaction_id"] = processed_req.transaction_id
            payment_doc["paid_at"] = processed_req.paid_at
        else:
            payment_doc["failure_reason"] = envelope.failure_reason

        pricing_service._payments_cache[payment_id] = payment_doc
        if pricing_service.payments_collection is not None:
            pricing_service.payments_collection.update_one(
                {"payment_id": payment_id},
                {"$set": payment_doc},
            )

    # If payment successfully verified, coordinate safe commit with BookingOperationEngine
    commit_result = None
    if processed_req.status == "verified_paid":
        booking_ref = processed_req.booking_reference
        quote_id = processed_req.quote_id
        cmd_key = body.command_id or f"sim_commit_{payment_id}"
        try:
            op_req = ExecuteBookingOperationRequest(
                operation_type="new_booking",
                booking_reference=booking_ref,
                quote_id=quote_id,
                payment_id=payment_id,
                idempotency_key=cmd_key,
            )
            op_resp = operation_engine.execute_operation(op_req)
            commit_result = op_resp.model_dump(mode="json")
        except Exception as exc:
            logger.error("Simulation auto-commit failed for %s: %s", booking_ref, exc)
            commit_result = {
                "status": "commit_failed",
                "error": str(exc),
            }

    return {
        "status": "success",
        "payment_id": payment_id,
        "payment_status": processed_req.status,
        "scenario": body.scenario,
        "evidence_source": "simulation",
        "is_test": True,
        "simulation_run_id": settings.simulation_run_id,
        "commit_result": commit_result,
    }


@router.get(
    "/{payment_id}",
    summary="Read simulation payment request state",
)
def get_simulation_payment(
    payment_id: str,
    settings: Settings = Depends(get_settings),
    simulator: PaymentSimulator = Depends(get_payment_simulator),
) -> Dict[str, Any]:
    _check_simulation_policy(settings)
    req = simulator.get_payment_request(payment_id)
    if not req:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Simulation payment '{payment_id}' was not found.",
        )
    return req.model_dump(mode="json")


@router.post(
    "/events/verify",
    summary="Private local event receiver for raw signed simulation webhook events",
)
async def verify_simulation_event(
    request: Request,
    x_simulation_signature: str = Header(..., alias="X-Simulation-Signature"),
    settings: Settings = Depends(get_settings),
    simulator: PaymentSimulator = Depends(get_payment_simulator),
) -> Dict[str, Any]:
    _check_simulation_policy(settings)
    secret = settings.simulation_signing_secret or simulator.default_secret
    raw_bytes = await request.body()
    try:
        processed = simulator.process_simulation_event(
            raw_bytes=raw_bytes,
            signature=x_simulation_signature,
            secret=secret,
            allowed_run_id=settings.simulation_run_id,
        )
        return {
            "status": "accepted",
            "payment_id": processed.payment_id,
            "payment_state": processed.status,
        }
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from None
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from None
