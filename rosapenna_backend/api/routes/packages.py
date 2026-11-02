"""API routes for Stay & Play package live pricing, quotes, and rules configuration."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from rosapenna_backend.api.dependencies import get_package_pricing_service
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
from rosapenna_backend.api.security import COMMON_DEPS
from rosapenna_backend.services.package_pricing import PackagePricingService

router = APIRouter(prefix="/packages", tags=["packages"], dependencies=COMMON_DEPS)


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
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None


@router.get(
    "/payments/{payment_id}",
    summary="Lookup payment link status and lifecycle details",
    description="Returns the current state (unpaid, verified_paid, failed, expired) of a payment link.",
)
def get_package_payment(
    payment_id: str,
    pricing_service: PackagePricingService = Depends(get_package_pricing_service),
):
    payment = pricing_service.get_payment(payment_id)
    if not payment:
        raise HTTPException(status_code=404, detail=f"Payment link '{payment_id}' was not found.")
    return payment


@router.post(
    "/payment-callback",
    summary="Webhook callback from payment provider with HMAC signature verification",
    description="Verifies constant-time signature and timestamp replay protection, updating payment state idempotently.",
)
def handle_payment_callback(
    request: PaymentCallbackRequest,
    pricing_service: PackagePricingService = Depends(get_package_pricing_service),
):
    try:
        return pricing_service.process_payment_callback(request)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None


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
