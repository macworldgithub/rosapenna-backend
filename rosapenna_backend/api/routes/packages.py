"""API routes for Stay & Play package live pricing, quotes, and rules configuration."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from rosapenna_backend.api.dependencies import get_booking_operation_engine, get_package_pricing_service
from rosapenna_backend.api.schemas.booking_operations import ExecuteBookingOperationRequest
from rosapenna_backend.api.schemas.packages import (
    PackagePaymentLinkRequest,
    PackagePaymentLinkResponse,
    PackageQuoteRequest,
    PackageQuoteResponse,
    PackageRulesConfig,
    PackageSearchRequest,
    PackageSearchResponse,
    PaymentCallbackRequest,
    UpdatePackageRulesRequest,
)
from rosapenna_backend.api.security import COMMON_DEPS, rate_limit
from rosapenna_backend.integrations.prommt.schemas import PrommtWebhookPayload
from rosapenna_backend.integrations.prommt.client import PrommtError
from rosapenna_backend.core.logging import logger
from rosapenna_backend.services.booking_operations import BookingOperationEngine
from rosapenna_backend.services.package_pricing import PackagePricingService

router = APIRouter(prefix="/packages", tags=["packages"], dependencies=COMMON_DEPS)
webhook_router = APIRouter(prefix="/packages", tags=["packages"], dependencies=[Depends(rate_limit)])


@router.post(
    "/quote",
    response_model=PackageQuoteResponse,
    summary="Calculate a live, itemized Stay & Play package quote",
    description="Combines live Opera room rates and BRS golf rates, enforces package rules, and itemizes pricing without reserving inventory.",
)
def calculate_package_quote(
    request: PackageQuoteRequest,
    pricing_service: PackagePricingService = Depends(get_package_pricing_service),
):
    quote = pricing_service.calculate_quote(request)
    return quote


@router.get(
    "/quote/{quote_id}",
    response_model=PackageQuoteResponse,
    summary="Get and verify an existing itemized package quote",
    description="Retrieves a calculated quote and verifies whether it has expired.",
)
def get_package_quote(
    quote_id: str,
    pricing_service: PackagePricingService = Depends(get_package_pricing_service),
):
    quote = pricing_service.get_quote(quote_id)
    if not quote:
        raise HTTPException(status_code=404, detail=f"Quote '{quote_id}' was not found.")
    if pricing_service.is_quote_expired(quote):
        quote.status = "unavailable"
        if "Quote has expired." not in " ".join(quote.errors):
            quote.errors.append("Quote has expired (TTL exceeded). Please reprice to verify current rates.")
    return quote


@router.post(
    "/payment-link",
    response_model=PackagePaymentLinkResponse,
    summary="Create a scoped 25% deposit payment link for an accepted quote",
    description="Enforces terms acceptance, links quote to booking intent, sets 7-day expiry and day-5 reminder with zero inventory held.",
)
def create_package_payment_link(
    request: PackagePaymentLinkRequest,
    pricing_service: PackagePricingService = Depends(get_package_pricing_service),
):
    try:
        return pricing_service.create_payment_link(request)
    except PrommtError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None


@router.get(
    "/payments/{payment_id}",
    summary="Lookup payment link status and lifecycle details",
    description="Returns payment state and booking confirmation state without exposing reservation details.",
)
def get_package_payment(
    payment_id: str,
    pricing_service: PackagePricingService = Depends(get_package_pricing_service),
    operation_engine: BookingOperationEngine = Depends(get_booking_operation_engine),
):
    payment = pricing_service.get_payment(payment_id)
    if not payment:
        raise HTTPException(status_code=404, detail="Payment link was not found.")

    booking_status = "awaiting_payment"
    booking_reference = payment.get("booking_reference")
    quote_id = payment.get("quote_id")
    if payment["status"] == "verified_paid":
        booking_status = "pending_confirmation"
        if booking_reference and quote_id:
            booking_status = operation_engine.get_checkout_booking_status(
                booking_reference, payment_id, quote_id,
            )

    return {"status": payment["status"], "booking_status": booking_status}


@router.post(
    "/payment-callback",
    summary="Legacy payment callback (disabled)",
    description="Disabled until an authorized provider contract and verification flow are implemented.",
)
def handle_payment_callback(
    request: PaymentCallbackRequest,
    pricing_service: PackagePricingService = Depends(get_package_pricing_service),
):
    raise HTTPException(status_code=503, detail="Legacy payment callback is disabled.")



@webhook_router.post(
    "/prommt/webhook",
    summary="Verify Prommt payment notifications in the isolated TEST environment",
)
async def handle_prommt_webhook(
    request: Request,
    pricing_service: PackagePricingService = Depends(get_package_pricing_service),
):
    client = pricing_service.prommt_client
    if not _prommt_test_enabled(pricing_service) or not client.settings.webhook_secret.get_secret_value():
        raise HTTPException(status_code=503, detail="Provider payment webhook is disabled.")

    raw = bytearray()
    async for chunk in request.stream():
        if len(raw) + len(chunk) > 16384:
            raise HTTPException(status_code=413, detail="Webhook body is too large.")
        raw.extend(chunk)
    body = bytes(raw)
    if not client.verify_webhook_signature(body, request.headers.get("X-Signature", "")):
        raise HTTPException(status_code=401, detail="Invalid Prommt signature.")
    try:
        event = PrommtWebhookPayload.model_validate_json(body)
    except ValidationError:
        raise HTTPException(status_code=400, detail="Invalid Prommt event.") from None
    if event.mode != "TEST" or event.type != "PAYMENT_REQUEST":
        raise HTTPException(status_code=400, detail="Only Prommt TEST payment requests are accepted.")
    if not client.verify_timestamp(event.timestamp):
        raise HTTPException(status_code=400, detail="Prommt event timestamp is outside the accepted window.")

    try:
        payment = await run_in_threadpool(pricing_service.apply_prommt_test_payment, event)
    except PrommtError:
        raise HTTPException(status_code=502, detail="Prommt TEST payment lookup failed.") from None
    except ValueError:
        raise HTTPException(status_code=409, detail="Prommt TEST event did not match a saved deposit.") from None
    if payment is None:
        return {"status": "ignored"}

    return await run_in_threadpool(_finish_or_accept_pending, request, pricing_service, payment)


@router.post(
    "/payments/{payment_id}/reconcile-test",
    summary="Recover a missed Prommt TEST payment notice using the provider status API",
)
def reconcile_prommt_test_payment(
    payment_id: str,
    request: Request,
    pricing_service: PackagePricingService = Depends(get_package_pricing_service),
):
    if not _prommt_test_enabled(pricing_service) or pricing_service.settings.security_bypass_in_dev:
        raise HTTPException(status_code=503, detail="Authenticated Prommt TEST recovery is disabled.")
    try:
        payment = pricing_service.reconcile_prommt_test_payment(payment_id)
    except PrommtError:
        raise HTTPException(status_code=502, detail="Prommt TEST payment lookup failed.") from None
    except ValueError:
        raise HTTPException(status_code=409, detail="Prommt TEST has no matching verified payment for this deposit.") from None
    if payment is None:
        raise HTTPException(status_code=404, detail="Prommt TEST deposit was not found.")

    return _finish_or_accept_pending(request, pricing_service, payment)


def _prommt_test_enabled(pricing_service: PackagePricingService) -> bool:
    settings = pricing_service.settings
    client = pricing_service.prommt_client.settings
    return (
        settings is not None
        and settings.environment == "test"
        and settings.payment_mode == "prommt_test"
        and client.mode == "TEST"
        and not client.allow_live_calls
        and pricing_service.payments_collection is not None
    )


def _finish_or_accept_pending(request: Request, pricing_service: PackagePricingService, payment: dict):
    """Run the booking. A failed commit keeps booking_work pending and answers 202 (payment is saved)."""
    try:
        return _finish_prommt_test_payment(request, pricing_service, payment)
    except Exception as exc:
        logger.error("Prommt TEST booking commit failed for payment %s: %s", payment["payment_id"], type(exc).__name__)
        return JSONResponse(status_code=202, content={"status": "accepted_pending_booking"})


def _finish_prommt_test_payment(request: Request, pricing_service: PackagePricingService, payment: dict) -> dict:
    engine = get_booking_operation_engine(request)
    operation = engine.execute_operation(
        ExecuteBookingOperationRequest(
            operation_type="new_booking",
            booking_reference=payment["booking_reference"],
            quote_id=payment["quote_id"],
            payment_id=payment["payment_id"],
            idempotency_key=payment.get("booking_idempotency_key") or f"prommt_test_{payment['payment_id']}",
        )
    )
    pricing_service.complete_booking_work(payment["payment_id"], operation.status)
    return {"status": "processed", "booking_status": operation.status}


@router.post(
    "/search",
    response_model=PackageSearchResponse,
    summary="Search live Stay & Play availability combining Opera rooms and BRS tee times",
    description="Combines live hotel room inventory and tee sheets across all three courses without reserving inventory.",
)
def search_packages(
    request: PackageSearchRequest,
    pricing_service: PackagePricingService = Depends(get_package_pricing_service),
):
    return pricing_service.search_stay_and_golf(request)


@router.get(
    "/rules",
    response_model=PackageRulesConfig,
    summary="Get active package rules configuration",
    description="Returns current configuration governing caps, room category buffers, deposit percentages, and minimum rounds.",
)
def get_package_rules(
    pricing_service: PackagePricingService = Depends(get_package_pricing_service),
):
    return pricing_service.get_rules()


@router.put(
    "/rules",
    response_model=PackageRulesConfig,
    summary="Update package rules configuration",
    description="Authorized staff endpoint to update rules with an immutable audit record.",
)
def update_package_rules(
    request: UpdatePackageRulesRequest,
    pricing_service: PackagePricingService = Depends(get_package_pricing_service),
):
    return pricing_service.update_rules(request)
