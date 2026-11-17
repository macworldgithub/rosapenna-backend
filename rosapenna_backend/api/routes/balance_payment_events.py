"""Event ingestion routes for Ticket N09 — Balance Payments.

Provides:
1. Future Prommt provider webhook ingress scaffold (disabled with 503 by default).
2. Dev / test simulation route for executing deterministic signed payment evidence.
"""

from __future__ import annotations

from typing import Any, Dict
from fastapi import APIRouter, Depends, HTTPException, Header, Request, Response, status

from rosapenna_backend.api.dependencies import get_balance_payment_service, get_settings
from rosapenna_backend.api.schemas.balance_payments import (
    DevSimulateBalancePaymentRequest,
)
from rosapenna_backend.config import Settings
from rosapenna_backend.integrations.payments.balance_gateway import LocalBalanceGateway
from rosapenna_backend.services.balance_payments import (
    BalanceEvidenceTamperedError,
    BalanceNotFoundError,
    BalancePaymentService,
)

router = APIRouter(tags=["Balance Payment Events"])


@router.post(
    "/payments/prommt/events",
    summary="Prommt payment provider webhook ingress (scaffold)",
)
async def handle_prommt_provider_event(
    request: Request,
    response: Response,
    settings: Settings = Depends(get_settings),
) -> Dict[str, Any]:
    """Future provider webhook ingress boundary.

    Disabled by default until official provider contract and signed payload specification are verified.
    """
    response.headers["Cache-Control"] = "no-store"
    if not settings.balance_provider_ingress_enabled:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Prommt provider webhook ingress is disabled pending official signed contract verification",
            headers={"Cache-Control": "no-store"},
        )

    # When enabled in future: read raw body up to 64 KiB, verify signature in constant time, ingest
    raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail="Provider contract unverified")


@router.post(
    "/dev/balance-payments/{payment_id}/simulate",
    summary="Simulate a signed balance payment event in development or test",
)
def simulate_balance_payment_event(
    payment_id: str,
    payload: DevSimulateBalancePaymentRequest,
    response: Response,
    settings: Settings = Depends(get_settings),
    service: BalancePaymentService = Depends(get_balance_payment_service),
) -> Dict[str, Any]:
    """Offline test and development payment simulator execution."""
    response.headers["Cache-Control"] = "no-store"
    env = settings.environment.strip().lower()
    if env not in ("development", "test") or settings.balance_payment_mode.strip().lower() != "simulation":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Simulated payment execution is permitted only in test or development environments with simulation mode",
            headers={"Cache-Control": "no-store"},
        )

    if not isinstance(service.gateway, LocalBalanceGateway):
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Balance gateway is not configured for local simulation",
        )

    clean_id = payment_id.strip()
    if service.requests_col is None:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Storage not configured")

    req_doc = service.requests_col.find_one({"payment_id": clean_id})
    if not req_doc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Payment request '{clean_id}' not found")

    from rosapenna_backend.api.schemas.balance_payments import BalancePaymentRequestDoc
    req_doc.pop("_id", None)
    req_obj = BalancePaymentRequestDoc(**req_doc)

    raw_bytes, sig, evidence = service.gateway.generate_simulated_event(
        payment_request=req_obj,
        scenario=payload.scenario,
    )

    try:
        receipt, booking_resp = service.ingest_verified_evidence(
            evidence=evidence,
            actor="dev_simulation_api",
        )
    except BalanceEvidenceTamperedError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    return {
        "status": "applied",
        "receipt_id": receipt.receipt_id,
        "payment_id": clean_id,
        "event_status": evidence.status,
        "booking_reference": evidence.booking_reference,
        "ledger_version": booking_resp.ledger_version,
        "balance_due": booking_resp.financials.get("balance_due"),
        "payment_status": booking_resp.financials.get("payment_status"),
    }
