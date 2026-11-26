"""Isolated pre-arrival routes, separate from the booking API."""

import hmac
import re
import threading
import time
from collections import deque
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, Path, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pymongo.errors import PyMongoError

from rosapenna_backend.api.schemas.pre_arrival_rooming import Identifier, InviteExchange, SecretToken, Submission
from rosapenna_backend.core.middleware import RequestBodySizeLimitMiddleware
from rosapenna_backend.services.pre_arrival_rooming import SandboxRooming
from rosapenna_backend.services.pre_arrival_caddy import CaddySubmission, SandboxCaddy


def create_rooming_sandbox(service: SandboxRooming, service_key: str) -> FastAPI:
    if not re.fullmatch(r"[A-Za-z0-9_-]{43,128}", service_key):
        raise ValueError("Configure a dedicated random sandbox key")
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, redirect_slashes=False)
    app.add_middleware(RequestBodySizeLimitMiddleware, max_bytes=32768)
    hits: deque[float] = deque()
    lock = threading.Lock()

    def authorize(request: Request, key: Annotated[str | None, Header(alias="X-API-Key")] = None):
        if not key or not key.isascii() or not hmac.compare_digest(key, service_key):
            raise HTTPException(401, "Sandbox authorization failed")
        current = time.monotonic()
        with lock:
            while hits and current - hits[0] >= 60:
                hits.popleft()
            if len(hits) >= 120:
                raise HTTPException(429, "Too many requests. Wait a minute and try again")
            hits.append(current)
        if request.method in {"POST", "PUT"} and request.headers.get(
            "content-type", ""
        ).split(";", 1)[0].strip().lower() != "application/json":
            raise HTTPException(415, "Send JSON data")

    def require_session(
        token: Annotated[SecretToken | None, Header(alias="X-Pre-Arrival-Session")] = None,
    ):
        if not token:
            raise HTTPException(401, "This rooming link or session is unavailable")
        return token

    @app.middleware("http")
    async def private_response(request: Request, call_next):
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    @app.exception_handler(RequestValidationError)
    async def validation_error(_request, _error):
        return JSONResponse({"detail": "Check the names, dates and rooming selections"}, status_code=422)

    @app.exception_handler(PyMongoError)
    async def database_error(_request, _error):
        return JSONResponse({"detail": "The test save or lookup could not be verified"}, status_code=503)

    dependencies = [Depends(authorize)]
    caddy = SandboxCaddy(service)

    @app.get("/pre-arrival/caddy", dependencies=dependencies)
    def caddy_rounds(session: str = Depends(require_session)):
        return caddy.read(session)

    @app.put("/pre-arrival/caddy/{round_id}", dependencies=dependencies)
    def request_caddy(round_id: Identifier, body: CaddySubmission, session: str = Depends(require_session)):
        return caddy.submit(session, round_id, body)

    @app.post("/pre-arrival/rooming/session", dependencies=dependencies)
    def exchange(body: InviteExchange):
        return service.exchange(body.invite_token)

    @app.delete("/pre-arrival/rooming/session", dependencies=dependencies)
    def logout(session: str = Depends(require_session)):
        return service.logout(session)

    @app.get("/pre-arrival/rooming", dependencies=dependencies)
    def form(session: str = Depends(require_session)):
        return service.form(session)

    @app.put("/pre-arrival/rooming", dependencies=dependencies)
    def submit(body: Submission, session: str = Depends(require_session)):
        return service.submit(session, body)

    @app.get("/pre-arrival/rooming/submissions/{submission_id}", dependencies=dependencies)
    def result(submission_id: Annotated[str, Path(pattern=r"^[a-f0-9]{32}$")], session: str = Depends(require_session)):
        return service.result(session, submission_id)

    return app
