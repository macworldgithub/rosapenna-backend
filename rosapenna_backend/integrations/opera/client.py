"""Oracle Hospitality Integration Platform (OHIP) / OPERA Cloud client."""

import base64
import datetime as dt
import json
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Any, Dict, List, Optional

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from rosapenna_backend.core.logging import logger
from rosapenna_backend.integrations.opera.schemas import (
    CreateReservationRequest,
    DateSharingAssignment,
    ReservationWriteResult,
    RoomAvailabilityResponse,
    RoomCategoryAvailability,
    RoomOccupant,
    RoomRatePlan,
    RoomReservationDetail,
    UpdateReservationRequest,
)


class OperaSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", extra="ignore", hide_input_in_errors=True,
    )

    hostname: str = Field("", validation_alias="HOSTNAME")
    gateway_url: str = Field("", validation_alias="OPERA_GATEWAY_URL")
    client_id: str = Field("", validation_alias="CLIENT_ID")
    client_secret: SecretStr = Field(SecretStr(""), validation_alias="CLIENT_SECRET")
    app_key: SecretStr = Field(SecretStr(""), validation_alias="APP_KEY")
    enterprise_id: str = Field("", validation_alias="ENTERPRISE_ID")
    hotel_id: str = Field("ROSA", validation_alias="HOTEL_ID")
    token: SecretStr = Field(SecretStr(""), validation_alias="TOKEN")
    username: str = Field("", validation_alias="OPERA_USERNAME")
    password: SecretStr = Field(SecretStr(""), validation_alias="OPERA_PASSWORD")
    timeout_seconds: int = Field(15, ge=2, le=60)
    enable_mock_fallback: bool = True
    allow_live_writes: bool = Field(False, validation_alias="OPERA_ALLOW_LIVE_WRITES")
    production_domain: str = Field("", validation_alias="OPERA_PRODUCTION_DOMAIN")

    @property
    def effective_gateway(self) -> str:
        return (self.hostname or self.gateway_url).rstrip("/")


class OperaIntegrationError(Exception):
    """Base exception for Opera integration errors."""

    def __init__(self, message: str, status_code: int = 502, detail_code: str = "opera_error"):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.detail_code = detail_code


class OperaAuthError(OperaIntegrationError):
    def __init__(self, message: str = "Oracle Opera Cloud authentication failed."):
        super().__init__(message, status_code=401, detail_code="opera_auth_error")


class OperaPermissionError(OperaIntegrationError):
    def __init__(self, message: str = "Oracle Opera Cloud permission denied."):
        super().__init__(message, status_code=403, detail_code="opera_permission_denied")


class OperaNotFoundError(OperaIntegrationError):
    def __init__(self, message: str = "Requested Opera resource was not found."):
        super().__init__(message, status_code=404, detail_code="opera_not_found")


class OperaConflictError(OperaIntegrationError):
    def __init__(self, message: str = "Opera reservation write conflicted with existing state."):
        super().__init__(message, status_code=409, detail_code="opera_conflict")


class OperaReadbackMismatchError(OperaIntegrationError):
    def __init__(self, message: str = "Opera post-write readback verification failed."):
        super().__init__(message, status_code=502, detail_code="opera_readback_mismatch")


class OperaRateLimitError(OperaIntegrationError):
    def __init__(self, message: str = "Oracle Opera Cloud rate limit reached.", retry_after: int = 60):
        super().__init__(message, status_code=429, detail_code="opera_rate_limited")
        self.retry_after = retry_after


class OperaUnavailableError(OperaIntegrationError):
    def __init__(self, message: str = "Oracle Opera Cloud service is currently unavailable."):
        super().__init__(message, status_code=503, detail_code="opera_unavailable")


class OperaClient:
    """Client for Oracle Hospitality OPERA Cloud REST APIs via OHIP."""

    def __init__(self, settings: Optional[OperaSettings] = None):
        self.settings = settings or OperaSettings()
        self._cached_token: Optional[str] = None
        self._token_expires_at: float = 0.0
        self._token_fallback_attempted: bool = False
        self._sandbox_reservations: Dict[str, RoomReservationDetail] = {}

    @property
    def is_configured(self) -> bool:
        """True if the minimum required OHIP credentials are provided."""
        return bool(
            self.settings.effective_gateway
            and self.settings.client_id.strip()
            and self.settings.client_secret.get_secret_value().strip()
            and self.settings.app_key.get_secret_value().strip()
        )

    def _get_oauth_token(self) -> str:
        """Fetch or return cached OAuth 2.0 access token."""
        if self._cached_token and time.time() < self._token_expires_at:
            return self._cached_token

        # If a pre-configured token exists and hasn't failed, use it
        pre_token = self.settings.token.get_secret_value().strip()
        if pre_token and not self._token_fallback_attempted:
            self._cached_token = pre_token
            self._token_expires_at = time.time() + 1800
            return self._cached_token

        gateway = self.settings.effective_gateway
        token_url = f"{gateway}/oauth/v1/tokens"

        client_id = self.settings.client_id.strip()
        client_secret = self.settings.client_secret.get_secret_value().strip()
        app_key = self.settings.app_key.get_secret_value().strip()
        enterprise_id = self.settings.enterprise_id.strip()

        auth_header = "Basic " + base64.b64encode(f"{client_id}:{client_secret}".encode("utf-8")).decode("utf-8")

        headers = {
            "Authorization": auth_header,
            "x-app-key": app_key,
            "Content-Type": "application/x-www-form-urlencoded",
        }
        if enterprise_id:
            headers["enterpriseId"] = enterprise_id

        username = self.settings.username.strip()
        password = self.settings.password.get_secret_value().strip()

        if username and password:
            data = urllib.parse.urlencode({
                "grant_type": "password",
                "username": username,
                "password": password,
            }).encode("utf-8")
        else:
            data = urllib.parse.urlencode({
                "grant_type": "client_credentials",
                "scope": "urn:opc:hgbu:ws:__myscopes__",
            }).encode("utf-8")

        req = urllib.request.Request(token_url, data=data, headers=headers, method="POST")

        try:
            with urllib.request.urlopen(req, timeout=self.settings.timeout_seconds) as resp:
                body = json.loads(resp.read().decode("utf-8"))
                token = body.get("access_token")
                expires_in = int(body.get("expires_in", 3600))
                if not token:
                    raise OperaAuthError("No access_token returned by Opera Cloud token endpoint.")
                self._cached_token = token
                # Refresh 5 minutes before actual expiry
                self._token_expires_at = time.time() + max(300, expires_in - 300)
                return token
        except urllib.error.HTTPError as exc:
            logger.error("Opera token fetch failed with HTTP %d", exc.code)
            if exc.code in {401, 403}:
                raise OperaAuthError("Opera Cloud credentials were rejected.") from exc
            if exc.code == 429:
                raise OperaRateLimitError() from exc
            raise OperaUnavailableError(f"Opera Cloud token service returned HTTP {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            logger.error("Opera token connection failed (%s)", type(exc).__name__)
            raise OperaUnavailableError("Could not connect to Opera Cloud OAuth gateway.") from exc

    def get_room_availability(
        self,
        arrival_date: dt.date,
        departure_date: dt.date,
        adults: int = 2,
        children: int = 0,
    ) -> RoomAvailabilityResponse:
        """Search available room categories and rates for the requested dates."""
        if departure_date <= arrival_date:
            raise OperaIntegrationError("Departure date must be after arrival date.", status_code=400, detail_code="invalid_dates")

        if not self.is_configured:
            if self.settings.enable_mock_fallback:
                return self._generate_fixture_availability(arrival_date, departure_date, adults, children)
            raise OperaUnavailableError("Oracle Opera Cloud integration is not configured.")

        token = self._get_oauth_token()
        gateway = self.settings.effective_gateway
        hotel_id = self.settings.hotel_id.strip()
        app_key = self.settings.app_key.get_secret_value().strip()

        params = urllib.parse.urlencode({
            "roomStayStartDate": arrival_date.isoformat(),
            "roomStayEndDate": departure_date.isoformat(),
            "adults": adults,
            "children": children,
        })
        url = f"{gateway}/par/v1/hotels/{hotel_id}/availability?{params}"

        headers = {
            "Authorization": f"Bearer {token}",
            "x-app-key": app_key,
            "x-hotelid": hotel_id,
            "Accept": "application/json",
        }
        if self.settings.enterprise_id:
            headers["enterpriseId"] = self.settings.enterprise_id

        req = urllib.request.Request(url, headers=headers, method="GET")

        try:
            with urllib.request.urlopen(req, timeout=self.settings.timeout_seconds) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
                return self._parse_availability_response(payload, arrival_date, departure_date, adults, children)
        except urllib.error.HTTPError as exc:
            logger.error("Opera availability lookup failed with HTTP %d", exc.code)
            if exc.code == 401:
                # Token may have expired or pre-configured token failed; invalidate cache and retry with OAuth
                self._cached_token = None
                self._token_fallback_attempted = True
                raise OperaAuthError() from exc
            if exc.code == 403:
                raise OperaPermissionError() from exc
            if exc.code == 404:
                raise OperaNotFoundError(f"Hotel '{hotel_id}' not found in Opera Cloud.") from exc
            if exc.code == 429:
                raise OperaRateLimitError() from exc
            raise OperaUnavailableError(f"Opera Cloud availability returned HTTP {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            logger.error("Opera availability connection failed (%s)", type(exc).__name__)
            raise OperaUnavailableError("Could not connect to Opera Cloud availability endpoint.") from exc

    def get_reservation(self, reservation_id: str) -> RoomReservationDetail:
        """Lookup an existing reservation by Opera reservation ID."""
        clean_id = reservation_id.strip()
        if not clean_id:
            raise OperaIntegrationError("A valid reservation_id is required.", status_code=400, detail_code="invalid_id")

        if clean_id in self._sandbox_reservations:
            return self._sandbox_reservations[clean_id]

        if not self.is_configured:
            if self.settings.enable_mock_fallback:
                return self._generate_fixture_reservation(clean_id)
            raise OperaUnavailableError("Oracle Opera Cloud integration is not configured.")

        token = self._get_oauth_token()
        gateway = self.settings.effective_gateway
        hotel_id = self.settings.hotel_id.strip()
        app_key = self.settings.app_key.get_secret_value().strip()

        url = f"{gateway}/rsv/v1/hotels/{hotel_id}/reservations/{urllib.parse.quote(clean_id)}"

        headers = {
            "Authorization": f"Bearer {token}",
            "x-app-key": app_key,
            "x-hotelid": hotel_id,
            "Accept": "application/json",
        }
        if self.settings.enterprise_id:
            headers["enterpriseId"] = self.settings.enterprise_id

        req = urllib.request.Request(url, headers=headers, method="GET")

        try:
            with urllib.request.urlopen(req, timeout=self.settings.timeout_seconds) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
                return self._parse_reservation_detail(payload, clean_id)
        except urllib.error.HTTPError as exc:
            logger.error("Opera reservation lookup failed with HTTP %d", exc.code)
            if exc.code == 401:
                self._cached_token = None
                raise OperaAuthError() from exc
            if exc.code == 403:
                raise OperaPermissionError() from exc
            if exc.code == 404:
                raise OperaNotFoundError(f"Reservation '{clean_id}' was not found.") from exc
            if exc.code == 429:
                raise OperaRateLimitError() from exc
            raise OperaUnavailableError(f"Opera Cloud reservation returned HTTP {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            logger.error("Opera reservation lookup connection failed (%s)", type(exc).__name__)
            raise OperaUnavailableError("Could not connect to Opera Cloud reservation endpoint.") from exc

    def _parse_availability_response(
        self,
        payload: Dict[str, Any],
        arrival: dt.date,
        departure: dt.date,
        adults: int,
        children: int,
    ) -> RoomAvailabilityResponse:
        """Map OHIP raw availability response into our internal domain model."""
        categories: List[RoomCategoryAvailability] = []
        hotel_avail = payload.get("hotelAvailability", [])

        for item in hotel_avail:
            room_cat = item.get("roomType", "CLASSIC").upper()
            cat_name = item.get("roomTypeDescription", room_cat.title())
            available_count = int(item.get("availableRooms", 5))

            rates_data: List[RoomRatePlan] = []
            for rate in item.get("ratePlans", []):
                rates_data.append(RoomRatePlan(
                    rate_plan_code=rate.get("ratePlanCode", "BB_STANDARD"),
                    rate_plan_name=rate.get("ratePlanName", "Bed & Breakfast Standard"),
                    total_amount=float(rate.get("totalAmount", 220.0)),
                    currency=rate.get("currencyCode", "EUR"),
                    breakfast_included=rate.get("breakfastIncluded", True),
                ))

            categories.append(RoomCategoryAvailability(
                room_category=room_cat,
                category_name=cat_name,
                available_rooms=available_count,
                rates=rates_data,
            ))

        return RoomAvailabilityResponse(
            hotel_id=self.settings.hotel_id,
            arrival_date=arrival,
            departure_date=departure,
            adults=adults,
            children=children,
            source="opera_cloud",
            categories=categories,
        )

    def _parse_reservation_detail(self, payload: Dict[str, Any], reservation_id: str) -> RoomReservationDetail:
        """Map OHIP reservation payload into domain detail."""
        res_data = payload.get("reservation", payload)
        if isinstance(payload.get("reservations", {}).get("reservation"), list) and payload["reservations"]["reservation"]:
            res_data = payload["reservations"]["reservation"][0]

        hotel_id = res_data.get("hotelId", self.settings.hotel_id)
        status = res_data.get("reservationStatus", "Confirmed")
        conf_num = res_data.get("confirmationNumber", reservation_id)

        stay_info = res_data.get("roomStay", {})
        arrival = dt.date.fromisoformat(stay_info.get("arrivalDate", dt.date.today().isoformat()))
        departure = dt.date.fromisoformat(stay_info.get("departureDate", (dt.date.today() + dt.timedelta(days=2)).isoformat()))
        room_category = stay_info.get("roomType", "CLASSIC")
        room_num = stay_info.get("assignedRoomNumber")  # Hotel physical room allocation

        occupants: List[RoomOccupant] = []
        for guest in res_data.get("guestProfiles", []):
            occupants.append(RoomOccupant(
                first_name=guest.get("firstName", "Guest"),
                last_name=guest.get("lastName", "Guest"),
                is_primary=bool(guest.get("isPrimary", False)),
            ))
        if not occupants and res_data.get("reservationGuests"):
            for rg in res_data.get("reservationGuests", []):
                profile = rg.get("profileInfo", {}).get("profile", {}).get("customer", {})
                names = profile.get("personName", [])
                if names:
                    occupants.append(RoomOccupant(
                        first_name=names[0].get("givenName", "Guest"),
                        last_name=names[0].get("surname", "Guest"),
                        is_primary=True,
                    ))

        total_amount = float(stay_info.get("totalAmount", 0.0))
        currency = stay_info.get("currencyCode", "EUR")

        return RoomReservationDetail(
            reservation_id=reservation_id,
            confirmation_number=conf_num,
            hotel_id=hotel_id,
            status=status,
            arrival_date=arrival,
            departure_date=departure,
            room_category=room_category,
            room_number=room_num,
            occupants=occupants,
            total_amount=total_amount,
            currency=currency,
            source="opera_cloud",
        )

    def _generate_fixture_availability(
        self,
        arrival: dt.date,
        departure: dt.date,
        adults: int,
        children: int,
    ) -> RoomAvailabilityResponse:
        """Deterministic fixture based on Rosapenna's published room types and 2027 rates."""
        nights = max(1, (departure - arrival).days)

        # Published 2027 season rate calculation
        # High season: May 1 to Sep 30
        is_high = 5 <= arrival.month <= 9
        single = (adults == 1)

        rates_table = {
            "CLASSIC": {
                "name": "Classic Room",
                "rate": (280.0 if single else 300.0) if is_high else (200.0 if single else 220.0),
                "count": 12,
            },
            "DELUXE": {
                "name": "Deluxe Room",
                "rate": (310.0 if single else 330.0) if is_high else (230.0 if single else 250.0),
                "count": 10,
            },
            "BAYVIEW": {
                "name": "Bayview Junior Suite",
                "rate": (360.0 if single else 380.0) if is_high else (280.0 if single else 300.0),
                "count": 6,
            },
            "PENTHOUSE": {
                "name": "Penthouse Suite",
                "rate": (480.0 if single else 500.0) if is_high else (400.0 if single else 420.0),
                "count": 2,
            },
        }

        categories: List[RoomCategoryAvailability] = []
        for cat_code, cat_info in rates_table.items():
            nightly_rate = cat_info["rate"]
            total = round(nightly_rate * nights, 2)
            categories.append(RoomCategoryAvailability(
                room_category=cat_code,
                category_name=cat_info["name"],
                available_rooms=cat_info["count"],
                rates=[
                    RoomRatePlan(
                        rate_plan_code="BB_STANDARD",
                        rate_plan_name="Bed & Breakfast (Traditional Irish Breakfast included)",
                        total_amount=total,
                        currency="EUR",
                        breakfast_included=True,
                    )
                ],
            ))

        return RoomAvailabilityResponse(
            hotel_id=self.settings.hotel_id,
            arrival_date=arrival,
            departure_date=departure,
            adults=adults,
            children=children,
            source="fixture_fallback",
            categories=categories,
        )

    def _generate_fixture_reservation(self, reservation_id: str) -> RoomReservationDetail:
        """Deterministic fixture reservation for lookup demonstration."""
        return RoomReservationDetail(
            reservation_id=reservation_id,
            confirmation_number=f"CONF-{reservation_id.upper()}",
            hotel_id=self.settings.hotel_id,
            status="Confirmed",
            arrival_date=dt.date(2027, 5, 14),
            departure_date=dt.date(2027, 5, 17),
            room_category="DELUXE",
            room_number=None,  # Hotel physical room allocated 2 days prior to arrival
            occupants=[
                RoomOccupant(first_name="Sample", last_name="Golfer", is_primary=True),
                RoomOccupant(first_name="Guest", last_name="Golfer", is_primary=False),
            ],
            total_amount=990.0,
            currency="EUR",
            source="fixture_fallback",
        )

    def create_reservation(self, request: CreateReservationRequest) -> ReservationWriteResult:
        """Create a room reservation in Opera Cloud with post-write readback verification."""
        if request.departure_date <= request.arrival_date:
            raise OperaIntegrationError("departure_date must be after arrival_date.", status_code=400, detail_code="invalid_dates")

        # Format and validate the OHIP payload
        _ = self._build_ohip_create_payload(request)

        # Non-production / test mode policy: do not create live reservations
        if not self.settings.allow_live_writes:
            logger.info("Opera live writes are disabled by policy (OPERA_ALLOW_LIVE_WRITES=false); using verified sandbox.")
            return self._generate_fixture_write_result(request)

        # Production path: verify configuration and allowed production domain
        if not self.is_configured:
            raise OperaUnavailableError("Oracle Opera Cloud integration is not configured.")

        if self.settings.production_domain:
            parsed_host = urllib.parse.urlparse(self.settings.effective_gateway).netloc.split(":")[0]
            if parsed_host != self.settings.production_domain:
                raise OperaPermissionError(
                    f"Opera live write blocked: gateway host '{parsed_host}' does not match production domain '{self.settings.production_domain}'."
                )

        token = self._get_oauth_token()
        gateway = self.settings.effective_gateway
        hotel_id = self.settings.hotel_id.strip()
        app_key = self.settings.app_key.get_secret_value().strip()

        url = f"{gateway}/rsv/v1/hotels/{hotel_id}/reservations"
        headers = {
            "Authorization": f"Bearer {token}",
            "x-app-key": app_key,
            "x-hotelid": hotel_id,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if self.settings.enterprise_id:
            headers["enterpriseId"] = self.settings.enterprise_id

        data = json.dumps(self._build_ohip_create_payload(request)).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")

        try:
            with urllib.request.urlopen(req, timeout=self.settings.timeout_seconds) as resp:
                resp_body = json.loads(resp.read().decode("utf-8"))
                location = resp.headers.get("Location", "")
                reservation_id = self._extract_reservation_id(location, resp_body)
                if not reservation_id:
                    raise OperaIntegrationError(
                        "Opera reservation created but no reservation ID was returned.",
                        status_code=502,
                        detail_code="missing_reservation_id",
                    )
        except urllib.error.HTTPError as exc:
            logger.error("Opera reservation create failed with HTTP %d", exc.code)
            if exc.code == 401:
                self._cached_token = None
                raise OperaAuthError() from exc
            if exc.code == 403:
                raise OperaPermissionError() from exc
            if exc.code == 409:
                raise OperaConflictError("Opera reported an inventory conflict during reservation creation.") from exc
            if exc.code == 429:
                raise OperaRateLimitError() from exc
            raise OperaUnavailableError(f"Opera Cloud reservation create returned HTTP {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            logger.error("Opera reservation create connection failed (%s)", type(exc).__name__)
            raise OperaUnavailableError("Could not connect to Opera Cloud reservation endpoint.") from exc

        # Post-write readback verification
        readback = self.get_reservation(reservation_id)
        self._verify_readback(request, readback)

        return ReservationWriteResult(
            reservation_id=readback.reservation_id,
            confirmation_number=readback.confirmation_number or readback.reservation_id,
            status=readback.status,
            hotel_id=readback.hotel_id,
            arrival_date=readback.arrival_date,
            departure_date=readback.departure_date,
            room_category=readback.room_category,
            occupants=readback.occupants or request.occupants,
            total_amount=readback.total_amount,
            currency=readback.currency,
            source="opera_cloud",
            verified_via_readback=True,
        )

    def amend_reservation(self, request: UpdateReservationRequest) -> ReservationWriteResult:
        """Amend an existing Opera reservation with conflict detection and readback verification."""
        clean_id = request.reservation_id.strip()
        if not clean_id:
            raise OperaIntegrationError("A valid reservation_id is required for amendment.", status_code=400, detail_code="invalid_id")

        # Read existing reservation to detect conflicts
        current = self.get_reservation(clean_id)

        # Conflict check
        if current.status in {"Cancelled", "CheckedOut"}:
            raise OperaConflictError(f"Cannot amend reservation '{clean_id}' because status is '{current.status}'.")

        arrival = request.arrival_date or current.arrival_date
        departure = request.departure_date or current.departure_date
        category = (request.room_category or current.room_category).upper()
        occupants = request.occupants if request.occupants is not None else current.occupants

        if departure <= arrival:
            raise OperaIntegrationError("departure_date must be after arrival_date.", status_code=400, detail_code="invalid_dates")

        # Non-production / test mode policy
        if not self.settings.allow_live_writes:
            logger.info("Opera live writes are disabled; using verified sandbox amendment.")
            return self._generate_fixture_amendment_result(clean_id, arrival, departure, category, occupants)

        if not self.is_configured:
            raise OperaUnavailableError("Oracle Opera Cloud integration is not configured.")

        if self.settings.production_domain:
            parsed_host = urllib.parse.urlparse(self.settings.effective_gateway).netloc.split(":")[0]
            if parsed_host != self.settings.production_domain:
                raise OperaPermissionError(
                    f"Opera live write blocked: gateway host '{parsed_host}' does not match production domain '{self.settings.production_domain}'."
                )

        token = self._get_oauth_token()
        gateway = self.settings.effective_gateway
        hotel_id = self.settings.hotel_id.strip()
        app_key = self.settings.app_key.get_secret_value().strip()

        url = f"{gateway}/rsv/v1/hotels/{hotel_id}/reservations/{urllib.parse.quote(clean_id)}"
        headers = {
            "Authorization": f"Bearer {token}",
            "x-app-key": app_key,
            "x-hotelid": hotel_id,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if self.settings.enterprise_id:
            headers["enterpriseId"] = self.settings.enterprise_id

        amend_body = self._build_ohip_amend_payload(request, current, arrival, departure, category, occupants)
        data = json.dumps(amend_body).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers=headers, method="PUT")

        try:
            with urllib.request.urlopen(req, timeout=self.settings.timeout_seconds) as resp:
                pass
        except urllib.error.HTTPError as exc:
            logger.error("Opera reservation amend failed with HTTP %d", exc.code)
            if exc.code == 401:
                self._cached_token = None
                raise OperaAuthError() from exc
            if exc.code == 403:
                raise OperaPermissionError() from exc
            if exc.code == 409:
                raise OperaConflictError("Opera detected an edit conflict during amendment.") from exc
            if exc.code == 404:
                raise OperaNotFoundError(f"Reservation '{clean_id}' not found.") from exc
            if exc.code == 429:
                raise OperaRateLimitError() from exc
            raise OperaUnavailableError(f"Opera Cloud reservation amend returned HTTP {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            logger.error("Opera reservation amend connection failed (%s)", type(exc).__name__)
            raise OperaUnavailableError("Could not connect to Opera Cloud reservation endpoint.") from exc

        # Readback verification
        readback = self.get_reservation(clean_id)
        if readback.arrival_date != arrival or readback.departure_date != departure or readback.room_category.upper() != category:
            raise OperaReadbackMismatchError("Readback verification failed: amended fields do not match Opera values.")

        return ReservationWriteResult(
            reservation_id=clean_id,
            confirmation_number=readback.confirmation_number or clean_id,
            status=readback.status,
            hotel_id=hotel_id,
            arrival_date=arrival,
            departure_date=departure,
            room_category=category,
            occupants=occupants,
            total_amount=readback.total_amount,
            currency=readback.currency,
            source="opera_cloud",
            verified_via_readback=True,
        )

    def _verify_readback(
        self,
        request: CreateReservationRequest,
        readback: RoomReservationDetail,
    ) -> None:
        """Verify that Opera readback strictly matches requested write values."""
        if readback.arrival_date != request.arrival_date:
            raise OperaReadbackMismatchError(
                f"Readback arrival date {readback.arrival_date} does not match requested {request.arrival_date}."
            )
        if readback.departure_date != request.departure_date:
            raise OperaReadbackMismatchError(
                f"Readback departure date {readback.departure_date} does not match requested {request.departure_date}."
            )
        if readback.room_category.upper() != request.room_category.upper():
            raise OperaReadbackMismatchError(
                f"Readback room category '{readback.room_category}' does not match requested '{request.room_category}'."
            )
        if readback.room_quantity != request.room_quantity:
            raise OperaReadbackMismatchError(
                f"Readback room quantity {readback.room_quantity} does not match requested {request.room_quantity}."
            )
        if readback.room_number is not None:
            logger.warning("Opera returned assignedRoomNumber on fresh reservation; Rosapenna rule expects None.")
        if request.occupants and readback.occupants:
            primary_req = next((o for o in request.occupants if o.is_primary), request.occupants[0])
            primary_rb = next((o for o in readback.occupants if o.is_primary), readback.occupants[0])
            if primary_req.last_name.strip().lower() != primary_rb.last_name.strip().lower():
                raise OperaReadbackMismatchError(
                    f"Readback occupant surname '{primary_rb.last_name}' does not match '{primary_req.last_name}'."
                )

    def _build_ohip_create_payload(self, req: CreateReservationRequest) -> Dict[str, Any]:
        """Format OHIP Create Reservation payload per Oracle Hospitality REST specs."""
        guest_profiles = []
        for occ in req.occupants:
            profile_data: Dict[str, Any] = {
                "customer": {
                    "personName": [
                        {
                            "givenName": occ.first_name,
                            "surname": occ.last_name,
                        }
                    ]
                }
            }
            if req.guest_phone:
                profile_data["customer"]["telephones"] = {
                    "telephoneInfo": [{"telephone": {"phoneNumber": req.guest_phone}}]
                }
            if req.guest_email:
                profile_data["customer"]["emails"] = {
                    "emailInfo": [{"email": {"emailAddress": req.guest_email}}]
                }
            guest_profiles.append({
                "profileInfo": {
                    "profile": profile_data
                }
            })

        room_stay: Dict[str, Any] = {
            "roomRates": [
                {
                    "rates": {
                        "rate": [
                            {
                                "startDate": req.arrival_date.isoformat(),
                                "endDate": req.departure_date.isoformat(),
                            }
                        ]
                    },
                    "ratePlanCode": req.rate_plan_code,
                    "roomType": req.room_category.upper(),
                    "numberOfUnits": req.room_quantity,
                }
            ],
            "guestCounts": {
                "adults": req.adults,
                "children": req.children,
            },
            "arrivalDate": req.arrival_date.isoformat(),
            "departureDate": req.departure_date.isoformat(),
        }
        
        comments = []
        if req.special_requests:
            comments.append({"comment": {"text": {"value": req.special_requests}}})
        if req.sharing_assignments:
            sharing_text = "; ".join(
                f"{sa.date}: {', '.join(sa.occupant_names)} ({sa.bed_type_preference})"
                for sa in req.sharing_assignments
            )
            comments.append({"comment": {"text": {"value": f"Room Sharing: {sharing_text}"}}})
        if comments:
            room_stay["comments"] = comments

        return {
            "reservations": {
                "reservation": [
                    {
                        "roomStay": room_stay,
                        "reservationGuests": guest_profiles,
                        "reservationPaymentMethods": [
                            {"paymentMethod": "CA"}
                        ],
                    }
                ]
            }
        }

    def _build_ohip_amend_payload(
        self,
        request: UpdateReservationRequest,
        current: RoomReservationDetail,
        arrival: dt.date,
        departure: dt.date,
        category: str,
        occupants: List[RoomOccupant],
    ) -> Dict[str, Any]:
        """Format OHIP Update Reservation payload preserving unmodified fields."""
        guest_profiles = []
        for occ in occupants:
            profile_data: Dict[str, Any] = {
                "customer": {
                    "personName": [
                        {
                            "givenName": occ.first_name,
                            "surname": occ.last_name,
                        }
                    ]
                }
            }
            if request.guest_phone:
                profile_data["customer"]["telephones"] = {
                    "telephoneInfo": [{"telephone": {"phoneNumber": request.guest_phone}}]
                }
            if request.guest_email:
                profile_data["customer"]["emails"] = {
                    "emailInfo": [{"email": {"emailAddress": request.guest_email}}]
                }
            guest_profiles.append({
                "profileInfo": {
                    "profile": profile_data
                }
            })

        quantity = request.room_quantity or current.room_quantity
        room_stay: Dict[str, Any] = {
            "roomRates": [
                {
                    "rates": {
                        "rate": [
                            {
                                "startDate": arrival.isoformat(),
                                "endDate": departure.isoformat(),
                            }
                        ]
                    },
                    "ratePlanCode": "BB_STANDARD",
                    "roomType": category,
                    "numberOfUnits": quantity,
                }
            ],
            "arrivalDate": arrival.isoformat(),
            "departureDate": departure.isoformat(),
        }
        comments = []
        if request.special_requests:
            comments.append({"comment": {"text": {"value": request.special_requests}}})
        sharing = request.sharing_assignments if request.sharing_assignments is not None else current.sharing_assignments
        if sharing:
            sharing_text = "; ".join(
                f"{sa.date}: {', '.join(sa.occupant_names)} ({sa.bed_type_preference})"
                for sa in sharing
            )
            comments.append({"comment": {"text": {"value": f"Room Sharing: {sharing_text}"}}})
        if comments:
            room_stay["comments"] = comments

        return {
            "reservation": {
                "roomStay": room_stay,
                "reservationGuests": guest_profiles,
            }
        }

    def _extract_reservation_id(self, location_header: str, resp_body: Dict[str, Any]) -> Optional[str]:
        """Extract reservation ID from HTTP Location header or OHIP response body."""
        if location_header:
            parts = location_header.rstrip("/").split("/")
            if parts:
                return parts[-1]

        for link in resp_body.get("links", []):
            if link.get("rel") == "self" and link.get("href"):
                return link["href"].rstrip("/").split("/")[-1]

        res = resp_body.get("reservation", {})
        id_list = res.get("reservationIdList", [])
        for entry in id_list:
            if isinstance(entry, dict) and entry.get("id"):
                return str(entry["id"])

        if resp_body.get("reservationId"):
            return str(resp_body["reservationId"])

        return None

    def _calculate_fixture_amount(self, category: str, arrival: dt.date, departure: dt.date, adults: int = 2) -> float:
        nights = max(1, (departure - arrival).days)
        is_high = 5 <= arrival.month <= 9
        single = (adults == 1)
        rates_table = {
            "CLASSIC": (280.0 if single else 300.0) if is_high else (200.0 if single else 220.0),
            "DELUXE": (310.0 if single else 330.0) if is_high else (230.0 if single else 250.0),
            "BAYVIEW": (360.0 if single else 380.0) if is_high else (280.0 if single else 300.0),
            "PENTHOUSE": (480.0 if single else 500.0) if is_high else (400.0 if single else 420.0),
        }
        nightly = rates_table.get(category.upper(), 220.0)
        return round(nightly * nights, 2)

    def _generate_fixture_write_result(self, request: CreateReservationRequest) -> ReservationWriteResult:
        res_id = f"OPERA-{uuid.uuid4().hex[:8].upper()}"
        conf_num = f"CONF-{res_id}"
        total = self._calculate_fixture_amount(request.room_category, request.arrival_date, request.departure_date, request.adults) * request.room_quantity
        room_group_id = request.room_group_id or f"GRP-{uuid.uuid4().hex[:6].upper()}"

        detail = RoomReservationDetail(
            reservation_id=res_id,
            confirmation_number=conf_num,
            hotel_id=self.settings.hotel_id,
            status="Confirmed",
            arrival_date=request.arrival_date,
            departure_date=request.departure_date,
            room_category=request.room_category.upper(),
            room_quantity=request.room_quantity,
            room_group_id=room_group_id,
            room_number=None,
            occupants=request.occupants,
            sharing_assignments=request.sharing_assignments,
            total_amount=total,
            currency="EUR",
            source="fixture_sandbox",
        )
        self._sandbox_reservations[res_id] = detail

        # Execute readback verification
        readback = self.get_reservation(res_id)
        self._verify_readback(request, readback)

        return ReservationWriteResult(
            reservation_id=res_id,
            confirmation_number=conf_num,
            status="Confirmed",
            hotel_id=self.settings.hotel_id,
            arrival_date=request.arrival_date,
            departure_date=request.departure_date,
            room_category=request.room_category.upper(),
            room_quantity=request.room_quantity,
            room_group_id=room_group_id,
            occupants=request.occupants,
            sharing_assignments=request.sharing_assignments,
            total_amount=total,
            currency="EUR",
            source="fixture_sandbox",
            verified_via_readback=True,
        )

    def _generate_fixture_amendment_result(
        self,
        clean_id: str,
        arrival: dt.date,
        departure: dt.date,
        category: str,
        occupants: List[RoomOccupant],
    ) -> ReservationWriteResult:
        total = self._calculate_fixture_amount(category, arrival, departure, len(occupants))
        conf_num = f"CONF-{clean_id}"

        existing = self._sandbox_reservations.get(clean_id)
        quantity = existing.room_quantity if existing else 1
        room_group_id = existing.room_group_id if existing else None
        sharing = existing.sharing_assignments if existing else []

        detail = RoomReservationDetail(
            reservation_id=clean_id,
            confirmation_number=conf_num,
            hotel_id=self.settings.hotel_id,
            status="Confirmed",
            arrival_date=arrival,
            departure_date=departure,
            room_category=category.upper(),
            room_quantity=quantity,
            room_group_id=room_group_id,
            room_number=None,
            occupants=occupants,
            sharing_assignments=sharing,
            total_amount=total * quantity,
            currency="EUR",
            source="fixture_sandbox",
        )
        self._sandbox_reservations[clean_id] = detail

        return ReservationWriteResult(
            reservation_id=clean_id,
            confirmation_number=conf_num,
            status="Confirmed",
            hotel_id=self.settings.hotel_id,
            arrival_date=arrival,
            departure_date=departure,
            room_category=category.upper(),
            room_quantity=quantity,
            room_group_id=room_group_id,
            occupants=occupants,
            sharing_assignments=sharing,
            total_amount=total * quantity,
            currency="EUR",
            source="fixture_sandbox",
            verified_via_readback=True,
        )
