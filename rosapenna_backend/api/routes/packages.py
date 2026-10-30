"""API routes for Stay & Play package live pricing, quotes, and rules configuration."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from rosapenna_backend.api.dependencies import get_package_pricing_service
from rosapenna_backend.api.schemas.packages import (
    PackageQuoteRequest,
    PackageQuoteResponse,
    PackageRulesConfig,
    PackageSearchRequest,
    PackageSearchResponse,
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
