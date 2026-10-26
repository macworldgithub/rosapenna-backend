"""Live availability and saved tee-sheet routes."""

import datetime as dt

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from pymongo.errors import PyMongoError

from rosapenna_backend.api.dependencies import get_settings, get_teesheet_cache
from rosapenna_backend.api.schemas.availability import AvailableTimesRequest, AvailableTimesResponse, QuoteSelection
from rosapenna_backend.api.security import COMMON_DEPS
from rosapenna_backend.config import Settings
from rosapenna_backend.core.logging import logger
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


@router.get("/teesheets/status", tags=["availability"], summary="Daily refresh status and configured date window")
def teesheet_status(
    cache: TeesheetCache = Depends(get_teesheet_cache),
    settings: Settings = Depends(get_settings),
):
    return {**cache.status(), "scheduler_enabled": settings.teesheet_refresh_enabled}


@router.get("/teesheets", tags=["availability"], summary="Read saved tee times and prices without contacting BRS")
def saved_teesheet(
    tee_date: dt.date = Query(alias="date"),
    course_id: int = Query(gt=0),
    cache: TeesheetCache = Depends(get_teesheet_cache),
):
    try:
        result = cache.get(tee_date, course_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    if result is None:
        raise HTTPException(404, "Saved tee times are not available for this date yet. Please try again after the daily update.")
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
