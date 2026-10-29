"""Live availability and saved tee-sheet routes."""

import datetime as dt
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from pymongo.errors import PyMongoError

from rosapenna_backend.api.dependencies import get_settings, get_teesheet_cache
from rosapenna_backend.api.schemas.availability import (
    AvailableTimesRequest,
    AvailableTimesResponse,
    BrsCourseAvailabilityResponse,
    QuoteSelection,
)
from rosapenna_backend.api.security import COMMON_DEPS
from rosapenna_backend.config import Settings
from rosapenna_backend.core.logging import logger
from rosapenna_backend.integrations.brs.tee_sheet import (
    BrsIntegrationError,
    BrsTenantUnconfiguredError,
    BrsUnsupportedQueryError,
)
from rosapenna_backend.services.availability_scraper import scrape_available_times
from rosapenna_backend.services.tee_sheets import TeesheetCache

router = APIRouter(dependencies=COMMON_DEPS)


@router.post(
    "/available-times", response_model=AvailableTimesResponse, tags=["availability"],
    summary="List open tee times for a course/date",
    description=("Headlessly loads the tee sheet, applies filters, and scrapes every visible tee time "
                 "with player-count/price options. Read-only — never fills the booking form."),
    response_description="Tee times grouped by rate/package.",
    responses={401: {"description": "Missing or invalid X-API-Key."}, 500: {"description": "Scrape failed."}},
)
def get_available_times(req: AvailableTimesRequest):
    try:
        return scrape_available_times(req)
    except Exception as exc:
        logger.error("available-times scrape failed (%s)", type(exc).__name__)
        raise HTTPException(status_code=500, detail="Failed to fetch available times. Please retry shortly.") from None


@router.get("/teesheets/status", tags=["availability"], summary="Cache status and configured date window")
def teesheet_status(
    cache: TeesheetCache = Depends(get_teesheet_cache),
    settings: Settings = Depends(get_settings),
):
    return {**cache.status(), "scheduler_enabled": False}


@router.get("/teesheets", tags=["availability"], summary="Read real-time tee times and prices with short in-memory cache")
def saved_teesheet(
    tee_date: dt.date = Query(alias="date"),
    course_id: int = Query(gt=0),
    cache: TeesheetCache = Depends(get_teesheet_cache),
):
    try:
        result = cache.get(tee_date, course_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    except Exception as exc:
        logger.exception("Failed to fetch real-time tee sheet from BRS")
        raise HTTPException(502, "Could not fetch tee times from BRS. Please try again.") from None
    if result is None:
        raise HTTPException(404, "Tee times are not available for this date.")
    return result


@router.post("/teesheets/revalidate", tags=["availability"], summary="Check a selected price and player availability against live BRS")
def revalidate_teesheet(
    selection: QuoteSelection, cache: TeesheetCache = Depends(get_teesheet_cache),
):
    try:
        result = cache.revalidate(selection)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    except BlockingIOError as exc:
        raise HTTPException(429, str(exc)) from None
    except PyMongoError:
        raise
    except Exception:
        logger.exception("BRS booking check failed")
        raise HTTPException(502, "Could not confirm the latest BRS price and availability. Please try again.") from None
    if not result["valid"]:
        return JSONResponse(status_code=409, content=result)
    return result


@router.get(
    "/golf/availability",
    response_model=BrsCourseAvailabilityResponse,
    tags=["availability"],
    summary="Read 3-course BRS tee-time availability and rates",
    description="Returns verified live tee-time availability, player slot capacity, and pricing for Old Tom Morris, Sandy Hills, and St Patrick's.",
)
def get_golf_availability(
    course_id: int = Query(..., description="Course ID: 1 (Old Tom Morris), 3 (Sandy Hills), 4 (St Patrick's)"),
    date: dt.date = Query(..., alias="date", description="Date to query (YYYY-MM-DD)"),
    players: Optional[int] = Query(None, ge=1, le=4, description="Filter for slots with minimum player capacity"),
    time_from: Optional[str] = Query(None, pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$", description="Earliest time (HH:MM)"),
    time_to: Optional[str] = Query(None, pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$", description="Latest time (HH:MM)"),
    cache: TeesheetCache = Depends(get_teesheet_cache),
):
    try:
        return cache.get_course_availability(
            day=date,
            course_id=course_id,
            players=players,
            time_from=time_from,
            time_to=time_to,
        )
    except BrsUnsupportedQueryError as exc:
        raise HTTPException(status_code=400, detail=exc.message)
    except BrsTenantUnconfiguredError as exc:
        raise HTTPException(status_code=503, detail=exc.message)
    except BrsIntegrationError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message)
    except Exception as exc:
        logger.exception("Unexpected error reading BRS golf availability")
        raise HTTPException(status_code=502, detail="Failed to fetch BRS tee times.")
